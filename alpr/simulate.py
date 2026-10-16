"""Demo footage with SIMULATED violations: digitally replace the plates of a few real vehicles with
fictitious plates, so the registry alerts can be shown on actual vehicles in the video.

Why: real registration data (JPJ) is not public, so no real vehicle in public footage can be shown
as stolen / expired / cloned - and must not be, the repository is public. Instead, chosen vehicles
get fictitious X-series plates (no Malaysian state uses X) or a deliberately malformed plate. The
ALPR pipeline then processes the altered video like any other: it has to find, read and check these
plates itself. Every other vehicle keeps its real plate and must come out VALID.

The alteration is a separate pre-processing step that writes a new video file, so the pipeline
never sees which vehicles were altered:

    python -m alpr.simulate                      # reads alpr/simulation.yaml
    python -m alpr.simulate --reuse-plan         # re-render only (pass 1 cached)

How a plate is replaced: pass 1 tracks vehicles and detects + reads plates (the same models the
pipeline uses) and identifies the vehicle(s) to alter by their real plate. Pass 2 re-detects the
plate on every frame and replaces it only where the OCR confirms it is that plate (plates too small
to read: at the detected / predicted position), with a Malaysian-style plate (light characters on
black) matched to the original's blur, brightness and noise.
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .plates import normalize

PLATE_FONTS = ["C:/Windows/Fonts/ARIALNB.TTF", "C:/Windows/Fonts/DINNextLTPro-Bold.ttf",
               "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"]


@lru_cache(maxsize=None)
def _plate_font(size: int):
    for path in PLATE_FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_plate(text: str, two_row: bool = False, width: int = 520) -> np.ndarray:
    """Malaysian-style plate (BGR): light characters on black with a thin light border.
    `text` is the display form ("XSB 2002"); two_row splits prefix and number onto two lines."""
    height = int(width * (0.48 if two_row else 0.22))
    img = Image.new("RGB", (width, height), (14, 14, 16))
    d = ImageDraw.Draw(img)
    b = max(2, width // 120)
    d.rounded_rectangle([b, b, width - 1 - b, height - 1 - b], radius=b * 3, outline=(225, 225, 225), width=b)
    rows = text.split(" ", 1) if two_row and " " in text else [text]
    row_h = (height - 4 * b) / len(rows)
    for i, row in enumerate(rows):
        size = int(row_h * 0.86)
        font = _plate_font(size)
        while font.getlength(row) > width * 0.86 and size > 8:
            size -= 2
            font = _plate_font(size)
        x0, y0, x1, y1 = d.textbbox((0, 0), row, font=font)
        tx = (width - (x1 - x0)) / 2 - x0
        ty = 2 * b + i * row_h + (row_h - (y1 - y0)) / 2 - y0
        d.text((tx, ty), row, fill=(232, 232, 232), font=font)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def paste_plate(frame: np.ndarray, box: Tuple[float, float, float, float], plate: np.ndarray,
                rng: Optional[np.random.Generator] = None) -> None:
    """Fit `plate` into `box` (x1, y1, x2, y2) of `frame` in place, matching the original plate's
    brightness / contrast, the camera's blur at that size, and sensor noise."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    bw, bh = x2 - x1, y2 - y1
    if bw < 6 or bh < 3:
        return
    orig = frame[y1:y2, x1:x2].astype(np.float32)
    new = cv2.resize(plate, (bw, bh), interpolation=cv2.INTER_AREA).astype(np.float32)
    # optics: a plate rendered at its final size is sharper than the camera image of a real plate
    sigma = 0.35 + 0.004 * bw
    new = cv2.GaussianBlur(new, (0, 0), sigma)
    # photometry: map the rendered dark / light levels onto the original plate's
    o_lo, o_hi = np.percentile(orig, 5), np.percentile(orig, 97)
    n_lo, n_hi = np.percentile(new, 5), np.percentile(new, 97)
    new = (new - n_lo) / max(1.0, n_hi - n_lo) * max(20.0, o_hi - o_lo) + o_lo
    new *= orig.mean(axis=(0, 1)) / np.maximum(1.0, new.mean(axis=(0, 1)))       # colour cast
    rng = rng or np.random.default_rng(0)
    new += rng.normal(0, 3.0, new.shape)
    # soft edge so the paste does not leave a hard seam
    m = np.ones((bh, bw), np.float32)
    e = max(1, min(bw, bh) // 10)
    m[:e, :] *= np.linspace(0.3, 1, e)[:, None]
    m[-e:, :] *= np.linspace(1, 0.3, e)[:, None]
    m[:, :e] *= np.linspace(0.3, 1, e)[None, :]
    m[:, -e:] *= np.linspace(1, 0.3, e)[None, :]
    out = orig * (1 - m[..., None]) + new * m[..., None]
    frame[y1:y2, x1:x2] = np.clip(out, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------- swap planning
@dataclass
class Swap:
    real: str            # real plate of the vehicle to alter (normalised), identifies the vehicle
    fake: str            # display text of the fictitious plate, e.g. "XSB 2002"
    two_row: bool = False
    note: str = ""


def _smooth(values: List[Tuple[int, Sequence[float]]], window: int = 9) -> Dict[int, np.ndarray]:
    """Per-frame relative plate boxes -> moving median (removes detector jitter)."""
    frames = [f for f, _ in values]
    arr = np.array([v for _, v in values], np.float32)
    out = {}
    for i, f in enumerate(frames):
        lo, hi = max(0, i - window // 2), min(len(frames), i + window // 2 + 1)
        out[f] = np.median(arr[lo:hi], axis=0)
    return out


def plan(video: str, crop: Optional[Sequence[int]], start: int, n_frames: int,
         vehicle_model: str, plate_model: str, ocr, imgsz: int = 640) -> dict:
    """Pass 1: track vehicles, detect + read plates. Returns vehicle boxes per frame, relative plate
    boxes per track (smoothed) and the plate readings per track (for `match_tracks`)."""
    from ultralytics import YOLO
    from .pipeline import TRACKER_CFG
    vm, pm = YOLO(vehicle_model), YOLO(plate_model)
    cap = cv2.VideoCapture(video)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    vboxes: Dict[int, Dict[int, List[float]]] = defaultdict(dict)
    rels: Dict[int, List[Tuple[int, List[float]]]] = defaultdict(list)
    reads: Dict[int, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for i in range(n_frames):
        ok, frame = cap.read()
        if not ok:
            break
        if crop:
            frame = np.ascontiguousarray(frame[crop[1]:crop[3], crop[0]:crop[2]])
        # same tracker settings as the pipeline, so a vehicle is one track here as well
        res = vm.track(frame, persist=True, tracker=TRACKER_CFG, classes=[2, 3, 5, 7], conf=0.10,
                       imgsz=imgsz, agnostic_nms=True, verbose=False)[0]
        if res.boxes is None or res.boxes.id is None:
            continue
        for xyxy, tid, conf in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.id.cpu().numpy(),
                                   res.boxes.conf.cpu().numpy()):
            x1, y1, x2, y2 = (int(v) for v in xyxy)
            tid = int(tid)
            vboxes[i][tid] = [x1, y1, x2, y2, float(conf)]
            if x2 - x1 < 60 or i % 2:
                continue
            car = frame[max(0, y1):y2, max(0, x1):x2]
            p = pm.predict(car, imgsz=480, conf=0.3, verbose=False)[0]
            if p.boxes is None or len(p.boxes) == 0:
                continue
            px1, py1, px2, py2 = (float(v) for v in p.boxes.xyxy[0].tolist())
            vw, vh = x2 - x1, y2 - y1
            rels[tid].append((i, [px1 / vw, py1 / vh, px2 / vw, py2 / vh]))
            if px2 - px1 >= 30:                          # identify the vehicle by its real plate
                r = ocr.read_plate(car[int(py1):int(py2), int(px1):int(px2)])
                if r is not None:
                    reads[tid][normalize(r.text)] += r.conf * (px2 - px1) ** 2
    cap.release()
    return {"vboxes": dict(vboxes), "rels": {t: _smooth(v) for t, v in rels.items()},
            "median_rel": {t: np.median(np.array([r for _, r in v]), axis=0) for t, v in rels.items() if v},
            "reads": {t: dict(v) for t, v in reads.items()}}


def match_tracks(reads: Dict[int, Dict[str, float]], swaps: List[Swap], min_share: float = 0.25) -> Dict[int, int]:
    """track id -> index into `swaps`. A vehicle is often split into several tracks (a passing car
    hides it for a moment): every track whose readings agree with the real plate (within two edits) for
    at least `min_share` of their weight is that vehicle, so its plate is replaced on all of them.
    Two edits, because an oblique view of the same plate is often read with two wrong characters."""
    from .evaluate import edit_distance
    out: Dict[int, int] = {}
    for tid, tally in reads.items():
        total = sum(tally.values())
        best, best_w = None, 0.0
        for k, s in enumerate(swaps):
            w = sum(v for t, v in tally.items() if edit_distance(t, s.real) <= 2)
            if total > 0 and w / total >= min_share and w > best_w:
                best, best_w = k, w
        if best is not None:
            out[tid] = best
    return out


def drop_coexisting(matched: Dict[int, int], p: dict, swaps: List[Swap], min_overlap: int = 10) -> Dict[int, int]:
    """A vehicle is one track at a time: of two tracks matched to the same swap that are in view
    together (e.g. a car passing in front whose box contains the plate), keep the one with more
    readings of the real plate."""
    from .evaluate import edit_distance
    frames: Dict[int, set] = defaultdict(set)
    for f, bx in p["vboxes"].items():
        for t in bx:
            frames[t].add(f)

    def weight(t, k):
        return sum(v for txt, v in p["reads"].get(t, {}).items() if edit_distance(txt, swaps[k].real) <= 2)

    out: Dict[int, int] = {}
    for k in set(matched.values()):
        kept: List[int] = []
        for t in sorted((t for t, j in matched.items() if j == k), key=lambda t: -weight(t, k)):
            if all(len(frames[t] & frames[o]) < min_overlap for o in kept):
                kept.append(t)
        out.update({t: k for t in kept})
    return out


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def render(video: str, out: str, crop: Optional[Sequence[int]], start: int, n_frames: int, p: dict,
           track_swap: Dict[int, Swap], plate_model: str, ocr, crf: int = 14) -> Dict[str, dict]:
    """Pass 2: write the altered video (H.264).

    On every frame the plate is re-detected inside the vehicle box and the candidate that reads
    closest to the real plate is replaced; a candidate the OCR reads confidently as a different legal
    plate (another vehicle's) is never touched, and a readable-size plate that is not detected at all
    (covered) is left alone. Plates too small to read are replaced at the detected / predicted
    position unless a nearer vehicle covers them."""
    import imageio_ffmpeg
    from ultralytics import YOLO
    from .evaluate import edit_distance
    pm = YOLO(plate_model)
    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    plates = {tid: render_plate(s.fake, s.two_row) for tid, s in track_swap.items()}
    rng = np.random.default_rng(7)
    stats: Dict[str, dict] = {}
    writer = None
    for i in range(n_frames):
        ok, frame = cap.read()
        if not ok:
            break
        if crop:
            frame = np.ascontiguousarray(frame[crop[1]:crop[3], crop[0]:crop[2]])
        frame = frame[:frame.shape[0] // 2 * 2, :frame.shape[1] // 2 * 2]      # H.264 / yuv420p: even size
        if writer is None:
            h, w = frame.shape[:2]
            writer = imageio_ffmpeg.write_frames(out, (w, h), fps=fps, codec="libx264", quality=None,
                                                 pix_fmt_out="yuv420p", macro_block_size=1,
                                                 output_params=["-crf", str(crf), "-preset", "slow"])
            writer.send(None)
        boxes = p["vboxes"].get(i, {})
        original = frame.copy()                          # detect / read on the untouched frame
        for tid, swap in track_swap.items():
            if tid not in boxes or tid not in p["median_rel"]:
                continue
            st = stats.setdefault(f"{swap.fake} (on {swap.real})",
                                  {"real": swap.real, "fake": swap.fake, "frames": 0, "frames_far": 0,
                                   "skipped_not_confirmed": 0, "skipped_covered": 0, "tracks": []})
            if tid not in st["tracks"]:
                st["tracks"].append(tid)
            vx1, vy1, vx2, vy2 = (int(v) for v in boxes[tid][:4])
            rel = p["rels"][tid].get(i)
            if rel is None:                              # detector missed it on this frame: median position
                rel = p["median_rel"][tid]
            vw, vh = vx2 - vx1, vy2 - vy1
            pred = (vx1 + rel[0] * vw, vy1 + rel[1] * vh, vx1 + rel[2] * vw, vy1 + rel[3] * vh)
            cands = []
            if vw >= 40:
                x0, y0 = max(0, vx1), max(0, vy1)
                det = pm.predict(original[y0:max(y0 + 1, vy2), x0:max(x0 + 1, vx2)], imgsz=480, conf=0.2,
                                 verbose=False)[0]
                if det.boxes is not None:
                    cands = [(x0 + b[0], y0 + b[1], x0 + b[2], y0 + b[3]) for b in det.boxes.xyxy.tolist()]
            chosen = None
            readable = [c for c in cands if c[2] - c[0] >= 30]
            if readable:
                # the candidate that reads closest to the real plate; a candidate the OCR reads with
                # confidence as a *different* legal plate belongs to another vehicle and is never touched
                scored = []
                for c in readable:
                    r = ocr.read_plate(original[int(c[1]):int(c[3]), int(c[0]):int(c[2])])
                    d = edit_distance(normalize(r.text), swap.real) if r is not None else 99
                    other = r is not None and r.valid and r.conf >= 0.8 and d >= 3
                    if not other:
                        scored.append((d, -(c[2] - c[0]), c))
                if scored:
                    chosen = min(scored)[2]
                else:
                    st["skipped_not_confirmed"] += 1
                    continue
            if chosen is None:
                if pred[2] - pred[0] >= 30:              # readable size but no plate detected: covered
                    st["skipped_not_confirmed"] += 1
                    continue
                near = [c for c in cands if _iou(c, pred) > 0.05]
                chosen = max(near, key=lambda c: _iou(c, pred)) if near else pred
                covered = any(o != tid and ob[4] >= 0.3 and ob[3] > vy2 and ob[0] < chosen[2] and chosen[0] < ob[2]
                              and ob[1] < chosen[3] and chosen[1] < ob[3] for o, ob in boxes.items())
                if covered:
                    st["skipped_covered"] += 1
                    continue
                st["frames_far"] += 1
            bw_, bh_ = chosen[2] - chosen[0], chosen[3] - chosen[1]
            pad = (chosen[0] - 0.05 * bw_, chosen[1] - 0.12 * bh_, chosen[2] + 0.05 * bw_, chosen[3] + 0.12 * bh_)
            paste_plate(frame, pad, plates[tid], rng)
            st["frames"] += 1
        writer.send(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    cap.release()
    if writer is not None:
        writer.close()
    return stats


def main() -> int:
    import yaml
    from .config import DEFAULT_PATH
    from .ocr import make_ocr
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", default=os.path.join(os.path.dirname(DEFAULT_PATH), "simulation.yaml"),
                    help="swap list + source video (see alpr/simulation.yaml)")
    ap.add_argument("--reuse-plan", action="store_true", help="reuse pass 1 from the last run (same spec)")
    args = ap.parse_args()
    with open(args.spec, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    cap = cv2.VideoCapture(spec["source"])
    W, H, fps = cap.get(3), cap.get(4), cap.get(5)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    fc = spec.get("frame_crop")
    crop = [int(fc[0] * W), int(fc[1] * H), int(fc[2] * W), int(fc[3] * H)] if fc else None
    start = int(spec.get("start_sec", 0) * fps)
    n = int(spec["duration"] * fps) if spec.get("duration") else total - start
    swaps = [Swap(normalize(s["real"]), s["fake"], bool(s.get("two_row")), s.get("note", "")) for s in spec["swaps"]]
    plate_model = spec.get("plate_model", "models/plate_malaysia.pt")
    ocr = make_ocr()

    cache = os.path.splitext(spec["out"])[0] + ".plan.pkl"
    key = (spec["source"], tuple(crop or ()), start, n, 2)            # pass 1 does not depend on the swaps
    p = None
    if args.reuse_plan and os.path.exists(cache):
        with open(cache, "rb") as f:
            saved = pickle.load(f)
        if saved.get("key") == key:
            p = saved["plan"]
            print(f"pass 1: reusing {cache}")
    if p is None:
        print(f"pass 1: tracking + reading plates ({n} frames)")
        p = plan(spec["source"], crop, start, n, spec.get("vehicle_model", "yolov8s.pt"), plate_model, ocr)
        os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
        with open(cache, "wb") as f:
            pickle.dump({"key": key, "plan": p}, f)

    matched = drop_coexisting(match_tracks(p["reads"], swaps), p, swaps)
    track_swap = {t: swaps[k] for t, k in matched.items()}
    for k, s in enumerate(swaps):
        tracks = sorted(t for t, j in matched.items() if j == k)
        print(f"  {s.real:<9} -> {s.fake:<12} {'tracks ' + str(tracks) if tracks else 'NOT FOUND'}")
    print(f"pass 2: rendering -> {spec['out']}")
    os.makedirs(os.path.dirname(spec["out"]) or ".", exist_ok=True)
    stats = render(spec["source"], spec["out"], crop, start, n, p, track_swap, plate_model, ocr)
    with open(os.path.splitext(spec["out"])[0] + ".swaps.json", "w", encoding="utf-8") as f:
        json.dump({"source": spec["source"], "start_sec": spec.get("start_sec", 0), "swaps": stats,
                   "disclaimer": "SIMULATED: these plates were digitally inserted; they are fictitious."},
                  f, indent=2, ensure_ascii=False)
    for name, st in stats.items():
        print(f"  {name:<24} replaced on {st['frames']} frames ({st['frames_far']} too small to read), "
              f"left alone: {st['skipped_not_confirmed']} plate covered or another vehicle's, {st['skipped_covered']} covered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
