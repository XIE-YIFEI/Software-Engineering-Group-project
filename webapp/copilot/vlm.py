"""Optional image checks with Claude's vision (OFF by default; an administrator switches them on in Settings).

They send camera images of real vehicles to a third-party API, which is a personal-data question (following the
data-minimisation principle of Malaysia's PDPA 2010), so they are opt-in, meant for local demos, police only, and
every call is audit-logged with the SHA-256 of what was sent (the images themselves are not stored). Both only
PROPOSE; a person decides.

  second reading   a provisional (TENTATIVE) plate: the model reads the best plate crop and says why it is hard; an
                   officer confirms the reading (it goes to the OCR correction pool) or rejects it.
  appearance       a TYPE_MISMATCH or DUPLICATE alert: the model describes the vehicle (type, colour, make) from the
                   saved camera frame, and the system compares that with the SIMULATED registry record. The result is
                   worded as an observation for human verification, never as a conclusion.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from .. import clock, db
from ..privacy import audit
from ..registry_provider import get_provider
from ..security import Forbidden, User, require
from ..services import alerts as alert_svc
from ..services.common import visible_camera_ids
from ..settings import active
from . import config, store

READING_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["plate_visible", "reading", "confidence", "uncertain_characters", "why_hard"],
    "properties": {
        "plate_visible": {"type": "boolean"},
        "reading": {"type": "string", "description": "The most likely plate text, e.g. VCV 1555; empty if unreadable"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "uncertain_characters": {"type": "string", "description": "Which characters are uncertain and the alternatives"},
        "why_hard": {"type": "string", "description": "Why the crop is hard to read (blur, angle, light, occlusion ...)"}}}
APPEARANCE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["vehicle_visible", "vehicle_type", "body_style", "colour", "make_model_guess", "confidence", "notes"],
    "properties": {
        "vehicle_visible": {"type": "boolean"},
        "vehicle_type": {"type": "string", "enum": ["Car", "Motorcycle", "Truck", "Bus", "Van", "Unknown"]},
        "body_style": {"type": "string", "description": "e.g. sedan, hatchback, SUV, MPV, pickup, van, lorry"},
        "colour": {"type": "string", "enum": ["White", "Silver", "Grey", "Black", "Red", "Orange", "Yellow", "Green",
                                              "Blue", "Brown", "Unknown"]},
        "make_model_guess": {"type": "string", "description": "Make and model if recognisable, else empty"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "notes": {"type": "string"}}}
READ_PROMPT = ("You help a police officer with a licence-plate crop from a Malaysian traffic camera that the OCR could "
               "only read provisionally. Propose the most likely reading of the plate (Malaysian format: 1-3 letters, 1-4 "
               "digits, an optional letter) and say which characters are uncertain and why the crop is hard to read. "
               "If the plate cannot be read, say so. This is a proposal for the officer to confirm, not a final reading.")
LOOK_PROMPT = ("You describe the vehicle in the image from a Malaysian traffic camera for a person who will compare it "
               "with a registration record. Describe only what is visible: the vehicle type, body style, main body "
               "colour, and the make and model only if you can recognise them. Do not read or discuss the plate and "
               "do not judge whether anything is wrong; if the vehicle is not clearly visible, say so.")
NOTE = {"differs": {"en": "Appearance differs from the registration record: needs human verification.",
                    "zh": "外观与登记不符，需人工核实。"},
        "consistent": {"en": "Appearance is consistent with the registration record (an AI observation, not proof).",
                       "zh": "外观与登记记录相符（AI 观察结果，并非证据）。"},
        "unclear": {"en": "The vehicle is not clearly visible: no comparison was made.",
                    "zh": "车辆不够清晰：未作比较。"}}


def _enabled(conn: sqlite3.Connection, user: User) -> config.CopilotConfig:
    require(user, "police")
    cfg = config.load(conn)
    if not cfg.vlm_enabled:
        raise Forbidden("the image checks are switched off (an administrator can switch them on in Settings)")
    return cfg


def _jpeg(img) -> bytes:
    import cv2
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError("could not encode the image")
    return buf.tobytes()


def _call(conn: sqlite3.Connection, user: User, cfg: config.CopilotConfig, feature: str, prompt: str, image: bytes,
          context: str, schema: Dict[str, Any], llm: Any = None) -> Tuple[Dict[str, Any], str]:
    if llm is None:
        st = config.status(conn, cfg)
        if not st["online"]:
            raise ValueError(f"the AI service is not available ({st['reason']})")
        from .agent import make_llm
        llm = make_llm(cfg)
    store.check_rate(conn, user, cfg.questions_per_hour)
    msg = {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.standard_b64encode(image).decode("ascii")}},
        {"type": "text", "text": context}]}
    from .llm import run
    text, turn = run(llm.stream(system=[{"type": "text", "text": prompt}], messages=[msg], max_tokens=4000,
                                output_schema=schema))
    store.log_usage(conn, user, feature, turn.model, turn.usage, turn.cost_usd, turn.stop_reason, turn.request_id)
    if turn.stop_reason == "refusal":
        raise ValueError("the model declined to describe this image")
    try:
        return json.loads(text), turn.model
    except ValueError:
        raise ValueError("the model's answer was not valid JSON; try again")


def _record(conn: sqlite3.Connection, user: User, kind: str, model: str, image: bytes, result: Dict[str, Any],
            sighting_id: Optional[int], alert_id: Optional[int]) -> Dict[str, Any]:
    digest = {"image.jpg": hashlib.sha256(image).hexdigest()}
    vid = db.insert(conn, "copilot_vlm", {"kind": kind, "sighting_id": sighting_id, "alert_id": alert_id,
                                          "user_id": user.id, "model": model, "image_sha256": json.dumps(digest),
                                          "result": json.dumps(result, ensure_ascii=False), "status": "proposed",
                                          "created_at": clock.now_iso()})
    audit(conn, replace(user, via="assistant"), f"vlm_{kind}", "sighting" if sighting_id else "alert",
          sighting_id or alert_id, {"vlm": vid, "model": model, "sent_image_sha256": digest["image.jpg"]},
          reason="Data-quality check" if kind == "second_reading" else "Alert follow-up")
    return {"id": vid, "kind": kind, "status": "proposed", "model": model, "result": result,
            "image_sha256": digest["image.jpg"], "ai": True}


def second_reading(conn: sqlite3.Connection, user: User, sighting_id: int, llm: Any = None) -> Dict[str, Any]:
    cfg = _enabled(conn, user)
    s = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (sighting_id,))
    if s is None or s["camera_id"] not in visible_camera_ids(conn, user):
        raise LookupError("no such sighting")
    if s["status"] != "TENTATIVE":
        raise ValueError("a second reading is for provisional (TENTATIVE) readings only")
    if s["provenance"] == "synthetic" or not s["crop_path"] or not (active().media_dir / s["crop_path"]).exists():
        raise ValueError("there is no camera crop of this plate")
    import cv2
    img = cv2.imread(str(active().media_dir / s["crop_path"]))
    if img is None:
        raise ValueError("the crop could not be read")
    h, w = img.shape[:2]
    if w < 360:
        img = cv2.resize(img, (360, int(h * 360 / w)), interpolation=cv2.INTER_CUBIC)
    data = _jpeg(img)
    ctx = (f"The OCR's provisional reading (it may be wrong): {s['plate_text']} (confidence {s['confidence']}, "
           f"{s['votes']} readings over the track).")
    result, model = _call(conn, user, cfg, "second_reading", READ_PROMPT, data, ctx, READING_SCHEMA, llm)
    result["ocr_reading"] = s["plate_text"]
    out = _record(conn, user, "second_reading", model, data, result, sighting_id, None)
    out["note"] = {"en": "AI proposal: an officer must confirm it.", "zh": "AI 建议：必须由警员确认。"}
    return out


def appearance(conn: sqlite3.Connection, user: User, alert_id: int, llm: Any = None) -> Dict[str, Any]:
    cfg = _enabled(conn, user)
    a = alert_svc.get_alert(conn, user, alert_id)
    if a.code not in ("TYPE_MISMATCH", "DUPLICATE"):
        raise ValueError("the appearance check is for TYPE_MISMATCH and DUPLICATE alerts")
    s = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (a.sighting_id,)) if a.sighting_id else None
    if s is None or s["provenance"] == "synthetic":
        raise ValueError("this alert has no vehicle in the footage (a synthetic event)")
    info = json.loads(s["scene_info"] or "{}")
    if not s["scene_path"] or not info.get("vehicle_box") or not (active().media_dir / s["scene_path"]).exists():
        raise ValueError("no saved camera frame of this vehicle (the run was made without --evidence)")
    import cv2
    img = cv2.imread(str(active().media_dir / s["scene_path"]))
    x1, y1, x2, y2 = info["vehicle_box"]
    mx, my = int(0.08 * (x2 - x1)), int(0.08 * (y2 - y1))
    crop = img[max(0, y1 - my):y2 + my, max(0, x1 - mx):x2 + mx]
    data = _jpeg(crop)
    result, model = _call(conn, user, cfg, "appearance", LOOK_PROMPT, data,
                          "Describe the vehicle in this crop of a camera frame.", APPEARANCE_SCHEMA, llm)
    rec = get_provider(str(active().registry_path)).lookup(s["compact"]) or {}
    rows, verdict = compare(rec, result, s["vtype"])
    result.update(comparison=rows, verdict=verdict, note=NOTE[verdict], registry_simulated=True)
    return _record(conn, user, "appearance", model, data, result, s["id"], alert_id)


def compare(rec: Dict[str, Any], seen: Dict[str, Any], detector_type: Optional[str]) -> Tuple[List[Dict[str, Any]], str]:
    """Registered (SIMULATED record) vs what the model saw; the verdict is computed here, not by the model."""
    from alpr.registry import BODY_TYPES, colours_match
    if not seen.get("vehicle_visible"):
        return [], "unclear"
    rows = []
    body = rec.get("body_type")
    if body:
        vt = seen.get("vehicle_type") or "Unknown"
        fits = None if vt == "Unknown" else (vt in BODY_TYPES.get(body, (body,)) or (vt == "Van" and body in ("Van", "MPV")))
        rows.append({"field": "Vehicle type", "registered": body, "observed": f"{vt} ({seen.get('body_style') or '-'})",
                     "match": fits})
    if rec.get("colour"):
        c = seen.get("colour") or "Unknown"
        rows.append({"field": "Colour", "registered": rec["colour"], "observed": c,
                     "match": None if c == "Unknown" else colours_match(rec["colour"], c)})
    if rec.get("make_model"):
        g = (seen.get("make_model_guess") or "").strip()
        make = rec["make_model"].split()[0].lower()
        rows.append({"field": "Make / model", "registered": rec["make_model"], "observed": g or "not recognised",
                     "match": None if not g else make in g.lower()})
    if detector_type:
        rows.append({"field": "Detector class (system)", "registered": "-", "observed": detector_type, "match": None})
    return rows, "differs" if any(r["match"] is False for r in rows) else "consistent"


def decide(conn: sqlite3.Connection, user: User, vlm_id: int, accept: bool, reading: str = "") -> Dict[str, Any]:
    """The officer's decision on a proposal. A confirmed second reading goes to the OCR correction pool."""
    require(user, "police")
    v = db.row(conn, "SELECT * FROM copilot_vlm WHERE id = ?", (vlm_id,))
    if v is None:
        raise LookupError("no such proposal")
    if v["status"] != "proposed":
        raise ValueError(f"this proposal is already {v['status']}")
    from ..analysis import fuzzy
    now = clock.now_iso()
    corrected = None
    if accept and v["kind"] == "second_reading":
        corrected = fuzzy.compact(reading or json.loads(v["result"]).get("reading", ""))
        if not corrected:
            raise ValueError("enter the confirmed reading")
        s = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (v["sighting_id"],))
        st = active()
        crop_rel = None
        if s and s["crop_path"] and (st.media_dir / s["crop_path"]).exists():
            crop_rel = f"vlm{vlm_id}_{corrected}{(st.media_dir / s['crop_path']).suffix}"
            st.corrections_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(st.media_dir / s["crop_path"], st.corrections_dir / crop_rel)
        db.insert(conn, "corrections", {"alert_id": None, "sighting_id": v["sighting_id"],
                                        "ocr_compact": (s or {}).get("compact", ""), "corrected_compact": corrected,
                                        "crop_path": crop_rel, "created_by": user.id, "created_at": now})
    status = "confirmed" if accept else "rejected"
    conn.execute("UPDATE copilot_vlm SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                 (status, user.id, now, vlm_id))
    audit(conn, replace(user, via="assistant"), f"vlm_{v['kind']}_{status}", "copilot_vlm", vlm_id,
          {"corrected_plate": corrected})
    return {"id": vlm_id, "status": status, "corrected_plate": corrected}
