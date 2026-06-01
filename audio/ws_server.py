# audio/ws_server.py — WebSocket hub: phones stream audio, manager sees dashboard
#
# Phones open http://<PC-IP>:8000 in their browser (phone_app/index.html).
# Manager opens http://<PC-IP>:8001 in PC browser (manager_app/index.html).
# Both connect to ws://<PC-IP>:8765 for real-time communication.
#
# This module also exposes thread-safe queues so main.py can pull audio
# chunks and feed them to SideListeners for YAMNet inference.

import asyncio
import json
import logging
import socket
import threading
import time
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
            _phone_audio_queues[phone_id] = asyncio.Queue(maxsize=20)
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
                        except asyncio.QueueFull:
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
        _loop.run_until_complete(_serve(base_dir))

    threading.Thread(target=_run, daemon=True).start()

    # HTTP servers
    phone_dir   = str(Path(base_dir) / "phone_app")
    manager_dir = str(Path(base_dir) / "manager_app")
    threading.Thread(target=_run_http, args=(PHONE_HTTP_PORT, phone_dir),   daemon=True).start()
    threading.Thread(target=_run_http, args=(MANAGER_HTTP_PORT, manager_dir), daemon=True).start()

    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "127.0.0.1"

    print("\n" + "═" * 60)
    print("  Smart Traffic — WebSocket Audio Server")
    print("═" * 60)
    print(f"  📱 Phone app     →  http://{ip}:{PHONE_HTTP_PORT}")
    print(f"  🖥  Manager dash  →  http://{ip}:{MANAGER_HTTP_PORT}")
    print(f"  🔌 WebSocket     →  ws://{ip}:{WS_PORT}")
    print("═" * 60 + "\n")


async def _serve(base_dir: str):
    async with websockets.serve(_handle_ws, "0.0.0.0", WS_PORT):
        await asyncio.Future()   # run forever


# ── Audio queue reader (called by main.py bridge thread) ─────────────────────

def get_audio_chunk_nowait(phone_id: str):
    """
    Non-blocking read of one raw PCM bytes chunk from a phone.
    Returns bytes or None if queue is empty.
    """
    q = _phone_audio_queues.get(phone_id)
    if q is None:
        return None
    try:
        # Safe cross-thread queue.get_nowait via call_soon_threadsafe trick
        # We use a simpler approach: store items in a plain list guarded by lock
        return q.get_nowait()
    except Exception:
        return None

def get_connected_phone_ids() -> list:
    return list(_phone_audio_queues.keys())