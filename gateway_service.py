import asyncio
import json
import os
import re
import time
import numpy as np
import torch
import zmq.asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from groq import AsyncGroq
from prometheus_client import make_asgi_app, Histogram

# Configuration
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
STT_WORKER_ADDR = os.getenv("STT_WORKER_ADDR", "tcp://localhost:5555")
TTS_WORKER_ADDR = os.getenv("TTS_WORKER_ADDR", "tcp://localhost:5556")
SAMPLE_RATE = 16000
VAD_CHUNK_SIZE = 512
MAX_SPEECH_DURATION_SEC = 5.0
VAD_THRESHOLD = 0.5
SILENCE_DURATION_SEC = 0.5

SYSTEM_PROMPT = """You are an elite, highly engaging Voice AI Study Coach. You are conversing with a student via a low-latency speech-to-speech interface.

CRITICAL INSTRUCTIONS FOR SPOKEN AUDIO:
- Keep it Brief: Limit your responses to 1-3 short sentences. The user is talking to you, not reading a textbook.
- No Formatting: Do NOT use markdown, asterisks, bullet points, code blocks, or special characters. The text you generate is being sent directly to a Text-to-Speech engine.
- Spell Out Numbers/Symbols: Write "one hundred percent" instead of "100%", and "equals" instead of "=".
- Conversational Tone: Use filler words naturally (e.g., "Got it," "Right," "Let's see"). Ask quick follow-up questions to test their understanding.
- Socratic Method: Do not just give the answer. Guide the student to the answer by asking targeted, thought-provoking questions."""

vad_model = None
vad_utils = None
groq_client = None
zmq_context = None

# Prometheus Metrics
vad_processing_metric = Histogram('voiceai_vad_processing_seconds', 'Time spent running VAD on an audio chunk')
stt_processing_metric = Histogram('voiceai_stt_processing_seconds', 'STT Worker Round Trip Time')
llm_ttft_metric = Histogram('voiceai_llm_ttft_seconds', 'LLM Time To First Token')
tts_processing_metric = Histogram('voiceai_tts_processing_seconds', 'TTS Worker Round Trip Time')

app = FastAPI()

# Mount Prometheus endpoint
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

# Serve React frontend build assets
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend_dist")
if os.path.isdir(os.path.join(FRONTEND_DIR, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIR, "assets")), name="assets")

@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse("<h1>Frontend not built. Run: cd frontend && npm run build</h1>", status_code=503)

@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard():
    with open("dashboard.html", "r") as f:
        return f.read()

@app.on_event("startup")
async def startup_event():
    global vad_model, vad_utils, groq_client, zmq_context
    
    if not GROQ_API_KEY:
        print("CRITICAL ERROR: GROQ_API_KEY is not set. Please export it before running the server.")
        
    print("Loading VAD model (Silero)...")
    vad_model, vad_utils = torch.hub.load(repo_or_dir='snakers4/silero-vad',
                                      model='silero_vad',
                                      force_reload=False,
                                      trust_repo=True)
    
    print("Initializing Groq client...")
    groq_client = AsyncGroq(api_key=GROQ_API_KEY)
    
    print("Initializing ZeroMQ Async Context...")
    zmq_context = zmq.asyncio.Context()
    print("Gateway ready to receive WebSockets.")

async def vad_processor_task(audio_in_queue: asyncio.Queue, stt_req_queue: asyncio.Queue, ws_out_queue: asyncio.Queue):
    """Reads incoming audio, runs VAD, and chunks it for STT."""
    import copy
    local_vad_model = copy.deepcopy(vad_model)
    local_vad_model.reset_states()
    
    buffer = bytearray()
    speech_buffer = bytearray()
    is_speaking = False
    silence_frames = 0
    speech_frames = 0
    
    VAD_CHUNK_SIZE = 512
    bytes_per_chunk = VAD_CHUNK_SIZE * 2
    
    try:
        while True:
            audio_chunk = await audio_in_queue.get()
            if audio_chunk is None:
                await stt_req_queue.put(None)
                break
                
            buffer.extend(audio_chunk)
            
            while len(buffer) >= bytes_per_chunk:
                frame = buffer[:bytes_per_chunk]
                del buffer[:bytes_per_chunk]
                
                t0 = time.perf_counter()
                audio_int16 = np.frombuffer(frame, dtype=np.int16)
                audio_float32 = audio_int16.astype(np.float32) / 32768.0
                
                # Offload VAD inference to thread pool to avoid blocking the event loop
                tensor = torch.from_numpy(audio_float32)
                speech_prob = await asyncio.to_thread(lambda: local_vad_model(tensor, 16000).item())
                vad_processing_metric.observe(time.perf_counter() - t0)
                
                if speech_prob > 0.5:
                    if not is_speaking:
                        is_speaking = True
                        silence_frames = 0
                    speech_frames += 1
                    speech_buffer.extend(frame)
                else:
                    if is_speaking:
                        silence_frames += 1
                        speech_buffer.extend(frame)
                
                reached_silence = is_speaking and silence_frames > 20
                reached_max_duration = is_speaking and speech_frames > 300
                
                if is_speaking and (reached_max_duration or reached_silence):
                    print(f"VAD chunking: Max Duration: {reached_max_duration}, Silence: {reached_silence}")
                    
                    # Notify frontend INSTANTLY that we caught the speech and are transcribing
                    await ws_out_queue.put(("text", json.dumps({"type": "vad_triggered"})))
                    
                    await stt_req_queue.put(bytes(speech_buffer))
                    speech_buffer = bytearray()
                    is_speaking = False
                    silence_frames = 0
                    speech_frames = 0
    except Exception as e:
        print(f"VAD Error: {e}")
        raise

async def stt_client_task(stt_req_queue: asyncio.Queue, llm_queue: asyncio.Queue, ws_out_queue: asyncio.Queue, stt_model: str):
    """Sends audio over ZMQ to STT worker and receives transcribed text.
    Forwards the client's model preference as a multi-part message [model, audio].
    Emits transcript events to ws_out_queue for the frontend."""
    stt_socket = zmq_context.socket(zmq.REQ)
    stt_socket.connect(STT_WORKER_ADDR)
    
    try:
        while True:
            audio_bytes = await stt_req_queue.get()
            if audio_bytes is None:
                await llm_queue.put(None)
                break
                
            t0 = time.perf_counter()
            await stt_socket.send_multipart([stt_model.encode(), audio_bytes])
            text = await stt_socket.recv_string()
            stt_processing_metric.observe(time.perf_counter() - t0)
            
            if text:
                print(f"STT Gateway received: {text}")
                await ws_out_queue.put(("text", json.dumps({"type": "user", "text": text})))
                await llm_queue.put(text)
    except Exception as e:
        print(f"STT Client Task Error: {e}")
        raise
    finally:
        stt_socket.close()

async def llm_processor_task(llm_queue: asyncio.Queue, tts_req_queue: asyncio.Queue, ws_out_queue: asyncio.Queue):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    try:
        while True:
            user_text = await llm_queue.get()
            if user_text is None:
                await tts_req_queue.put(None)
                break
                
            messages.append({"role": "user", "content": user_text})
            
            if os.getenv("GROQ_API_KEY") == "dummy":
                async def dummy_stream():
                    class Delta: content = " Hello, this is a simulated LLM response for load testing."
                    class Choice: delta = Delta()
                    class Chunk: choices = [Choice()]
                    yield Chunk()
                    await asyncio.sleep(0.1)
                stream = dummy_stream()
            else:
                stream = await groq_client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=messages,
                    stream=True,
                )
            
            current_clause = ""
            full_response = ""
            
            t0 = time.perf_counter()
            first_token_received = False
            
            async for chunk in stream:
                token = chunk.choices[0].delta.content
                if token:
                    if not first_token_received:
                        llm_ttft_metric.observe(time.perf_counter() - t0)
                        first_token_received = True
                        
                    full_response += token
                    current_clause += token
                    
                    # Stream each token to the frontend for live typing effect
                    await ws_out_queue.put(("text", json.dumps({"type": "ai_delta", "text": token})))
                    
                    match = re.search(r'([.!?]+)', current_clause)
                    if match:
                        split_idx = match.end()
                        clause_to_send = current_clause[:split_idx].strip()
                        remainder = current_clause[split_idx:].lstrip()
                        
                        if clause_to_send:
                            print(f"LLM Clause: {clause_to_send}")
                            await tts_req_queue.put(clause_to_send)
                            
                        current_clause = remainder
            
            if current_clause.strip():
                print(f"LLM Clause (Final): {current_clause.strip()}")
                await tts_req_queue.put(current_clause.strip())
                
            await ws_out_queue.put(("text", json.dumps({"type": "ai_done"})))
            messages.append({"role": "assistant", "content": full_response})
            
    except Exception as e:
        print(f"LLM Processor Error: {e}")
        raise

async def tts_client_task(tts_req_queue: asyncio.Queue, ws_out_queue: asyncio.Queue, tts_voice: str):
    """Sends text over ZMQ to TTS worker and receives synthesized audio bytes.
    Forwards the client's voice preference as a multi-part message [voice, text]."""
    tts_socket = zmq_context.socket(zmq.REQ)
    tts_socket.connect(TTS_WORKER_ADDR)
    
    try:
        while True:
            text = await tts_req_queue.get()
            if text is None:
                break
                
            print(f"TTS Gateway sending: [{tts_voice}] {text}")
            t0 = time.perf_counter()
            await tts_socket.send_multipart([tts_voice.encode(), text.encode()])
            audio_bytes = await tts_socket.recv()
            tts_processing_metric.observe(time.perf_counter() - t0)
            
            if audio_bytes:
                chunk_size = 4096
                for i in range(0, len(audio_bytes), chunk_size):
                    await ws_out_queue.put(("binary", audio_bytes[i:i+chunk_size]))
    except Exception as e:
        print(f"TTS Client Task Error: {e}")
        raise
    finally:
        tts_socket.close()

@app.websocket("/ws/audio")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("Client connected via WebSocket.")
    
    # Read the first message as JSON config
    config_raw = await websocket.receive_text()
    try:
        config = json.loads(config_raw)
    except json.JSONDecodeError:
        print(f"Malformed config received, closing connection.")
        await websocket.close(code=1003, reason="Invalid JSON config")
        return
    tts_voice = config.get("tts_voice", "en-US-JennyNeural")
    stt_model = config.get("stt_model", "distil-large-v3")
    print(f"Client config — Voice: {tts_voice} | STT: {stt_model}")
    
    audio_in_queue = asyncio.Queue()
    stt_req_queue = asyncio.Queue()
    llm_queue = asyncio.Queue()
    tts_req_queue = asyncio.Queue()
    ws_out_queue = asyncio.Queue()  # Unified: ("binary", bytes) or ("text", str)
    
    tasks = [
        asyncio.create_task(vad_processor_task(audio_in_queue, stt_req_queue, ws_out_queue)),
        asyncio.create_task(stt_client_task(stt_req_queue, llm_queue, ws_out_queue, stt_model)),
        asyncio.create_task(llm_processor_task(llm_queue, tts_req_queue, ws_out_queue)),
        asyncio.create_task(tts_client_task(tts_req_queue, ws_out_queue, tts_voice))
    ]
    
    async def receiver():
        try:
            while True:
                data = await websocket.receive_bytes()
                await audio_in_queue.put(data)
        except WebSocketDisconnect:
            print("Client disconnected.")
            await audio_in_queue.put(None)
            
    async def sender():
        """Dispatches both binary audio and text transcript messages."""
        try:
            while True:
                msg_type, data = await ws_out_queue.get()
                if msg_type == "binary":
                    await websocket.send_bytes(data)
                else:
                    await websocket.send_text(data)
        except Exception as e:
            print(f"Sender error: {e}")
            
    receiver_task = asyncio.create_task(receiver())
    sender_task = asyncio.create_task(sender())
    
    all_tasks = tasks + [receiver_task, sender_task]
    
    done, pending = await asyncio.wait(
        all_tasks,
        return_when=asyncio.FIRST_COMPLETED
    )
    
    for task in pending:
        task.cancel()
        
    try:
        await websocket.close()
    except Exception:
        pass
    print("Cleaned up WebSocket connection and background tasks.")
