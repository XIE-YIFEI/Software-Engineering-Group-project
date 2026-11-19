"""P03 OCR evidence adapter; raw readings are evidence, never registry decisions."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .. import db
from ..privacy import audit
from . import plates

MAX_LINES = 100_000
MAX_BYTES = 64 * 1024 * 1024
MAX_LINE = 16 * 1024
REASONS = {None, "quality_gate", "read_budget", "empty_read", "nonstandard_below_min_width"}
TEXT_FIELDS = ("raw_text", "voter_input_text")


def _number(v):
    return type(v) in (int, float) and math.isfinite(v)


def _event(d):
    if not isinstance(d, dict):
        raise ValueError("event must be an object")
    for k, minimum in (("track_id", 0), ("frame", 1), ("n_backend_calls", 0)):
        if type(d.get(k)) is not int or d[k] < minimum:
            raise ValueError("invalid integer")
    if not _number(d.get("time_sec")) or d["time_sec"] < 0:
        raise ValueError("invalid time")
    for k in TEXT_FIELDS:
        if d.get(k) is not None and (not isinstance(d[k], str) or len(d[k]) > 256):
            raise ValueError("invalid OCR text")
    if d.get("ocr_valid_format") is not None and type(d["ocr_valid_format"]) is not bool:
        raise ValueError("invalid format verdict")
    if type(d.get("accepted_for_vote")) is not bool:
        raise ValueError("invalid vote flag")
    reason = d.get("reject_reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("invalid reason")
    if reason not in REASONS:
        raise ValueError("unknown reason")
    for k in ("crop_quality_score", "ocr_confidence"):
        if d.get(k) is None and k == "ocr_confidence":
            continue
        if not _number(d.get(k)) or not 0 <= d[k] <= 1:
            raise ValueError("invalid score")
    return {k: d.get(k) for k in ("track_id", "frame", "time_sec", *TEXT_FIELDS,
        "ocr_valid_format", "ocr_confidence", "crop_quality_score", "accepted_for_vote",
        "reject_reason", "n_backend_calls")}


def import_stream(conn, run_id: int, path: Path) -> None:
    """Import only unambiguous readings inside the terminal snapshot; replace on retry.

    Called after child exit, in the results transaction. File/line errors degrade
    evidence only; database errors still propagate and roll back the import.
    """
    run = db.row(conn, "SELECT * FROM runs WHERE id=?", (run_id,))
    if run is None:
        raise LookupError("no such run")
    by_track = {}
    for s in db.rows(conn, "SELECT * FROM sightings WHERE run_id=? AND provenance='camera'", (run_id,)):
        for tid in db.unjs(s["track_ids"], []):
            by_track.setdefault(tid, []).append(s)
    valid_coverage = (type(run["frames"]) is int and run["frames"] > 0
        and _number(run["window_start_sec"]) and _number(run["window_end_sec"])
        and run["window_end_sec"] >= run["window_start_sec"])
    state, imported, invalid, unmatched, tail, limited = "available", 0, 0, 0, False, False
    with db.transaction(conn):
        conn.execute("DELETE FROM ocr_read_evidence WHERE sighting_id IN (SELECT id FROM sightings WHERE run_id=?)", (run_id,))
        try:
            if not valid_coverage:
                state = "invalid_coverage"
            else:
                with path.open("rb") as stream:
                    for line_no in range(1, MAX_LINES + 2):
                        if line_no > MAX_LINES or stream.tell() >= MAX_BYTES:
                            limited = bool(stream.read(1))
                            break
                        line = stream.readline(MAX_LINE + 1)
                        if not line:
                            break
                        if len(line) > MAX_LINE:
                            limited = True
                            break
                        # A hard kill may leave a torn final record. Only terminated lines count.
                        if not line.endswith(b"\n"):
                            tail = True
                            break
                        if not line.strip():
                            continue
                        try:
                            e = _event(json.loads(line.decode("utf-8")))
                        except (ValueError, UnicodeError, TypeError):
                            invalid += 1
                            continue
                        if (e["frame"] > run["frames"]
                                or not run["window_start_sec"] - .011 <= e["time_sec"] <= run["window_end_sec"] + .011):
                            unmatched += 1
                            continue
                        matches = [s for s in by_track.get(e["track_id"], [])
                            if type(s["first_frame"]) is int and type(s["last_frame"]) is int
                            and s["first_frame"] <= e["frame"] <= s["last_frame"]
                            and s["first_sec"] - .011 <= e["time_sec"] <= s["last_sec"] + .011]
                        if len(matches) != 1:
                            unmatched += 1
                            continue
                        conn.execute("INSERT INTO ocr_read_evidence (sighting_id, source_line, frame, event) VALUES (?,?,?,?)",
                            (matches[0]["id"], line_no, e["frame"], db.js(e)))
                        imported += 1
        except FileNotFoundError:
            state = "missing"
        except OSError:
            state = "unreadable"
        if state == "available" and (invalid or tail or limited):
            state = "incomplete"
        db.upsert(conn, "ocr_streams", {"run_id": run_id, "state": state, "imported": imported,
            "invalid": invalid, "unmatched": unmatched, "trailing_line": int(tail), "limited": int(limited)}, key=("run_id",))


def get(conn, user, sighting_id: int, *, offset=0, limit=100, exporting=False) -> dict:
    # Re-check current camera/role permissions on every read and export.
    s = plates.get(conn, user, sighting_id, "OCR evidence review")
    run = db.row(conn, "SELECT * FROM runs WHERE id=?", (s.run_id,))
    meta = db.row(conn, "SELECT * FROM ocr_streams WHERE run_id=?", (s.run_id,))
    offset, limit = max(0, int(offset)), min(500, max(1, int(limit)))
    rows = db.rows(conn, "SELECT event FROM ocr_read_evidence WHERE sighting_id=? ORDER BY frame,source_line LIMIT ? OFFSET ?",
        (sighting_id, limit, offset))
    items = [db.unjs(r["event"]) for r in rows]
    # A reveal of the final plate must not expose different raw OCR alternatives.
    raw_visible = not user.masked
    if not raw_visible:
        items = [{**e, **{k: None for k in TEXT_FIELDS}} for e in items]
    total = db.scalar(conn, "SELECT COUNT(*) FROM ocr_read_evidence WHERE sighting_id=?", (sighting_id,))
    audit(conn, user, "ocr_export" if exporting else "ocr_view", "sighting", sighting_id,
        {"raw_visible": raw_visible, "offset": offset, "returned": len(items)}, reason="OCR evidence review")
    return {"sighting_id": sighting_id, "run_id": s.run_id, "complete": bool(run["complete"]),
        "saved_frames": run["frames"], "saved_window": [run["window_start_sec"], run["window_end_sec"]],
        "stream": {k: meta[k] for k in ("state", "imported", "invalid", "unmatched", "trailing_line", "limited")}
            if meta else {"state": "not_recorded"},
        "raw_visible": raw_visible, "items": items, "total": total, "offset": offset, "limit": limit,
        "has_more": offset + len(items) < total,
        "note": "Selected OCR read events, not confirmed plates or rule decisions. Only unambiguous readings inside the saved coverage are linked."}
