"""Evidence pack of one alert, as HTML (self-contained) or PDF.

Contents: case summary; scene frame; best plate crop; registry comparison (SIMULATED record vs what the camera saw);
for a cloned plate the conflicting sightings; speed +- error when the event has one; the Road Transport Act 1987
sections the alert relates to (webapp/legal.yaml: verified against the official texts, with the sources); processing
details; the SHA-256 of every embedded image. Every page carries "Course project demo - not an official document";
no PDRM / JPJ logos, no imitation of official forms. Police only; every export is recorded (evidence_exports and the
audit log, with the SHA-256 of the exported file).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .. import clock, db
from ..privacy import audit
from ..registry_provider import get_provider
from ..security import Forbidden, User
from ..settings import ROOT, active
from . import alerts as alert_svc
from . import plates as plate_svc
from .common import provenance_label

MARK = "Course project demo — not an official document"
HASH_NOTE = ("These SHA-256 values only prove that the image files were not changed after this pack was exported. "
             "They do not prove when, where or how the footage was recorded.")
DISCLAIMER = ("Course project demo. The vehicle registry is SIMULATED (no JPJ data); fictitious X-series plates were used "
              "for every flagged vehicle. This pack is not an official document and is not evidence of an offence.")


@lru_cache(maxsize=1)
def legal() -> dict:
    with open(Path(__file__).resolve().parent.parent / "legal.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def legal_refs(code: str) -> dict:
    """{sections: [...], note, sources, checked} for an alert code ({} when no section is cited)."""
    L = legal()
    entry = L["alerts"].get(code)
    if not entry:
        return {}
    return {"sections": [L["sections"][k] for k in entry["sections"]], "note": entry.get("note", ""),
            "sources": L["sources"], "checked": L["checked"], "source_note": L.get("note", "")}


@lru_cache(maxsize=1)
def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True, timeout=10)
        return out.stdout.strip()[:40] or "unknown"
    except Exception:                                                   # noqa: BLE001
        return "unknown"


def _jpeg(img) -> bytes:
    import cv2
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise ValueError("could not encode an image")
    return buf.tobytes()


def _scene_image(st, s: dict) -> Optional[dict]:
    import cv2
    path = st.media_dir / s["scene_path"]
    img = cv2.imread(str(path))
    if img is None:
        return None
    info = json.loads(s["scene_info"] or "{}")
    for key, colour in (("vehicle_box", (48, 209, 88)), ("plate_box", (255, 214, 10))):     # RGB: #30d158, #ffd60a
        b = info.get(key)
        if b:
            cv2.rectangle(img, (b[0], b[1]), (b[2], b[3]), colour[::-1], 3)
    when = f"frame {info.get('frame')} at {info.get('time_sec')} s of the source" if info else ""
    return {"name": "scene.jpg", "title": "Scene frame",
            "caption": f"Camera frame the best plate crop was cut from ({when}); the vehicle (green) and plate (yellow) "
                       f"boxes were drawn by the system on this copy.", "bytes": _jpeg(img)}


def _video_frame(run: dict, at_sec: float) -> Optional[dict]:
    """Fallback when no raw frame was saved: a frame of the committed annotated video at the record's time."""
    import cv2
    if not run.get("video_path"):
        return None
    p = Path(run["video_path"])
    p = p if p.is_absolute() else ROOT / p
    cap = cv2.VideoCapture(str(p))
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, at_sec - (run.get("video_offset_sec") or 0.0)) * 1000.0)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return None
    return {"name": "video_frame.jpg", "title": "Frame of the annotated demo video",
            "caption": f"No raw camera frame was saved for this record (the run was made without --evidence), so this "
                       f"is the frame of the annotated video at {at_sec:.1f} s of the source, while the vehicle was in "
                       f"view; its overlays were drawn by the system.",
            "bytes": _jpeg(frame)}


def _crop_image(st, s: dict) -> Optional[dict]:
    import cv2
    img = cv2.imread(str(st.media_dir / s["crop_path"]))
    if img is None:
        return None
    if s["provenance"] == "synthetic":
        return {"name": "plate.jpg", "title": "Rendered plate (no camera image)",
                "caption": "Synthetic event: there is no vehicle in the footage, so this is the plate rendered by the system.",
                "bytes": _jpeg(img)}
    h, w = img.shape[:2]
    if w < 360:                                                            # enlarge small crops for reading
        img = cv2.resize(img, (360, int(h * 360 / w)), interpolation=cv2.INTER_CUBIC)
    return {"name": "plate_crop.jpg", "title": "Best plate crop",
            "caption": "The sharpest crop of the plate over the vehicle's track (enlarged for reading).",
            "bytes": _jpeg(img)}


def build(conn: sqlite3.Connection, user: User, alert_id: int) -> dict:
    """Everything the pack shows (also used by the tests)."""
    if user.role != "police":
        raise Forbidden("only police officers export evidence packs")
    st = active()
    detail = alert_svc.get_alert(conn, user, alert_id)
    a = db.row(conn, "SELECT * FROM alerts WHERE id = ?", (alert_id,))
    s = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (a["sighting_id"],)) if a["sighting_id"] else None
    ev = db.row(conn, "SELECT * FROM lane_events WHERE id = ?", (a["lane_event_id"],)) if a["lane_event_id"] else None
    run = db.row(conn, "SELECT r.*, c.name AS camera_name, c.lat, c.lon, c.location_note, c.source_credit, "
                       "c.notional_clock FROM runs r JOIN cameras c ON c.id = r.camera_id WHERE r.id = ?", (a["run_id"],))
    # fallback frame: late in the vehicle's track, where an approaching vehicle is closest to the camera
    at_sec = (s["first_sec"] + 0.7 * (s["last_sec"] - s["first_sec"])) if s else ev["time_sec"] if ev else None
    images: List[dict] = []
    if s and s["scene_path"] and (st.media_dir / s["scene_path"]).exists():
        img = _scene_image(st, s)
        if img:
            images.append(img)
    if not images and at_sec is not None:
        img = _video_frame(run, at_sec)
        if img:
            images.append(img)
    if s and s["crop_path"] and (st.media_dir / s["crop_path"]).exists():
        img = _crop_image(st, s)
        if img:
            images.append(img)
    for img in images:
        img["sha256"] = hashlib.sha256(img["bytes"]).hexdigest()
    rec = get_provider(str(st.registry_path)).lookup(a["compact"]) if a["compact"] else None
    travel = None
    if a["code"] == "DUPLICATE" and a["compact"]:
        tl = plate_svc.timeline_of(conn, user, a["compact"])
        travel = {"points": [p.model_dump() for p in tl.points], "legs": [l.model_dump() for l in tl.legs],
                  "rule": tl.rule}
    with open(ROOT / "alpr" / "config.yaml", encoding="utf-8") as f:
        common = (yaml.safe_load(f) or {}).get("common", {})
    return {
        "mark": MARK, "disclaimer": DISCLAIMER, "hash_note": HASH_NOTE,
        "alert": detail.model_dump(), "provenance_label": provenance_label(a["provenance"]) or "Camera observation",
        "sighting": s, "lane_event": ev, "run": run,
        "clock_note": "The demo footage has no recording time: times use the run's notional clock."
        if run and run["notional_clock"] else "",
        "registry": rec, "registry_name": get_provider(str(st.registry_path)).name, "comparison":
            [c.model_dump() for c in detail.comparison], "travel": travel, "speed_text": detail.speed_text,
        "legal": legal_refs(a["code"]), "images": images,
        "processing": {"software": f"SECS Vision (git commit {git_commit()})", "run": f"{run['label']} ({run['code']})",
                       "profile": run["profile"], "results_file": run["results_path"],
                       "results_sha256": run["results_sha256"], "vehicle_model": common.get("vehicle_model"),
                       "plate_model": common.get("plate_model"), "ocr_model": common.get("ocr_model"),
                       "processing": json.loads(run["processing"]) if run["processing"] else None},
        "exported_by": f"{user.display_name} ({user.username})", "exported_at": clock.now_iso()[:19],
    }


# ---------------------------------------------------------------------------- HTML
def render_html(pack: dict) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "templates")),
                      autoescape=select_autoescape(["html"]))
    imgs = [{**i, "data": base64.b64encode(i["bytes"]).decode("ascii")} for i in pack["images"]]
    return env.get_template("evidence.html").render(p=pack, images=imgs)


# ---------------------------------------------------------------------------- PDF
_FONTS = [("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
          ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
          ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
          ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
          ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf")]
_ASCII = {"—": "-", "–": "-", "±": "+/-", "→": "->", "·": "-", "σ": "sigma", "≥": ">=", "≤": "<=", "…": "...",
          "’": "'", "‘": "'", "“": '"', "”": '"', "×": "x"}


def pdf_document(title: str, mark: str):
    """An fpdf2 document with the demo mark in the header and footer of every page and a diagonal watermark.
    Returns (pdf, text) where text() makes a string safe for the font that was found."""
    import logging
    from fpdf import FPDF
    logging.getLogger("fontTools").setLevel(logging.ERROR)            # "MERG NOT subset" notes while embedding fonts
    font = next(((r, b) for r, b in _FONTS if Path(r).exists() and Path(b).exists()), None)

    def text(s) -> str:
        s = "" if s is None else str(s)
        if font:
            return s
        for k, v in _ASCII.items():
            s = s.replace(k, v)
        return s.encode("latin-1", "replace").decode("latin-1")

    class Doc(FPDF):
        def header(self):
            self.set_text_color(235, 235, 238)
            self.set_font(FAMILY, "B", 44)
            with self.rotation(32, self.w / 2, self.h / 2):
                self.text(self.w / 2 - 95, self.h / 2, text("COURSE PROJECT DEMO"))
            self.set_fill_color(255, 69, 58)
            self.set_text_color(255, 255, 255)
            self.set_font(FAMILY, "B", 11)
            self.set_xy(10, 8)
            self.cell(self.w - 20, 9, text(mark.upper()), fill=True, align="C")
            self.set_text_color(20, 20, 24)
            self.ln(13)

        def footer(self):
            self.set_y(-14)
            self.set_font(FAMILY, "", 8)
            self.set_text_color(110, 110, 118)
            self.cell(0, 6, text(f"{mark} · {title} · page {self.page_no()}/{{nb}}"), align="C")

    pdf = Doc(format="A4")
    if font:
        pdf.add_font("Body", "", font[0])
        pdf.add_font("Body", "B", font[1])
        FAMILY = "Body"
    else:
        FAMILY = "helvetica"
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    pdf.family_name = FAMILY
    return pdf, text


def render_pdf(pack: dict) -> bytes:
    a = pack["alert"]
    pdf, t = pdf_document(f"Evidence pack, alert #{a['id']}", pack["mark"])
    F = pdf.family_name
    W = pdf.w - 20

    def h(txt):
        pdf.set_font(F, "B", 12)
        pdf.set_text_color(20, 20, 24)
        pdf.ln(2)
        pdf.cell(W, 7, t(txt), new_x="LMARGIN", new_y="NEXT")

    def p(txt, size=9.5, colour=(40, 40, 46)):
        pdf.set_font(F, "", size)
        pdf.set_text_color(*colour)
        pdf.multi_cell(W, 5, t(txt), align="L", new_x="LMARGIN", new_y="NEXT")

    def table(rows, widths):
        pdf.set_font(F, "", 9)
        pdf.set_text_color(40, 40, 46)
        with pdf.table(col_widths=widths, text_align="LEFT", line_height=5.2, first_row_as_headings=True) as tb:
            for r in rows:
                row = tb.row()
                for c in r:
                    row.cell(t(c))

    pdf.set_font(F, "B", 16)
    pdf.cell(W, 9, t(f"Evidence pack · {a['title']} · {a['plate'] or 'no plate'}"), new_x="LMARGIN", new_y="NEXT")
    p(pack["disclaimer"], 9, (160, 40, 30))
    h("Case summary")
    run = pack["run"]
    rows = [["Field", "Value"], ["Alert", f"#{a['id']} {a['code']} ({a['severity']}): {a['message']}"],
            ["Plate", a["plate"] or "-"], ["Seen at", a["seen_at"][:19]], ["Camera", run["camera_name"]],
            ["Location", run["location_note"] or "-"], ["Run", run["label"]],
            ["Provenance", pack["provenance_label"]], ["Status", a["status"]]]
    if pack["clock_note"]:
        rows.append(["Clock", pack["clock_note"]])
    table(rows, (30, 160))
    for img in pack["images"]:
        h(img["title"])
        pdf.image(io.BytesIO(img["bytes"]), w=min(W, 190) if img["name"] != "plate_crop.jpg" else 90)
        p(img["caption"], 8.5, (90, 90, 98))
        p(f"SHA-256 {img['sha256']}", 8, (90, 90, 98))
    if pack["comparison"]:
        h(f"Registry comparison ({pack['registry_name']})")
        table([["Field", "Registered (SIMULATED)", "Observed", "Match"]] +
              [[c["field"], c["registered"], c["observed"], "" if c["match"] is None else ("yes" if c["match"] else "NO")]
               for c in pack["comparison"]], (35, 65, 60, 30))
    if pack["travel"]:
        h("Sightings of this plate (cross-camera)")
        table([["Time", "Place", "Source"]] + [[x["seen_at"][:19], x["place"], x["provenance_label"] or x["source"]]
                                               for x in pack["travel"]["points"]], (40, 90, 60))
        for leg in pack["travel"]["legs"]:
            p("· " + leg["message"])
        p(pack["travel"]["rule"], 8.5, (90, 90, 98))
    if pack["speed_text"]:
        h("Measurement")
        p(pack["speed_text"] + ". Speeds come only from calibrated cameras (6.5 % 1-sigma calibration error).")
    h("Legal reference (Road Transport Act 1987, Act 333)")
    L = pack["legal"]
    if not L:
        p("No section of the Road Transport Act 1987 is cited for this kind of alert.")
    else:
        for sec in L["sections"]:
            p(f"Section {sec['section']}: {sec['title']} ({sec['title_ms']}). {sec['text']}")
        if L.get("note"):
            p(L["note"], 9, (90, 90, 98))
        p(f"Checked on {L['checked']} against: " + "; ".join(f"{s['title']}, {s['url']}" for s in L["sources"])
          + f". {L['source_note']} Penalties are not quoted (amended since).", 8, (90, 90, 98))
    h("Processing")
    pr = pack["processing"]
    table([["Item", "Value"], ["Software", pr["software"]], ["Run", pr["run"]], ["Profile", pr["profile"] or "-"],
           ["Results file", pr["results_file"]], ["Results SHA-256", pr["results_sha256"]],
           ["Models", f"{pr['vehicle_model']}, {pr['plate_model']}, OCR {pr['ocr_model']}"]], (35, 155))
    h("Integrity")
    p(pack["hash_note"])
    p(f"Exported by {pack['exported_by']} at {pack['exported_at']} (Malaysia time). The SHA-256 of this file is "
      f"recorded in the system's audit log.")
    return bytes(pdf.output())


def export(conn: sqlite3.Connection, user: User, alert_id: int, fmt: str = "html") -> Path:
    """Write the pack to data/web/exports/evidence/ and record the export. Returns the file path."""
    if fmt not in ("html", "pdf"):
        raise ValueError("format must be html or pdf")
    pack = build(conn, user, alert_id)
    data = render_html(pack).encode("utf-8") if fmt == "html" else render_pdf(pack)
    out_dir = active().export_dir / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"alert_{alert_id}_{clock.now().strftime('%Y%m%d_%H%M%S')}.{fmt}"
    path.write_bytes(data)
    file_hash = hashlib.sha256(data).hexdigest()
    hashes: Dict[str, str] = {i["name"]: i["sha256"] for i in pack["images"]}
    with db.transaction(conn):
        db.insert(conn, "evidence_exports", {"alert_id": alert_id, "user_id": user.id, "format": fmt, "path": str(path),
                                             "file_sha256": file_hash, "image_hashes": db.js(hashes),
                                             "at": clock.now_iso()})
        db.insert(conn, "alert_actions", {"alert_id": alert_id, "user_id": user.id, "action": "evidence_export",
                                          "from_status": None, "to_status": None, "note": f"{fmt.upper()} evidence pack",
                                          "corrected_plate": None, "at": clock.now_iso()})
        audit(conn, user, "evidence_export", "alert", alert_id, {"format": fmt, "file_sha256": file_hash,
                                                                  "images": hashes})
    return path
