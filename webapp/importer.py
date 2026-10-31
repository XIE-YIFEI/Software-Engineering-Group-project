"""Fill the database from the committed results, so the system has real data without reprocessing anything.

    python -m webapp.importer --demo                 # cameras, demo runs, congestion series, forecasts, demo lists
    python -m webapp.importer --demo --demo-users    # + the test accounts of webapp/seed/demo_users.yaml

Sources (paths in webapp/seed/cameras.yaml): demo/*.plates.json (plates, simulated events, flow, lanes, registry),
demo/jb_ciq_forecast_timelapse.forecast.json, docs/data/causeway/*.csv and *.calibration.json. The importer is
idempotent: rows are matched by natural keys (run code + first frame + plate ...), so running it again updates the
data and keeps the workflow state of alerts. The SHA-256 of every source file is stored with its run.

`import_results()` is also what the upload worker uses to load the results of a new analysis.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

import yaml

from . import clock, db
from .alerting import create_alert
from .privacy import audit
from .security import SYSTEM, create_user
from .settings import ROOT, Settings, load_settings

SEED_DIR = Path(__file__).with_name("seed")
Log = Callable[[str], None]


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    """Repo-relative text path when inside the repository, else absolute."""
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _plate(text: str):
    from alpr.plates import normalize, parse_plate
    compact = normalize(text)
    parse = parse_plate(compact, 0)
    return (parse.text, parse.compact, parse.region) if parse and parse.compact == compact else (text, compact, "")


def _add_plate(conn: sqlite3.Connection, compact: str, text: str) -> None:
    conn.execute("INSERT INTO plates VALUES (?, ?, ?) ON CONFLICT (compact) DO UPDATE SET plate_text = excluded.plate_text",
                 (compact, text, len(compact)))


def _at(origin: datetime, sec: float) -> str:
    return clock.iso(origin + timedelta(seconds=float(sec)))


def _rendered_plate(settings: Settings, text: str, compact: str) -> str:
    """A rendered plate image (alpr.hud.plate_image) for a synthetic sighting, which has no camera crop."""
    import cv2
    from alpr.hud import plate_image
    rel_path = f"plates/{compact}.png"
    dst = settings.media_dir / rel_path
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(dst), plate_image(text, 64))
    return rel_path


# ---------------------------------------------------------------------------- plate / lane results
def import_results(conn: sqlite3.Connection, settings: Settings, *, code: str, label: str, camera_id: int,
                   results: Path, clock_origin: datetime, origin: str = "demo", video: Optional[Path] = None,
                   video_kind: str = "annotated", video_offset: Optional[float] = None, note: str = "",
                   flow_primary: bool = True, inserted: Set[str] = frozenset(), evidence: Optional[Path] = None,
                   job_id: Optional[int] = None, check_lists: bool = True, log: Log = print) -> int:
    """Load one *.plates.json (alpr pipeline) as a run: sightings (plates[] + simulated_events[]), registry
    alerts, counting-line flow, and for lane cameras the per-lane totals, 10 s series, crossings, events and
    withheld counts. `inserted`: compact plates that were digitally inserted into the footage (provenance
    inserted_plate). Returns the run id."""
    with open(results, encoding="utf-8") as f:
        d = json.load(f)
    window = d.get("window_sec") or [0.0, 0.0]
    flow = d.get("flow") or {}
    reg = d.get("registry")
    now = clock.now_iso()
    with db.transaction(conn):
        run_id = db.upsert(conn, "runs", {
            "code": code, "label": label, "camera_id": camera_id, "origin": origin, "media": "video",
            "results_path": rel(results), "results_sha256": sha256_file(results),
            "video_path": rel(video) if video else None, "video_kind": video_kind if video else None,
            "video_offset_sec": float(window[0] if video_offset is None else video_offset),
            "image_path": None, "profile": d.get("profile"), "fps": d.get("fps"), "frames": d.get("frames"),
            "window_start_sec": window[0], "window_end_sec": window[1], "clock_origin": clock.iso(clock_origin),
            "provenance_note": note, "flow_primary": int(flow_primary), "count_line_y": flow.get("count_line_y"),
            "flow_total": flow.get("total"), "flow_by_type": db.js(flow.get("by_type") or {}),
            "registry_rules": db.js(reg["rules"]) if reg else None,
            "registry_disclaimer": reg.get("disclaimer") if reg else None,
            "processing": db.js(d["processing"]) if d.get("processing") else None, "job_id": job_id,
            "imported_at": now}, key=("code",))
        keys: List[str] = []
        for r in d.get("plates", []):
            key = f"p:{r['first_frame']}:{r['compact']}"
            keys.append(key)
            prov = "inserted_plate" if r["compact"] in inserted else "camera"
            _add_plate(conn, r["compact"], r["plate"])
            sid = db.upsert(conn, "sightings", {
                "run_id": run_id, "camera_id": camera_id, "natural_key": key, "compact": r["compact"],
                "plate_text": r["plate"], "confidence": r.get("confidence"), "votes": r.get("votes"),
                "status": r.get("status", "CONFIRMED"), "valid_format": int(bool(r.get("valid_format", True))),
                "vtype": r.get("vtype"), "region": r.get("region") or "", "track_ids": db.js(r.get("track_ids", [])),
                "first_frame": r.get("first_frame"), "last_frame": r.get("last_frame"), "first_sec": r["first_sec"],
                "last_sec": r["last_sec"], "seen_at": _at(clock_origin, r["first_sec"]),
                "last_seen_at": _at(clock_origin, r["last_sec"]), "provenance": prov, "recorded_at": now},
                key=("run_id", "natural_key"), keep=("crop_path", "scene_path", "scene_info"))
            for a in r.get("alerts", []):
                create_alert(conn, natural_key=f"{code}|{key}|{a['code']}", source="registry", code=a["code"],
                             severity=a["severity"], message=a["message"], seen_at=_at(clock_origin, r["first_sec"]),
                             provenance=prov, run_id=run_id, camera_id=camera_id, sighting_id=sid,
                             compact=r["compact"], plate_text=r["plate"])
        for ev in d.get("simulated_events", []):
            key = f"s:{ev['time_sec']}:{ev['compact']}"
            keys.append(key)
            _add_plate(conn, ev["compact"], ev["plate"])
            _, _, region = _plate(ev["plate"])
            sid = db.upsert(conn, "sightings", {
                "run_id": run_id, "camera_id": camera_id, "natural_key": key, "compact": ev["compact"],
                "plate_text": ev["plate"], "confidence": None, "votes": 0, "status": "CONFIRMED", "valid_format": 1,
                "vtype": ev.get("vtype"), "region": region, "track_ids": "[]", "first_frame": None, "last_frame": None,
                "first_sec": ev["time_sec"], "last_sec": ev["time_sec"], "seen_at": _at(clock_origin, ev["time_sec"]),
                "last_seen_at": _at(clock_origin, ev["time_sec"]), "provenance": "synthetic",
                "crop_path": _rendered_plate(settings, ev["plate"], ev["compact"]), "recorded_at": now},
                key=("run_id", "natural_key"))
            for a in ev.get("alerts", []):
                create_alert(conn, natural_key=f"{code}|{key}|{a['code']}", source="registry", code=a["code"],
                             severity=a["severity"], message=a["message"], seen_at=_at(clock_origin, ev["time_sec"]),
                             provenance="synthetic", run_id=run_id, camera_id=camera_id, sighting_id=sid,
                             compact=ev["compact"], plate_text=ev["plate"])
        _drop_stale(conn, "sightings", run_id, keys, prefixes=("p:", "s:"))   # g: = seeded gate arrivals
        conn.execute("DELETE FROM flow_bins WHERE run_id = ?", (run_id,))
        for b in flow.get("per_10s", []):
            by_type = {k: v for k, v in b.items() if k not in ("t_start", "t_end", "total", "observed_sec")}
            db.insert(conn, "flow_bins", {"run_id": run_id, "t_start": b["t_start"], "t_end": b["t_end"],
                                          "total": b.get("total", 0), "by_type": db.js(by_type)})
        if d.get("lanes"):
            _import_lanes(conn, code, run_id, camera_id, d["lanes"], clock_origin)
    if evidence is not None and evidence.exists():
        n = attach_evidence(conn, settings, run_id, code, evidence)
        log(f"  {code}: evidence (crops + scene frames) for {n} plates from {rel(evidence)}")
    if check_lists:
        match_lists(conn, run_id)
    return run_id


def import_image_results(conn: sqlite3.Connection, settings: Settings, *, code: str, label: str, camera_id: int,
                         results: Path, image_dir: Path, recorded_at: datetime, job_id: Optional[int] = None) -> int:
    """Load the results.json of run_image.py (one photo) as a run: one sighting per plate, its crop cut from the
    photo. A photo has one view and no voting, so a legal plate counts as confirmed and a non-standard text stays a
    provisional reading."""
    import cv2
    with open(results, encoding="utf-8") as f:
        d = json.load(f)
    img = (d.get("images") or [None])[0]
    annotated = image_dir / Path(img["image"]).name if img else None
    now = clock.now_iso()
    with db.transaction(conn):
        run_id = db.upsert(conn, "runs", {
            "code": code, "label": label, "camera_id": camera_id, "origin": "upload", "media": "image",
            "results_path": rel(results), "results_sha256": sha256_file(results), "video_path": None, "video_kind": None,
            "video_offset_sec": 0.0, "image_path": str(annotated) if annotated and annotated.exists() else None,
            "profile": "image", "fps": None, "frames": 1, "window_start_sec": 0.0, "window_end_sec": 0.0,
            "clock_origin": clock.iso(recorded_at), "provenance_note": "Uploaded photo", "flow_primary": 0,
            "count_line_y": None, "flow_total": None, "flow_by_type": "{}", "registry_rules": None,
            "registry_disclaimer": None, "processing": None, "job_id": job_id, "imported_at": now}, key=("code",))
        photo = cv2.imread(img["image"]) if img else None
        for k, p in enumerate((img or {}).get("plates", [])):
            key = f"i:{k}:{p['compact']}"
            crop_rel = None
            if photo is not None:
                x1, y1, x2, y2 = p["box"]
                pad = int(0.06 * (x2 - x1)) + 2
                crop = photo[max(0, y1 - pad):y2 + pad, max(0, x1 - pad):x2 + pad]
                if crop.size:
                    crop_rel = f"runs/{code}/plate_{k}_{p['compact']}.jpg"
                    (settings.media_dir / crop_rel).parent.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(settings.media_dir / crop_rel), crop)
            _add_plate(conn, p["compact"], p["text"])
            db.upsert(conn, "sightings", {
                "run_id": run_id, "camera_id": camera_id, "natural_key": key, "compact": p["compact"],
                "plate_text": p["text"], "confidence": p.get("ocr_conf"), "votes": 1,
                "status": "CONFIRMED" if p.get("valid") else "TENTATIVE", "valid_format": int(bool(p.get("valid"))),
                "vtype": None, "region": p.get("region") or "", "track_ids": "[]", "first_frame": 0, "last_frame": 0,
                "first_sec": 0.0, "last_sec": 0.0, "seen_at": clock.iso(recorded_at), "last_seen_at": clock.iso(recorded_at),
                "provenance": "camera", "crop_path": crop_rel, "recorded_at": now}, key=("run_id", "natural_key"))
    match_lists(conn, run_id)
    return run_id


def _drop_stale(conn: sqlite3.Connection, table: str, run_id: int, keys: List[str], prefixes=("",)) -> None:
    """Rows of a re-imported run (with one of the key `prefixes`) that the results no longer contain."""
    have = {r["natural_key"] for r in db.rows(conn, f"SELECT natural_key FROM {table} WHERE run_id = ?", (run_id,))}
    for k in have - set(keys):
        if k.startswith(tuple(prefixes)):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ? AND natural_key = ?", (run_id, k))


def _import_lanes(conn, code: str, run_id: int, camera_id: int, lanes: dict, origin: datetime) -> None:
    for t in ("lanes", "lane_bins", "crossings", "withheld"):
        conn.execute(f"DELETE FROM {t} WHERE run_id = ?", (run_id,))
    for pos, (name, r) in enumerate(lanes.get("per_lane", {}).items()):
        db.insert(conn, "lanes", {"run_id": run_id, "name": name, "grp": r.get("group", ""),
                                  "direction": r.get("direction", ""), "position": pos, "count": r.get("count", 0),
                                  "by_type": db.js(r.get("by_type") or {}), "veh_per_min": r.get("veh_per_min"),
                                  "occupancy": r.get("occupancy"), "density": r.get("density"),
                                  "density_unit": r.get("density_unit"), "zone_length_m": r.get("zone_length_m"),
                                  "mean_speed_kmh": r.get("mean_speed_kmh")})
    for lane, bins in lanes.get("per_10s", {}).items():
        for b in bins:
            db.insert(conn, "lane_bins", {"run_id": run_id, "lane": lane, "t_start": b["t_start"], "t_end": b["t_end"],
                                          "count": b.get("count"), "veh_per_min": b.get("veh_per_min"),
                                          "occupancy": b.get("occupancy"), "mean_vehicles": b.get("mean_vehicles"),
                                          "density_veh_km": b.get("density_veh_km"),
                                          "mean_speed_kmh": b.get("mean_speed_kmh"), "observed_sec": b.get("observed_sec")})
    conn.executemany("INSERT INTO crossings (run_id, time_sec, seen_at, lane, vtype, track_id, direction) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [(run_id, c["time_sec"], _at(origin, c["time_sec"]), c.get("lane"), c.get("vtype"),
                       c.get("track_id"), c.get("direction")) for c in lanes.get("crossings", [])])
    for k, n in (lanes.get("withheld_violations") or {}).items():
        db.insert(conn, "withheld", {"run_id": run_id, "code": k, "count": n, "note": lanes.get("withheld_note", "")})
    keys = []
    for ev in lanes.get("events", []):
        key = f"e:{ev['time_sec']}:{ev['code']}:{ev.get('track_id')}"
        keys.append(key)
        prov = "synthetic" if ev.get("simulated") else "camera"
        text, compact, _ = _plate(ev["plate"]) if ev.get("plate") else (None, None, None)
        if compact:
            _add_plate(conn, compact, text)
        eid = db.upsert(conn, "lane_events", {
            "run_id": run_id, "natural_key": key, "code": ev["code"], "severity": ev["severity"],
            "time_sec": ev["time_sec"], "seen_at": _at(origin, ev["time_sec"]), "lane": ev.get("lane"),
            "from_lane": ev.get("from_lane") or None, "message": ev["message"], "vtype": ev.get("vtype"),
            "track_id": ev.get("track_id"), "compact": compact, "plate_text": text, "provenance": prov,
            "value": ev.get("value"), "error_1sigma": ev.get("error_1sigma")}, key=("run_id", "natural_key"))
        if ev["severity"] in ("violation", "hazard"):
            create_alert(conn, natural_key=f"{code}|{key}", source="lane", code=ev["code"], severity=ev["severity"],
                         message=ev["message"], seen_at=_at(origin, ev["time_sec"]), provenance=prov, run_id=run_id,
                         camera_id=camera_id, lane_event_id=eid, compact=compact, plate_text=text)
    _drop_stale(conn, "lane_events", run_id, keys)


def attach_evidence(conn: sqlite3.Connection, settings: Settings, run_id: int, code: str, evidence_json: Path) -> int:
    """Copy the crops and scene frames of a --evidence run into the media folder and link them to the rows of
    the same run (matched by first frame and plate: the evidence run gives identical results)."""
    with open(evidence_json, encoding="utf-8") as f:
        d = json.load(f)
    n = 0
    for r in d.get("plates", []):
        ev = r.get("evidence")
        if not ev:
            continue
        sid = db.scalar(conn, "SELECT id FROM sightings WHERE run_id = ? AND natural_key = ?",
                        (run_id, f"p:{r['first_frame']}:{r['compact']}"))
        if sid is None:
            continue
        paths = {}
        for kind in ("crop", "scene"):
            src = (ROOT / ev[kind]) if not Path(ev[kind]).is_absolute() else Path(ev[kind])
            if not src.exists():
                break
            dst_rel = f"runs/{code}/{src.name}"
            (settings.media_dir / dst_rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, settings.media_dir / dst_rel)
            paths[kind] = dst_rel
        if len(paths) == 2:
            info = {k: ev.get(k) for k in ("frame", "time_sec", "vehicle_box", "plate_box")}
            info["source"] = rel(evidence_json)
            conn.execute("UPDATE sightings SET crop_path = ?, scene_path = ?, scene_info = ? WHERE id = ?",
                         (paths["crop"], paths["scene"], db.js(info), sid))
            n += 1
    return n


def match_lists(conn: sqlite3.Connection, run_id: int) -> int:
    """Alerts for sightings of this run that are on an active list: police watch list (WATCHLIST, medium) and
    blacklist (BLACKLIST, high) for road cameras; the site's ban list (BANNED_AT_SITE, high) for its gate."""
    run = db.row(conn, "SELECT r.code, r.camera_id, c.kind, c.site_id FROM runs r JOIN cameras c ON c.id = r.camera_id "
                       "WHERE r.id = ?", (run_id,))
    if run is None:
        return 0
    if run["kind"] == "gate":
        lists = db.rows(conn, "SELECT * FROM list_entries WHERE active = 1 AND list = 'banned' AND site_id = ?",
                        (run["site_id"],))
    else:
        lists = db.rows(conn, "SELECT * FROM list_entries WHERE active = 1 AND list IN ('watch', 'blacklist')")
    by_plate: Dict[str, List[dict]] = {}
    for e in lists:
        by_plate.setdefault(e["compact"], []).append(e)
    n = 0
    for s in db.rows(conn, "SELECT * FROM sightings WHERE run_id = ? AND status = 'CONFIRMED'", (run_id,)):
        for e in by_plate.get(s["compact"], []):
            if e["valid_to"] and e["valid_to"] < s["seen_at"]:
                continue
            code, sev, src = {"watch": ("WATCHLIST", "medium", "list"), "blacklist": ("BLACKLIST", "high", "list"),
                              "banned": ("BANNED_AT_SITE", "high", "gate")}[e["list"]]
            create_alert(conn, natural_key=f"{run['code']}|{s['natural_key']}|list:{e['id']}", source=src, code=code,
                         severity=sev, message=e["reason"] or f"on the {e['list']} list", seen_at=s["seen_at"],
                         provenance=s["provenance"], run_id=run_id, camera_id=run["camera_id"], sighting_id=s["id"],
                         compact=s["compact"], plate_text=s["plate_text"])
            n += 1
    return n


# ---------------------------------------------------------------------------- congestion series and forecasts
def import_series(conn: sqlite3.Connection, spec: dict, camera_id: int) -> int:
    import csv
    csv_path = ROOT / spec["csv"]
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    calib = None
    if spec.get("calibration"):
        with open(ROOT / spec["calibration"], encoding="utf-8") as f:
            calib = json.load(f)
    video = ROOT / spec["video"] if spec.get("video") else None
    vfrom = vto = vdur = None
    if video is not None and video.exists() and spec.get("video_window"):
        import cv2
        cap = cv2.VideoCapture(str(video))
        n, fps = cap.get(cv2.CAP_PROP_FRAME_COUNT), cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.release()
        hh = [int(t[:2]) * 60 + int(t[3:5]) for t in spec["video_window"]]
        vfrom, vto, vdur = hh[0], hh[1], round(n / fps, 3)
    with db.transaction(conn):
        day_id = db.upsert(conn, "series_days", {
            "code": spec["code"], "camera_id": camera_id, "label": spec["label"], "date": rows[0]["time"][:10],
            "csv_path": rel(csv_path), "csv_sha256": sha256_file(csv_path), "calibrated": int(calib is not None),
            "video_path": rel(video) if vfrom is not None else None, "video_from_minute": vfrom,
            "video_to_minute": vto, "video_duration_sec": vdur, "imported_at": clock.now_iso()}, key=("code",))
        conn.execute("DELETE FROM zone_minutes WHERE day_id = ?", (day_id,))
        entities = list(dict.fromkeys(c.rsplit(".", 1)[0] for c in rows[0] if "." in c))

        def num(v):
            return float(v) if v not in (None, "") else None
        conn.executemany(
            "INSERT INTO zone_minutes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(day_id, e, r["time"], int(r["minute_of_day"]), num(r.get(f"{e}.occupancy")), num(r.get(f"{e}.excess")),
              num(r.get(f"{e}.density")), num(r.get(f"{e}.fill")), num(r.get(f"{e}.queue")),
              num(r.get(f"{e}.observed_sec"))) for r in rows for e in entities])
        conn.execute("DELETE FROM calibrations WHERE day_id = ?", (day_id,))
        if calib:
            cap = calib.get("capacity", {})
            for z, c in calib.get("zones", {}).items():
                db.insert(conn, "calibrations", {"day_id": day_id, "entity": z, "kind": "zone", "slope": c.get("slope"),
                                                 "intercept": c.get("intercept"), "night_offset": c.get("night_offset"),
                                                 "n": c.get("n"), "loo_mae": c.get("loo_mae"),
                                                 "loo_bias": c.get("loo_bias"), "mean_count": c.get("mean_count"),
                                                 "corr": c.get("corr"), "relative_mae": None, "capacity": cap.get(z)})
            for g, c in calib.get("groups", {}).items():
                db.insert(conn, "calibrations", {"day_id": day_id, "entity": g, "kind": "group", "slope": None,
                                                 "intercept": None, "night_offset": None, "n": c.get("frames"),
                                                 "loo_mae": c.get("loo_mae"), "loo_bias": c.get("loo_bias"),
                                                 "mean_count": c.get("mean_count"), "corr": c.get("corr"),
                                                 "relative_mae": c.get("relative_mae"),
                                                 "capacity": float(sum(cap.values())) if cap else None})
    return day_id


def import_forecast_json(conn: sqlite3.Connection, day_id: int, entity: str, path: Path) -> int:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    with db.transaction(conn):
        fid = db.upsert(conn, "forecast_runs", {
            "day_id": day_id, "entity": d.get("entity", entity), "model": d["model"], "train_day": d["train"],
            "horizons": db.js(d["horizons"]), "congestion_fill": d["congestion_fill"],
            "error_band": db.js({str(k): v for k, v in d["error_band_mae"].items()}), "origin": "committed",
            "source_path": rel(path), "source_sha256": sha256_file(path)}, key=("day_id", "entity"))
        conn.execute("DELETE FROM forecast_points WHERE run_id = ?", (fid,))
        fc = d["forecast_fill"]
        conn.executemany("INSERT INTO forecast_points VALUES (?, ?, ?, ?, ?, ?)",
                         [(fid, m, d["measured_fill"][i], fc["5"][i], fc["15"][i], fc["30"][i])
                          for i, m in enumerate(d["minutes"])])
    return fid


def compute_forecasts(conn: sqlite3.Connection, seed: dict, log: Log = print) -> int:
    """LightGBM forecasts for the sections the committed JSON does not cover (the zones, and the other day):
    the recipe of alpr/forecast_demo.py - one model trained on the other day's direction, pooled over its zones,
    error band = MAE on the last 30 % of the training day of a model refitted on the first 70 %."""
    try:
        import lightgbm  # noqa: F401
        import numpy as np
        from alpr.forecast import HORIZONS, LightGBMForecaster, Target, future_target, load_series, mae_rmse, target_of
    except ImportError as e:
        log(f"  computed forecasts skipped ({e})")
        return 0
    specs = {s["code"]: s for s in seed["series"] if s.get("calibration")}
    sections = seed.get("computed_forecast_sections", ["To CIQ"])
    n_runs = 0
    for code, spec in specs.items():
        train_code = spec.get("train_on")
        if train_code not in specs:
            continue
        day_id = db.scalar(conn, "SELECT id FROM series_days WHERE code = ?", (code,))
        tr, te = load_series(str(ROOT / specs[train_code]["csv"])), load_series(str(ROOT / spec["csv"]))
        secs = [s for s in sections if f"{s}.fill" in tr.columns and f"{s}.fill" in te.columns]
        zones = [s for s in secs if s != "To CIQ"]
        direction = "To CIQ"
        t_tr = target_of(tr, direction, "fill")
        pool = [target_of(tr, z, "fill") for z in zones]
        n = len(t_tr.y)
        cut = int(0.7 * n)
        head = Target(t_tr.y[:cut], t_tr.minute_of_day[:cut], {})
        m70 = LightGBMForecaster().fit(head, HORIZONS, [Target(p.y[:cut], p.minute_of_day[:cut], {}) for p in pool])
        model = LightGBMForecaster().fit(t_tr, HORIZONS, pool)
        for sec in secs:
            train_sec = target_of(tr, sec, "fill")
            fc70 = m70.forecast(train_sec, HORIZONS)
            band = {}
            for h in HORIZONS:
                o = np.arange(cut, n - h)
                mae = mae_rmse(future_target(train_sec.y, h)[o], fc70[h][o])[0]
                if mae == mae:                                     # NaN: no data in that part of the day
                    band[str(h)] = mae
            t_te = target_of(te, sec, "fill")
            fc = model.forecast(t_te, HORIZONS)
            existing = db.row(conn, "SELECT id, origin FROM forecast_runs WHERE day_id = ? AND entity = ?", (day_id, sec))
            if existing and existing["origin"] == "committed":
                pts = db.rows(conn, "SELECT f5, f15, f30 FROM forecast_points WHERE run_id = ? ORDER BY minute",
                              (existing["id"],))
                diff = max((abs(p[f"f{h}"] - fc[h][i]) for i, p in enumerate(pts) for h in HORIZONS
                            if p[f"f{h}"] is not None and fc[h][i] == fc[h][i]), default=0.0)
                log(f"  {code} {sec}: committed forecast kept (recomputed here: max difference {diff:.4f})")
                continue
            with db.transaction(conn):
                fid = db.upsert(conn, "forecast_runs", {
                    "day_id": day_id, "entity": sec, "model": "LightGBM (fill, pooled over the zones of the training day)",
                    "train_day": train_code, "horizons": db.js(list(HORIZONS)), "congestion_fill": 0.5,
                    "error_band": db.js(band), "origin": "computed", "source_path": None, "source_sha256": None},
                    key=("day_id", "entity"))
                conn.execute("DELETE FROM forecast_points WHERE run_id = ?", (fid,))

                def v(x):
                    return None if x != x else round(float(x), 4)
                conn.executemany("INSERT INTO forecast_points VALUES (?, ?, ?, ?, ?, ?)",
                                 [(fid, m, v(t_te.y[i]), v(fc[5][i]), v(fc[15][i]), v(fc[30][i]))
                                  for i, m in enumerate(te.times)])
            n_runs += 1
    return n_runs


# ---------------------------------------------------------------------------- demo seed
def _rel_time(now: datetime, text: str) -> datetime:
    """'-6:12' -> now - 6 h 12 min; '+8:00' -> now + 8 h."""
    sign = -1 if text.strip().startswith("-") else 1
    h, m = text.strip().lstrip("+-").split(":")
    return now + sign * timedelta(hours=int(h), minutes=int(m))


def seed_lists(conn: sqlite3.Connection, settings: Settings, seed: dict, gate_run_id: Optional[int]) -> None:
    now = clock.now()
    site_id = db.scalar(conn, "SELECT id FROM sites WHERE code = ?", (seed["site"],))

    def entry(lst: str, plate: str, site: Optional[int], **kw) -> None:
        text, compact, _ = _plate(plate)
        if db.scalar(conn, "SELECT id FROM list_entries WHERE list = ? AND compact = ? AND simulated = 1 AND "
                           "COALESCE(site_id, 0) = COALESCE(?, 0)", (lst, compact, site)):
            return
        db.insert(conn, "list_entries", {"list": lst, "site_id": site, "compact": compact, "plate_text": text,
                                         "label": kw.get("label", ""), "unit_no": kw.get("unit", ""),
                                         "reason": kw.get("reason", ""), "valid_to": None, "simulated": 1,
                                         "created_by": None, "created_at": clock.iso(now), "active": 1})
    with db.transaction(conn):
        for r in seed.get("residents", []):
            entry("resident", r["plate"], site_id, label=r.get("label", ""), unit=r.get("unit", ""))
        for r in seed.get("banned", []):
            entry("banned", r["plate"], site_id, reason=r.get("reason", ""))
        for r in (seed.get("police") or {}).get("watch", []):
            entry("watch", r["plate"], None, reason=r.get("reason", ""))
        for r in (seed.get("police") or {}).get("blacklist", []):
            entry("blacklist", r["plate"], None, reason=r.get("reason", ""))
        pass_ids = {}
        for p in seed.get("passes", []):
            text, compact, _ = _plate(p["plate"])
            pass_ids[p["key"]] = db.upsert(conn, "visitor_passes", {
                "site_id": site_id, "compact": compact, "plate_text": text, "unit_no": p["unit"],
                "visitor_label": p.get("label", ""), "valid_from": clock.iso(_rel_time(now, p["from"])),
                "valid_to": clock.iso(_rel_time(now, p["to"])), "status": "active", "simulated": 1,
                "seed_key": p["key"], "created_by": None, "created_at": clock.iso(now)}, key=("seed_key",))
        gate_cam = db.scalar(conn, "SELECT id FROM cameras WHERE kind = 'gate' AND site_id = ?", (site_id,))
        for a in seed.get("access_events", []):
            text, compact, _ = _plate(a["plate"])
            db.upsert(conn, "access_events", {
                "site_id": site_id, "camera_id": gate_cam, "sighting_id": None, "compact": compact,
                "plate_text": text, "direction": a["direction"], "decision": a["decision"], "identity": a["identity"],
                "pass_id": pass_ids.get(a.get("pass")), "unit_no": a.get("unit", ""), "guard_id": None,
                "note": a.get("note", ""), "at": clock.iso(_rel_time(now, a["at"])), "simulated": 1,
                "seed_key": a["key"]}, key=("seed_key",))
        if gate_run_id is not None:
            run = db.row(conn, "SELECT * FROM runs WHERE id = ?", (gate_run_id,))
            origin = clock.parse(run["clock_origin"])
            for g in seed.get("gate_arrivals", []):
                text, compact, region = _plate(g["plate"])
                _add_plate(conn, compact, text)
                db.upsert(conn, "sightings", {
                    "run_id": gate_run_id, "camera_id": run["camera_id"], "natural_key": f"g:{g['at_sec']}:{compact}",
                    "compact": compact, "plate_text": text, "confidence": None, "votes": 0, "status": "CONFIRMED",
                    "valid_format": 1, "vtype": g.get("vtype"), "region": region, "track_ids": "[]",
                    "first_frame": None, "last_frame": None, "first_sec": g["at_sec"], "last_sec": g["at_sec"],
                    "seen_at": _at(origin, g["at_sec"]), "last_seen_at": _at(origin, g["at_sec"]),
                    "provenance": "synthetic", "crop_path": _rendered_plate(settings, text, compact),
                    "recorded_at": clock.iso(now)}, key=("run_id", "natural_key"))


def seed_users(conn: sqlite3.Connection, path: Path, log: Log = print) -> int:
    n = 0
    for u in load_yaml(path).get("users", []):
        if db.scalar(conn, "SELECT id FROM users WHERE username = ?", (u["username"],)):
            continue
        site = db.scalar(conn, "SELECT id FROM sites WHERE code = ?", (u["site"],)) if u.get("site") else None
        create_user(conn, u["username"], u["display_name"], u["role"], u["password"], site, demo=True)
        n += 1
    log(f"  demo accounts: {n} created (see {rel(path)}; local demo only)")
    return n


def import_demo(conn: sqlite3.Connection, settings: Settings, demo_users: bool = False,
                computed_forecasts: bool = True, log: Log = print) -> Dict[str, int]:
    """The whole demo data set (see the module docstring)."""
    settings.ensure_dirs()
    seed = load_yaml(SEED_DIR / "cameras.yaml")
    with db.transaction(conn):
        for s in seed["sites"]:
            db.upsert(conn, "sites", {"code": s["code"], "name": s["name"], "kind": s["kind"]}, key=("code",))
        for c in seed["cameras"]:
            site = db.scalar(conn, "SELECT id FROM sites WHERE code = ?", (c.get("site"),))
            lat, lon = (c.get("latlon") or [None, None])
            db.upsert(conn, "cameras", {"code": c["code"], "name": c["name"], "kind": c["kind"], "site_id": site,
                                        "lat": lat, "lon": lon, "location_note": c.get("location_note", ""),
                                        "source_credit": c.get("source_credit", ""), "status": "replay",
                                        "notional_clock": int(bool(c.get("notional_clock")))}, key=("code",))
    lists_seed = load_yaml(SEED_DIR / "demo_lists.yaml")
    # lists first, so that the runs below are matched against them
    seed_lists(conn, settings, {**lists_seed, "gate_arrivals": []}, None)
    counts: Dict[str, int] = {}
    gate_run = None
    for r in seed["runs"]:
        cam = db.scalar(conn, "SELECT id FROM cameras WHERE code = ?", (r["camera"],))
        inserted: Set[str] = set()
        if r.get("inserted_plates_from"):
            from alpr.plates import normalize
            inserted = {normalize(s["fake"]) for s in load_yaml(ROOT / r["inserted_plates_from"]).get("swaps", [])}
        rid = import_results(conn, settings, code=r["code"], label=r["label"], camera_id=cam, results=ROOT / r["results"],
                             clock_origin=clock.parse(r["clock_origin"]), video=ROOT / r["video"] if r.get("video") else None,
                             note=r.get("note", ""), flow_primary=r.get("flow_primary", True), inserted=inserted,
                             evidence=ROOT / r["evidence"] if r.get("evidence") else None, log=log)
        if db.scalar(conn, "SELECT kind FROM cameras WHERE id = ?", (cam,)) == "gate":
            gate_run = rid
        counts[r["code"]] = db.scalar(conn, "SELECT COUNT(*) FROM sightings WHERE run_id = ?", (rid,))
        log(f"  {r['code']}: {counts[r['code']]} sightings, "
            f"{db.scalar(conn, 'SELECT COUNT(*) FROM alerts WHERE run_id = ?', (rid,))} alerts")
    seed_lists(conn, settings, lists_seed, gate_run)
    if gate_run is not None:
        match_lists(conn, gate_run)
    for s in seed["series"]:
        cam = db.scalar(conn, "SELECT id FROM cameras WHERE code = ?", (s["camera"],))
        day_id = import_series(conn, s, cam)
        log(f"  {s['code']}: {db.scalar(conn, 'SELECT COUNT(DISTINCT minute) FROM zone_minutes WHERE day_id = ?', (day_id,))} minutes")
    for f in seed.get("forecasts", []):
        day_id = db.scalar(conn, "SELECT id FROM series_days WHERE code = ?", (f["series"],))
        import_forecast_json(conn, day_id, f["entity"], ROOT / f["file"])
        log(f"  forecast {f['series']} / {f['entity']}: {rel(ROOT / f['file'])}")
    if computed_forecasts:
        log(f"  computed forecasts: {compute_forecasts(conn, seed, log)} sections")
    if demo_users:
        seed_users(conn, SEED_DIR / "demo_users.yaml", log)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('demo_imported_at', ?)", (clock.now_iso(),))
    totals = {t: db.scalar(conn, f"SELECT COUNT(*) FROM {t}") for t in
              ("runs", "sightings", "alerts", "lane_events", "crossings", "zone_minutes", "forecast_points")}
    audit(conn, SYSTEM, "import", "demo", None, totals)
    return totals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="import the committed demo data set")
    ap.add_argument("--demo-users", action="store_true", help="also create the test accounts of seed/demo_users.yaml")
    ap.add_argument("--no-computed-forecasts", action="store_true", help="skip the LightGBM forecasts of the zones")
    args = ap.parse_args(argv)
    if not args.demo:
        ap.print_help()
        return 1
    settings = load_settings()
    conn = db.connect(settings.db_path)
    print(f"importing the demo data -> {settings.db_path}")
    totals = import_demo(conn, settings, args.demo_users, not args.no_computed_forecasts)
    print("done: " + ", ".join(f"{k} {v}" for k, v in totals.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
