import asyncio
import zmq.asyncio

async def main():
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.REQ)
    sock.connect("tcp://localhost:5555")
    
    print("Sending request 1...")
    await sock.send_multipart([b"base.en", b"\x00" * 32000]) # 1 second of silence
    text = await sock.recv_string()
    print(f"Reply 1: {text}")
    
    print("Sending request 2...")
    await sock.send_multipart([b"base.en", b"\x00" * 32000])
    text = await sock.recv_string()
    print(f"Reply 2: {text}")

asyncio.run(main())
