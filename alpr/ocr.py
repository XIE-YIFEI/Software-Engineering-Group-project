"""Plate OCR backends behind one interface, so the image and video pipelines can swap engines.

* ``FastPlateOCR`` (default): fast-plate-ocr's plate-specific model (``cct-s-v2-global-model``,
  trained on plates from 60+ countries incl. Malaysia, reads two-row plates natively). Its per-slot
  probabilities are decoded straight into the most probable *legal* Malaysian plate
  (``plates.decode_slots``). On the hand-labelled Malaysian val crops it reads ~95% of plates
  exactly, against ~38% for EasyOCR.
* ``EasyPlateOCR``: generic scene-text OCR, kept as a fallback when fast-plate-ocr is missing.

Both expose ``read_plate(crop | [crops]) -> OcrRead | None`` and the older
``read(crop) -> (text, conf) | None`` used by the video pipeline.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union

import cv2
import numpy as np

from .plates import PlateParse, decode_slots, normalize, parse_plate

ALLOWLIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
Crops = Union[np.ndarray, Sequence[np.ndarray]]


@dataclass(frozen=True)
class OcrRead:
    text: str                     # compact plate ("VJF1234"), or the raw text when no legal plate fits
    conf: float                   # 0..1
    raw: str                      # what the engine read before syntax decoding / correction
    parse: Optional[PlateParse]   # legal Malaysian interpretation, None for non-standard text

    @property
    def valid(self) -> bool:
        return self.parse is not None


def _as_list(crops: Crops) -> List[np.ndarray]:
    items = [crops] if isinstance(crops, np.ndarray) else list(crops)
    return [c for c in items if c is not None and c.size and c.shape[0] >= 2 and c.shape[1] >= 2]


class FastPlateOCR:
    def __init__(self, model: str = "cct-s-v2-global-model", device: str = "auto", threads: int = 2):
        import onnxruntime as ort
        from fast_plate_ocr import LicensePlateRecognizer

        opts = ort.SessionOptions()
        opts.log_severity_level = 3          # silence harmless shape-inference warnings
        # The OCR model is tiny (~2 ms per plate). ONNX Runtime's default pool takes every core and keeps its
        # threads spinning after each call, which starved the YOLO models running in between: the vehicle
        # detector took 153 ms per frame instead of 46 ms (measured, TEAM_GUIDE.md section 11).
        # threads 0 = ONNX Runtime's default (the old behaviour, for benchmarks).
        if threads > 0:
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self._rec = LicensePlateRecognizer(model, device=device, sess_options=opts)
        cfg = self._rec.config
        self.alphabet, self.pad_char = cfg.alphabet, cfg.pad_char
        self._size = (cfg.img_width, cfg.img_height)
        self._rgb = cfg.image_color_mode == "rgb"
        self._keep_ratio = bool(getattr(cfg, "keep_aspect_ratio", False))
        self._pad_color = tuple(getattr(cfg, "padding_color", (114, 114, 114)))
        self._output = self._rec.plate_output_name

    def _prepare(self, crop_bgr: np.ndarray) -> np.ndarray:
        img = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB if self._rgb else cv2.COLOR_BGR2GRAY)
        w, h = self._size
        if self._keep_ratio:                     # letterbox, as the model was trained
            s = min(w / img.shape[1], h / img.shape[0])
            nw, nh = max(1, int(img.shape[1] * s)), max(1, int(img.shape[0] * s))
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
            fill = self._pad_color if self._rgb else self._pad_color[0]
            top, left = (h - nh) // 2, (w - nw) // 2
            img = cv2.copyMakeBorder(img, top, h - nh - top, left, w - nw - left, cv2.BORDER_CONSTANT, value=fill)
        else:
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_LINEAR)
        return img if img.ndim == 3 else img[..., None]

    def probs(self, crops: Crops) -> np.ndarray:
        """(N, slots, len(alphabet)) per-slot character probabilities."""
        batch = np.stack([self._prepare(c) for c in _as_list(crops)]).astype(np.uint8)
        out = self._rec.model.run([self._output], {"input": batch})[0]
        return out.reshape(len(batch), -1, len(self.alphabet))

    def read_plate(self, crops: Crops) -> Optional[OcrRead]:
        """Read one plate. Several crops of the *same* plate (e.g. different paddings) are averaged."""
        if not _as_list(crops):
            return None
        d = decode_slots(self.probs(crops).mean(0), self.alphabet, self.pad_char)
        if d.parse is not None:
            return OcrRead(d.parse.compact, d.conf, d.raw, d.parse)
        return OcrRead(d.raw, d.raw_conf, d.raw, None) if d.raw else None

    def read(self, crop_bgr: np.ndarray) -> Optional[Tuple[str, float]]:
        r = self.read_plate(crop_bgr)
        return (r.text, r.conf) if r else None


class EasyPlateOCR:
    def __init__(self, gpu: Optional[bool] = None, max_corrections: int = 1):
        # EasyOCR compares torchvision.__version__ and chokes on local builds like "0.19.0+cu121".
        import torchvision
        if "+" in torchvision.__version__:
            torchvision.__version__ = torchvision.__version__.split("+")[0]
        import easyocr
        if gpu is None:
            import torch
            gpu = torch.cuda.is_available()
        self._reader = easyocr.Reader(["en"], gpu=gpu, verbose=False)
        self.max_corrections = max_corrections

    @staticmethod
    def _preprocess(crop_bgr: np.ndarray, min_h: int = 96) -> np.ndarray:
        h, w = crop_bgr.shape[:2]
        if h < min_h:
            s = min_h / h
            crop_bgr = cv2.resize(crop_bgr, (max(1, int(w * s)), min_h), interpolation=cv2.INTER_CUBIC)
        return cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)

    @staticmethod
    def _reading_order(segs):
        """Group segments into text rows by vertical overlap (two-row plates: top row first)."""
        rows = []
        for s in sorted(segs, key=lambda s: min(p[1] for p in s[0])):
            y1, y2 = min(p[1] for p in s[0]), max(p[1] for p in s[0])
            row = next((r for r in rows if r["y1"] <= (y1 + y2) / 2 <= r["y2"]), None)
            if row is None:
                rows.append({"y1": y1, "y2": y2, "segs": [s]})
            else:
                row["segs"].append(s)
                row["y1"], row["y2"] = min(row["y1"], y1), max(row["y2"], y2)
        return [s for r in rows for s in sorted(r["segs"], key=lambda s: min(p[0] for p in s[0]))]

    def read(self, crop_bgr: np.ndarray) -> Optional[Tuple[str, float]]:
        """Return (raw_text, confidence 0..1) or None. Fragments are ordered row-then-left-to-right."""
        if crop_bgr is None or crop_bgr.size == 0:
            return None
        segs = self._reader.readtext(self._preprocess(crop_bgr), detail=1, allowlist=ALLOWLIST)
        if not segs:
            return None
        segs = self._reading_order(segs)
        text = "".join(s[1] for s in segs)
        total = sum(len(s[1]) for s in segs) or 1
        conf = sum(float(s[2]) * len(s[1]) for s in segs) / total
        return text, conf

    def read_plate(self, crops: Crops) -> Optional[OcrRead]:
        items = _as_list(crops)
        r = self.read(items[0]) if items else None
        if not r:
            return None
        parse = parse_plate(r[0], self.max_corrections)
        return OcrRead(parse.compact if parse else normalize(r[0]), r[1], r[0], parse)


PlateOCR = EasyPlateOCR      # backwards-compatible name


def make_ocr(backend: str = "fast_plate_ocr", model: str = "cct-s-v2-global-model", threads: int = 2):
    """OCR engine by name; falls back to EasyOCR when fast-plate-ocr is not installed."""
    if backend == "fast_plate_ocr":
        try:
            return FastPlateOCR(model, threads=threads)
        except ImportError as e:
            warnings.warn(f"fast-plate-ocr unavailable ({e}); falling back to EasyOCR. "
                          "Install with: pip install fast-plate-ocr onnxruntime")
    elif backend != "easyocr":
        raise ValueError(f"unknown OCR backend '{backend}' (fast_plate_ocr | easyocr)")
    return EasyPlateOCR()
