import asyncio
import json
import time
import re
import aiohttp
import websockets
import edge_tts
import miniaudio
from rich.console import Console
from rich.table import Table

console = Console()

URI = "ws://localhost:8000/ws/audio"
CHUNK_SIZE = 2048
CHUNK_INTERVAL = 0.064  # 64ms for 2048 bytes at 16kHz 16-bit
SILENCE_DURATION = 1.5

async def generate_payload(text: str) -> bytes:
    """Dynamically generates 16kHz PCM audio for a given text."""
    comm = edge_tts.Communicate(text, "en-US-JennyNeural")
    mp3_chunks = []
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            mp3_chunks.append(chunk["data"])
    mp3_data = b"".join(mp3_chunks)
    decoded = miniaudio.decode(mp3_data, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=16000)
    return decoded.samples.tobytes()

async def simulate_normal_client(client_id: int):
    """Simulates a normal client connecting, speaking a unique sentence, and waiting for a response."""
    text = f"Hello, I am client number {client_id}. How are you doing today?"
    audio_bytes = await generate_payload(text)
    
    start_time = time.time()
    try:
        async with websockets.connect(URI) as ws:
            # Send config
            await ws.send(json.dumps({"tts_voice": "en-US-JennyNeural", "stt_model": "distil-large-v3"}))
            
            # Stream audio in real-time chunks
            for i in range(0, len(audio_bytes), CHUNK_SIZE):
                await ws.send(audio_bytes[i:i+CHUNK_SIZE])
                await asyncio.sleep(CHUNK_INTERVAL)
            
            # Stream silence to trigger VAD
            silence_chunks = int(SILENCE_DURATION / CHUNK_INTERVAL)
            silence_payload = bytes(CHUNK_SIZE)
            
            silence_start = time.time()
            for _ in range(silence_chunks):
                await ws.send(silence_payload)
                await asyncio.sleep(CHUNK_INTERVAL)
            
            # Await TTS response
            response = await asyncio.wait_for(ws.recv(), timeout=60.0)
            rtt = time.time() - silence_start
            
            return {"id": client_id, "status": "Success", "rtt": rtt, "bytes_received": len(response)}
            
    except Exception as e:
        return {"id": client_id, "status": f"Failed: {repr(e)}", "rtt": 0, "bytes_received": 0}


async def simulate_edge_disconnect():
    """Simulates a client that abruptly disconnects mid-speech."""
    audio_bytes = await generate_payload("I am about to disconnect.")
    
    try:
        async with websockets.connect(URI) as ws:
            await ws.send(json.dumps({"tts_voice": "en-US-JennyNeural", "stt_model": "distil-large-v3"}))
            # Stream half the audio then disconnect
            for i in range(0, len(audio_bytes) // 2, CHUNK_SIZE):
                await ws.send(audio_bytes[i:i+CHUNK_SIZE])
                await asyncio.sleep(CHUNK_INTERVAL)
            # Abrupt disconnect
            return {"id": "Edge (Disconnect)", "status": "Success (Expected Drop)", "rtt": 0, "bytes_received": 0}
    except Exception as e:
        return {"id": "Edge (Disconnect)", "status": f"Failed: {repr(e)}", "rtt": 0, "bytes_received": 0}


async def simulate_edge_malformed():
    """Simulates a client sending malformed config."""
    try:
        async with websockets.connect(URI) as ws:
            await ws.send("NOT JSON")
            # The server might drop it or ignore it. Let's see if we get disconnected.
            await asyncio.wait_for(ws.recv(), timeout=2.0)
            return {"id": "Edge (Malformed)", "status": "Unexpectedly kept alive", "rtt": 0, "bytes_received": 0}
    except Exception as e:
        return {"id": "Edge (Malformed)", "status": f"Success (Expected Fail: {repr(e)})", "rtt": 0, "bytes_received": 0}


async def simulate_edge_silent():
    """Simulates a client sending pure silence for a long time."""
    try:
        async with websockets.connect(URI) as ws:
            await ws.send(json.dumps({"tts_voice": "en-US-JennyNeural", "stt_model": "distil-large-v3"}))
            silence_payload = bytes(CHUNK_SIZE)
            for _ in range(50):
                await ws.send(silence_payload)
                await asyncio.sleep(CHUNK_INTERVAL)
            return {"id": "Edge (Silent)", "status": "Success (No crash)", "rtt": 0, "bytes_received": 0}
    except Exception as e:
        return {"id": "Edge (Silent)", "status": f"Failed: {repr(e)}", "rtt": 0, "bytes_received": 0}


def parse_prometheus(text, metric_name):
    sum_match = re.search(f"{metric_name}_sum\\s+([\\d\\.]+)", text)
    count_match = re.search(f"{metric_name}_count\\s+([\\d\\.]+)", text)
    if sum_match and count_match:
        s = float(sum_match.group(1))
        c = float(count_match.group(1))
        return s / c if c > 0 else 0
    return 0


async def fetch_metrics():
    """Fetches metrics from the Gateway and prints a beautiful summary."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get("http://localhost:8000/metrics") as resp:
                text = await resp.text()
                
                vad = parse_prometheus(text, "voiceai_vad_processing_seconds")
                stt = parse_prometheus(text, "voiceai_stt_processing_seconds")
                llm = parse_prometheus(text, "voiceai_llm_ttft_seconds")
                tts = parse_prometheus(text, "voiceai_tts_processing_seconds")
                
                table = Table(title="Gateway Metrics Summary")
                table.add_column("Pipeline Stage", style="cyan")
                table.add_column("Average Latency", style="magenta")
                
                table.add_row("VAD Latency", f"{(vad * 1000):.1f} ms")
                table.add_row("STT Transcription", f"{stt:.2f} s")
                table.add_row("LLM Time-To-First-Token", f"{(llm * 1000):.0f} ms")
                table.add_row("TTS Generation", f"{tts:.2f} s")
                
                console.print(table)
        except Exception as e:
            console.print(f"[red]Failed to fetch metrics: {repr(e)}[/red]")


async def main():
    console.print("[bold green]Starting Load & Edge Case Test Suite...[/bold green]")
    
    # Pre-generate unique audio payloads for 8 clients (2x workers)
    console.print("Generating dynamic payloads for 8 concurrent clients...")
    
    tasks = []
    # 8 Normal Clients
    for i in range(1, 9):
        tasks.append(simulate_normal_client(i))
        
    # 3 Edge Cases
    tasks.append(simulate_edge_disconnect())
    tasks.append(simulate_edge_malformed())
    tasks.append(simulate_edge_silent())
    
    console.print(f"Launching {len(tasks)} concurrent WebSocket connections...")
    results = await asyncio.gather(*tasks)
    
    # Print Client Results Table
    table = Table(title="Client Interaction Results")
    table.add_column("Client ID", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("End-to-End RTT", justify="right")
    table.add_column("Bytes Received", justify="right")
    
    for r in results:
        status_color = "green" if "Success" in r["status"] else "red"
        rtt_str = f"{r['rtt']:.2f}s" if r['rtt'] > 0 else "-"
        table.add_row(
            str(r["id"]),
            f"[{status_color}]{r['status']}[/{status_color}]",
            rtt_str,
            str(r["bytes_received"])
        )
        
    console.print(table)
    
    # Fetch and Print Metrics
    console.print("\nFetching final Prometheus metrics...")
    await fetch_metrics()
    
    console.print("[bold green]Test Suite Complete![/bold green]")


if __name__ == "__main__":
    asyncio.run(main())
