import { useRef, useCallback, useState, useEffect } from 'react';

const SAMPLE_RATE_IN = 16000;
const SAMPLE_RATE_OUT = 24000;
const CHUNK_BYTES = 2048;
const MIC_COOLDOWN_MS = 300;

function downsample(inputBuffer, inputRate, outputRate) {
  if (inputRate === outputRate) return inputBuffer;
  const ratio = inputRate / outputRate;
  const outLen = Math.floor(inputBuffer.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const srcIdx = i * ratio;
    const lo = Math.floor(srcIdx);
    const hi = Math.min(lo + 1, inputBuffer.length - 1);
    const frac = srcIdx - lo;
    out[i] = inputBuffer[lo] * (1 - frac) + inputBuffer[hi] * frac;
  }
  return out;
}

function float32ToInt16(float32) {
  const int16 = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return int16;
}

function int16ToFloat32(int16) {
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] / 32768.0;
  }
  return float32;
}

/**
 * useAudioSession — manages WebSocket + Web Audio for continuous voice streaming.
 * Mirrors client.py architecture: connect once, stream mic continuously,
 * rely on server-side VAD for speech boundary detection.
 */
export default function useAudioSession() {
  const [status, setStatus] = useState('idle');         // idle | connecting | connected | error
  const [pipeline, setPipeline] = useState('listening'); // listening | processing | thinking | speaking | muted
  const [messages, setMessages] = useState([]);
  const [currentAiText, setCurrentAiText] = useState('');

  const wsRef = useRef(null);
  const audioCtxRef = useRef(null);
  const analyserRef = useRef(null);
  const micStreamRef = useRef(null);
  const scriptProcRef = useRef(null);
  const sendBufferRef = useRef(new Float32Array(0));
  const isPlayingRef = useRef(false);
  const lastPlayTimeRef = useRef(0);
  const nextPlayTimeRef = useRef(0);
  const activeSourcesRef = useRef([]);
  const currentAiTextRef = useRef('');

  // ─── Playback (mirrors speaker_thread_fn) ───
  const playPcmChunk = useCallback((arrayBuffer) => {
    const ctx = audioCtxRef.current;
    const analyser = analyserRef.current;
    if (!ctx || !analyser) return;

    const int16 = new Int16Array(arrayBuffer);
    const float32 = int16ToFloat32(int16);

    const buffer = ctx.createBuffer(1, float32.length, SAMPLE_RATE_OUT);
    buffer.getChannelData(0).set(float32);

    const source = ctx.createBufferSource();
    source.buffer = buffer;
    
    // Connect to analyser for the visualizer (dead end)
    source.connect(analyser);
    // Connect directly to destination for playback
    source.connect(ctx.destination);

    const now = ctx.currentTime;
    if (nextPlayTimeRef.current < now) {
      nextPlayTimeRef.current = now + 0.05;
    }

    source.start(nextPlayTimeRef.current);
    isPlayingRef.current = true;
    setPipeline('speaking');
    activeSourcesRef.current.push(source);

    source.onended = () => {
      activeSourcesRef.current = activeSourcesRef.current.filter(n => n !== source);
      if (activeSourcesRef.current.length === 0) {
        isPlayingRef.current = false;
        lastPlayTimeRef.current = performance.now();
        setPipeline('listening');
      }
    };

    nextPlayTimeRef.current += buffer.duration;
  }, []);

  // ─── Handle incoming transcript events ───
  const handleTextMessage = useCallback((data) => {
    try {
      const msg = JSON.parse(data);
      switch (msg.type) {
        case 'vad_triggered':
          setPipeline('processing');
          break;
        case 'user':
          setMessages(prev => [...prev, { role: 'user', text: msg.text }]);
          setPipeline('thinking');
          break;
        case 'ai_delta':
          currentAiTextRef.current += msg.text;
          setCurrentAiText(currentAiTextRef.current);
          setPipeline('speaking');
          break;
        case 'ai_done': {
          const finalText = currentAiTextRef.current;
          if (finalText) {
            setMessages(prev => [...prev, { role: 'ai', text: finalText }]);
          }
          currentAiTextRef.current = '';
          setCurrentAiText('');
          // Pipeline will go to 'listening' when audio finishes playing
          // If no audio, go to listening now
          if (!isPlayingRef.current) {
            setPipeline('listening');
          }
          break;
        }
        default:
          break;
      }
    } catch {
      // ignore
    }
  }, []);

  // ─── Start session (mirrors client.py main) ───
  const startSession = useCallback(async (voice, model) => {
    setStatus('connecting');
    setMessages([]);
    setCurrentAiText('');
    currentAiTextRef.current = '';

    // Audio context
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    audioCtxRef.current = ctx;
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    analyserRef.current = analyser;

    if (ctx.state === 'suspended') await ctx.resume();

    // WebSocket
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/audio`);
    ws.binaryType = 'arraybuffer';
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ tts_voice: voice, stt_model: model }));
      setStatus('connected');
      setPipeline('listening');
    };

    ws.onmessage = (evt) => {
      if (typeof evt.data === 'string') {
        handleTextMessage(evt.data);
      } else {
        playPcmChunk(evt.data);
      }
    };

    ws.onclose = () => {
      setStatus('idle');
      stopMic();
    };

    ws.onerror = () => {
      setStatus('error');
    };

    // Mic capture (mirrors mic_thread_fn)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 }
      });
      micStreamRef.current = stream;

      const micSource = ctx.createMediaStreamSource(stream);
      const scriptProc = ctx.createScriptProcessor(4096, 1, 1);
      scriptProcRef.current = scriptProc;

      micSource.connect(analyser);
      micSource.connect(scriptProc);
      scriptProc.connect(ctx.destination);

      const nativeSr = ctx.sampleRate;
      const targetSamples = CHUNK_BYTES / 2;

      scriptProc.onaudioprocess = (e) => {
        if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;

        // Echo cancellation gate: drop mic frames while TTS is playing + cooldown
        // (mirrors client.py's is_playing + MIC_COOLDOWN_SEC)
        if (isPlayingRef.current) return;
        if (performance.now() - lastPlayTimeRef.current < MIC_COOLDOWN_MS) return;

        const input = e.inputBuffer.getChannelData(0);
        const downsampled = downsample(input, nativeSr, SAMPLE_RATE_IN);

        const prev = sendBufferRef.current;
        const merged = new Float32Array(prev.length + downsampled.length);
        merged.set(prev);
        merged.set(downsampled, prev.length);
        sendBufferRef.current = merged;

        while (sendBufferRef.current.length >= targetSamples) {
          const chunk = sendBufferRef.current.slice(0, targetSamples);
          sendBufferRef.current = sendBufferRef.current.slice(targetSamples);
          const int16 = float32ToInt16(chunk);
          wsRef.current.send(int16.buffer);
        }
      };
    } catch (err) {
      console.error('Mic error:', err);
      setStatus('error');
    }
  }, [handleTextMessage, playPcmChunk]);

  // ─── Stop mic ───
  const stopMic = useCallback(() => {
    if (scriptProcRef.current) {
      scriptProcRef.current.disconnect();
      scriptProcRef.current = null;
    }
    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach(t => t.stop());
      micStreamRef.current = null;
    }
    sendBufferRef.current = new Float32Array(0);
  }, []);

  // ─── End session ───
  const endSession = useCallback(() => {
    // Stop any playing audio
    activeSourcesRef.current.forEach(n => { try { n.stop(); } catch(e) { /* */ } });
    activeSourcesRef.current = [];
    isPlayingRef.current = false;
    nextPlayTimeRef.current = 0;

    stopMic();

    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    if (audioCtxRef.current) {
      audioCtxRef.current.close();
      audioCtxRef.current = null;
    }
    analyserRef.current = null;

    setStatus('idle');
    setPipeline('listening');
  }, [stopMic]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      endSession();
    };
  }, [endSession]);

  return {
    status,
    pipeline,
    messages,
    currentAiText,
    analyserRef,
    startSession,
    endSession,
  };
}
