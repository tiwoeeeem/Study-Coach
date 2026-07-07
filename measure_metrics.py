import asyncio
import json
import time
import re
import argparse
import aiohttp
import websockets
import miniaudio
from rich.console import Console
from rich.table import Table

console = Console()

URI = "ws://localhost:8000/ws/audio"
METRICS_URL = "http://localhost:8000/metrics"
CHUNK_SIZE = 2048
CHUNK_INTERVAL = 0.064  # 64ms for 2048 bytes at 16kHz 16-bit
SILENCE_DURATION = 1.5

def read_and_convert_audio(filepath: str) -> bytes:
    """Reads any audio file and converts it to 16kHz 16-bit mono PCM using miniaudio, or reads directly if .pcm."""
    try:
        if filepath.lower().endswith(('.pcm', '.raw')):
            with open(filepath, 'rb') as f:
                return f.read()
        
        decoded = miniaudio.decode_file(filepath, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=16000)
        return decoded.samples.tobytes()
    except Exception as e:
        console.print(f"[red]Error reading audio file: {e}[/red]")
        exit(1)

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
            async with session.get(METRICS_URL) as resp:
                text = await resp.text()
                
                vad = parse_prometheus(text, "voiceai_vad_processing_seconds")
                stt = parse_prometheus(text, "voiceai_stt_processing_seconds")
                llm = parse_prometheus(text, "voiceai_llm_ttft_seconds")
                tts = parse_prometheus(text, "voiceai_tts_processing_seconds")
                
                table = Table(title="Resume Metrics Summary")
                table.add_column("Pipeline Stage", style="cyan")
                table.add_column("Average Latency", style="magenta")
                
                table.add_row("VAD Chunking Latency", f"{(vad * 1000):.1f} ms")
                table.add_row("STT Transcription Round Trip", f"{stt:.2f} s")
                table.add_row("LLM Time-To-First-Token (TTFT)", f"{(llm * 1000):.0f} ms")
                table.add_row("TTS Generation Round Trip", f"{tts:.2f} s")
                
                console.print(table)
        except Exception as e:
            console.print(f"[red]Failed to fetch metrics: {repr(e)}[/red]")

async def measure_single_client(audio_bytes: bytes):
    """Simulates a single client sending the audio file and measures End-to-End RTT."""
    start_time = time.time()
    try:
        async with websockets.connect(URI) as ws:
            # Send config
            await ws.send(json.dumps({"tts_voice": "en-US-JennyNeural", "stt_model": "distil-large-v3"}))
            
            # Stream audio in real-time chunks
            console.print("[cyan]Streaming audio chunks to Gateway...[/cyan]")
            for i in range(0, len(audio_bytes), CHUNK_SIZE):
                await ws.send(audio_bytes[i:i+CHUNK_SIZE])
                await asyncio.sleep(CHUNK_INTERVAL)
            
            # Stream silence to trigger VAD
            console.print("[cyan]Streaming silence to trigger VAD...[/cyan]")
            silence_chunks = int(SILENCE_DURATION / CHUNK_INTERVAL)
            silence_payload = bytes(CHUNK_SIZE)
            
            silence_start = time.time()
            for _ in range(silence_chunks):
                await ws.send(silence_payload)
                await asyncio.sleep(CHUNK_INTERVAL)
            
            # Await TTS response
            console.print("[cyan]Waiting for AI response...[/cyan]")
            response = await asyncio.wait_for(ws.recv(), timeout=60.0)
            rtt = time.time() - silence_start
            
            console.print(f"[bold green]Success![/bold green] Received {len(response)} bytes.")
            console.print(f"[bold yellow]End-to-End User Latency (RTT): {rtt:.2f}s[/bold yellow]\n")
            
    except Exception as e:
        console.print(f"[red]Failed during WebSocket interaction: {repr(e)}[/red]")

async def main():
    parser = argparse.ArgumentParser(description="Measure metrics from a sample audio file.")
    parser.add_argument("audio_file", type=str, help="Path to the sample audio file (e.g., sample.wav or test_payload.pcm)")
    args = parser.parse_args()

    console.print(f"[bold green]Starting Metrics Measurement on {args.audio_file}[/bold green]")
    audio_bytes = read_and_convert_audio(args.audio_file)
    console.print(f"Loaded {len(audio_bytes)} bytes of audio data.")

    await measure_single_client(audio_bytes)
    
    console.print("Fetching Prometheus metrics...")
    await fetch_metrics()
    
if __name__ == "__main__":
    asyncio.run(main())
