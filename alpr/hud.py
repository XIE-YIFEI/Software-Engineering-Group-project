"""On-screen rendering: CCTV dashboard, dashcam lead HUD, session summary card, plate thumbnails.

Visual language (Apple-like, dark): the camera view sits as a rounded card on an ambient backdrop
(the frame itself, blurred and dimmed); panels are frosted glass; colours are the system palette
(green = valid, yellow = still reading, orange / red = alerts, purple = simulated) and are used
sparingly; text is sentence case in Segoe UI Variable (the closest Windows font to SF Pro), plates
are drawn as real plates (light characters on a black badge).

Shapes are drawn with anti-aliased masks (supersampled, cached by size), all text in one PIL pass.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .voting import ACQUIRING, CONFIRMED, TENTATIVE, PlateResult

# ---------------------------------------------------------------------------- palette (RGB)
GREEN, YELLOW, ORANGE, RED = (48, 209, 88), (255, 214, 10), (255, 159, 10), (255, 69, 58)
PURPLE, BLUE, GRAY = (191, 90, 242), (10, 132, 255), (142, 142, 147)
LABEL, LABEL2, LABEL3 = (255, 255, 255), (152, 152, 160), (99, 99, 104)
_STATUS_RGB = {CONFIRMED: GREEN, TENTATIVE: YELLOW, ACQUIRING: GRAY}
_ALERT_RGB = {"high": RED, "medium": ORANGE}
_SIM_RGB = PURPLE
_STATUS_WORD = {CONFIRMED: "Confirmed", TENTATIVE: "Reading", ACQUIRING: "Searching"}
ALERT_TITLE = {"UNMATCHED": "Not registered", "EXPIRED": "Road tax expired", "ILLEGAL_FORMAT": "Illegal format",
               "BLACKLIST": "Blacklisted", "TYPE_MISMATCH": "Type mismatch", "DUPLICATE": "Cloned plate"}
_TYPE_ORDER = (("Car", "Cars"), ("Motorcycle", "Motorcycles"), ("Bus", "Buses"), ("Truck", "Trucks"))
_BG = (12, 12, 14)                                                      # backdrop when there is no frame


def _bgr(rgb: Sequence[int]) -> Tuple[int, int, int]:
    return int(rgb[2]), int(rgb[1]), int(rgb[0])


# ---------------------------------------------------------------------------- fonts
_VAR = "C:/Windows/Fonts/SegUIVar.ttf"
_STATIC = {   # fallbacks when the variable font is missing (older Windows, macOS, Linux)
    "regular": ["C:/Windows/Fonts/segoeui.ttf", "/System/Library/Fonts/SFNS.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "semibold": ["C:/Windows/Fonts/seguisb.ttf", "/System/Library/Fonts/SFNS.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "bold": ["C:/Windows/Fonts/segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "plate": ["C:/Windows/Fonts/ARIALNB.TTF", "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"],
}
# kind -> (variable-font instance, static fallback)
_KINDS = {"display": ("Semibold Display", "semibold"), "display_regular": ("Regular Display", "regular"),
          "semibold": ("Semibold Text", "semibold"), "regular": ("Regular", "regular"),
          "small": ("Semibold Small", "semibold"), "bold": ("Bold Text", "bold"), "plate": (None, "plate"),
          "mono": ("Semibold Text", "semibold")}


@lru_cache(maxsize=None)
def _font(kind: str, size: int):
    variation, static = _KINDS.get(kind, (None, "regular"))
    if variation:
        try:
            f = ImageFont.truetype(_VAR, size)
            f.set_variation_by_name(variation)
            return f
        except (OSError, ValueError):
            pass
    for path in _STATIC[static]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)      # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _fit(text: str, font, width: float) -> str:
    """`text`, cut with an ellipsis to fit `width` pixels."""
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _clock(sec: float) -> str:
    return f"{int(sec // 60):02d}:{sec % 60:04.1f}"


def alert_title(alert) -> str:
    return ALERT_TITLE.get(alert.code, alert.code.replace("_", " ").capitalize())


def _alert_label(alerts) -> str:
    """'Blacklisted' or 'Road tax expired +1' (alerts are sorted most severe first)."""
    return alert_title(alerts[0]) + (f" +{len(alerts) - 1}" if len(alerts) > 1 else "")


# ---------------------------------------------------------------------------- anti-aliased shapes
@lru_cache(maxsize=4096)
def _rr_mask(w: int, h: int, r: int) -> np.ndarray:
    """Rounded-rectangle coverage mask (float32 0..1, h x w), 4x supersampled."""
    w, h = max(1, int(w)), max(1, int(h))
    ss = 4
    r = max(0, min(int(r), w // 2, h // 2)) * ss
    big = np.zeros((h * ss, w * ss), np.uint8)
    if r == 0:
        big[:] = 255
    else:
        W, H = w * ss - 1, h * ss - 1
        cv2.rectangle(big, (r, 0), (W - r, H), 255, -1)
        cv2.rectangle(big, (0, r), (W, H - r), 255, -1)
        for cx, cy in ((r, r), (W - r, r), (r, H - r), (W - r, H - r)):
            cv2.circle(big, (cx, cy), r, 255, -1)
    m = cv2.resize(big, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    m.setflags(write=False)
    return m


@lru_cache(maxsize=4096)
def _ring_mask(w: int, h: int, r: int, t: int) -> np.ndarray:
    outer = _rr_mask(w, h, r)
    ring = outer.copy()
    if w > 2 * t and h > 2 * t:
        ring[t:h - t, t:w - t] -= _rr_mask(w - 2 * t, h - 2 * t, max(0, r - t))
    np.clip(ring, 0, 1, out=ring)
    ring.setflags(write=False)
    return ring


def _blend(img: np.ndarray, x: int, y: int, mask: np.ndarray, src, alpha: float = 1.0) -> None:
    """img[y:y+h, x:x+w] = mix(img, src, mask * alpha), clipped to the image. src: colour or uint8 array
    (h x w). Uses cv2.blendLinear (per-pixel weights in C++), much faster than numpy float maths."""
    h, w = mask.shape
    H, W = img.shape[:2]
    x1, y1, x2, y2 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 >= x2 or y1 >= y2 or alpha <= 0:
        return
    m = mask[y1 - y:y2 - y, x1 - x:x2 - x]
    m = np.ascontiguousarray(m * alpha if alpha != 1.0 else m, np.float32)
    roi = np.ascontiguousarray(img[y1:y2, x1:x2])
    if isinstance(src, np.ndarray):
        sv = np.ascontiguousarray(src[y1 - y:y2 - y, x1 - x:x2 - x], np.uint8)
    else:
        sv = np.empty_like(roi)
        sv[:] = src
    img[y1:y2, x1:x2] = cv2.blendLinear(roi, sv, 1.0 - m, m)


def _box(b) -> Tuple[int, int, int, int]:
    x1, y1, x2, y2 = (int(round(v)) for v in b)
    return x1, y1, max(x1 + 1, x2), max(y1 + 1, y2)


def fill(img, b, r: int, rgb, alpha: float = 1.0) -> None:
    x1, y1, x2, y2 = _box(b)
    _blend(img, x1, y1, _rr_mask(x2 - x1, y2 - y1, r), _bgr(rgb), alpha)


def stroke(img, b, r: int, rgb, t: int = 2, alpha: float = 1.0) -> None:
    """Rounded outline. Only the four edge bands are blended (the inside of a ring is empty)."""
    x1, y1, x2, y2 = _box(b)
    w, h = x2 - x1, y2 - y1
    ring = _ring_mask(w, h, r, t)
    e = max(min(r, w // 2, h // 2), t) + 1
    if w <= 2 * e or h <= 2 * e:
        _blend(img, x1, y1, ring, _bgr(rgb), alpha)
        return
    for sx, sy, sw, sh in ((0, 0, w, e), (0, h - e, w, e), (0, e, e, h - 2 * e), (w - e, e, e, h - 2 * e)):
        _blend(img, x1 + sx, y1 + sy, ring[sy:sy + sh, sx:sx + sw], _bgr(rgb), alpha)


def _frost(roi: np.ndarray, k: int = 6, sigma: float = 2.0) -> np.ndarray:
    """Cheap heavy blur: downscale, blur, upscale."""
    h, w = roi.shape[:2]
    small = cv2.resize(roi, (max(1, w // k), max(1, h // k)), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), sigma)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def glass(img, b, r: int, tint=(28, 28, 30), alpha: float = 0.55, border_alpha: float = 0.12,
          blur: bool = True) -> None:
    """Frosted-glass panel: the content underneath blurred, tinted, with a hairline light border."""
    x1, y1, x2, y2 = _box(b)
    H, W = img.shape[:2]
    cx1, cy1, cx2, cy2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
    if cx1 >= cx2 or cy1 >= cy2:
        return
    roi = img[cy1:cy2, cx1:cx2]
    base = _frost(roi) if blur else roi
    tint_img = np.empty_like(base)
    tint_img[:] = _bgr(tint)
    panel = cv2.addWeighted(np.ascontiguousarray(base), 1 - alpha, tint_img, alpha, 0)
    mask = _rr_mask(x2 - x1, y2 - y1, r)[cy1 - y1:cy2 - y1, cx1 - x1:cx2 - x1]
    _blend(img, cx1, cy1, mask, panel)
    if border_alpha > 0:
        stroke(img, (x1, y1, x2, y2), r, (255, 255, 255), 1, border_alpha)


def dot(img, center, radius: int, rgb) -> None:
    cx, cy = int(center[0]), int(center[1])
    fill(img, (cx - radius, cy - radius, cx + radius, cy + radius), radius, rgb)


_Q = 4                                                                  # backdrop works at 1/4 resolution


@lru_cache(maxsize=16)
def _shadow_q(W: int, H: int, b: Optional[Tuple[int, int, int, int]], r: int) -> np.ndarray:
    """Multiplier (1 - shadow) at 1/_Q resolution for a soft drop shadow under box b."""
    w, h = W // _Q, H // _Q
    m = np.zeros((h, w), np.float32)
    if b is not None:
        x1, y1, x2, y2 = (v // _Q for v in b)
        oy = 14 // _Q
        cv2.rectangle(m, (x1 + 2, y1 + oy + 2), (x2 - 2, y2 + oy - 2), 1.0, -1)
        m = cv2.GaussianBlur(m, (0, 0), 22 / _Q) * 0.55
    out = (1.0 - m)[..., None]
    out.setflags(write=False)
    return out


def _backdrop(frame: Optional[np.ndarray], W: int, H: int,
              card: Optional[Tuple[int, int, int, int]] = None) -> np.ndarray:
    """Ambient background: the frame, heavily blurred and dimmed, with a soft shadow under `card`.
    Everything here is smooth, so it is computed at 1/4 resolution and upscaled."""
    w, h = W // _Q, H // _Q
    if frame is None or not frame.size:
        q = np.full((h, w, 3), _BG, np.float32)
    else:
        small = cv2.resize(cv2.resize(frame, (192, 108), interpolation=cv2.INTER_LINEAR), (48, 27),
                           interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (0, 0), 2.5)
        q = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC).astype(np.float32) * 0.28 + 6
    q = np.clip(q * _shadow_q(W, H, card, 26), 0, 255).astype(np.uint8)
    return cv2.resize(q, (W, H), interpolation=cv2.INTER_LINEAR)


def _paste_rounded(canvas: np.ndarray, view: np.ndarray, x: int, y: int, r: int) -> None:
    """Paste `view` at (x, y) with anti-aliased rounded corners (only the corners are blended)."""
    h, w = view.shape[:2]
    corners = [(0, 0), (w - r, 0), (0, h - r), (w - r, h - r)]
    saved = [canvas[y + cy:y + cy + r, x + cx:x + cx + r].copy() for cx, cy in corners]
    canvas[y:y + h, x:x + w] = view
    m = _rr_mask(w, h, r)
    for (cx, cy), bg in zip(corners, saved):
        mm = m[cy:cy + r, cx:cx + r, None]
        fg = canvas[y + cy:y + cy + r, x + cx:x + cx + r].astype(np.float32)
        canvas[y + cy:y + cy + r, x + cx:x + cx + r] = (bg + (fg - bg) * mm).astype(np.uint8)


class _Text:
    """Text queued for one PIL pass at the end (PIL is the slow part; one conversion per frame)."""

    def __init__(self):
        self.items: List[tuple] = []

    def add(self, xy, s: str, rgb, font, anchor: str = "la") -> None:
        self.items.append(((int(xy[0]), int(xy[1])), s, tuple(int(c) for c in rgb), font, anchor))

    def draw(self, img: np.ndarray) -> np.ndarray:
        if not self.items:
            return img
        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        d = ImageDraw.Draw(pil)
        for xy, s, rgb, font, anchor in self.items:
            d.text(xy, s, fill=rgb, font=font, anchor=anchor)
        return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def _pill(img, T: _Text, x: int, y: int, text: str, font, fg=LABEL, bg=None, bg_alpha: float = 0.9,
          dot_rgb=None, h: int = 30, glassy: bool = False, extra: str = "", extra_font=None) -> int:
    """A capsule label at (x, y) (top-left); returns its width. bg None + glassy: frosted dark pill."""
    pad = h // 2
    dw = (h // 5) * 2 + 8 if dot_rgb else 0
    tw = font.getlength(text) + (extra_font.getlength(extra) + 6 if extra else 0)
    w = int(pad + dw + tw + pad)
    if glassy:
        glass(img, (x, y, x + w, y + h), h // 2, alpha=0.62, border_alpha=0.14)
    if bg is not None:
        fill(img, (x, y, x + w, y + h), h // 2, bg, bg_alpha)
    cx = x + pad
    if dot_rgb:
        dot(img, (cx + h // 5, y + h // 2), h // 5, dot_rgb)
        cx += dw
    T.add((cx, y + h // 2), text, fg, font, "lm")
    if extra:
        T.add((cx + font.getlength(text) + 6, y + h // 2), extra, LABEL2, extra_font, "lm")
    return w


def plate_badge(img, T: _Text, x: int, y: int, text: str, h: int, dim: bool = False) -> int:
    """A Malaysian plate as it looks on the car: light characters on a black badge. Returns width."""
    font = _font("plate", int(h * 0.66))
    w = int(font.getlength(text) + h * 0.7)
    fill(img, (x, y, x + w, y + h), max(4, h // 7), (10, 10, 11), 0.96)
    stroke(img, (x, y, x + w, y + h), max(4, h // 7), (235, 235, 240), max(1, h // 22), 0.55)
    T.add((x + w / 2, y + h / 2 + 1), text, (150, 150, 155) if dim else (245, 245, 247), font, "mm")
    return w


def plate_image(text: str, height: int = 46) -> np.ndarray:
    """A synthetic plate badge image (BGR) - the thumbnail of a SIMULATED event."""
    font = _font("plate", int(height * 0.66))
    w = int(font.getlength(text) + height * 0.7) + 4
    img = np.full((height, w, 3), 30, np.uint8)
    T = _Text()
    plate_badge(img, T, 2, 0, text, height)
    return T.draw(img)


def _thumb(canvas, crop: np.ndarray, x2: int, y: int, h: int, max_w: int) -> int:
    """Rounded plate-crop thumbnail, right-aligned at x2. Returns its width."""
    tw = min(max_w, max(1, int(crop.shape[1] * h / max(1, crop.shape[0]))))
    th = cv2.resize(crop, (tw, h), interpolation=cv2.INTER_CUBIC if tw > crop.shape[1] else cv2.INTER_AREA)
    _blend(canvas, x2 - tw, y, _rr_mask(tw, h, 8), th)
    stroke(canvas, (x2 - tw, y, x2, y + h), 8, (255, 255, 255), 1, 0.15)
    return tw


# ---------------------------------------------------------------------------- shared: vehicles on the view
def _status_rgb(res: Optional[PlateResult]):
    return _STATUS_RGB[res.status] if res else GRAY


def _draw_vehicles(view: np.ndarray, T: _Text, vehicles: List[Dict], s: float, ox: int, oy: int) -> None:
    """Rounded vehicle outlines, plate outlines and capsule labels (plate, status dot, alert)."""
    vh, vw = view.shape[:2]

    def P(b):
        return tuple(int(v * s) for v in b)

    f_plate, f_id, f_alert = _font("semibold", 16), _font("regular", 13), _font("semibold", 14)
    placed: List[Tuple[int, int, int, int]] = []
    # biggest (closest) vehicles first: they keep the label position right above their box
    for v in sorted(vehicles, key=lambda v: -(v["box"][2] - v["box"][0]) * (v["box"][3] - v["box"][1])):
        res: Optional[PlateResult] = v.get("result")
        alerts = v.get("alerts") or []
        x1, y1, x2, y2 = P(v["box"])
        rgb = _ALERT_RGB[alerts[0].severity] if alerts else (_status_rgb(res) if res else LABEL)
        rad = int(np.clip((x2 - x1) * 0.06, 6, 18))
        stroke(view, (x1, y1, x2, y2), rad, rgb, 3 if alerts else 2, 0.95 if (res or alerts) else 0.35)
        pb = v.get("plate_box")
        if pb is not None:
            stroke(view, P(pb), 4, (255, 255, 255), 2, 0.9)
        if res is None and not alerts:
            continue                                     # still searching: outline only, no label clutter
        # label: [alert pill] over [● plate  #id]
        text = res.text if res else v["vtype"]
        dot_rgb = _ALERT_RGB[alerts[0].severity] if alerts else (_status_rgb(res) if res else GRAY)
        lab_w = int(15 + 18 + f_plate.getlength(text) + 6 + f_id.getlength(f"#{v['track_id']}") + 15)
        lab_h = 30 + (34 if alerts else 0)
        ly = max(4, y1 - lab_h - 6)
        lx = int(np.clip(x1, 4, max(4, vw - lab_w - 4)))
        while ly + lab_h < vh and any(lx < r[2] and r[0] < lx + lab_w and ly < r[3] and r[1] < ly + lab_h
                                      for r in placed):
            ly += 8
        placed.append((lx, ly, lx + lab_w, ly + lab_h))
        y_lab = ly
        if alerts:
            _pill(view, _Offset(T, ox, oy), lx, ly, _alert_label(alerts), f_alert,
                  fg=LABEL, bg=_ALERT_RGB[alerts[0].severity], bg_alpha=0.92, h=28)
            y_lab = ly + 34
        _pill(view, _Offset(T, ox, oy), lx, y_lab, text, f_plate, fg=LABEL if res else LABEL2,
              dot_rgb=dot_rgb, glassy=True, extra=f"#{v['track_id']}", extra_font=f_id)


class _Offset:
    """Adapter: queue text drawn in view coordinates at canvas coordinates."""

    def __init__(self, T: _Text, ox: int, oy: int):
        self.T, self.ox, self.oy = T, ox, oy

    def add(self, xy, s, rgb, font, anchor="la"):
        self.T.add((xy[0] + self.ox, xy[1] + self.oy), s, rgb, font, anchor)


def _count_line(view, T, cfg, fh: int, s: float, ox: int, oy: int, count: int) -> None:
    if cfg.count_line_y <= 0:
        return
    vw = view.shape[1]
    ly = int(cfg.count_line_y * fh * s)
    line = np.zeros((4, vw), np.float32)
    for x in range(0, vw, 26):
        cv2.line(line, (x, 1), (min(vw, x + 14), 1), 1.0, 2, cv2.LINE_AA)
    _blend(view, 0, ly - 1, line, (255, 255, 255), 0.55)
    _pill(view, _Offset(T, ox, oy), 14, ly - 40, f"Count line · {count}", _font("semibold", 14),
          dot_rgb=(100, 210, 255), glassy=True, h=30)


# ---------------------------------------------------------------------------- CCTV dashboard
_M, _GAP, _SIDE_W, _TOP = 32, 24, 420, 112


def _layout(W: int, H: int, fw: int, fh: int):
    """Video card and sidebar boxes for a W x H canvas and an fw x fh camera frame."""
    side = (W - _M - _SIDE_W, _TOP, W - _M, H - _M)
    area = (_M, _TOP, side[0] - _GAP, H - _M - 52)              # 52 px under the video: legend row
    s = min((area[2] - area[0]) / fw, (area[3] - area[1]) / fh)
    vw, vh = int(fw * s), int(fh * s)
    ox = area[0] + (area[2] - area[0] - vw) // 2
    oy = area[1] + (area[3] - area[1] - vh) // 2
    return s, (ox, oy, ox + vw, oy + vh), side


def _header(canvas, T: _Text, cfg, kpis: List[Tuple[str, str, tuple]], W: int) -> None:
    parts = [p.strip() for p in cfg.hud_title.replace("|", "·").split("·") if p.strip()]
    title, sub = (parts[0], " · ".join(parts[1:])) if parts else ("SECS Vision", "")
    # app icon: rounded square with a plate glyph
    ix, iy, isz = _M, 30, 52
    grad = np.zeros((isz, isz, 3), np.float32)
    for i in range(isz):
        t = i / (isz - 1)
        grad[i] = np.array(_bgr(BLUE)) * (1 - t) + np.array(_bgr((94, 92, 230))) * t
    _blend(canvas, ix, iy, _rr_mask(isz, isz, 13), grad)
    stroke(canvas, (ix + 11, iy + 18, ix + isz - 11, iy + isz - 18), 4, (255, 255, 255), 2, 0.95)
    fill(canvas, (ix + 17, iy + 24, ix + isz - 17, iy + 28), 1, (255, 255, 255), 0.95)
    T.add((ix + isz + 16, iy + 2), title, LABEL, _font("display", 30))
    if sub:
        T.add((ix + isz + 18, iy + 40), sub, LABEL2, _font("regular", 16))
    # KPI capsule (right): caption over value, hairline separators
    f_cap, f_val = _font("small", 12), _font("display", 26)
    cols = []
    for cap, val, rgb in kpis:
        cols.append((cap, val, rgb, int(max(f_cap.getlength(cap), f_val.getlength(val)) + 36)))
    total = sum(c[3] for c in cols) + 12
    x = W - _M - total
    y1, y2 = 28, 92
    glass(canvas, (x, y1, W - _M, y2), 20, alpha=0.5, border_alpha=0.12, blur=False)
    cx = x + 6
    for k, (cap, val, rgb, cw) in enumerate(cols):
        T.add((cx + cw / 2, y1 + 18), cap, LABEL2, f_cap, "mm")
        T.add((cx + cw / 2, y1 + 44), val, rgb, f_val, "mm")
        if k:
            fill(canvas, (cx, y1 + 14, cx + 1, y2 - 14), 0, (255, 255, 255), 0.12)
        cx += cw


def _legend(canvas, T: _Text, y: int, x_left: int, x_right: int, by_type: Dict, registry_on: bool,
            simulated: bool) -> None:
    f = _font("regular", 14)
    items = [(GREEN, "Valid" if registry_on else "Confirmed"), (YELLOW, "Reading"), (GRAY, "Searching")]
    if registry_on:
        items += [(ORANGE, "Alert"), (RED, "High alert")]
    if simulated:
        items.append((PURPLE, "Simulated"))
    x = x_left + 4
    for rgb, name in items:
        dot(canvas, (x + 5, y), 5, rgb)
        T.add((x + 16, y), name, LABEL2, f, "lm")
        x += 16 + int(f.getlength(name)) + 24
    # per-type counts, right aligned: "Cars 14   Motorcycles 1 ..."
    f_num = _font("semibold", 15)
    parts = [(plural, str(by_type.get(t, 0))) for t, plural in _TYPE_ORDER]
    widths = [int(f.getlength(p) + 6 + f_num.getlength(n)) for p, n in parts]
    x = x_right - sum(widths) - 22 * (len(parts) - 1)
    for (p, n), wdt in zip(parts, widths):
        T.add((x, y), p, LABEL2, f, "lm")
        T.add((x + f.getlength(p) + 6, y), n, LABEL, f_num, "lm")
        x += wdt + 22


def _log_card(canvas, T: _Text, e, x1: int, y: int, x2: int, cfg, fps_video: float, registry_on: bool,
              max_y: int) -> Optional[int]:
    """One plate-log card; returns its bottom y, or None when it does not fit above max_y."""
    r = e.result
    alerts = getattr(e, "alerts", None) or []
    sim = getattr(e, "simulated", False)
    h = 108 + 32 * len(alerts)
    if y + h > max_y:
        return None
    acc = _ALERT_RGB[alerts[0].severity] if alerts else (PURPLE if sim else None)
    fill(canvas, (x1, y, x2, y + h), 18, acc or (255, 255, 255), 0.10 if acc else 0.06)
    if acc:
        stroke(canvas, (x1, y, x2, y + h), 18, acc, 2, 0.55)
    bw = plate_badge(canvas, T, x1 + 16, y + 16, r.text, 38, dim=r.status != CONFIRMED and not sim)
    if e.crop is not None and e.crop.size and not sim:
        _thumb(canvas, e.crop, x2 - 16, y + 16, 38, max(40, x2 - 16 - (x1 + 16 + bw + 12)))
    when = _clock(cfg.start_sec + e.first_frame / max(1e-6, fps_video))
    f_st, f_meta = _font("semibold", 15), _font("regular", 14)
    if sim:
        dot(canvas, (x1 + 22, y + 70), 5, PURPLE)
        T.add((x1 + 34, y + 70), "Simulated event", PURPLE, f_st, "lm")
        T.add((x1 + 16, y + 92), _fit(f"Fictitious plate · not from the camera · {when}", f_meta, x2 - x1 - 32),
              LABEL2, f_meta, "lm")
    else:
        word = "Valid" if (registry_on and not alerts and r.status == CONFIRMED) else _STATUS_WORD[r.status]
        rgb = _STATUS_RGB[r.status] if not alerts else _ALERT_RGB[alerts[0].severity]
        if alerts:
            word = "Flagged"
        dot(canvas, (x1 + 22, y + 70), 5, rgb)
        T.add((x1 + 34, y + 70), word, rgb, f_st, "lm")
        T.add((x1 + 34 + f_st.getlength(word) + 8, y + 70), f"· {r.confidence * 100:.0f}% · {r.votes} reads",
              LABEL2, f_meta, "lm")
        where = e.region or ("Non-standard plate" if not getattr(r, "valid", True) else "Unknown region")
        T.add((x1 + 16, y + 92), _fit(f"{where} · {e.vtype} · {when}", f_meta, x2 - x1 - 32), LABEL2, f_meta, "lm")
    f_pill, f_msg = _font("semibold", 13), _font("regular", 13)
    for i, a in enumerate(alerts):
        ay = y + 108 + i * 32
        rgb = _ALERT_RGB[a.severity]
        pw = _pill(canvas, T, x1 + 16, ay, alert_title(a), f_pill, fg=LABEL, bg=rgb, bg_alpha=0.9, h=24)
        T.add((x1 + 16 + pw + 8, ay + 12), _fit(a.message, f_msg, x2 - (x1 + 16 + pw + 8) - 14), LABEL2, f_msg, "lm")
    return y + h


def render_dashboard(frame: np.ndarray, vehicles: List[Dict], log: List, stats: Dict, cfg) -> np.ndarray:
    """CCTV layout: header with KPIs, camera view as a rounded card, plate log in a glass sidebar,
    legend + per-type counts under the view.

    vehicles: dicts from AlprPipeline (box, track_id, vtype, plate_box, result, alerts); log: LogEntry
    list; stats: fps, fps_video, time_sec, count, by_type, rate_per_min, plates, confirmed, alerts
    (None = registry checks off), valid, flagged.
    """
    W, H = cfg.dashboard_size
    fh, fw = frame.shape[:2]
    s, (vx1, vy1, vx2, vy2), side = _layout(W, H, fw, fh)
    canvas = _backdrop(frame, W, H, (vx1, vy1, vx2, vy2))        # ambient backdrop + card shadow
    T = _Text()
    registry_on = stats.get("alerts") is not None

    # the camera view with its overlays
    view = cv2.resize(frame, (vx2 - vx1, vy2 - vy1), interpolation=cv2.INTER_AREA if s < 0.6 else cv2.INTER_LINEAR)
    _count_line(view, T, cfg, fh, s, vx1, vy1, stats.get("count", 0))
    _draw_vehicles(view, T, vehicles, s, vx1, vy1)
    banner = getattr(cfg, "hud_banner", "")
    if banner:
        f_b = _font("semibold", 14)
        txt = _fit(banner, f_b, view.shape[1] - 120)
        glass(view, (16, 16, 16 + int(15 + 18 + f_b.getlength(txt) + 15), 48), 16, tint=(60, 30, 80), alpha=0.7,
              border_alpha=0.25)
        dot(view, (16 + 15 + 6, 32), 6, PURPLE)
        T.add((vx1 + 16 + 33, vy1 + 32), txt, (235, 225, 250), f_b, "lm")
    _paste_rounded(canvas, view, vx1, vy1, 26)
    stroke(canvas, (vx1, vy1, vx2, vy2), 26, (255, 255, 255), 1, 0.10)

    # header
    kpis = [("Time", _clock(stats["time_sec"]), LABEL), ("Counted", str(stats["count"]), LABEL),
            ("Flow", f"{stats['rate_per_min']:.0f}/min", LABEL), ("Plates", str(stats["plates"]), LABEL)]
    if registry_on:
        n_flag = stats.get("flagged", 0)
        kpis += [("Valid", str(stats.get("valid", 0)), GREEN), ("Flagged", str(n_flag), RED if n_flag else LABEL)]
    else:
        kpis.append(("Confirmed", str(stats["confirmed"]), GREEN))
    kpis.append(("FPS", f"{stats['fps']:.1f}", LABEL2))
    _header(canvas, T, cfg, kpis, W)

    # legend row under the view
    _legend(canvas, T, H - _M - 22, vx1, vx2, stats.get("by_type", {}), registry_on, bool(banner))

    # sidebar: plate log, newest first
    sx1, sy1, sx2, sy2 = side
    glass(canvas, side, 28, tint=(30, 30, 33), alpha=0.6, border_alpha=0.12, blur=False)
    T.add((sx1 + 24, sy1 + 22), "Plate log", LABEL, _font("display", 24))
    n_rows = len(log)
    T.add((sx2 - 24, sy1 + 38), f"{n_rows} vehicle{'s' if n_rows != 1 else ''}", LABEL2, _font("regular", 15), "rm")
    T.add((sx1 + 24, sy1 + 64), "One row per vehicle · Malaysian format check"
          + (" · Simulated registry" if registry_on else ""), LABEL2, _font("regular", 13))
    y = sy1 + 92
    for e in sorted(log, key=lambda e: e.last_frame, reverse=True):
        bottom = _log_card(canvas, T, e, sx1 + 16, y, sx2 - 16, cfg, stats.get("fps_video", 30.0), registry_on,
                           sy2 - 16)
        if bottom is None:
            break
        y = bottom + 12
    return T.draw(canvas)


def render_summary(log: List, stats: Dict, cfg, title: str = "Session summary",
                   frame: Optional[np.ndarray] = None) -> np.ndarray:
    """End card: every vehicle in the plate log with its verdict (Valid, or the registry alerts)."""
    W, H = cfg.dashboard_size
    canvas = _backdrop(frame, W, H)
    T = _Text()
    parts = [p.strip() for p in cfg.hud_title.replace("|", "·").split("·") if p.strip()]
    T.add((64, 54), title, LABEL, _font("display", 40))
    T.add((66, 108), " · ".join(parts) if parts else "", LABEL2, _font("regular", 17))
    camera = [e for e in log if not getattr(e, "simulated", False)]
    flagged = [e for e in log if getattr(e, "alerts", None)]
    valid = [e for e in camera if e.result.status == CONFIRMED and not getattr(e, "alerts", None)]
    tiles = [("Vehicles counted", str(stats.get("count", 0)), LABEL), ("Plates read", str(len(camera)), LABEL),
             ("Valid", str(len(valid)), GREEN), ("Flagged", str(len(flagged)), RED if flagged else LABEL)]
    x, ty, tw, th = 64, 150, 250, 104
    for cap, val, rgb in tiles:
        glass(canvas, (x, ty, x + tw, ty + th), 22, alpha=0.55, blur=False)
        T.add((x + 22, ty + 22), cap, LABEL2, _font("semibold", 15))
        T.add((x + 22, ty + 46), val, rgb, _font("display", 40))
        x += tw + 20
    by_type = stats.get("by_type", {})
    glass(canvas, (x, ty, W - 64, ty + th), 22, alpha=0.55, blur=False)
    T.add((x + 22, ty + 22), "By type (count line)", LABEL2, _font("semibold", 15))
    cx = x + 22
    for t, plural in _TYPE_ORDER:
        T.add((cx, ty + 70), str(by_type.get(t, 0)), LABEL, _font("display", 30), "lm")
        cx += _font("display", 30).getlength(str(by_type.get(t, 0))) + 8
        T.add((cx, ty + 74), plural, LABEL2, _font("regular", 15), "lm")
        cx += _font("regular", 15).getlength(plural) + 28

    # table
    tx1, ty1, tx2, ty2 = 64, 284, W - 64, H - 92
    glass(canvas, (tx1, ty1, tx2, ty2), 26, alpha=0.55, blur=False)
    cols = [("#", tx1 + 28), ("Plate", tx1 + 76), ("Vehicle", tx1 + 300), ("Region", tx1 + 430),
            ("First seen", tx1 + 650), ("Verdict", tx1 + 790)]
    f_cap = _font("semibold", 14)
    for name, cx in cols:
        T.add((cx, ty1 + 30), name, LABEL2, f_cap, "lm")
    rows = sorted(log, key=lambda e: e.first_frame)
    row_h = int(np.clip((ty2 - ty1 - 70) // max(1, len(rows)), 30, 48))
    fps_v = max(1e-6, stats.get("fps_video", 30.0))
    f_row, f_pill = _font("regular", 15), _font("semibold", 13)
    y = ty1 + 54
    for i, e in enumerate(rows):
        if y + row_h > ty2 - 10:
            T.add((tx1 + 28, y + row_h / 2), f"… {len(rows) - i} more", LABEL2, f_row, "lm")
            break
        fill(canvas, (tx1 + 20, y, tx2 - 20, y + 1), 0, (255, 255, 255), 0.08)
        alerts = getattr(e, "alerts", None) or []
        sim = getattr(e, "simulated", False)
        cy = y + row_h // 2
        T.add((tx1 + 28, cy), str(i + 1), LABEL3, f_row, "lm")
        plate_badge(canvas, T, tx1 + 76, cy - (row_h - 12) // 2, e.result.text, row_h - 12,
                    dim=e.result.status != CONFIRMED and not sim)
        T.add((tx1 + 300, cy), e.vtype, LABEL, f_row, "lm")
        where = e.region or ("Non-standard" if not getattr(e.result, "valid", True) else "Unknown")
        T.add((tx1 + 430, cy), where, LABEL, f_row, "lm")
        T.add((tx1 + 650, cy), _clock(cfg.start_sec + e.first_frame / fps_v), LABEL, f_row, "lm")
        x = tx1 + 790
        if not alerts:
            ok = e.result.status == CONFIRMED
            name, rgb = ("Valid", GREEN) if ok else (_STATUS_WORD[e.result.status], _STATUS_RGB[e.result.status])
            _pill(canvas, T, x, cy - 12, name, f_pill, fg=(10, 10, 12) if not ok else LABEL, bg=rgb, h=24)
        for a in alerts:
            x += _pill(canvas, T, x, cy - 12, alert_title(a), f_pill, bg=_ALERT_RGB[a.severity], h=24) + 8
        if alerts:
            msg = "; ".join(a.message for a in alerts)
            right = tx2 - (200 if sim else 28)
            T.add((x + 4, cy), _fit(msg, f_row, right - x - 4), LABEL2, f_row, "lm")
        if sim:
            _pill(canvas, T, tx2 - 190, cy - 12, "Simulated event", f_pill, fg=PURPLE, bg=PURPLE, bg_alpha=0.18, h=24)
        y += row_h
    banner = getattr(cfg, "hud_banner", "")
    if banner:
        dot(canvas, (70, H - 50), 6, PURPLE)
        T.add((84, H - 50), banner, (215, 200, 240), _font("regular", 15), "lm")
    return T.draw(canvas)


# ---------------------------------------------------------------------------- dashcam lead HUD
def render_hud(img: np.ndarray, lead: Optional[Dict], secondary: List[Dict], fps: float,
               n_tracks: int, title: str) -> np.ndarray:
    """Overlay on the frame itself: thin rounded outlines for every vehicle, a floating glass card
    for the lead vehicle (plate badge, status, sharpest plate crop), and a status capsule on top.
    lead: dict(box, track_id, vtype, plate_box|None, result|None, crop|None, alerts)."""
    h, w = img.shape[:2]
    T = _Text()
    _draw_vehicles(img, T, secondary, 1.0, 0, 0)
    if lead:
        x1, y1, x2, y2 = (int(v) for v in lead["box"])
        result: Optional[PlateResult] = lead.get("result")
        alerts = lead.get("alerts") or []
        rgb = _ALERT_RGB[alerts[0].severity] if alerts else (_status_rgb(result) if result else LABEL)
        stroke(img, (x1, y1, x2, y2), int(np.clip((x2 - x1) * 0.06, 8, 20)), rgb, 3, 0.95)
        pb = lead.get("plate_box")
        if pb is not None:
            stroke(img, pb, 4, (255, 255, 255), 2, 0.95)
        cw, ch = 360, 132
        cx1 = x2 + 24 if x2 + 24 + cw <= w - 20 else max(20, x1 - cw - 24)
        cx1 = max(20, min(w - cw - 20, cx1))
        anchor_y = pb[1] if pb is not None else y1
        cy1 = max(96, min(h - ch - 24, anchor_y - 50))
        if pb is not None:                                  # tether: plate -> card
            tp = (int((pb[0] + pb[2]) / 2), int(pb[1]))
            cp = (cx1 if cx1 > tp[0] else cx1 + cw, cy1 + ch // 2)
            line = np.zeros(img.shape[:2], np.float32)
            cv2.line(line, tp, cp, 1.0, 2, cv2.LINE_AA)
            ys, xs = slice(max(0, min(tp[1], cp[1]) - 2), max(tp[1], cp[1]) + 3), slice(max(0, min(tp[0], cp[0]) - 2), max(tp[0], cp[0]) + 3)
            _blend(img[ys, xs], 0, 0, line[ys, xs], (255, 255, 255), 0.6)
            dot(img, tp, 4, (255, 255, 255))
        glass(img, (cx1, cy1, cx1 + cw, cy1 + ch), 22, alpha=0.6, border_alpha=0.16)
        T.add((cx1 + 18, cy1 + 24), f"{lead['vtype']} · #{lead['track_id']}", LABEL2, _font("semibold", 14), "lm")
        if result:
            plate_badge(img, T, cx1 + 18, cy1 + 42, result.text, 42, dim=result.status != CONFIRMED)
        else:
            T.add((cx1 + 18, cy1 + 63), "Searching for plate…", LABEL2, _font("regular", 18), "lm")
        crop = lead.get("crop")
        if crop is not None and crop.size:
            _thumb(img, crop, cx1 + cw - 18, cy1 + 46, 34, 120)
        status = result.status if result else ACQUIRING
        if alerts:
            word, srgb, rest = _alert_label(alerts), _ALERT_RGB[alerts[0].severity], ""
        else:
            word, srgb = _STATUS_WORD[status], _STATUS_RGB[status]
            rest = f"· {result.confidence * 100:.0f}% · {result.votes} reads" if result else ""
        dot(img, (cx1 + 24, cy1 + 108), 5, srgb)
        T.add((cx1 + 36, cy1 + 108), word, srgb, _font("semibold", 15), "lm")
        if rest:
            T.add((cx1 + 36 + _font("semibold", 15).getlength(word) + 8, cy1 + 108), rest, LABEL2,
                  _font("regular", 14), "lm")
    # status capsule
    parts = [p.strip() for p in title.replace("|", "·").split("·") if p.strip()]
    lead_txt = lead["result"].text if lead and lead.get("result") else "Searching"
    line1 = " · ".join(parts) if parts else title
    line2 = f"Lead {lead_txt} · {n_tracks} tracks · {fps:.1f} FPS"
    f1, f2 = _font("semibold", 16), _font("regular", 13)
    capw = int(max(f1.getlength(line1), f2.getlength(line2)) + 70)
    px1 = (w - capw) // 2
    glass(img, (px1, 20, px1 + capw, 76), 28, alpha=0.6, border_alpha=0.16)
    dot(img, (px1 + 26, 48), 6, GREEN)
    T.add((px1 + 44, 37), line1, LABEL, f1, "lm")
    T.add((px1 + 44, 60), line2, LABEL2, f2, "lm")
    return T.draw(img)
