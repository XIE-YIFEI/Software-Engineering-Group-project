"""Static-image ALPR: plate detection on the whole image -> crop -> plate OCR -> Malaysian syntax.

    alpr = ImageALPR(load_config("image"))
    for p in alpr(cv2.imread("car.jpg")):
        print(p.text, p.valid, p.region, p.ocr_conf)

The video pipeline (pipeline.py) reads many frames of the same vehicle and votes; a photo has one
view, so instead the plate is read from a few crops with different margins and the OCR
probabilities can be averaged (``ocr_tta``). On the Malaysian val set this was a wash (+-1 plate of
94 depending on the margins), so it is off by default.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .config import AlprConfig
from .hud import _font
from .ocr import OcrRead, make_ocr

Box = Tuple[int, int, int, int]
# (horizontal, vertical) crop margin as a fraction of the plate box. The first one is the margin the
# video pipeline uses; the others are only used for test-time augmentation.
CROP_MARGINS = ((0.04, 0.10), (0.02, 0.05), (0.06, 0.15))


@dataclass
class PlateResult:
    box: Box                  # pixel x1, y1, x2, y2
    det_conf: float           # plate detector confidence
    text: str                 # "VJF 1234" for a legal plate, otherwise the raw OCR text
    compact: str              # "VJF1234"
    ocr_conf: float           # 0..1
    valid: bool               # matches a legal Malaysian format (standard or special series)
    region: str               # registration state / category, "" when not valid
    raw: str                  # OCR text before syntax decoding

    def to_dict(self) -> dict:
        d = asdict(self)
        d["box"] = list(self.box)
        d["det_conf"], d["ocr_conf"] = round(self.det_conf, 4), round(self.ocr_conf, 4)
        return d


def crop_plate(img: np.ndarray, box: Box, margin: Tuple[float, float] = CROP_MARGINS[0]) -> np.ndarray:
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box
    px, py = int(margin[0] * (x2 - x1)) + 2, int(margin[1] * (y2 - y1)) + 1
    return img[max(0, y1 - py):min(h, y2 + py), max(0, x1 - px):min(w, x2 + px)]


class ImageALPR:
    def __init__(self, cfg: AlprConfig, detector=None, ocr=None):
        self.cfg = cfg
        if detector is None:
            from ultralytics import YOLO
            detector = YOLO(cfg.plate_model)
        self.detector = detector
        self.ocr = ocr or make_ocr(cfg.ocr_backend, cfg.ocr_model)

    def detect(self, img: np.ndarray) -> List[Tuple[Box, float]]:
        """Plate boxes (pixels) with detector confidence, after size / aspect sanity checks."""
        cfg = self.cfg
        res = self.detector.predict(img, imgsz=cfg.plate_imgsz, conf=cfg.plate_conf, verbose=False)[0]
        out = []
        for xyxy, conf in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.conf.cpu().numpy()):
            x1, y1, x2, y2 = (int(round(v)) for v in xyxy)
            pw, ph = x2 - x1, y2 - y1
            if pw < cfg.min_plate_w_px or ph < cfg.min_plate_h_px:
                continue
            if not cfg.plate_aspect_min <= pw / max(1, ph) <= cfg.plate_aspect_max:
                continue
            out.append(((x1, y1, x2, y2), float(conf)))
        return sorted(out, key=lambda b: b[0][0])

    def read(self, img: np.ndarray, box: Box) -> Optional[OcrRead]:
        margins = CROP_MARGINS if self.cfg.ocr_tta else CROP_MARGINS[:1]
        return self.ocr.read_plate([crop_plate(img, box, m) for m in margins])

    def __call__(self, img: np.ndarray) -> List[PlateResult]:
        results = []
        for box, det_conf in self.detect(img):
            r = self.read(img, box)
            if r is None:
                continue
            results.append(PlateResult(box, det_conf, r.parse.text if r.valid else r.text, r.text,
                                       r.conf, r.valid, r.parse.region if r.valid else "", r.raw))
        return results


def annotate(img: np.ndarray, plates: List[PlateResult]) -> np.ndarray:
    """Draw plate boxes and readings: green = legal Malaysian plate, orange = non-standard text."""
    out = img.copy()
    h, w = out.shape[:2]
    size = max(14, int(min(h, w) / 32))
    labels = []
    for p in plates:
        color = (60, 200, 90) if p.valid else (0, 150, 255)
        x1, y1, x2, y2 = p.box
        cv2.rectangle(out, (x1, y1), (x2, y2), color, max(2, size // 8), cv2.LINE_AA)
        label = f"{p.text}  {p.ocr_conf:.0%}" + ((f"  {p.region}" if p.region else "") if p.valid
                                                  else "  (non-standard)")
        tw = int(len(label) * size * 0.58) + 10
        ty = y1 - size - 10 if y1 - size - 10 > 0 else y2 + 4
        tx = min(max(0, x1), max(0, w - tw))
        cv2.rectangle(out, (tx, ty), (tx + tw, ty + size + 8), color, -1)
        labels.append(((tx + 5, ty + 2), label))
    if not labels:
        return out
    pil = Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    for xy, label in labels:
        draw.text(xy, label, fill=(255, 255, 255), font=_font("bold", size))
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
