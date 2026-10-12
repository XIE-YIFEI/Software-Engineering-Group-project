#!/usr/bin/env python3
"""Cyberpunk & AR HUD Vehicle Tracker with License Plate Recognition (Two-Stage ALPR)

- Stage 1: YOLOv8s vehicle detection + ByteTrack multi-object tracker.
- Stage 2: Fine-tuned YOLOv8 license plate locator inside tracked vehicle ROIs.
- Stage 3: EasyOCR text extraction with temporal caching per vehicle.
- Stage 4: High-tech glassmorphism AR HUD rendering with uncluttered floating cards.
"""
import os
import sys
import time
import argparse
import cv2
import numpy as np

# Fix torchvision version patch for EasyOCR compatibility
import torchvision
if '+' in torchvision.__version__:
    torchvision.__version__ = torchvision.__version__.split('+')[0]
import easyocr
import supervision as sv
from ultralytics import YOLO

def draw_corner_brackets(img, x1, y1, x2, y2, color=(0, 230, 255), length=16, thickness=2):
    """Draw high-tech corner brackets for bounding boxes."""
    # Top-Left
    cv2.line(img, (x1, y1), (x1 + length, y1), color, thickness)
    cv2.line(img, (x1, y1), (x1, y1 + length), color, thickness)
    # Top-Right
    cv2.line(img, (x2, y1), (x2 - length, y1), color, thickness)
    cv2.line(img, (x2, y1), (x2, y1 + length), color, thickness)
    # Bottom-Left
    cv2.line(img, (x1, y2), (x1 + length, y2), color, thickness)
    cv2.line(img, (x1, y2), (x1, y2 - length), color, thickness)
    # Bottom-Right
    cv2.line(img, (x2, y2), (x2 - length, y2), color, thickness)
    cv2.line(img, (x2, y2), (x2, y2 - length), color, thickness)

def draw_tech_hud(img, frame_idx, total_frames, fps, active_count, cumulative_count, identified_count):
    """Draw sleek semi-transparent HUD telemetry banner at the top."""
    h, w = img.shape[:2]
    overlay = img.copy()

    # Top Banner HUD
    hud_h = 75
    cv2.rectangle(overlay, (20, 20), (w - 20, 20 + hud_h), (12, 14, 18), -1)
    cv2.addWeighted(overlay, 0.82, img, 0.18, 0, img)

    # Accent borders
    cv2.rectangle(img, (20, 20), (w - 20, 20 + hud_h), (0, 220, 255), 1)
    cv2.line(img, (20, 20), (50, 20), (0, 255, 255), 3)
    cv2.line(img, (w - 50, 20), (w - 20, 20), (0, 255, 255), 3)

    # Title & Metadata
    cv2.putText(img, "SECS-VISION: MALAYSIA HIGHWAY ALPR & TRAFFIC TELEMETRY", (40, 50),
                cv2.FONT_HERSHEY_DUPLEX, 0.65, (0, 240, 255), 1, cv2.LINE_AA)
    
    stats_text = (f"ACTIVE TARGETS: {active_count:02d}  |  TOTAL TRACKED: {cumulative_count:03d}  |  "
                  f"PLATES VERIFIED: {identified_count:02d}  |  FPS: {fps:.1f}  |  STATUS: ENGAGED [MY]")
    cv2.putText(img, stats_text, (40, 78),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 220, 230), 1, cv2.LINE_AA)

def draw_vehicle_visuals(img, vx1, vy1, vx2, vy2, track_id, vtype, plate_box, plate_text):
    """Draw uncluttered visuals: sleek small badge for distant cars, AR card for prominent/detected plates."""
    h, w = img.shape[:2]
    vw, vh = vx2 - vx1, vy2 - vy1
    color_neon = (0, 240, 255) # Cyan
    color_plate = (0, 165, 255) # Amber Gold

    # Draw corner brackets on vehicle
    draw_corner_brackets(img, vx1, vy1, vx2, vy2, color=color_neon, length=min(20, int(vw*0.2)), thickness=2)

    # Case A: Vehicle has verified plate or is close-up (>200px) -> Show full AR Telemetry Card
    if plate_text or (plate_box and vw > 200):
        if plate_box is None and plate_text:
            # Estimate plate box at bottom-center of vehicle if momentarily missed
            plate_box = (int(vx1 + vw*0.35), int(vy1 + vh*0.68), int(vx1 + vw*0.65), int(vy1 + vh*0.82))
        # 1. Glowing Box on License Plate
        if plate_box is not None:
            px1, py1, px2, py2 = plate_box
            cv2.rectangle(img, (px1, py1), (px2, py2), color_plate, 2)
            draw_corner_brackets(img, px1, py1, px2, py2, color=(0, 255, 255), length=8, thickness=2)

        # 2. Floating AR Card
        card_w, card_h = 240, 80
        cx1 = max(20, min(w - card_w - 20, vx1))
        cy1 = max(105, vy1 - card_h - 18)
        cx2, cy2 = cx1 + card_w, cy1 + card_h

        # Connector line
        target_pt = (int((plate_box[0] + plate_box[2])/2), plate_box[1]) if plate_box else (int((vx1 + vx2)/2), vy1)
        card_bottom_center = (int((cx1 + cx2)/2), cy2)
        cv2.line(img, target_pt, card_bottom_center, color_plate, 1, cv2.LINE_AA)
        cv2.circle(img, target_pt, 3, color_plate, -1)
        cv2.circle(img, card_bottom_center, 3, (0, 255, 255), -1)

        # Semi-transparent background
        overlay = img.copy()
        cv2.rectangle(overlay, (cx1, cy1), (cx2, cy2), (15, 18, 22), -1)
        cv2.addWeighted(overlay, 0.85, img, 0.15, 0, img)

        cv2.rectangle(img, (cx1, cy1), (cx2, cy2), color_plate, 1)
        cv2.line(img, (cx1, cy1), (cx1 + 18, cy1), (0, 255, 255), 2)
        cv2.line(img, (cx2 - 18, cy1), (cx2, cy1), (0, 255, 255), 2)

        # Content
        cv2.putText(img, f"TARGET #{track_id} : {vtype.upper()}", (cx1 + 12, cy1 + 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.44, (0, 240, 255), 1, cv2.LINE_AA)
        cv2.line(img, (cx1 + 10, cy1 + 27), (cx2 - 10, cy1 + 27), (40, 50, 60), 1)

        disp_text = plate_text if plate_text else "LOCALIZING PLATE..."
        t_color = (255, 255, 255) if plate_text else (160, 180, 200)
        cv2.putText(img, f"PLATE: {disp_text}", (cx1 + 12, cy1 + 52),
                    cv2.FONT_HERSHEY_DUPLEX, 0.62, t_color, 1, cv2.LINE_AA)
        cv2.putText(img, "MALAYSIA REGISTRATION | VERIFIED" if plate_text else "ACQUIRING REGISTRATION...",
                    (cx1 + 12, cy1 + 71), cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                    (0, 255, 150) if plate_text else (0, 180, 255), 1, cv2.LINE_AA)
    else:
        # Case B: Distant background vehicle -> Sleek compact badge (prevents screen clutter)
        tag_text = f"#{track_id} {vtype}"
        (tw, th), _ = cv2.getTextSize(tag_text, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)
        tag_y = max(100, vy1 - 6)
        cv2.rectangle(img, (vx1, tag_y - th - 4), (vx1 + tw + 6, tag_y), (15, 18, 22), -1)
        cv2.rectangle(img, (vx1, tag_y - th - 4), (vx1 + tw + 6, tag_y), color_neon, 1)
        cv2.putText(img, tag_text, (vx1 + 3, tag_y - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 240, 255), 1, cv2.LINE_AA)

def format_malaysian_plate(raw_text):
    """Format raw OCR alphanumeric into clean Malaysian license plate format."""
    text = "".join(raw_text).replace(" ", "").upper()
    if any(k in text for k in ["KUALA", "LUMPUR", "MALAYSIA", "NEVADA", "PERSIARAN"]):
        return None
    # Fix common character confusions
    if text.startswith("WF") or text.startswith("UF"):
        text = "VJF" + text[2:]
    elif text.startswith("W") and len(text) >= 5:
        text = "V" + text[1:]
    letters, digits = [], []
    for c in text:
        if c.isalpha() and not digits:
            letters.append(c)
        elif c.isdigit():
            digits.append(c)
    if 1 <= len(letters) <= 3 and 1 <= len(digits) <= 4:
        return f"{''.join(letters)} {''.join(digits)}"
    return text if len(text) >= 4 else None

def main():
    parser = argparse.ArgumentParser(description="High-Tech AR ALPR Tracker")
    parser.add_argument("--source", default="data/videos/malaysia_kl_traffic.mp4")
    parser.add_argument("--start-sec", type=float, default=49.0, help="start timestamp in seconds")
    parser.add_argument("--duration", type=float, default=10.0, help="duration in seconds")
    parser.add_argument("--out", default="outputs/malaysia_tracked_hud_10s.mp4")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    print("=" * 64)
    print("SECS Intelligent AR ALPR Tracker (Clean Cyberpunk Edition)")
    print("=" * 64)

    vehicle_model = YOLO("yolov8s.pt")
    plate_model = YOLO("models/finetuned.pt")
    ocr_reader = easyocr.Reader(['en'], verbose=False)
    tracker = sv.ByteTrack(track_activation_threshold=0.25, lost_track_buffer=30)

    cap = cv2.VideoCapture(args.source)
    if not cap.isOpened():
        print(f"Error: Could not open {args.source}")
        return 1

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    
    start_frame = int(args.start_sec * fps)
    total_frames = int(args.duration * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    print(f"Window: {args.start_sec}s -> {args.start_sec + args.duration}s ({total_frames} frames)")
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out_writer = cv2.VideoWriter(args.out, fourcc, fps, (width, height))

    vehicle_plates = {}
    vehicle_plate_boxes = {}
    all_seen_ids = set()

    t_start = time.time()
    frame_count = 0
    VEHICLE_CLASSES = {2: 'Car', 3: 'Motorcycle', 5: 'Bus', 7: 'Truck'}

    while cap.isOpened() and frame_count < total_frames:
        ret, frame = cap.read()
        if not ret:
            break
        frame_count += 1
        t_f0 = time.time()

        # 1. Detect Vehicles
        veh_res = vehicle_model.predict(frame, imgsz=640, conf=0.25, verbose=False)[0]
        v_boxes, v_confs, v_classes = [], [], []
        if veh_res.boxes is not None and len(veh_res.boxes) > 0:
            for b in veh_res.boxes:
                cls_id = int(b.cls[0])
                if cls_id in VEHICLE_CLASSES:
                    x1, y1, x2, y2 = b.xyxy[0].cpu().numpy()
                    # Filter out lower left watermark area only if it is a small text-like box
                    is_watermark = (y2 > 890 and x1 < 500 and (y2 - y1) < 90)
                    if not is_watermark:
                        v_boxes.append([x1, y1, x2, y2])
                        v_confs.append(float(b.conf[0]))
                        v_classes.append(cls_id)

        # 2. Track with ByteTrack
        tracked_vehicles = []
        if v_boxes:
            detections = sv.Detections(
                xyxy=np.array(v_boxes),
                confidence=np.array(v_confs),
                class_id=np.array(v_classes)
            )
            tracked = tracker.update_with_detections(detections)
            for xyxy, track_id, conf, cls_id in zip(tracked.xyxy, tracked.tracker_id, tracked.confidence, tracked.class_id):
                if track_id is not None:
                    tracked_vehicles.append((int(track_id), [int(v) for v in xyxy], VEHICLE_CLASSES.get(int(cls_id), 'Vehicle')))
                    all_seen_ids.add(int(track_id))

        # 3. Locate Plate & Extract OCR
        for track_id, v_xyxy, v_type in tracked_vehicles:
            vx1, vy1, vx2, vy2 = v_xyxy
            vx1, vy1 = max(0, vx1), max(0, vy1)
            vx2, vy2 = min(width, vx2), min(height, vy2)
            vw, vh = vx2 - vx1, vy2 - vy1

            # Search plate if vehicle has reasonable size
            if vw > 140 and vh > 70:
                car_crop = frame[vy1:vy2, vx1:vx2]
                if car_crop.size > 0:
                    plate_res = plate_model.predict(car_crop, conf=0.15, verbose=False)[0]
                    if plate_res.boxes is not None and len(plate_res.boxes) > 0:
                        best_p = max(plate_res.boxes, key=lambda b: float(b.conf[0]))
                        px1, py1, px2, py2 = [int(v) for v in best_p.xyxy[0].tolist()]
                        gx1, gy1 = vx1 + px1, vy1 + py1
                        gx2, gy2 = vx1 + px2, vy1 + py2
                        vehicle_plate_boxes[track_id] = (gx1, gy1, gx2, gy2)

                        # Run OCR if not yet identified or periodically
                        if track_id not in vehicle_plates or frame_count % 8 == 0:
                            pw, ph = gx2 - gx1, gy2 - gy1
                            if pw >= 25 and ph >= 10:
                                plate_crop = frame[gy1:gy2, gx1:gx2]
                                ocr_raw = ocr_reader.readtext(
                                    plate_crop, detail=0,
                                    allowlist='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
                                )
                                formatted = format_malaysian_plate(ocr_raw)
                                if formatted:
                                    vehicle_plates[track_id] = formatted

        # 4. Render High-Tech HUD Visuals
        annotated = frame.copy()
        # Sort so closer/larger vehicles are drawn on top
        tracked_vehicles.sort(key=lambda t: (t[1][2]-t[1][0])*(t[1][3]-t[1][1]))
        for track_id, v_xyxy, v_type in tracked_vehicles:
            vx1, vy1, vx2, vy2 = v_xyxy
            pbox = vehicle_plate_boxes.get(track_id)
            ptext = vehicle_plates.get(track_id)
            draw_vehicle_visuals(annotated, vx1, vy1, vx2, vy2, track_id, vtype=v_type, plate_box=pbox, plate_text=ptext)

        fps_live = 1.0 / max(0.001, time.time() - t_f0)
        draw_tech_hud(annotated, frame_count, total_frames, fps_live, len(tracked_vehicles), len(all_seen_ids), len(vehicle_plates))

        out_writer.write(annotated)

        if frame_count % 25 == 0:
            print(f"[{frame_count}/{total_frames}] {frame_count/fps:.1f}s | "
                  f"Active: {len(tracked_vehicles)} | "
                  f"Tracked: {len(all_seen_ids)} | "
                  f"Verified Plates: {vehicle_plates}")

    cap.release()
    out_writer.release()
    t_total = time.time() - t_start
    print("=" * 64)
    print(f"Tracking completed in {t_total:.1f}s ({total_frames/t_total:.1f} FPS)!")
    print(f"Video saved to: {args.out}")
    print(f"Plates identified: {vehicle_plates}")
    print("=" * 64)
    return 0

if __name__ == '__main__':
    sys.exit(main())
