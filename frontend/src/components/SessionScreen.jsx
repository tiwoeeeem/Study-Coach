import { useRef, useEffect, useCallback } from 'react';

const PIPELINE_LABELS = {
  listening:  'Listening...',
  processing: 'Processing speech...',
  thinking:   'Coach is thinking...',
  speaking:   'Coach is speaking...',
  muted:      'Mic muted (echo cancel)',
};

export default function SessionScreen({
  status,
  pipeline,
  messages,
  currentAiText,
  analyserRef,
  onEnd,
}) {
  const chatRef = useRef(null);
  const canvasRef = useRef(null);
  const animRef = useRef(null);

  // Auto-scroll chat
  useEffect(() => {
    if (chatRef.current) {
      chatRef.current.scrollTop = chatRef.current.scrollHeight;
    }
  }, [messages, currentAiText]);

  // ─── Waveform visualiser ───
  const drawWaveform = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();

    if (canvas.width !== rect.width * dpr || canvas.height !== rect.height * dpr) {
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      ctx.scale(dpr, dpr);
    }

    const w = rect.width;
    const h = rect.height;
    ctx.clearRect(0, 0, w, h);

    const analyser = analyserRef.current;
    if (!analyser) {
      // Idle sine wave
      const time = performance.now() / 1000;
      ctx.strokeStyle = 'rgba(59, 130, 246, 0.15)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      for (let x = 0; x < w; x++) {
        const y = h / 2 + Math.sin(x * 0.04 + time * 1.5) * 3;
        x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.stroke();
    } else {
      const bufLen = analyser.frequencyBinCount;
      const data = new Uint8Array(bufLen);
      analyser.getByteTimeDomainData(data);

      const colors = {
        listening:  'rgba(34, 197, 94, 0.6)',
        processing: 'rgba(245, 158, 11, 0.6)',
        thinking:   'rgba(139, 92, 246, 0.5)',
        speaking:   'rgba(59, 130, 246, 0.7)',
        muted:      'rgba(107, 114, 128, 0.3)',
      };
      ctx.strokeStyle = colors[pipeline] || 'rgba(59, 130, 246, 0.35)';
      ctx.lineWidth = 2;
      ctx.beginPath();
      const sliceW = w / bufLen;
      let x = 0;
      for (let i = 0; i < bufLen; i++) {
        const v = data[i] / 128.0;
        const y = (v * h) / 2;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        x += sliceW;
      }
      ctx.lineTo(w, h / 2);
      ctx.stroke();
    }

    animRef.current = requestAnimationFrame(drawWaveform);
  }, [analyserRef, pipeline]);

  useEffect(() => {
    animRef.current = requestAnimationFrame(drawWaveform);
    return () => {
      if (animRef.current) cancelAnimationFrame(animRef.current);
    };
  }, [drawWaveform]);

  const hasMessages = messages.length > 0 || currentAiText;

  return (
    <div className="session-container">
      <div className="bg-mesh" />

      {/* Header */}
      <div className="session-header">
        <div className="session-logo">
          <div className="session-logo-icon">🎓</div>
          <span>Study Coach</span>
        </div>
        <div className="header-right">
          <div className={`connection-badge ${status}`}>
            <div className="connection-dot" />
            <span>{status === 'connected' ? 'Connected' : 'Connecting...'}</span>
          </div>
          <button className="end-btn" onClick={onEnd}>End Session</button>
        </div>
      </div>

      {/* Chat */}
      <div className="chat-area" ref={chatRef}>
        {!hasMessages && (
          <div className="empty-state">
            <div className="icon">🎙️</div>
            <p>Just start speaking. The AI coach will automatically detect when you pause and respond.</p>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} className={`message ${msg.role}`}>
            <div className="msg-label">{msg.role === 'user' ? 'You' : 'Coach'}</div>
            {msg.text}
          </div>
        ))}

        {/* Streaming AI message */}
        {currentAiText && (
          <div className="message ai">
            <div className="msg-label">Coach</div>
            {currentAiText}
            <div className="typing-dots">
              <span /><span /><span />
            </div>
          </div>
        )}
      </div>

      {/* Controls */}
      <div className="controls-area">
        <div className="waveform-wrap">
          <canvas ref={canvasRef} />
        </div>

        <div className={`pipeline-status ${pipeline}`}>
          <div className="pulse-ring" />
          <span>{PIPELINE_LABELS[pipeline] || 'Ready'}</span>
        </div>
      </div>
    </div>
  );
}
