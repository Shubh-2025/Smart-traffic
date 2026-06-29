# audio/ws_server.py — WebSocket hub: phones stream audio, manager sees dashboard
#
# Phones open http://<PC-IP>:8000 in their browser (phone_app/index.html).
# Manager opens http://<PC-IP>:8001 in PC browser (manager_app/index.html).
# Both connect to ws://<PC-IP>:8765 for real-time communication.
#
# This module also exposes thread-safe queues so main.py can pull audio
# chunks and feed them to SideListeners for YAMNet inference.

import psutil

import asyncio
import json
import logging
import queue as _queue   # thread-safe queue for audio bridge
import socket
import threading
import time
import webbrowser

# mDNS — advertises this PC as smarttraffic.local on the network
# Install: pip install zeroconf
try:
    from zeroconf import ServiceInfo, Zeroconf
    import struct
    MDNS_AVAILABLE = True
except ImportError:
    MDNS_AVAILABLE = False
    print("[mDNS] zeroconf not installed — run: pip install zeroconf")
from collections import defaultdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import numpy as np
import websockets

from config import WS_PORT, PHONE_HTTP_PORT, MANAGER_HTTP_PORT

log = logging.getLogger("ws_server")

# ── Shared state (thread-safe via asyncio loop + GIL) ────────────────────────
_phones: dict       = {}          # phone_id → {ws, name, side, recording, rms}
_managers: set      = set()       # all connected manager websockets
_phone_counter: int = 0
_lock               = None        # asyncio.Lock — created inside event loop

# Audio queues: main.py reads from these to feed SideListeners
# phone_id → asyncio.Queue of np.ndarray chunks
_phone_audio_queues: dict = {}

# Mapping helpers for main.py
_phone_sides: dict  = {}    # phone_id → side number
_side_to_phone: dict = {}   # side number → phone_id

# Asyncio loop reference (set when server starts)
_loop: asyncio.AbstractEventLoop = None

# Callbacks set by main.py
on_siren_result = None    # called with (side, score, label, active, is_new)
on_decision     = None    # called with decision dict


# ── HTTP static file servers ──────────────────────────────────────────────────

def _make_handler(directory: str):
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)
        def log_message(self, *a): pass
    return H

def _run_http(port: int, directory: str):
    HTTPServer(("0.0.0.0", port), _make_handler(directory)).serve_forever()

def get_local_ip():
    """
    Return the IP address of the interface that is actually
    used to reach the network (Wi-Fi/LAN), ignoring VirtualBox,
    VMware, Hyper-V, etc.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No data is sent; this is only used to determine
        # which local interface Windows would use.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

# ── Broadcast helpers ─────────────────────────────────────────────────────────

async def _broadcast(msg: dict):
    """Send msg to all connected managers."""
    dead = set()
    for ws in _managers:
        try:
            await ws.send(json.dumps(msg))
        except Exception:
            dead.add(ws)
    _managers.difference_update(dead)

def broadcast_siren_to_managers(side, score, label, active, is_new_alert):
    """Thread-safe: called from SideListener inference thread."""
    if _loop and not _loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            _broadcast({
                "type"        : "audio_result",
                "phone_id"    : _side_to_phone.get(side, f"side_{side}"),
                "side"        : side,
                "rms"         : 0.0,
                "siren_score" : round(score, 4),
                "siren_active": active,
                "siren_label" : label,
                "siren_alert" : is_new_alert,
                "top1_label"  : label,
            }),
            _loop,
        )

def broadcast_decision_to_managers(decision: dict):
    """Thread-safe: called from main.py after each traffic cycle."""
    if _loop and not _loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            _broadcast({"type": "signal_decision", **decision}),
            _loop,
        )

def broadcast_level_to_managers(phone_id: str, rms: float):
    """Thread-safe: called from audio worker."""
    if _loop and not _loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            _broadcast({"type": "level", "phone_id": phone_id, "rms": round(rms, 4)}),
            _loop,
        )


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def _handle_ws(ws):
    global _phone_counter

    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    except Exception:
        return

    role = hello.get("role")

    # ── Manager connection ────────────────────────────────────────────────────
    if role == "manager":
        _managers.add(ws)
        log.info("Manager connected")

        # Send current phone list + server state
        await ws.send(json.dumps({
            "type"         : "init",
            "yamnet_ready" : True,
            "phones"       : [
                {
                    "phone_id"    : pid,
                    "name"        : p["name"],
                    "side"        : p["side"],
                    "recording"   : p["recording"],
                    "siren_active": False,
                    "siren_score" : 0.0,
                }
                for pid, p in _phones.items()
            ],
        }))

        try:
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    await _handle_manager_cmd(data)
                except Exception:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            _managers.discard(ws)
            log.info("Manager disconnected")

    # ── Phone connection ──────────────────────────────────────────────────────
    elif role == "phone":
        async with _lock:
            if len(_phones) >= 5:
                await ws.send(json.dumps({"type": "error", "msg": "Server full"}))
                return
            _phone_counter += 1
            phone_id = f"phone_{_phone_counter}"
            side     = int(hello.get("side", _phone_counter))
            _phones[phone_id] = {
                "ws": ws, "name": hello.get("name", f"Phone {_phone_counter}"),
                "side": side, "recording": False, "rms": 0.0, "chunks": 0,
            }
            _phone_audio_queues[phone_id] = _queue.Queue(maxsize=30)  # thread-safe
            _phone_sides[phone_id]  = side
            _side_to_phone[side]    = phone_id

        log.info(f"Phone {phone_id} ({_phones[phone_id]['name']}) — Side {side}")
        await ws.send(json.dumps({"type": "hello", "phone_id": phone_id}))
        await _broadcast({
            "type": "phone_joined",
            "phone_id": phone_id,
            "name": _phones[phone_id]["name"],
            "side": side,
        })

        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    if _phones[phone_id]["recording"]:
                        _phones[phone_id]["chunks"] += 1
                        # Put raw PCM bytes into queue for SideListener
                        try:
                            _phone_audio_queues[phone_id].put_nowait(msg)
                        except _queue.Full:
                            pass
                        # Live RMS level every 5 chunks
                        if _phones[phone_id]["chunks"] % 5 == 0:
                            pcm = np.frombuffer(msg, dtype=np.int16).astype(np.float32) / 32768.0
                            rms = float(np.sqrt(np.mean(pcm ** 2)))
                            _phones[phone_id]["rms"] = rms
                            await _broadcast({"type": "level", "phone_id": phone_id,
                                              "rms": round(rms, 4)})
                elif isinstance(msg, str):
                    try:
                        data = json.loads(msg)
                        if data.get("type") == "recording_state":
                            _phones[phone_id]["recording"] = data["recording"]
                            await _broadcast({
                                "type": "recording_state",
                                "phone_id": phone_id,
                                "recording": data["recording"],
                            })
                    except Exception:
                        pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            async with _lock:
                _phones.pop(phone_id, None)
                _phone_audio_queues.pop(phone_id, None)
                _phone_sides.pop(phone_id, None)
                _side_to_phone.pop(side, None)
            await _broadcast({"type": "phone_left", "phone_id": phone_id})
            log.info(f"Phone {phone_id} disconnected")


    # ── ESP32 hardware mic ────────────────────────────────────────────────────
    elif role == "esp32_mic":
        await _handle_esp32_mic(ws, hello)

    # ── Unknown role ──────────────────────────────────────────────────────────
    else:
        log.warning(f"Unknown role: {role}")
        return


async def _handle_esp32_mic(ws, hello: dict):
    """
    Handle ESP32 hardware mic connection.
    ESP32 sends: [1 byte side][raw PCM16 bytes] per packet.
    We push audio into the same queue that SideListener.feed_audio() reads.
    Dashboard shows ESP32 units as source channels automatically.
    """
    side     = int(hello.get("side", 1))
    phone_id = f"esp32_side_{side}"
    name     = f"ESP32 Side {side}"

    log.info(f"ESP32 mic connected — Side {side}")

    async with _lock:
        _phone_audio_queues[phone_id] = _queue.Queue(maxsize=30)  # thread-safe
        _phone_sides[phone_id]        = side
        _side_to_phone[side]          = phone_id

    # Tell ESP32 it's accepted
    await ws.send(json.dumps({"type": "hello", "side": side}))

    # Tell dashboard a new source appeared
    await _broadcast({
        "type"     : "phone_joined",
        "phone_id" : phone_id,
        "name"     : name,
        "side"     : side,
        "recording": True,    # ESP32 is always recording
    })

    buf = bytearray()
    TARGET = 16000 * 2    # accumulate 1 second of 16-bit audio before queuing

    try:
        async for msg in ws:
            if not isinstance(msg, bytes) or len(msg) < 2:
                continue

            # First byte is side number (sent by ESP32 firmware)
            # — ignore it, we already know the side from hello
            raw = msg[1:]
            buf.extend(raw)

            # Accumulate until we have ~1 second then push to queue
            while len(buf) >= TARGET:
                chunk = bytes(buf[:TARGET])
                buf   = buf[TARGET:]
                try:
                    _phone_audio_queues[phone_id].put_nowait(chunk)
                except _queue.Full:
                    pass   # drop oldest — better than blocking

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        async with _lock:
            _phone_audio_queues.pop(phone_id, None)
            _phone_sides.pop(phone_id, None)
            _side_to_phone.pop(side, None)
        await _broadcast({"type": "phone_left", "phone_id": phone_id})
        log.info(f"ESP32 Side {side} disconnected")


async def _handle_manager_cmd(data: dict):
    cmd      = data.get("cmd")
    phone_id = data.get("phone_id")
    if cmd == "start_all":
        for p in _phones.values():
            try: await p["ws"].send(json.dumps({"type": "cmd", "cmd": "start"}))
            except Exception: pass
    elif cmd == "stop_all":
        for p in _phones.values():
            try: await p["ws"].send(json.dumps({"type": "cmd", "cmd": "stop"}))
            except Exception: pass
    elif cmd in ("start", "stop") and phone_id in _phones:
        try: await _phones[phone_id]["ws"].send(json.dumps({"type": "cmd", "cmd": cmd}))
        except Exception: pass


# ── Server startup ────────────────────────────────────────────────────────────

def start_ws_server(base_dir: str):
    """
    Start WebSocket server + HTTP servers in a background thread.
    base_dir: project root (so phone_app/ and manager_app/ can be found).
    """
    global _loop, _lock

    def _run():
        global _loop, _lock
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _lock = asyncio.Lock()
        try:
            _loop.run_until_complete(_serve(base_dir))
        except OSError as e:
            print(f"[WS] Port {WS_PORT} busy: {e}")
            print("[WS] Kill old process or change WS_PORT in config.py")

    threading.Thread(target=_run, daemon=True).start()

    # HTTP servers
    phone_dir   = str(Path(base_dir) / "phone_app")
    manager_dir = str(Path(base_dir) / "manager_app")
    threading.Thread(target=_run_http, args=(PHONE_HTTP_PORT, phone_dir),   daemon=True).start()
    threading.Thread(target=_run_http, args=(MANAGER_HTTP_PORT, manager_dir), daemon=True).start()

    ip = get_local_ip()

    print("\n" + "═" * 60)
    print("  Smart Traffic — WebSocket Audio Server")
    print("═" * 60)
    print(f"  📱 Phone app     →  http://{ip}:{PHONE_HTTP_PORT}")
    print(f"  🖥  Manager dash  →  http://{ip}:{MANAGER_HTTP_PORT}")
    print(f"  🔌 WebSocket     →  ws://{ip}:{WS_PORT}")
    print("═" * 60 + "\n")
    print(f"  🌐 mDNS hostname →  smarttraffic.local")
    print(f"  ESP32 firmware   →  use smarttraffic.local, no IP needed")
    print("═" * 60 + "\n")

    # Start mDNS
    threading.Thread(target=_start_mdns, args=(ip, WS_PORT), daemon=True).start()

    # Auto-open manager dashboard in default browser after server starts
    def _open_browser():
        time.sleep(4)   # wait for HTTP + WS servers to be fully ready
        url = f"http://localhost:{MANAGER_HTTP_PORT}"
        print(f"[SYSTEM] Opening dashboard -> {url}")
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()


def _start_mdns(ip: str, ws_port: int):
    """
    Advertise this PC as smarttraffic.local via mDNS.
    ESP32 connects to smarttraffic.local:8765 — no IP needed.
    """
    if not MDNS_AVAILABLE:
        return
    try:
        ip_bytes = socket.inet_aton(ip)
        info = ServiceInfo(
            "_smarttraffic._tcp.local.",
            "SmartTraffic._smarttraffic._tcp.local.",
            addresses=[ip_bytes],
            port=ws_port,
            properties={"version": "1.0"},
            server="smarttraffic.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        print(f"[mDNS] Advertising as smarttraffic.local → {ip}:{ws_port}")
    except Exception as e:
        print(f"[mDNS] Failed: {e}")


async def _serve(base_dir: str):
    async with websockets.serve(_handle_ws, "0.0.0.0", WS_PORT, reuse_address=True):
        await asyncio.Future()   # run forever


# ── Audio queue reader (called by main.py bridge thread) ─────────────────────

def get_audio_chunk_nowait(phone_id: str):
    """
    Thread-safe non-blocking read from the audio queue.
    Uses queue.Queue (not asyncio.Queue) so bridge thread can call safely.
    Returns bytes or None if empty.
    """
    q = _phone_audio_queues.get(phone_id)
    if q is None:
        return None
    try:
        return q.get_nowait()   # thread-safe — queue.Queue not asyncio.Queue
    except _queue.Empty:
        return None

def get_connected_phone_ids() -> list:
    return list(_phone_audio_queues.keys())