# traffic/detector.py — YOLO vehicle counting + 2×2 visualization

import cv2
import numpy as np
import torch
from ultralytics import YOLO
from config import VEHICLE_CLASSES, MODEL_NAME


class VehicleDetector:
    def __init__(self):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[YOLO] Device: {device}")
        self.model = YOLO(MODEL_NAME)
        self.model.to(device)
        print(f"[YOLO] Model ready: {MODEL_NAME}\n")

    def count(self, image_path: str) -> int:
        try:
            img = cv2.imread(image_path)
            if img is None:
                return 0
            results = self.model(img)[0]
            return sum(1 for b in results.boxes if int(b.cls[0]) in VEHICLE_CLASSES)
        except Exception as e:
            print(f"[YOLO] Count error: {e}")
            return 0

    def visualize(self, images: dict, counts: dict, siren_sides: list):
        try:
            frames = [self._draw(s, p, counts, siren_sides) for s, p in images.items()]
            grid   = np.vstack([np.hstack(frames[:2]), np.hstack(frames[2:])])
            cv2.imshow("Smart Traffic — 4 Sides", grid)
            cv2.waitKey(1)
        except Exception as e:
            print(f"[VIZ] {e}")

    def _draw(self, side, path, counts, siren_sides):
        try:
            img = cv2.imread(path)
            if img is None: raise FileNotFoundError
            for box in self.model(img)[0].boxes:
                if int(box.cls[0]) in VEHICLE_CLASSES:
                    x1, y1, x2, y2 = map(int, box.xyxy[0].cpu().numpy())
                    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        except Exception:
            img = np.zeros((300, 500, 3), dtype=np.uint8)

        if side in siren_sides:
            ov  = img.copy()
            cv2.rectangle(ov, (0, 0), (img.shape[1], img.shape[0]), (0, 0, 255), -1)
            img = cv2.addWeighted(img, 0.72, ov, 0.28, 0)
            cv2.putText(img, "SIREN!", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (255, 255, 255), 3)

        cv2.putText(img, f"Side {side}: {counts.get(side,0)} vehicles",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        return cv2.resize(img, (500, 300))