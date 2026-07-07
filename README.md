# 🎙️ Study Voice Coach (Scalable Voice AI Gateway)

[![CI — Build, Boot & Test](https://github.com/tiwoeeeem/study-coach/actions/workflows/ci.yml/badge.svg)](https://github.com/tiwoeeeem/study-coach/actions/workflows/ci.yml)

An ultra-low latency, Highly-Concurrent Voice AI Pipeline designed to act as a conversational study coach. 
It ingests raw binary audio streams over WebSockets, intelligently chunks the audio using PyTorch Silero VAD, routes the audio to a scalable ZeroMQ worker pool for Speech-To-Text (Faster-Whisper), processes the text through a Groq-powered LLM, and streams the responses back via a Text-To-Speech (Edge-TTS) worker.

## 🏗 Architecture

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

## 🚀 Features
- **Real-Time VAD Chunking**: Accurately detects when the user starts and stops speaking using Silero VAD.
- **Thread-Safe ML Execution**: Isolated model states per-connection to prevent multi-threading memory corruption on the CPU/GPU.
- **Prometheus Metrics**: Live metrics dashboard for tracking VAD latency, STT/TTS processing times, and LLM TTFT.
- **Edge-Case Resilient**: Full ASGI `try-except-finally` hardening prevents hanging workers during sudden client disconnects, malformed JSON configs, or silent clients.
- **Containerized Microservices**: Each service has its own Dockerfile and isolated dependency set, orchestrated via Docker Compose.
- **CI/CD Pipeline**: GitHub Actions workflow that builds, boots, and runs the full test suite on every push.

## 🐳 Docker Deployment (Recommended)

The fastest way to get the entire stack running:

```bash
# Clone the repository
git clone https://github.com/tiwoeeeem/study-coach.git
cd study-coach

# Set your API key (use "dummy" for testing without a real LLM)
export GROQ_API_KEY="your-groq-api-key"

# Build and start all services
docker compose up --build
```

This boots three containers:
| Service | Container | Port | Description |
|---------|-----------|------|-------------|
| Gateway | `gateway` | `8000` (exposed) | FastAPI + Silero VAD + WebSocket handler |
| STT Worker | `stt-worker` | `5555` (internal) | Faster-Whisper via ZeroMQ ROUTER |
| TTS Worker | `tts-worker` | `5556` (internal) | Edge-TTS via ZeroMQ ROUTER |

To stop everything:
```bash
docker compose down
```

## 💻 Local Development Setup

If you prefer running without Docker:

1. **Clone & Environment**:
```bash
git clone https://github.com/tiwoeeeem/study-coach.git
cd study-coach
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. **API Keys**:
```bash
export GROQ_API_KEY="your-groq-api-key"
```

3. **Start the ZeroMQ Workers** (separate terminals):
```bash
python stt_worker.py --workers 4 --precision int8
python tts_worker.py
```

4. **Start the API Gateway**:
```bash
uvicorn gateway_service:app --port 8000
```

## 🧪 Load Testing
The repository includes a rigorous load-testing suite (`load_test.py`) that simulates multiple clients connecting concurrently. 

```bash
pip install -r requirements.test.txt
python load_test.py
```

**Latest Benchmark Results** (8 concurrent clients + 3 edge cases):
- **VAD Latency**: 1.3 ms
- **LLM Time-To-First-Token**: ~0 ms (Mocked for testing)
- **TTS Generation**: 1.15 s
- **Edge Cases**: 100% Pass (No memory leaks on Disconnect, Malformed JSON, or Silent streams).

## 📊 Metrics Dashboard
Navigate to `http://localhost:8000/dashboard` while the server is running to view the live Prometheus metrics parsed into a clean HTML table.
