import asyncio
import threading
import queue
import signal
import time
import sys
import json
import websockets
import pyaudio

# Audio settings
FORMAT = pyaudio.paInt16
CHANNELS = 1
IN_RATE = 16000   # Microphone rate (Silero VAD / Whisper)
OUT_RATE = 24000  # Edge TTS outputs 24kHz audio
CHUNK = 2048

# ──────────────────────────────────────────────────
# TTS Voice Selection — pick one from the list below
# ──────────────────────────────────────────────────
# Female voices:
#   en-US-JennyNeural        (warm, conversational)
#   en-US-AriaNeural         (professional, clear)
#   en-US-SaraNeural         (friendly, youthful)
#   en-GB-SoniaNeural        (British, polished)
#   en-AU-NatashaNeural      (Australian)
#   en-IN-NeerjaNeural       (Indian English)
#
# Male voices:
#   en-US-GuyNeural          (casual, natural)
#   en-US-DavisNeural        (deep, authoritative)
#   en-US-TonyNeural         (friendly, upbeat)
#   en-GB-RyanNeural         (British, clear)
#   en-AU-WilliamNeural      (Australian)
#   en-IN-PrabhatNeural      (Indian English)
#
# Change this to any voice from the list above:
# Default TTS Voice
DEFAULT_TTS_VOICE = "en-IN-PrabhatNeural"
# Default STT Model
DEFAULT_STT_MODEL = "base.en"

# Cooldown after playback stops before mic re-enables (seconds).
# Prevents the mic from catching the tail echo of the last TTS chunk.
MIC_COOLDOWN_SEC = 0.3

# Thread-safe queues to bridge audio threads <-> async websocket
mic_queue = queue.Queue(maxsize=100)
speaker_queue = queue.Queue(maxsize=200)

# Global shutdown flag
shutdown_event = threading.Event()

# Playback gating: when True, the mic thread drops all frames
is_playing = threading.Event()
last_playback_time = 0.0
playback_lock = threading.Lock()


def mic_thread_fn(stream_in):
    """Dedicated thread: reads from the pre-opened mic stream.
    Drops all frames while TTS audio is playing to prevent feedback loops."""
    global last_playback_time
    print("[Mic Thread] Recording started.")
    try:
        while not shutdown_event.is_set():
            try:
                data = stream_in.read(CHUNK, exception_on_overflow=False)

                # Gate: drop mic data while speaker is active or during cooldown
                if is_playing.is_set():
                    continue
                with playback_lock:
                    if time.monotonic() - last_playback_time < MIC_COOLDOWN_SEC:
                        continue

                mic_queue.put_nowait(data)
            except queue.Full:
                pass
            except OSError:
                break
            except Exception as e:
                print(f"[Mic Thread] Error: {e}")
                break
    finally:
        print("[Mic Thread] Stopped.")


def speaker_thread_fn(stream_out):
    """Dedicated thread: pulls audio bytes from speaker_queue → plays them.
    Sets the is_playing flag so the mic thread knows to drop frames."""
    global last_playback_time
    print("[Speaker Thread] Playback started.")
    try:
        while not shutdown_event.is_set():
            try:
                data = speaker_queue.get(timeout=0.1)

                # Signal mic to mute
                is_playing.set()
                stream_out.write(data)

                # If queue is now empty, we just finished a burst of playback
                if speaker_queue.empty():
                    is_playing.clear()
                    with playback_lock:
                        last_playback_time = time.monotonic()

            except queue.Empty:
                # No audio to play — ensure gate is released
                if is_playing.is_set():
                    is_playing.clear()
                    with playback_lock:
                        last_playback_time = time.monotonic()
                continue
            except OSError:
                break
            except Exception as e:
                print(f"[Speaker Thread] Error: {e}")
                break
    finally:
        is_playing.clear()
        print("[Speaker Thread] Stopped.")


async def websocket_loop(tts_voice, stt_model):
    """Async loop: bridges mic_queue → WebSocket → speaker_queue."""
    uri = "ws://localhost:8000/ws/audio"
    print(f"Connecting to {uri} ...")

    try:
        async with websockets.connect(uri) as ws:
            # Send config as the first message
            config = json.dumps({"tts_voice": tts_voice, "stt_model": stt_model})
            await ws.send(config)
            print(f"Connected! Voice: {tts_voice} | STT: {stt_model}")
            print("Start speaking...\n")

            async def sender():
                while not shutdown_event.is_set():
                    try:
                        data = mic_queue.get_nowait()
                        await ws.send(data)
                    except queue.Empty:
                        await asyncio.sleep(0.01)
                    except (websockets.exceptions.ConnectionClosed, websockets.exceptions.ConnectionClosedError):
                        print("Server closed connection (sender).")
                        shutdown_event.set()
                        break

            async def receiver():
                while not shutdown_event.is_set():
                    try:
                        data = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        if isinstance(data, str):
                            try:
                                msg = json.loads(data)
                                if msg["type"] == "vad_triggered":
                                    print("\n[VAD] Silence detected. Processing speech...")
                                elif msg["type"] == "user":
                                    print(f"[You]: {msg['text']}")
                                elif msg["type"] == "ai_delta":
                                    # Print token inline without newline
                                    sys.stdout.write(msg['text'])
                                    sys.stdout.flush()
                                elif msg["type"] == "ai_done":
                                    print("\n[VAD] Listening...")
                            except json.JSONDecodeError:
                                pass
                        else:
                            speaker_queue.put(data, timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    except queue.Full:
                        pass
                    except (websockets.exceptions.ConnectionClosed, websockets.exceptions.ConnectionClosedError):
                        print("\nServer closed connection.")
                        shutdown_event.set()
                        break

            await asyncio.gather(sender(), receiver())

    except ConnectionRefusedError:
        print("ERROR: Could not connect. Is uvicorn running on localhost:8000?")
    except Exception as e:
        print(f"Connection error: {e}")
    finally:
        shutdown_event.set()


def main():
    import inquirer

    questions = [
        inquirer.List('model',
            message="Select STT Model (smaller = faster, larger = more accurate)",
            choices=[
                'base.en',
                'small.en',
                'medium.en',
                'distil-large-v3',
                'large-v3'
            ],
            default='base.en'
        ),
        inquirer.List('voice',
            message="Select TTS Voice",
            choices=[
                'en-US-JennyNeural (US Female)',
                'en-US-GuyNeural (US Male)',
                'en-US-AriaNeural (US Female)',
                'en-GB-SoniaNeural (UK Female)',
                'en-GB-RyanNeural (UK Male)',
                'en-IN-NeerjaNeural (IN Female)',
                'en-IN-PrabhatNeural (IN Male)'
            ],
            default='en-IN-PrabhatNeural (IN Male)'
        )
    ]
    answers = inquirer.prompt(questions)
    
    if not answers:
        print("Cancelled.")
        return

    # Extract just the ID from the voice choice (before the space)
    selected_model = answers['model']
    selected_voice = answers['voice'].split(' ')[0]

    def handle_signal(sig, frame):
        print("\nShutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Initialize PyAudio ONCE on the main thread
    p = pyaudio.PyAudio()

    try:
        stream_in = p.open(format=FORMAT, channels=CHANNELS, rate=IN_RATE,
                           input=True, frames_per_buffer=CHUNK)
        stream_out = p.open(format=FORMAT, channels=CHANNELS, rate=OUT_RATE,
                            output=True)
    except Exception as e:
        print(f"Failed to open audio devices: {e}")
        p.terminate()
        return

    mic = threading.Thread(target=mic_thread_fn, args=(stream_in,), daemon=True)
    speaker = threading.Thread(target=speaker_thread_fn, args=(stream_out,), daemon=True)
    mic.start()
    speaker.start()

    try:
        asyncio.run(websocket_loop(selected_voice, selected_model))
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        mic.join(timeout=2)
        speaker.join(timeout=2)
        stream_in.stop_stream()
        stream_in.close()
        stream_out.stop_stream()
        stream_out.close()
        p.terminate()
        print("Exited cleanly.")


if __name__ == "__main__":
    main()
