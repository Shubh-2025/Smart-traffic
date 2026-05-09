## Main Installations

pip install numpy==1.26.4
pip install opencv-python
pip install ultralytics
pip install paho-mqtt
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
pip install tensorflow tensorflow-hub sounddevice scipy
## Folder Structure

Smart traffic/
│
├── main.py
├── yolov26x.pt
└── images/

traffic_system/
├── main.py                  ← run this 
├── config.py                ← all settings in one place
├── phone_sender.py          ← run on each phone via Termux
│
├── audio/
│   └── listeners.py         ← SideListener: UDP/HTTP/device/simulate
│
├── traffic/
│   ├── detector.py          ← YOLO vehicle counting + visualization
│   └── controller.py        ← scoring, siren priority, green time
│
└── utils/
    ├── yamnet.py             ← YAMNet load + inference
    └── mqtt_pub.py          ← MQTT with auto-reconnect