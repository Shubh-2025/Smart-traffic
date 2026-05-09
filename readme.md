# 🚦 Smart Traffic System with Ambulance Siren Detection

A real-time intelligent traffic control system that uses **YOLOv8** for vehicle detection and **YAMNet** for ambulance siren detection across 4 road sides. Signals are sent to an **ESP32** via **MQTT** over HiveMQ cloud.

---

## ✨ Features

- 🚗 **Vehicle counting** on 4 road sides using YOLOv8 object detection
- 🚨 **Ambulance siren detection** from 4 mobile phones acting as microphones
- 📡 **MQTT control** — sends green signal + timing decisions to ESP32
- ⚡ **Siren priority override** — ambulance side instantly gets green light
- 🔁 **Auto-recovery** — phone disconnections never crash the system
- 📊 **Live visualization** — 2×2 grid with bounding boxes + siren overlays
- 🧠 **Smart scoring** — EWMA smoothing + wait time + consecutive win penalty

---

## 🗂️ Folder Structure

```
Smart-traffic/
│
├── main.py                  ← Entry point, run this
├── config.py                ← All settings (MQTT, thresholds, ports)
├── phone_sender.py          ← Run on each phone via Termux (Android)
├── yolov26x.pt              ← Your YOLO model weights
├── README.md
│
├── images/                  ← Road side images (1.jpeg … 10.jpeg)
│   ├── 1.jpeg
│   ├── 2.jpeg
│   └── ...
│
├── audio/
│   ├── __init__.py
│   └── listeners.py         ← SideListener: UDP / HTTP / device / simulate
│
├── traffic/
│   ├── __init__.py
│   ├── detector.py          ← YOLO vehicle counting + visualization
│   └── controller.py        ← Priority scoring + green signal decision
│
└── utils/
    ├── __init__.py
    ├── yamnet.py             ← YAMNet model load + inference helpers
    └── mqtt_pub.py          ← MQTT publisher with auto-reconnect
```

---

## ⚙️ Installation

> Tested on **Python 3.11** — Windows / Linux

### 1. Clone / download the project

```
Smart-traffic/
├── main.py
├── config.py
└── ...
```

### 2. Install dependencies in order

```bash
pip install numpy==1.26.4
pip install opencv-python
pip install ultralytics
pip install paho-mqtt
pip install scipy sounddevice
pip install tensorflow tensorflow-hub
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
```

> ⚠️ **numpy must be installed first** and pinned to `1.26.4`.  
> TensorFlow and pandas require numpy 1.x — numpy 2.x causes a binary incompatibility crash.

---

## 🚀 Usage

### Run the main system

```bash
# Default — phones stream UDP audio (recommended)
python main.py

# Test without phones (simulated audio)
python main.py --audio-mode simulate

# Use IP Webcam app (HTTP stream from phones)
python main.py --audio-mode http

# USB audio adapters plugged into PC
python main.py --audio-mode device

# List available audio device indices
python main.py --list-devices
```

### Run on each phone (via Termux on Android)

```bash
# Install on phone
pkg install python
pip install sounddevice numpy

# Phone covering Side 1
python phone_sender.py --pc-ip 192.168.1.X --side 1

# Phone covering Side 2
python phone_sender.py --pc-ip 192.168.1.X --side 2
```

Each phone streams raw **PCM16 mono 16kHz UDP** to the PC:

| Side | UDP Port |
|------|----------|
| 1    | 5001     |
| 2    | 5002     |
| 3    | 5003     |
| 4    | 5004     |

---

## 🏗️ System Architecture

```
Phone 1 (Side 1) ──UDP:5001──┐
Phone 2 (Side 2) ──UDP:5002──┤
Phone 3 (Side 3) ──UDP:5003──┼──► PC (main.py)
Phone 4 (Side 4) ──UDP:5004──┘        │
                                       ├── YAMNet → siren detection per side
                                       ├── YOLO   → vehicle count per side
                                       └── MQTT   → ESP32 signal controller
```

### Signal decision flow

```
Vehicle counts (YOLO)
        +                  ──► Priority Score ──► Best Side ──► Green Time
Siren active? (YAMNet)              ↑
                           +1000 boost if siren
```

---

## 📡 MQTT Topics

| Topic | Direction | Payload |
|-------|-----------|---------|
| `traffic/control` | PC → ESP32 | `{"open_side": 2, "green_time": 10, "siren_override": true, "siren_sides": [2]}` |
| `traffic/vehicle_count/side1` | PC → ESP32 | `"5"` |
| `traffic/siren/side1` | PC → ESP32 | `{"side": 1, "score": 0.82, "active": true}` |

---

## ⚙️ Configuration (`config.py`)

All tunable parameters are in one file:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SIREN_THRESHOLD` | `0.25` | YAMNet score (0–1) to declare siren detected |
| `SIREN_DECAY` | `30.0` s | How long siren priority stays active after detection |
| `SIREN_COOLDOWN` | `3.0` s | Min seconds between repeated siren alerts |
| `SIREN_PRIORITY_BOOST` | `1000.0` | Score boost added to siren side |
| `MIN_GREEN` | `5` s | Minimum green light duration |
| `MAX_GREEN` | `25` s | Maximum green light duration |
| `MODEL_NAME` | `yolov26x.pt` | YOLO weights file |
| `PHONE_UDP` | ports 5001–5004 | UDP ports per side |
| `PHONE_HTTP` | `192.168.1.10x:8080` | IP Webcam URLs per side |

---

## 🔇 Audio Modes

| Mode | How it works | Best for |
|------|-------------|----------|
| `udp` | Phone sends raw PCM16 packets via UDP | Prototype / LAN setup |
| `http` | Streams from IP Webcam app on phone | Easy Android setup |
| `device` | USB audio adapter + 3.5mm mic cable | Stable wired setup |
| `simulate` | Generates random audio, injects fake sirens | Testing without phones |

---

## 🛡️ Error Resilience

The system is designed to **never crash** due to a phone or network failure:

| Component | Recovery behaviour |
|-----------|--------------------|
| Phone goes offline (UDP) | `socket.timeout` silently ignored, listener keeps running |
| HTTP stream drops | Reconnects automatically every 5 seconds |
| sounddevice error | Restarts stream every 5 seconds |
| YAMNet inference error | Logged and skipped, does not stop the loop |
| MQTT disconnects | Auto-reconnects in background thread |
| Missing/corrupt image | Returns 0 vehicles, shows black frame |

---

## 🧩 Module Reference

| File | Class / Function | Role |
|------|-----------------|------|
| `main.py` | `run()` | Main loop — orchestrates all modules |
| `config.py` | — | Central settings file |
| `audio/listeners.py` | `SideListener` | Per-side audio capture + YAMNet inference |
| `traffic/detector.py` | `VehicleDetector` | YOLO counting + 2×2 visualization |
| `traffic/controller.py` | `SignalController` | EWMA scoring + green signal decision |
| `utils/yamnet.py` | `load_yamnet()`, `infer()` | YAMNet model + inference helpers |
| `utils/mqtt_pub.py` | `MQTTPublisher` | MQTT with reconnect + publish methods |
| `phone_sender.py` | `stream()` | Phone mic → UDP stream |

---

## 🧪 Quick Test (no phones)

```bash
python main.py --audio-mode simulate
```

The simulate mode randomly injects siren-like tones (~4% chance per audio chunk per side) so you can verify the priority override and MQTT output without any phones connected.

---

## 📋 Requirements Summary

| Package | Version | Purpose |
|---------|---------|---------|
| `numpy` | `==1.26.4` | Must be pinned — TF/pandas incompatible with 2.x |
| `tensorflow` | latest | YAMNet siren detection |
| `tensorflow-hub` | latest | YAMNet model download |
| `torch` | cu118 | YOLO GPU inference |
| `ultralytics` | latest | YOLOv8 vehicle detection |
| `opencv-python` | latest | Image reading + visualization |
| `paho-mqtt` | latest | MQTT to ESP32 |
| `sounddevice` | latest | USB audio capture (device mode) |
| `scipy` | latest | Audio resampling |

---

## 👤 Author

**Dilraj** **Shubhranil** — Smart Traffic Prototype Project  
HiveMQ Cloud · ESP32 · YOLOv8 · YAMNet