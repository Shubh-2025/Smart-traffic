# -*- coding: utf-8 -*-
"""
Created on Sat May  9 11:54:47 2026

@author: samra
"""

import cv2
import json
import time
import torch
import random
import numpy as np
import paho.mqtt.client as mqtt
from ultralytics import YOLO
import os

# -------- MQTT CONFIG ----------
MQTT_HOST = "bf10fe86ca344f07b38ce2444db2e9c0.s1.eu.hivemq.cloud"
MQTT_PORT = 8883
MQTT_USER = "Dilraj135"
MQTT_PASS = "Dilraj@123"

TOPIC_CONTROL = "traffic/control"
TOPIC_COUNT_FMT = "traffic/vehicle_count/side{}"

# -------- IMAGE POOL (10 IMAGES) ----------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ALL_IMAGES = [
    os.path.join(BASE_DIR, "images/", "1.jpeg"),
    os.path.join(BASE_DIR, "images/", "2.jpeg"),
    os.path.join(BASE_DIR, "images/", "3.jpeg"),
    os.path.join(BASE_DIR, "images/", "4.jpeg"),
    os.path.join(BASE_DIR, "images/", "5.jpeg"),
    os.path.join(BASE_DIR, "images/", "6.jpeg"),
    os.path.join(BASE_DIR, "images/", "7.jpeg"),
    os.path.join(BASE_DIR, "images/", "8.jpeg"),
    os.path.join(BASE_DIR, "images/", "9.jpeg"),
    os.path.join(BASE_DIR, "images/", "10.jpeg"),
]


# Random initial 4 images
selected = random.sample(ALL_IMAGES, 4)

IMAGES = {
    1: selected[0],
    2: selected[1],
    3: selected[2],
    4: selected[3],
}

VEHICLE_CLASSES = [2, 3, 5, 7]  # car, motorcycle, bus, truck

# -------- MQTT CONNECT ----------
client = mqtt.Client()
client.username_pw_set(MQTT_USER, MQTT_PASS)
client.tls_set()
client.tls_insecure_set(True)
client.connect(MQTT_HOST, MQTT_PORT)

# -------- YOLO MODEL ----------
# =========================================================
# DEVICE SETUP
# =========================================================

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

print(f"\n🚀 Using Device: {DEVICE}")

# =========================================================
# LOAD YOLO26x MODEL
# =========================================================

MODEL_NAME = "yolov8l.pt"
# MODEL_NAME = "yolo26x.pt"

model = YOLO(MODEL_NAME)

model.to(DEVICE)

print(f"✅ Model Loaded: {MODEL_NAME}")
# -------- VEHICLE COUNT ----------
def count_vehicles(image_path):
    img = cv2.imread(image_path)
    if img is None:
        print("Image missing:", image_path)
        return 0

    results = model(img)[0]
    count = 0

    for box in results.boxes:
        cls = int(box.cls[0])
        if cls in VEHICLE_CLASSES:
            count += 1

    return count

# -------- VISUALIZATION ----------
def visualize_all_sides(counts):
    imgs = []

    for side, image_path in IMAGES.items():
        img = cv2.imread(image_path)
        results = model(img)[0]

        for box in results.boxes:
            cls = int(box.cls[0])
            if cls in VEHICLE_CLASSES:
                x1, y1, x2, y2 = map(int, box.xyxy[0].cpu().numpy())
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)

        text = f"Side {side}: {counts[side]} vehicles"
        cv2.putText(img, text, (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

        img = cv2.resize(img, (500, 300))
        imgs.append(img)

    top = np.hstack((imgs[0], imgs[1]))
    bottom = np.hstack((imgs[2], imgs[3]))
    final_frame = np.vstack((top, bottom))

    cv2.imshow("Smart Traffic System - 4 Sides", final_frame)
    cv2.waitKey(1)

# -------- IMAGE ROTATION ----------
def update_images_after_green(best_side):
    used = set(IMAGES.values())
    available = list(set(ALL_IMAGES) - used)

    if available:
        IMAGES[best_side] = random.choice(available)

# -------- MAIN LOGIC ----------
counts = {}
wait_time = {1: 0, 2: 0, 3: 0, 4: 0}
smoothed = {1: 0, 2: 0, 3: 0, 4: 0}
consecutive_wins = {1: 0, 2: 0, 3: 0, 4: 0}

MIN_GREEN = 5
MAX_GREEN = 25
FACTOR = 0.1
ALPHA = 0.5
WEIGHT_TRAFFIC = 1.0
WEIGHT_WAIT = 2.5
MAX_CONSECUTIVE = 2

print("\n🚦 Smart Traffic System Started (Infinite Loop)\n")

try:
    while True:
        counts.clear()

        print("\n--- New Traffic Cycle ---")

        # Step 1: Detect vehicles
        for side, image in IMAGES.items():
            c = count_vehicles(image)
            counts[side] = c
            print(f"Side {side} = {c} vehicles")
            client.publish(TOPIC_COUNT_FMT.format(side), str(c))
            client.loop()

        # Step 2: EWMA + waiting
        for s in range(1, 5):
            smoothed[s] = ALPHA * counts[s] + (1 - ALPHA) * smoothed[s]
            wait_time[s] += 1

        # Step 3: Priority score
        scores = {}
        for s in range(1, 5):
            scores[s] = smoothed[s] * WEIGHT_TRAFFIC + wait_time[s] * WEIGHT_WAIT
            if consecutive_wins[s] >= MAX_CONSECUTIVE:
                scores[s] *= 0.6

        # Step 4: Choose green side
        best_side = max(scores, key=scores.get)

        # Step 5: Green time
        green_time = min(
            MAX_GREEN,
            max(MIN_GREEN, int(MIN_GREEN + smoothed[best_side] * FACTOR))
        )

        # Step 6: Update cycle stats
        for s in range(1, 5):
            if s == best_side:
                consecutive_wins[s] += 1
                wait_time[s] = 0
            else:
                consecutive_wins[s] = 0

        # Step 7: Publish MQTT
        payload = {
            "open_side": best_side,
            "green_time": green_time
        }
        client.publish(TOPIC_CONTROL, json.dumps(payload))
        client.loop()

        # Status output
        print("\n--- SMART DECISION ---")
        for s in range(1, 5):
            print(f"Side {s}: Count={counts[s]}, Wait={wait_time[s]}, Score={scores[s]:.2f}")
        print(f"✅ Green Side → {best_side}")
        print(f"⏱ Green Time → {green_time} sec")

        # Step 8: Visualization
        visualize_all_sides(counts)

        # Step 9: Green wait
        time.sleep(green_time)

        # Step 10: Rotate only green side image
        update_images_after_green(best_side)

        time.sleep(2)

except KeyboardInterrupt:
    print("\n🛑 Traffic system stopped manually")
    cv2.destroyAllWindows()
    client.disconnect()