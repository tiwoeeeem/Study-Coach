# Study Voice Coach (Scalable Voice AI Gateway)

An ultra-low latency, Highly-Concurrent Voice AI Pipeline designed to act as a conversational study coach. 
It ingests raw binary audio streams over WebSockets, intelligently chunks the audio using PyTorch Silero VAD, routes the audio to a scalable ZeroMQ worker pool for Speech-To-Text (Faster-Whisper), processes the text through a Groq-powered LLM, and streams the responses back via a Text-To-Speech (Edge-TTS) worker.

## Architecture

The system is built on a **Many-to-Few (M:N) Architecture**. Instead of loading ML models 1:1 for every user (which crashes GPUs/CPUs and wastes memory), this architecture decouples the WebSocket connection handling from the heavy ML inference.

1. **FastAPI Gateway (`gateway_service.py`)**: 
   - Handles 1000s of lightweight WebSocket connections.
   - Runs PyTorch Silero VAD (Voice Activity Detection) natively to chunk raw `.pcm` audio streams by isolating active speech from silence.
   - Forwards audio chunks over ZeroMQ.
2. **ZeroMQ Worker Pools**:
   - `stt_worker.py`: Subscribes to STT jobs using a ZeroMQ `ROUTER` socket. It batches inference through CTranslate2 `faster-whisper`.
   - `tts_worker.py`: Subscribes to TTS jobs, synthezises speech via `edge-tts` and streams bytes back to the gateway.
3. **LLM Processor**:
   - The Gateway streams the transcribed text to a Groq LLM (e.g. `llama3-8b-8192`). 
   - It parses sentence clauses in real-time and streams them to the TTS worker to achieve ultra-low Time-To-First-Token (TTFT) latency.

## Features
- **Real-Time VAD Chunking**: Accurately detects when the user starts and stops speaking using Silero VAD.
- **Thread-Safe ML Execution**: Isolated model states per-connection to prevent multi-threading memory corruption on the CPU/GPU.
- **Prometheus Metrics**: Live metrics dashboard for tracking VAD latency, STT/TTS processing times, and LLM TTFT.
- **Edge-Case Resilient**: Full ASGI `try-except-finally` hardening prevents hanging workers during sudden client disconnects, malformed JSON configs, or silent clients.

## Installation & Setup

1. **Clone & Environment**:
```bash
git clone https://github.com/yourusername/study-voice-coach.git
cd study-voice-coach
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. **API Keys**:
Export your Groq API key for the LLM.
```bash
export GROQ_API_KEY="your-groq-api-key"
# Note: Use export GROQ_API_KEY="dummy" to bypass the LLM for local load testing.
```

3. **Start the ZeroMQ Workers**:
Run each worker in a separate terminal:
```bash
python stt_worker.py --workers 4 --precision int8
python tts_worker.py
```

4. **Start the API Gateway**:
```bash
uvicorn gateway_service:app --port 8000
```

## Load Testing
The repository includes a rigorous load-testing suite (`load_test.py`) that simulates multiple clients connecting concurrently. 

**Latest Benchmark Results** (8 concurrent clients + 3 edge cases):
- **VAD Latency**: 1.3 ms
- **LLM Time-To-First-Token**: ~0.5 ms
- **TTS Generation**: 1.15 s
- **Edge Cases**: 100% Pass (No memory leaks on Disconnect, Malformed JSON, or Silent streams).

## Metrics Dashboard
Navigate to `http://localhost:8000/dashboard` while the server is running to view the live Prometheus metrics parsed into a clean HTML table.
