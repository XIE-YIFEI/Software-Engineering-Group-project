import cv2
import numpy as np

def draw_corner_brackets(img, x1, y1, x2, y2, color=(0, 230, 255), length=22, thickness=2):
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

def draw_tech_hud(img, frame_idx=1280, total_frames=250, fps=28.4, active_count=3, cumulative_count=18):
    h, w = img.shape[:2]
    overlay = img.copy()

    # --- Top Banner HUD (Glassmorphism Dark Panel) ---
    hud_h = 75
    cv2.rectangle(overlay, (20, 20), (w - 20, 20 + hud_h), (12, 14, 18), -1)
    cv2.addWeighted(overlay, 0.82, img, 0.18, 0, img)
    
    # Outer accent border
    cv2.rectangle(img, (20, 20), (w - 20, 20 + hud_h), (0, 220, 255), 1)
    cv2.line(img, (20, 20), (45, 20), (0, 255, 255), 3)
    cv2.line(img, (w - 45, 20), (w - 20, 20), (0, 255, 255), 3)

    # Title & Metadata
    cv2.putText(img, "SECS-VISION: MALAYSIA HIGHWAY ALPR & TRAFFIC TELEMETRY", (40, 50),
                cv2.FONT_HERSHEY_DUPLEX, 0.65, (0, 240, 255), 1, cv2.LINE_AA)
    
    # Stats row
    stats_text = f"ACTIVE TARGETS: {active_count:02d}  |  TOTAL TRACKED: {cumulative_count:03d}  |  FRAME: {frame_idx}  |  FPS: {fps:.1f}  |  STATUS: ENGAGED [MY]"
    cv2.putText(img, stats_text, (40, 78),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 220, 230), 1, cv2.LINE_AA)

def draw_vehicle_ar_card(img, vx1, vy1, vx2, vy2, track_id, vtype, px1, py1, px2, py2, plate_text="VJF 2627"):
    h, w = img.shape[:2]
    color_neon = (0, 240, 255) # Cyan
    color_plate = (0, 165, 255) # Amber / Gold

    # 1. Sleek Corner Reticle on Vehicle
    draw_corner_brackets(img, vx1, vy1, vx2, vy2, color=color_neon, length=24, thickness=2)
    # Subtle dashed bounding box
    cv2.rectangle(img, (vx1, vy1), (vx2, vy2), (0, 180, 200), 1)

    # 2. Glowing Box on License Plate
    cv2.rectangle(img, (px1, py1), (px2, py2), color_plate, 2)
    draw_corner_brackets(img, px1, py1, px2, py2, color=(0, 255, 255), length=8, thickness=2)

    # 3. Floating AR Info Card above vehicle
    card_w, card_h = 240, 85
    cx1 = max(20, min(w - card_w - 20, vx1))
    cy1 = max(100, vy1 - card_h - 20)
    cx2, cy2 = cx1 + card_w, cy1 + card_h

    # Connector line from plate to card
    cv2.line(img, (int((px1+px2)/2), py1), (int((cx1+cx2)/2), cy2), color_plate, 1, cv2.LINE_AA)
    cv2.circle(img, (int((px1+px2)/2), py1), 3, color_plate, -1)
    cv2.circle(img, (int((cx1+cx2)/2), cy2), 3, (0, 255, 255), -1)

    # Semi-transparent card background
    card_overlay = img.copy()
    cv2.rectangle(card_overlay, (cx1, cy1), (cx2, cy2), (15, 18, 22), -1)
    cv2.addWeighted(card_overlay, 0.85, img, 0.15, 0, img)
    
    # Card borders
    cv2.rectangle(img, (cx1, cy1), (cx2, cy2), color_plate, 1)
    cv2.line(img, (cx1, cy1), (cx1 + 20, cy1), (0, 255, 255), 2)
    cv2.line(img, (cx2 - 20, cy1), (cx2, cy1), (0, 255, 255), 2)

    # Card Content
    # Header: Vehicle ID
    cv2.putText(img, f"TARGET #{track_id} : {vtype.upper()}", (cx1 + 12, cy1 + 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 240, 255), 1, cv2.LINE_AA)
    cv2.line(img, (cx1 + 10, cy1 + 28), (cx2 - 10, cy1 + 28), (40, 50, 60), 1)

    # Plate text (Large & Prominent)
    cv2.putText(img, f"PLATE: {plate_text}", (cx1 + 12, cy1 + 54),
                cv2.FONT_HERSHEY_DUPLEX, 0.65, (255, 255, 255), 1, cv2.LINE_AA)
    
    # Sub-status
    cv2.putText(img, "MALAYSIA REGISTRATION | VERIFIED", (cx1 + 12, cy1 + 74),
                cv2.FONT_HERSHEY_SIMPLEX, 0.36, (0, 255, 150), 1, cv2.LINE_AA)

if __name__ == '__main__':
    img = cv2.imread('outputs/test_frame_1280.jpg')
    # Silver car: [164, 600, 396, 850]
    draw_vehicle_ar_card(
        img, 
        vx1=164, vy1=595, vx2=396, vy2=845, 
        track_id=1, vtype="Car", 
        px1=218, py1=710, px2=272, py2=734, 
        plate_text="VJF 2627"
    )
    draw_tech_hud(img, frame_idx=1280, total_frames=250, fps=27.2, active_count=2, cumulative_count=14)
    cv2.imwrite('outputs/preview_hud.jpg', img)
    print('Preview saved to outputs/preview_hud.jpg')
