import argparse
import asyncio
import zmq
import zmq.asyncio
import numpy as np
from faster_whisper import WhisperModel
import os

# ──────────────────────────────────────────────────
# STT Model Selection
# ──────────────────────────────────────────────────
VALID_MODELS = {
    "tiny.en", "tiny",
    "base.en", "base",
    "small.en", "small",
    "medium.en", "medium",
    "large-v3",
    "distil-large-v3",
}

# In-memory model cache: { model_name: WhisperModel }
model_cache = {}


def parse_args():
    parser = argparse.ArgumentParser(description="STT Worker (faster-whisper) with M:N Batching")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of concurrent workers for CTranslate2 batching (default: 4)")
    parser.add_argument("--device", type=str, default="cpu",
                        choices=["cpu", "cuda"],
                        help="Device to run on (default: cpu)")
    parser.add_argument("--precision", type=str, default="int8",
                        choices=["int8", "float16", "float32"],
                        help="Compute type / precision (default: int8)")
    parser.add_argument("--port", type=int, default=5555,
                        help="ZeroMQ port to bind (default: 5555)")
    return parser.parse_args()


def get_model(model_name: str, device: str, precision: str, num_workers: int) -> WhisperModel:
    """Load model on first request, return cached instance on subsequent requests."""
    if model_name in model_cache:
        return model_cache[model_name]

    if model_name not in VALID_MODELS:
        print(f"WARNING: Unknown model '{model_name}', falling back to 'distil-large-v3'.")
        model_name = "distil-large-v3"

    print(f"Loading model '{model_name}' (device={device}, precision={precision}, workers={num_workers})...")
    model = WhisperModel(model_name, device=device, compute_type=precision, num_workers=num_workers)
    model_cache[model_name] = model
    print(f"  ✓ '{model_name}' loaded and cached. ({len(model_cache)} model(s) in cache)")
    return model


def process_audio(model: WhisperModel, audio_bytes: bytes) -> str:
    """Blocking function to process audio. Runs in a thread pool."""
    audio_data = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(audio_data, beam_size=5, vad_filter=True)
    text = "".join([segment.text for segment in segments]).strip()
    return text


async def handle_request(socket: zmq.asyncio.Socket, client_id: bytes, empty: bytes, requested_model: str, audio_bytes: bytes, args):
    """Handles a single request concurrently."""
    try:
        if not audio_bytes:
            await socket.send_multipart([client_id, empty, b""])
            return

        # Load/get model
        model = get_model(requested_model, args.device, args.precision, args.workers)

        # Offload transcription to thread pool so the async loop isn't blocked
        text = await asyncio.to_thread(process_audio, model, audio_bytes)
        
        if text:
            print(f"[{requested_model}] Transcribed: {text}")

        # Send response back to the EXACT client via ROUTER socket
        await socket.send_multipart([client_id, empty, text.encode()])
        
    except Exception as e:
        print(f"STT Error: {e}")
        await socket.send_multipart([client_id, empty, b""])


async def main_async():
    args = parse_args()

    context = zmq.asyncio.Context()
    socket = context.socket(zmq.ROUTER)
    socket.bind(f"tcp://*:{args.port}")
    
    print(f"STT Worker (ROUTER) listening on tcp://*:{args.port}")
    print(f"Concurrency: Up to {args.workers} concurrent requests will be batched.")
    print("Models will be loaded on-demand as clients request them.\n")

    while True:
        try:
            # Receive multi-part message from ROUTER:
            # [client_id, empty_frame, model_name, audio_bytes]
            # Wait, gateway is sending: [model_name, audio_bytes]
            # Since gateway uses REQ, it actually sends: [client_id, empty, model_name, audio_bytes]
            parts = await socket.recv_multipart()
            
            client_id = parts[0]
            empty = parts[1]
            
            # The remaining parts are what the Gateway explicitly sent
            if len(parts) >= 4:
                requested_model = parts[2].decode()
                audio_bytes = parts[3]
            else:
                requested_model = "distil-large-v3"
                audio_bytes = parts[2] if len(parts) > 2 else b""

            # Dispatch background task immediately
            asyncio.create_task(handle_request(socket, client_id, empty, requested_model, audio_bytes, args))

        except Exception as e:
            print(f"Router Error: {e}")


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("Shutting down...")

if __name__ == "__main__":
    main()
