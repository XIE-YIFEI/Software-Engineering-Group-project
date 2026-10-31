"""Static-image ALPR: plate detection on the whole image -> crop -> plate OCR -> Malaysian syntax.

    alpr = ImageALPR(load_config("image"))
    for p in alpr(cv2.imread("car.jpg")):
        print(p.text, p.valid, p.region, p.ocr_conf)

The video pipeline (pipeline.py) reads many frames of the same vehicle and votes; a photo has one
view, so instead the plate is read from a few crops with different margins and the OCR
probabilities can be averaged (``ocr_tta``). On the Malaysian val set this was a wash (+-1 plate of
94 depending on the margins), so it is off by default.

With a ``PlateChecker`` (alpr/registry.py, SIMULATED registry) every plate also gets registry alerts:
illegal format, unmatched, expired, blacklisted, seen elsewhere (the vehicle type is unknown in a photo).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

import cv2
import numpy as np

from . import hud
from .config import AlprConfig
from .ocr import OcrRead, make_ocr
from .registry import HIGH, ILLEGAL_FORMAT, Alert, PlateChecker

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
    alerts: List[Alert] = field(default_factory=list)   # registry alerts (only with a PlateChecker)

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
    def __init__(self, cfg: AlprConfig, detector=None, ocr=None, checker: Optional[PlateChecker] = None):
        self.cfg = cfg
        if detector is None:
            from ultralytics import YOLO
            detector = YOLO(cfg.plate_model)
        self.detector = detector
        self.ocr = ocr or make_ocr(cfg.ocr_backend, cfg.ocr_model)
        self.checker = checker

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

    def __call__(self, img: np.ndarray, when: Optional[datetime] = None) -> List[PlateResult]:
        """when: observation time for the registry checks (None = now)."""
        results = []
        for box, det_conf in self.detect(img):
            r = self.read(img, box)
            if r is None:
                continue
            alerts = self.checker.check(r.text, valid=r.valid, when=when) if self.checker else []
            results.append(PlateResult(box, det_conf, r.parse.text if r.valid else r.text, r.text,
                                       r.conf, r.valid, r.parse.region if r.valid else "", r.raw, alerts))
        return results


def annotate(img: np.ndarray, plates: List[PlateResult]) -> np.ndarray:
    """Draw plate outlines and capsule labels in the dashboard style: green dot = legal Malaysian
    plate, orange = non-standard text or a registry alert, red = a high-severity alert."""
    out = img.copy()
    h, w = out.shape[:2]
    scale = max(1.0, min(h, w) / 720)
    T = hud._Text()
    ph = int(30 * scale)
    f_plate, f_meta = hud._font("semibold", int(16 * scale)), hud._font("regular", int(13 * scale))
    for p in plates:
        alerts = [a for a in p.alerts if a.code != ILLEGAL_FORMAT]
        rgb = hud.GREEN if p.valid and not alerts else hud.ORANGE
        if any(a.severity == HIGH for a in p.alerts):
            rgb = hud.RED
        x1, y1, x2, y2 = p.box
        hud.stroke(out, (x1, y1, x2, y2), max(3, int(4 * scale)), rgb, max(2, int(2 * scale)), 0.95)
        meta = f"{p.ocr_conf:.0%}" + (f" · {p.region}" if p.valid and p.region else "" if p.valid else " · Non-standard")
        lw = int(ph + f_plate.getlength(p.text) + 6 + f_meta.getlength(meta) + ph / 2)
        lx = int(np.clip(x1, 2, max(2, w - lw - 2)))
        ly = y1 - ph - 6 if y1 - ph - 6 > 0 else y2 + 6
        hud._pill(out, T, lx, ly, p.text, f_plate, dot_rgb=rgb, glassy=True, h=ph, extra=meta, extra_font=f_meta)
        if alerts:                                          # alert capsule above the plate label
            ay = ly - ph - 4 if ly - ph - 4 > 0 else ly + ph + 4
            hud._pill(out, T, lx, ay, hud._alert_label(alerts), f_meta, bg=hud._ALERT_RGB[alerts[0].severity],
                      h=int(26 * scale))
    return T.draw(out)
