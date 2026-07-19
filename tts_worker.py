import asyncio
import zmq
import zmq.asyncio
import edge_tts
import miniaudio

# Default voice (overridden per-request by the Gateway)
DEFAULT_VOICE = "en-US-JennyNeural"

# Fixed output sample rate — must match client.py OUT_RATE
OUTPUT_SAMPLE_RATE = 24000

# Will be detected from the first decode and printed to console
detected_sample_rate = None


async def synthesize(text: str, voice: str) -> bytes:
    """Stream MP3 from Edge TTS with the specified voice, decode to 16-bit PCM at 24kHz."""
    global detected_sample_rate

    communicate = edge_tts.Communicate(text, voice)
    mp3_chunks = []

    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_chunks.append(chunk["data"])

    mp3_data = b"".join(mp3_chunks)

    # First call: detect and log the native rate for debugging
    if detected_sample_rate is None:
        probe = miniaudio.decode(mp3_data, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1)
        detected_sample_rate = probe.sample_rate
        print(f">>> Native Edge TTS sample rate: {detected_sample_rate} Hz")
        print(f">>> Resampling all output to {OUTPUT_SAMPLE_RATE} Hz to match client")

    # Decode and resample to fixed OUTPUT_SAMPLE_RATE so client playback is always correct
    decoded = miniaudio.decode(mp3_data,
                               output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1,
                               sample_rate=OUTPUT_SAMPLE_RATE)

    return decoded.samples.tobytes()


async def handle_request(socket: zmq.asyncio.Socket, client_id: bytes, empty: bytes, voice: str, text: str):
    """Handles a single synthesis request concurrently."""
    try:
        if not text:
            await socket.send_multipart([client_id, empty, b""])
            return

        print(f"Synthesizing [{voice}]: {text}")
        pcm_bytes = await synthesize(text, voice)
        print(f"Synthesized {len(pcm_bytes)} bytes")
        
        # Send response back to the EXACT client via ROUTER socket
        await socket.send_multipart([client_id, empty, pcm_bytes])
        
    except Exception as e:
        print(f"TTS Error: {e}")
        await socket.send_multipart([client_id, empty, b""])


async def main_async():
    print("Starting Edge TTS Worker (ROUTER)...")

    context = zmq.asyncio.Context()
    socket = context.socket(zmq.ROUTER)
    socket.bind("tcp://*:5556")
    print(f"TTS Worker (ROUTER) listening on tcp://*:5556")
    print("Voices will be synthesized dynamically.\n")

    background_tasks = set()

    while True:
        try:
            # Receive multi-part message from ROUTER:
            # [client_id, empty_frame, voice, text]
            parts = await socket.recv_multipart()
            
            client_id = parts[0]
            empty = parts[1]
            
            if len(parts) >= 4:
                voice = parts[2].decode()
                text = parts[3].decode()
            else:
                voice = DEFAULT_VOICE
                text = parts[2].decode() if len(parts) > 2 else ""

            # Dispatch background task immediately
            task = asyncio.create_task(handle_request(socket, client_id, empty, voice, text))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

        except Exception as e:
            print(f"Router Error: {e}")


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("Shutting down...")

if __name__ == "__main__":
    main()
