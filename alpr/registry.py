"""Plate verification against a vehicle registry: unmatched, expired, illegal, blacklisted, wrong-type
and duplicate (cloned) plates.

THE REGISTRY IS SIMULATED. There is no public Malaysian (JPJ) registration database, so the demo
registry built by ``python -m alpr.registry --make-demo`` holds only
  * fictitious vehicles, all with plates starting with ``X`` (no Malaysian state series uses that
    letter, see ``plates.STATE_BY_PREFIX``), so they do not stand for real vehicles. A few are
    deliberately expired / blacklisted / of the wrong type / seen at another camera, to demonstrate
    the alerts;
  * a *neutral placeholder* for every real plate hand-labelled in this project (``alpr/gt``): status
    ok, no road-tax date, no body type, no colour - so no rule can raise an alert against a real vehicle.

Rules (pure functions, unit-tested), applied by ``PlateChecker.check``:
  UNMATCHED       the plate is not in the registry, and no registered plate is one character away
                  (that is far more often an OCR misread of the registered plate than a fake one)
  EXPIRED         road tax expired before the observation time
  ILLEGAL_FORMAT  the text is not a legal Malaysian plate (``plates.parse_plate`` / ``OcrRead.valid``)
  BLACKLIST       registry status is blacklisted (e.g. reported stolen)
  TYPE_MISMATCH   the observed vehicle type (optionally its colour) does not fit the registered vehicle
  DUPLICATE       the same plate on two vehicles in view at once, or seen at another camera too far
                  away to have driven here in the time in between (cloned plate)

    python -m alpr.registry --make-demo                    # (re)build data/registry/demo_registry.db
    python -m alpr.registry --check "XSB 2002" --vtype Car # one plate against the registry
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .evaluate import edit_distance
from .plates import normalize, parse_plate

UNMATCHED, EXPIRED, ILLEGAL_FORMAT = "UNMATCHED", "EXPIRED", "ILLEGAL_FORMAT"
BLACKLIST, TYPE_MISMATCH, DUPLICATE = "BLACKLIST", "TYPE_MISMATCH", "DUPLICATE"
ALL_RULES = (UNMATCHED, EXPIRED, ILLEGAL_FORMAT, BLACKLIST, TYPE_MISMATCH, DUPLICATE)
HIGH, MEDIUM = "high", "medium"                      # dashboard: red / orange
SEVERITY = {UNMATCHED: MEDIUM, EXPIRED: MEDIUM, ILLEGAL_FORMAT: MEDIUM, TYPE_MISMATCH: MEDIUM,
            BLACKLIST: HIGH, DUPLICATE: HIGH}
OK, BLACKLISTED = "ok", "blacklisted"

DEFAULT_PATH = "data/registry/demo_registry.db"
GT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gt")
DEMO_SCHEMA = "5"            # bump when the demo-registry format changes; content changes are detected

DISCLAIMER = ("SIMULATED registry for a course demo - not real registration data. Fictitious vehicles use "
              "plates starting with X; real plates from alpr/gt are neutral placeholders (status ok, no dates).")

# Registered body type -> detector (COCO) classes it may show up as. Tolerant on purpose: SUVs, pickups
# and vans are often detected as "truck". A car plate on a motorcycle (or a lorry) is the mismatch
# that matters.
BODY_TYPES: Dict[str, Tuple[str, ...]] = {
    "Motorcycle": ("Motorcycle",),
    "Sedan": ("Car",), "Hatchback": ("Car",),
    "SUV": ("Car", "Truck"), "MPV": ("Car", "Truck"), "Pickup": ("Car", "Truck"), "Van": ("Car", "Truck", "Bus"),
    "Lorry": ("Truck", "Bus"), "Bus": ("Bus", "Truck"),
}

COLOURS = ("White", "Silver", "Grey", "Black", "Red", "Orange", "Yellow", "Green", "Blue", "Brown")
# colours an HSV estimate cannot tell apart reliably (lighting, shade): not a mismatch
_SIMILAR_COLOURS = {frozenset(p) for p in (("White", "Silver"), ("Silver", "Grey"), ("Grey", "Black"),
                                           ("Red", "Orange"), ("Orange", "Yellow"), ("Orange", "Brown"),
                                           ("Red", "Brown"))}


@dataclass(frozen=True)
class Alert:
    code: str
    severity: str        # HIGH | MEDIUM
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass(frozen=True)
class VehicleRecord:
    plate: str                       # compact, e.g. "VJF1234"
    body_type: Optional[str] = None  # key of BODY_TYPES; None = not recorded (not checked)
    colour: Optional[str] = None     # one of COLOURS; None = not recorded
    make_model: str = ""
    road_tax_expiry: Optional[date] = None   # None = not recorded (not checked)
    status: str = OK                 # OK | BLACKLISTED
    note: str = ""
    simulated: bool = True           # fictitious record (False: neutral placeholder for a real plate)


@dataclass(frozen=True)
class Sighting:
    """A plate seen by some camera (another site, or earlier) - input to the cloned-plate travel check."""
    plate: str
    seen_at: datetime
    location: str
    lat: float
    lon: float


# ---------------------------------------------------------------------------- rules (pure)
def _alert(code: str, message: str) -> Alert:
    return Alert(code, SEVERITY[code], message)


def check_format(plate: str, valid: Optional[bool] = None) -> Optional[Alert]:
    """ILLEGAL_FORMAT when the text is no legal Malaysian plate. `valid` is the OCR's own verdict
    (``OcrRead.valid``); without it the text is parsed (no corrections allowed)."""
    if valid is None:
        valid = parse_plate(plate, 0) is not None
    return None if valid else _alert(ILLEGAL_FORMAT, f"'{plate}' is not a legal Malaysian plate format")


def check_expiry(rec: VehicleRecord, when: datetime) -> Optional[Alert]:
    if rec.road_tax_expiry is None or when.date() <= rec.road_tax_expiry:
        return None
    days = (when.date() - rec.road_tax_expiry).days
    return _alert(EXPIRED, f"road tax expired {rec.road_tax_expiry.isoformat()} ({days} days ago)")


def check_blacklist(rec: VehicleRecord) -> Optional[Alert]:
    if rec.status != BLACKLISTED:
        return None
    return _alert(BLACKLIST, rec.note or "plate is blacklisted")


def colours_match(registered: str, observed: str) -> bool:
    return registered == observed or frozenset((registered, observed)) in _SIMILAR_COLOURS


def check_type(rec: VehicleRecord, vtype: Optional[str], colour: Optional[str] = None) -> Optional[Alert]:
    """Observed detector class (Car / Motorcycle / Bus / Truck) and, if given, colour vs the record."""
    issues = []
    if vtype and rec.body_type and vtype not in BODY_TYPES.get(rec.body_type, (rec.body_type,)):
        issues.append(f"registered {rec.body_type}, seen as {vtype}")
    if colour and rec.colour and not colours_match(rec.colour, colour):
        issues.append(f"registered {rec.colour.lower()}, looks {colour.lower()}")
    return _alert(TYPE_MISMATCH, "; ".join(issues)) if issues else None


def haversine_km(a: Sequence[float], b: Sequence[float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def check_travel(when: datetime, where: Sequence[float], sightings: Iterable[Sighting],
                 max_speed_kmh: float = 250.0, min_km: float = 2.0) -> Optional[Alert]:
    """DUPLICATE when the plate was also seen somewhere it could not have driven from/to in time
    (implied speed > max_speed_kmh). Sightings closer than `min_km` (the same site) never count."""
    worst = None
    for s in sightings:
        km = haversine_km(where, (s.lat, s.lon))
        if km < min_km:
            continue
        hours = abs((when - s.seen_at).total_seconds()) / 3600.0
        speed = km / hours if hours > 0 else math.inf
        if speed > max_speed_kmh and (worst is None or speed > worst[0]):
            worst = (speed, km, hours, s)
    if worst is None:
        return None
    speed, km, hours, s = worst
    need = "at the same time" if math.isinf(speed) else f"{speed:.0f} km/h needed"
    return _alert(DUPLICATE, f"seen at {s.location} {s.seen_at:%H:%M}, {km:.0f} km away ({need}): cloned plate?")


def check_coexisting(other_tracks: Iterable[int]) -> Optional[Alert]:
    """DUPLICATE when other vehicles in view carry the same plate (the pipeline decides who coexists)."""
    others = sorted(set(other_tracks))
    if not others:
        return None
    return _alert(DUPLICATE, "same plate on vehicle " + ", ".join(f"#{t}" for t in others) +
                  " in view: cloned plate?")


def _sort(alerts: List[Alert]) -> List[Alert]:
    return sorted(alerts, key=lambda a: (a.severity != HIGH, ALL_RULES.index(a.code)))


# ---------------------------------------------------------------------------- colour estimate
def estimate_colour(vehicle_bgr: np.ndarray, min_chroma_share: float = 0.25) -> Optional[str]:
    """Rough body colour of a vehicle crop (HSV). Uses the middle of the box (bonnet / doors), so
    background, windows and tyres matter less; a hue wins if >= `min_chroma_share` of the pixels are
    saturated, otherwise the median brightness picks white / silver / grey / black."""
    if vehicle_bgr is None or vehicle_bgr.ndim != 3 or min(vehicle_bgr.shape[:2]) < 8:
        return None
    h, w = vehicle_bgr.shape[:2]
    roi = vehicle_bgr[int(0.30 * h):int(0.80 * h), int(0.15 * w):int(0.85 * w)]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
    hue, sat, val = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    chroma = (sat >= 80) & (val >= 60)
    if chroma.mean() >= min_chroma_share:
        hc = hue[chroma]                                   # OpenCV hue: 0..179
        bins = {"Red": (hc < 8) | (hc >= 165), "Orange": (hc >= 8) & (hc < 20), "Yellow": (hc >= 20) & (hc < 35),
                "Green": (hc >= 35) & (hc < 85), "Blue": (hc >= 85) & (hc < 135)}
        name = max(bins, key=lambda k: int(bins[k].sum()))
        if name == "Orange" and float(np.median(val[chroma])) < 140:
            name = "Brown"
        return name if bins[name].any() else None
    v = float(np.median(val[~chroma])) if (~chroma).any() else float(np.median(val))
    return "White" if v >= 190 else "Silver" if v >= 140 else "Grey" if v >= 80 else "Black"


# ---------------------------------------------------------------------------- registry (SQLite)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    plate TEXT PRIMARY KEY,              -- compact, upper case
    body_type TEXT,                      -- Sedan / Hatchback / SUV / MPV / Pickup / Van / Lorry / Bus / Motorcycle
    colour TEXT,
    make_model TEXT NOT NULL DEFAULT '',
    road_tax_expiry TEXT,                -- ISO date
    status TEXT NOT NULL DEFAULT 'ok',   -- ok | blacklisted
    note TEXT NOT NULL DEFAULT '',
    simulated INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sightings (
    plate TEXT NOT NULL, seen_at TEXT NOT NULL, location TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sightings_plate ON sightings (plate);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Registry:
    """SQLite vehicle registry. ``Registry()`` is an empty in-memory registry (tests)."""

    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.executescript(_SCHEMA)
        self._cache: Dict[str, Optional[VehicleRecord]] = {}
        self._near_cache: Dict[str, List[str]] = {}

    @classmethod
    def open(cls, path: str = DEFAULT_PATH, build_demo: bool = True) -> "Registry":
        """Open a registry file. A missing demo registry, or one built from other inputs (e.g. new real
        plates labelled in alpr/gt since), is (re)built as the SIMULATED demo registry."""
        if os.path.exists(path):
            reg = cls(path)
            if reg.get_meta("kind") != "demo" or reg.get_meta("version") == demo_fingerprint():
                return reg
            reg.close()
        if not build_demo:
            raise FileNotFoundError(path)
        print(f"registry: building the SIMULATED demo registry -> {path}")
        return build_demo_registry(path)

    def close(self) -> None:
        self.db.close()

    def __len__(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]

    def get_meta(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    def add(self, records: Iterable[VehicleRecord]) -> None:
        rows = [(normalize(r.plate), r.body_type, r.colour, r.make_model,
                 r.road_tax_expiry.isoformat() if r.road_tax_expiry else None, r.status, r.note, int(r.simulated))
                for r in records]
        self.db.executemany("INSERT OR REPLACE INTO vehicles VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.db.commit()
        self._cache.clear()
        self._near_cache.clear()

    def add_sightings(self, sightings: Iterable[Sighting]) -> None:
        self.db.executemany("INSERT INTO sightings VALUES (?, ?, ?, ?, ?)",
                            [(normalize(s.plate), s.seen_at.isoformat(), s.location, s.lat, s.lon) for s in sightings])
        self.db.commit()

    def lookup(self, plate: str) -> Optional[VehicleRecord]:
        key = normalize(plate)
        if key not in self._cache:
            row = self.db.execute("SELECT * FROM vehicles WHERE plate = ?", (key,)).fetchone()
            self._cache[key] = None if row is None else VehicleRecord(
                row[0], row[1], row[2], row[3], date.fromisoformat(row[4]) if row[4] else None, row[5], row[6],
                bool(row[7]))
        return self._cache[key]

    def near(self, plate: str) -> List[str]:
        """Registered plates exactly one edit (substitution / insertion / deletion) away from `plate`.
        Looks up the ~600 neighbours by key, so it stays fast on a registry of any size."""
        key = normalize(plate)
        if key not in self._near_cache:
            alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            cands = {key[:i] + key[i + 1:] for i in range(len(key))}
            cands |= {key[:i] + c + key[i + 1:] for i in range(len(key)) for c in alphabet}
            cands |= {key[:i] + c + key[i:] for i in range(len(key) + 1) for c in alphabet}
            cands = sorted(cands - {key, ""})
            found = []
            for i in range(0, len(cands), 500):                    # SQLite parameter limit
                chunk = cands[i:i + 500]
                found += [r[0] for r in self.db.execute(
                    f"SELECT plate FROM vehicles WHERE plate IN ({','.join('?' * len(chunk))})", chunk)]
            self._near_cache[key] = sorted(found)
        return self._near_cache[key]

    def sightings(self, plate: str) -> List[Sighting]:
        rows = self.db.execute("SELECT * FROM sightings WHERE plate = ?", (normalize(plate),)).fetchall()
        return [Sighting(r[0], datetime.fromisoformat(r[1]), r[2], r[3], r[4]) for r in rows]


# ---------------------------------------------------------------------------- checker
class PlateChecker:
    """Applies the enabled rules to one observed plate.

    camera: (name, lat, lon) of the observing camera; without it the travel check is skipped.
    misread_tolerance: a plate one character away from a registered plate is taken as an OCR misread
    of it (no UNMATCHED; the record's other rules are not applied either, it may be another vehicle).
    """

    def __init__(self, registry: Registry, rules: Iterable[str] = ALL_RULES, check_colour: bool = False,
                 max_speed_kmh: float = 250.0, camera: Optional[Tuple[str, float, float]] = None,
                 misread_tolerance: bool = True):
        self.registry = registry
        self.misread_tolerance = misread_tolerance
        self.rules = {r.upper() for r in rules}
        unknown = self.rules - set(ALL_RULES)
        if unknown:
            raise ValueError(f"unknown registry rule(s) {sorted(unknown)}; available: {', '.join(ALL_RULES)}")
        self.check_colour = check_colour
        self.max_speed_kmh = max_speed_kmh
        self.camera = camera

    @classmethod
    def from_config(cls, cfg) -> "PlateChecker":
        camera = (cfg.camera_name or "camera", *cfg.camera_latlon) if len(cfg.camera_latlon) == 2 else None
        return cls(Registry.open(cfg.registry_path), cfg.registry_rules, cfg.registry_check_colour,
                   cfg.registry_max_speed_kmh, camera, cfg.registry_misread_tolerance)

    def check(self, plate: str, valid: Optional[bool] = None, when: Optional[datetime] = None,
              vtype: Optional[str] = None, colour: Optional[str] = None,
              coexisting_tracks: Iterable[int] = ()) -> List[Alert]:
        """Alerts for one plate, most severe first. `valid`: the OCR's format verdict (None = parse the
        text); `when`: observation time (None = now); `vtype`: detector class (None = unknown, e.g. a
        photo); `coexisting_tracks`: other vehicles in view with the same plate (video)."""
        when = when or datetime.now()
        on = self.rules
        fmt = check_format(plate, valid)
        if fmt is not None:                      # not a plate: registry rules do not apply
            return [fmt] if ILLEGAL_FORMAT in on else []
        rec = self.registry.lookup(plate)
        found: List[Optional[Alert]] = []
        if rec is None:
            if UNMATCHED in on and not (self.misread_tolerance and self.registry.near(plate)):
                found.append(_alert(UNMATCHED, "no registration record"))
        else:
            if BLACKLIST in on:
                found.append(check_blacklist(rec))
            if EXPIRED in on:
                found.append(check_expiry(rec, when))
            if TYPE_MISMATCH in on:
                found.append(check_type(rec, vtype, colour if self.check_colour else None))
        if DUPLICATE in on:
            dup = check_coexisting(coexisting_tracks)
            if dup is None and self.camera is not None:
                dup = check_travel(when, self.camera[1:], self.registry.sightings(plate), self.max_speed_kmh)
            found.append(dup)
        return _sort([a for a in found if a is not None])


# ---------------------------------------------------------------------------- demo registry
DEMO_DAY = date(2026, 10, 14)        # the demo profile's notional observation day (video_start_time)

# Fictitious vehicles the demo profile injects as SIMULATED events (alpr/config.yaml, registry_sim_events).
DEMO_SCENARIOS = [
    VehicleRecord("XSB2002", "Sedan", "Silver", "Proton Saga", date(2027, 5, 31), BLACKLISTED,
                  "SIMULATED: reported stolen"),
    VehicleRecord("XSA1001", "Motorcycle", "Red", "Honda EX5", date(2026, 3, 31), OK,
                  "SIMULATED: road tax expired; registered as a motorcycle"),
    VehicleRecord("XSD4004", "Hatchback", "White", "Perodua Myvi", date(2027, 1, 31), OK,
                  "SIMULATED: same plate seen in Johor Bahru shortly before"),
    VehicleRecord("XSG7007", "Sedan", "Red", "Proton Iswara", date(2027, 8, 31), OK,
                  "SIMULATED: valid record; the plate is cloned onto a second car in the demo video"),
]
DEMO_SIGHTINGS = [Sighting("XSD4004", datetime(2026, 10, 14, 7, 45), "Johor Bahru (SIM)", 1.4927, 103.7414)]
DEMO_UNREGISTERED = ("XSE5005",)     # fictitious plate deliberately NOT in the registry (UNMATCHED demo)

_MAKES = {
    "Sedan": ["Proton Saga", "Perodua Bezza", "Toyota Vios", "Honda City", "Proton Persona"],
    "Hatchback": ["Perodua Myvi", "Perodua Axia", "Proton Iriz", "Honda Jazz"],
    "SUV": ["Proton X70", "Honda CR-V", "Perodua Ativa", "Toyota Corolla Cross"],
    "MPV": ["Perodua Alza", "Toyota Innova", "Proton Exora"],
    "Pickup": ["Toyota Hilux", "Isuzu D-Max", "Mazda BT-50"],
    "Van": ["Toyota Hiace", "Nissan NV200"],
    "Lorry": ["Isuzu NLR", "Hino 300"],
    "Bus": ["Scania K250"],
    "Motorcycle": ["Honda EX5", "Yamaha Y15ZR", "Modenas Kriss"],
}
_POP_COLOURS = ("White", "Silver", "Grey", "Black", "Red", "Blue")
_BODY_WEIGHTS = {"Sedan": 30, "Hatchback": 25, "SUV": 12, "MPV": 8, "Pickup": 6, "Van": 3, "Lorry": 3, "Bus": 1,
                 "Motorcycle": 12}


def real_plates(gt_dir: Optional[str] = None) -> List[str]:
    """Every real plate hand-labelled in this project (video GT, incl. unverifiable '?' lines, and the
    image val CSV) - these get neutral placeholder records so no alert can name a real vehicle.
    Labels that are no legal plate (dealer frames) are skipped. (Files in sub-folders, e.g. the GT of
    the simulated video with its fictitious plates, are not read.)"""
    gt_dir = gt_dir or GT_DIR
    texts = []
    for path in glob.glob(os.path.join(gt_dir, "*.txt")):
        with open(path, encoding="utf-8-sig") as f:
            texts += [line.split("#")[0].split("!")[0].strip().lstrip("?") for line in f]
    for path in glob.glob(os.path.join(gt_dir, "*.csv")):
        with open(path, encoding="utf-8") as f:
            texts += [r.get("plate") or "" for r in csv.DictReader(f)]
    plates = {normalize(t) for t in texts}
    return sorted(p for p in plates if p and getattr(parse_plate(p, 0), "compact", None) == p)


def _fictitious_population(n: int, keep_away: Iterable[str] = (), seed: int = 2026) -> List[VehicleRecord]:
    """`n` random X-plates, each >= 2 edits from every plate in `keep_away` (real plates, demo scenarios),
    so an OCR misread of a real plate cannot land on a fictitious record (or be excused as one)."""
    rng = random.Random(seed)
    letters = "ABCDEFGHJKLMNPRSTUVWY"                    # no I / O / Q, as on real plates
    reserved = {r.plate for r in DEMO_SCENARIOS} | set(DEMO_UNREGISTERED) | set(keep_away)
    bodies, weights = zip(*_BODY_WEIGHTS.items())
    out: Dict[str, VehicleRecord] = {}
    while len(out) < n:
        plate = "X" + "".join(rng.choice(letters) for _ in range(rng.choice((1, 2)))) + str(rng.randint(1, 9999))
        if rng.random() < 0.15:
            plate += rng.choice(letters)
        parse = parse_plate(plate, 0)
        if plate in out or parse is None or parse.compact != plate or any(
                edit_distance(plate, r) <= 1 for r in reserved):
            continue
        body = rng.choices(bodies, weights)[0]
        expiry = DEMO_DAY + timedelta(days=rng.randint(-200, 400))       # ~1/3 expired on the demo day
        blacklisted = rng.random() < 0.03
        out[plate] = VehicleRecord(plate, body, rng.choice(_POP_COLOURS), rng.choice(_MAKES[body]),
                                   expiry, BLACKLISTED if blacklisted else OK,
                                   "SIMULATED: reported stolen" if blacklisted else "SIMULATED")
    return list(out.values())


def demo_fingerprint() -> str:
    """Identifies the inputs of the demo registry. Labelling a new real plate in alpr/gt changes it, so
    the registry is rebuilt and the plate gets its neutral placeholder - otherwise that real vehicle
    would be flagged UNMATCHED."""
    import hashlib
    h = hashlib.sha1()
    for part in (real_plates(), DEMO_SCENARIOS, DEMO_SIGHTINGS, DEMO_UNREGISTERED):
        h.update(repr(part).encode("utf-8"))
    return f"{DEMO_SCHEMA}-{h.hexdigest()[:12]}"


def build_demo_registry(path: str = DEFAULT_PATH, n_fictitious: int = 300) -> Registry:
    """(Re)build the SIMULATED demo registry: fictitious X-plates + neutral placeholders for real plates."""
    if path != ":memory:" and os.path.exists(path):
        os.remove(path)
    reg = Registry(path)
    real = real_plates()
    reg.add(_fictitious_population(n_fictitious, keep_away=real))
    reg.add(DEMO_SCENARIOS)
    reg.add(VehicleRecord(p, note="real plate from the demo footage / dataset (alpr/gt): neutral placeholder",
                          simulated=False) for p in real)
    reg.add_sightings(DEMO_SIGHTINGS)
    for k, v in (("kind", "demo"), ("version", demo_fingerprint()), ("disclaimer", DISCLAIMER)):
        reg.set_meta(k, v)
    return reg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_PATH)
    ap.add_argument("--make-demo", action="store_true", help="(re)build the SIMULATED demo registry at --db")
    ap.add_argument("--check", metavar="PLATE", help="check one plate against the registry")
    ap.add_argument("--vtype", help="observed vehicle type for --check (Car / Motorcycle / Bus / Truck)")
    ap.add_argument("--at", help="observation time for --check, ISO format (default: now)")
    args = ap.parse_args(argv)
    if args.make_demo:
        reg = build_demo_registry(args.db)
        print(f"{len(reg)} vehicles ({len(real_plates())} neutral real-plate placeholders) -> {args.db}\n{DISCLAIMER}")
    if args.check:
        reg = Registry.open(args.db)
        rec = reg.lookup(args.check)
        print(f"record: {rec}")
        when = datetime.fromisoformat(args.at) if args.at else None
        alerts = PlateChecker(reg, camera=("demo camera", 3.1390, 101.6869)).check(args.check, when=when,
                                                                                     vtype=args.vtype)
        for a in alerts:
            print(f"  [{a.severity.upper()}] {a.code}: {a.message}")
        if not alerts:
            print("  no alerts")
    if not (args.make_demo or args.check):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
