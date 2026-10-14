"""Plate-crop quality gate: only sharp, large-enough, well-exposed crops are sent to OCR."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class PlateQuality:
    sharpness: float       # variance of Laplacian on a width-normalised grayscale crop
    brightness: float      # mean gray level 0..255
    score: float           # 0..1, used as the voting weight and to pick the best thumbnail
    ok: bool               # passes the gate -> worth running OCR on


def sharpness(crop_bgr: np.ndarray, norm_width: int = 128) -> float:
    """Blur metric, normalised for crop size so a small sharp plate and a big sharp plate compare."""
    if crop_bgr.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY) if crop_bgr.ndim == 3 else crop_bgr
    h, w = gray.shape[:2]
    if w < 2 or h < 2:
        return 0.0
    scale = norm_width / w
    gray = cv2.resize(gray, (norm_width, max(2, int(round(h * scale)))),
                      interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def assess(crop_bgr: np.ndarray, min_w: int = 28, min_h: int = 10,
           min_sharpness: float = 30.0, sharp_ref: float = 300.0) -> PlateQuality:
    if crop_bgr.size == 0:
        return PlateQuality(0.0, 0.0, 0.0, False)
    h, w = crop_bgr.shape[:2]
    sharp = sharpness(crop_bgr)
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY) if crop_bgr.ndim == 3 else crop_bgr
    mean = float(gray.mean())
    exposure = max(0.0, 1.0 - abs(mean - 128.0) / 128.0)
    size_term = min(1.0, w / 120.0)
    score = 0.6 * min(1.0, sharp / sharp_ref) + 0.25 * size_term + 0.15 * exposure
    ok = w >= min_w and h >= min_h and sharp >= min_sharpness and 20.0 <= mean <= 240.0
    return PlateQuality(sharp, mean, round(score, 4), ok)
