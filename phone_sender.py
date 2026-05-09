"""
phone_sender.py — Run on each phone via Termux (Android)
=========================================================
Streams mic audio as raw PCM16 UDP to the PC.

Setup on phone:
    pkg install python
    pip install sounddevice numpy
    python phone_sender.py --pc-ip 192.168.1.X --side 1

Side 1 → port 5001, Side 2 → port 5002, etc.
"""

import argparse
import socket
import sys
import time

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sys.exit("Install: pip install sounddevice numpy")

SAMPLE_RATE    = 16_000
CHUNK_DURATION = 0.975
CHUNK_SAMPLES  = int(SAMPLE_RATE * CHUNK_DURATION)
BASE_PORT      = 5000


def stream(pc_ip: str, side: int):
    port = BASE_PORT + side
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[Phone Side {side}] Streaming mic → {pc_ip}:{port}")
    print("Press Ctrl-C to stop.\n")

    def callback(indata, frames, time_info, status):
        try:
            mono  = indata[:, 0]
            pcm16 = (mono * 32767).clip(-32768, 32767).astype("int16")
            sock.sendto(pcm16.tobytes(), (pc_ip, port))
        except Exception as e:
            print(f"[Phone] Send error: {e}")

    while True:
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, blocksize=CHUNK_SAMPLES,
                                channels=1, dtype="float32", callback=callback):
                while True:
                    sd.sleep(1000)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"[Phone] Stream error: {e}. Restarting in 3s ...")
            time.sleep(3)

    sock.close()


def main():
    p = argparse.ArgumentParser(description="Phone mic UDP sender")
    p.add_argument("--pc-ip", required=True)
    p.add_argument("--side", type=int, required=True, choices=[1, 2, 3, 4])
    args = p.parse_args()
    stream(args.pc_ip, args.side)


if __name__ == "__main__":
    main()
