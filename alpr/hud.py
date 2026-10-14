"""Minimal AR HUD (corner reticles, glass telemetry card, status capsule).

Same visual language as the earlier scripts, but the card now shows *real* values: the voted plate,
its vote-derived confidence, the number of readings and a status (ACQUIRING / TENTATIVE / CONFIRMED).
Shapes are drawn with OpenCV, then all text in a single PIL pass (anti-aliased TrueType).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .voting import ACQUIRING, CONFIRMED, TENTATIVE, PlateResult

_FONTS = {
    "bold": ["C:/Windows/Fonts/segoeuib.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "regular": ["C:/Windows/Fonts/segoeui.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "mono": ["C:/Windows/Fonts/consola.ttf", "/System/Library/Fonts/Menlo.ttc",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
}

# RGB for PIL text
_STATUS_RGB = {CONFIRMED: (50, 220, 130), TENTATIVE: (255, 190, 60), ACQUIRING: (140, 150, 165)}
CYAN_BGR, GOLD_BGR = (255, 230, 0), (0, 190, 255)


@lru_cache(maxsize=None)
def _font(kind: str, size: int):
    for path in _FONTS[kind]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)      # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def reticle(img, x1, y1, x2, y2, color, length=16, thickness=2):
    """Thin corner L-brackets instead of a full rectangle."""
    for (px, py, dx, dy) in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x1, y2, 1, -1), (x2, y2, -1, -1)):
        cv2.line(img, (px, py), (px + dx * length, py), color, thickness, cv2.LINE_AA)
        cv2.line(img, (px, py), (px, py + dy * length), color, thickness, cv2.LINE_AA)


def _glass(img, x1, y1, x2, y2, bg=(18, 22, 28), alpha=0.88, border=(60, 70, 85)):
    roi = img[y1:y2, x1:x2]
    if roi.size:
        img[y1:y2, x1:x2] = cv2.addWeighted(np.full_like(roi, bg), alpha, roi, 1 - alpha, 0)
    cv2.rectangle(img, (x1, y1), (x2, y2), border, 1, cv2.LINE_AA)


def _plate_text(result: Optional[PlateResult]) -> str:
    return result.text if result else "ACQUIRING..."


def render_hud(img: np.ndarray, lead: Optional[Dict], secondary: List[Dict], fps: float,
               n_tracks: int, title: str) -> np.ndarray:
    """lead: dict(box, track_id, vtype, plate_box|None, result|None, crop|None)."""
    h, w = img.shape[:2]
    texts = []   # (xy, string, rgb, font)

    for v in secondary:
        reticle(img, *v["box"], color=(120, 180, 205), length=14, thickness=1)
        pb = v.get("plate_box")
        if pb is not None:
            reticle(img, *pb, color=(0, 210, 255), length=6, thickness=1)
            cv2.rectangle(img, (pb[0], pb[1]), (pb[2], pb[3]), GOLD_BGR, 1, cv2.LINE_AA)
        res = v.get("result")
        label = f"#{v['track_id']} {res.text}" if res and res.text else f"#{v['track_id']} {v['vtype'].upper()}"
        col = _STATUS_RGB[res.status] if res else (160, 190, 215)
        bx1, by1 = v["box"][0], max(12, v["box"][1] - 14)
        texts.append(((bx1, by1), label, col, _font("bold", 10)))

    if lead:
        x1, y1, x2, y2 = lead["box"]
        result: Optional[PlateResult] = lead.get("result")
        status = result.status if result else ACQUIRING
        reticle(img, x1, y1, x2, y2, CYAN_BGR, length=24)

        pb = lead.get("plate_box")
        if pb is not None:
            reticle(img, *pb, color=(0, 255, 255), length=8)
            cv2.rectangle(img, (pb[0], pb[1]), (pb[2], pb[3]), GOLD_BGR, 1, cv2.LINE_AA)

        cw, ch = 330, 105
        cx1 = x2 + 25 if x2 + 25 + cw <= w - 20 else max(20, x1 - cw - 25)
        cx1 = max(20, min(w - cw - 20, cx1))
        anchor_y = pb[1] if pb is not None else y1
        cy1 = max(90, min(h - ch - 30, anchor_y - 40))
        cx2, cy2 = cx1 + cw, cy1 + ch

        if pb is not None:                                  # tether plate -> card
            tp = (int((pb[0] + pb[2]) / 2), pb[1])
            cp = (cx1 if cx1 > tp[0] else cx2, int((cy1 + cy2) / 2))
            cv2.line(img, tp, cp, GOLD_BGR, 1, cv2.LINE_AA)
            cv2.circle(img, tp, 3, GOLD_BGR, -1, cv2.LINE_AA)
            cv2.circle(img, cp, 3, (0, 255, 255), -1, cv2.LINE_AA)

        _glass(img, cx1, cy1, cx2, cy2)
        cv2.line(img, (cx1, cy1), (cx1 + 30, cy1), CYAN_BGR, 2, cv2.LINE_AA)

        crop = lead.get("crop")                             # sharpest plate crop seen so far
        if crop is not None and crop.size:
            th = 32
            tw = min(110, max(1, int(crop.shape[1] * th / max(1, crop.shape[0]))))
            thumb = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_CUBIC)
            tx, ty = cx2 - tw - 14, cy1 + 38
            img[ty:ty + th, tx:tx + tw] = thumb
            cv2.rectangle(img, (tx, ty), (tx + tw, ty + th), (120, 130, 145), 1)

        shown = _plate_text(result)
        sub = (f"{status}  |  {result.confidence * 100:.0f}% CONF  |  {result.votes} READS"
               if result else "SEARCHING PLATE")
        texts += [
            ((cx1 + 14, cy1 + 10), f"PRIMARY TARGET #{lead['track_id']:02d} : {lead['vtype'].upper()}", (0, 225, 255), _font("bold", 11)),
            ((cx1 + 14, cy1 + 36), shown, (255, 255, 255) if status != ACQUIRING else (170, 178, 190), _font("mono", 25)),
            ((cx1 + 14, cy1 + 76), sub, _STATUS_RGB[status], _font("regular", 12)),
        ]

    # status capsule
    capw, caph = 580, 50
    px1, py1 = int((w - capw) / 2), 25
    _glass(img, px1, py1, px1 + capw, py1 + caph, bg=(12, 16, 22), border=(55, 65, 80))
    cv2.circle(img, (px1 + 22, py1 + caph // 2), 5, (0, 240, 100), -1, cv2.LINE_AA)
    lead_txt = _plate_text(lead.get("result")) if lead else "SEARCHING"
    return _draw_text(img, texts, (px1, py1), title, lead_txt, n_tracks, fps)


def _draw_text(img, texts, cap_xy, title, lead_txt, n_tracks, fps):
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    for xy, s, rgb, font in texts:
        d.text(xy, s, fill=rgb, font=font)
    px1, py1 = cap_xy
    d.text((px1 + 36, py1 + 8), title, fill=(240, 245, 250), font=_font("bold", 16))
    d.text((px1 + 36, py1 + 28), f"LEAD: {lead_txt}    TRACKS: {n_tracks:02d}    FPS: {fps:.1f}",
           fill=(130, 150, 175), font=_font("regular", 12))
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


# ---------------------------------------------------------------------------- CCTV dashboard
_BG, _PANEL, _LINE = (14, 17, 22), (22, 26, 33), (52, 60, 72)          # BGR
_STATUS_BGR = {k: (v[2], v[1], v[0]) for k, v in _STATUS_RGB.items()}
_TYPE_ORDER = ("Car", "Motorcycle", "Bus", "Truck")


def _clock(sec: float) -> str:
    return f"{int(sec // 60):02d}:{sec % 60:04.1f}"


def render_dashboard(frame: np.ndarray, vehicles: List[Dict], log: List, stats: Dict, cfg) -> np.ndarray:
    """CCTV layout: top status bar, camera view (left), plate log (right), per-type counts (bottom).

    vehicles: dicts from AlprPipeline (box, track_id, vtype, plate_box, result); log: LogEntry list;
    stats: fps, time_sec, count, by_type, rate_per_min, plates, confirmed.
    """
    W, H = cfg.dashboard_size
    top, bottom, panel_w = 64, 54, 424
    canvas = np.full((H, W, 3), _BG, np.uint8)
    texts = []                                                      # (xy, text, rgb, font)

    # ---- camera view
    fh, fw = frame.shape[:2]
    avail_w, avail_h = W - panel_w, H - top - bottom
    s = min(avail_w / fw, avail_h / fh)
    vw, vh = int(fw * s), int(fh * s)
    ox, oy = (avail_w - vw) // 2, top + (avail_h - vh) // 2
    view = cv2.resize(frame, (vw, vh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)

    def P(x, y):
        return int(x * s), int(y * s)

    if cfg.count_line_y > 0:                                        # dashed counting line
        ly = int(cfg.count_line_y * fh * s)
        for x in range(0, vw, 28):
            cv2.line(view, (x, ly), (min(vw, x + 16), ly), (255, 220, 0), 2, cv2.LINE_AA)
        _glass(view, 6, max(0, ly - 30), 250, max(0, ly - 6), alpha=0.8)
        texts.append(((ox + 14, oy + ly - 28), f"COUNT LINE  |  {stats['count']} vehicles", (0, 220, 255), _font("bold", 14)))

    for v in vehicles:
        res: Optional[PlateResult] = v.get("result")
        status = res.status if res else ACQUIRING
        col = _STATUS_BGR[status] if res else (205, 180, 120)
        x1, y1 = P(*v["box"][:2])
        x2, y2 = P(*v["box"][2:])
        reticle(view, x1, y1, x2, y2, col, length=max(10, (x2 - x1) // 7), thickness=2)
        pb = v.get("plate_box")
        if pb is not None:
            px1, py1 = P(*pb[:2])
            px2, py2 = P(*pb[2:])
            cv2.rectangle(view, (px1, py1), (px2, py2), GOLD_BGR, 2, cv2.LINE_AA)
        label = f"#{v['track_id']} {v['vtype'].upper()}"
        plate = res.text if res else ""
        tw = max(len(label) * 8, len(plate) * 12) + 16
        ty = max(0, y1 - (46 if plate else 24))
        _glass(view, x1, ty, min(vw - 1, x1 + tw), ty + (44 if plate else 22), alpha=0.75)
        texts.append(((ox + x1 + 7, oy + ty + 3), label, (175, 190, 205), _font("bold", 12)))
        if plate:
            texts.append(((ox + x1 + 7, oy + ty + 19), plate, _STATUS_RGB[status], _font("mono", 19)))
    canvas[oy:oy + vh, ox:ox + vw] = view

    # ---- top bar
    canvas[:top] = _PANEL
    cv2.line(canvas, (0, top - 1), (W, top - 1), _LINE, 1)
    cv2.circle(canvas, (26, top // 2), 7, (0, 230, 90), -1, cv2.LINE_AA)
    texts.append(((44, 12), cfg.hud_title, (240, 245, 250), _font("bold", 22)))
    kpis = [("TIME", _clock(stats["time_sec"])), ("COUNTED", str(stats["count"])),
            ("FLOW", f"{stats['rate_per_min']:.0f}/min"), ("PLATES", f"{stats['plates']}"),
            ("CONFIRMED", f"{stats['confirmed']}"), ("FPS", f"{stats['fps']:.1f}")]
    x = W - 20
    for name, val in reversed(kpis):
        w_box = max(len(val) * 15, len(name) * 8) + 26
        x -= w_box
        texts.append(((x, 8), name, (120, 140, 165), _font("bold", 11)))
        texts.append(((x, 24), val, (235, 240, 245), _font("mono", 24)))

    # ---- bottom bar: per-type counts + legend
    canvas[H - bottom:] = _PANEL
    cv2.line(canvas, (0, H - bottom), (W - panel_w, H - bottom), _LINE, 1)
    x = 20
    by_type = stats.get("by_type", {})
    for t in _TYPE_ORDER:
        texts.append(((x, H - bottom + 9), t.upper(), (120, 140, 165), _font("bold", 11)))
        texts.append(((x, H - bottom + 24), str(by_type.get(t, 0)), (235, 240, 245), _font("mono", 20)))
        x += 130
    x = W - panel_w - 470
    for st in (CONFIRMED, TENTATIVE, ACQUIRING):
        cv2.rectangle(canvas, (x, H - bottom + 22), (x + 12, H - bottom + 34), _STATUS_BGR[st], -1)
        texts.append(((x + 18, H - bottom + 19), st, (190, 200, 212), _font("regular", 13)))
        x += 150

    # ---- right panel: plate log, newest first
    px0 = W - panel_w
    canvas[top:, px0:] = _PANEL
    cv2.line(canvas, (px0, top), (px0, H), _LINE, 1)
    texts.append(((px0 + 20, top + 14), "PLATE LOG", (0, 225, 255), _font("bold", 16)))
    texts.append(((px0 + 20, top + 36), "one row per vehicle  |  Malaysian format check", (120, 140, 165), _font("regular", 12)))
    row_h, y = 118, top + 66
    for e in sorted(log, key=lambda e: e.last_frame, reverse=True)[: (H - y) // row_h]:
        r = e.result
        cv2.rectangle(canvas, (px0 + 12, y), (W - 12, y + row_h - 10), (30, 35, 44), -1)
        cv2.rectangle(canvas, (px0 + 12, y), (px0 + 16, y + row_h - 10), _STATUS_BGR[r.status], -1)
        if e.crop is not None and e.crop.size:
            th = 46
            tw = min(150, max(1, int(e.crop.shape[1] * th / max(1, e.crop.shape[0]))))
            canvas[y + 12:y + 12 + th, W - 24 - tw:W - 24] = cv2.resize(e.crop, (tw, th), interpolation=cv2.INTER_CUBIC)
            cv2.rectangle(canvas, (W - 24 - tw, y + 12), (W - 24, y + 12 + th), (110, 120, 135), 1)
        texts.append(((px0 + 28, y + 10), r.text, (255, 255, 255), _font("mono", 30)))
        texts.append(((px0 + 28, y + 52), f"{r.status}  {r.confidence * 100:.0f}%  |  {r.votes} reads",
                      _STATUS_RGB[r.status], _font("bold", 13)))
        texts.append(((px0 + 28, y + 74), f"{e.region or 'Unknown'}  |  {e.vtype}  |  "
                      f"{_clock(cfg.start_sec + e.first_frame / max(1e-6, stats.get('fps_video', 30.0)))}",
                      (150, 165, 185), _font("regular", 13)))
        y += row_h

    pil = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    for xy, t, rgb, font in texts:
        d.text(xy, t, fill=rgb, font=font)
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
