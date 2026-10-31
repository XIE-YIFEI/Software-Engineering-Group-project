"""Daily and weekly reports (CSV or PDF): traffic (flow by camera and vehicle type, lanes, congestion episodes),
alerts (by code and status, handling time) and gate (entries, stops, exits, overstays)."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date as Date, timedelta
from typing import Dict, List

from .. import db
from ..privacy import audit, mask_plate
from ..security import Forbidden, User
from .common import in_clause, title, visible_camera_ids

KINDS = {"traffic": ("traffic", "police", "admin"), "alerts": ("police", "admin"), "gate": ("guard",)}


def period(day: str, weekly: bool):
    d = Date.fromisoformat(day)
    if weekly:
        d = d - timedelta(days=d.weekday())                    # the Monday of that week
        return d.isoformat(), (d + timedelta(days=6)).isoformat()
    return d.isoformat(), d.isoformat()


def build(conn: sqlite3.Connection, user: User, kind: str, day: str, weekly: bool = False) -> dict:
    """{title, period, sections: [{title, columns, rows, note}]} for one report."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if user.role not in KINDS[kind]:
        raise Forbidden(f"the {user.role} role has no {kind} report")
    start, end = period(day, weekly)
    lo, hi = start, end + "T23:59:59.999"
    cams = visible_camera_ids(conn, user)
    sections: List[dict] = []
    if kind == "traffic":
        rows = []
        for r in db.rows(conn, f"SELECT r.*, c.name AS camera FROM runs r JOIN cameras c ON c.id = r.camera_id WHERE "
                               f"r.camera_id IN {in_clause(cams)} AND r.flow_primary = 1 AND r.flow_total IS NOT NULL "
                               f"AND substr(r.clock_origin, 1, 10) BETWEEN ? AND ? ORDER BY r.clock_origin", (start, end)):
            bt = json.loads(r["flow_by_type"] or "{}")
            dur = (r["window_end_sec"] or 0) - (r["window_start_sec"] or 0)
            rows.append([r["camera"], r["label"], r["clock_origin"][:16], f"{dur:.0f} s", r["flow_total"],
                         bt.get("Car", 0), bt.get("Motorcycle", 0), bt.get("Truck", 0), bt.get("Bus", 0)])
        sections.append({"title": "Vehicles counted (counting line)", "columns": ["Camera", "Run", "Start", "Duration",
                                                                                "Vehicles", "Cars", "Motorcycles",
                                                                                "Trucks", "Buses"], "rows": rows,
                         "note": "Counted at the counting line, independent of the plates; synthetic vehicles are not counted."})
        lane_rows = []
        for l in db.rows(conn, f"SELECT l.*, r.label FROM lanes l JOIN runs r ON r.id = l.run_id WHERE r.camera_id IN "
                               f"{in_clause(cams)} AND substr(r.clock_origin, 1, 10) BETWEEN ? AND ? ORDER BY r.id, "
                               f"l.position", (start, end)):
            lane_rows.append([l["label"], l["name"], l["direction"], l["count"], l["veh_per_min"],
                              f"{(l['occupancy'] or 0) * 100:.0f} %", l["density"],
                              "" if l["mean_speed_kmh"] is None else f"{l['mean_speed_kmh']:.0f}"])
        if lane_rows:
            sections.append({"title": "Per lane", "columns": ["Run", "Lane", "Direction", "Vehicles", "Veh/min", "Occupancy",
                                                             "Density (veh/km)", "Speed (km/h)"], "rows": lane_rows,
                             "note": "Speeds ±6.5 % (1σ) calibration error."})
        from . import forecast as fsvc
        cong = []
        for d in fsvc.days(conn, user):
            if start <= d.date <= end:
                cong.append([d.label, ", ".join(o[11:16] for o in d.onsets) or "none",
                             d.first_warning[11:16] if d.first_warning else "-", d.model or "-"])
        if cong:
            sections.append({"title": "Congestion (JB CIQ approach)", "columns": ["Day", "Congestion onsets",
                                                                                  "First warning", "Model"], "rows": cong,
                             "note": "Fill >= 50 % of the jam capacity for 10 minutes or more (TEAM_GUIDE section 10)."})
    elif kind == "alerts":
        w = f"camera_id IN {in_clause(cams)} AND seen_at BETWEEN ? AND ?"
        rows = [[title(r["code"]), r["code"], r["n"], r["open"], r["sim"]] for r in db.rows(
            conn, f"SELECT code, COUNT(*) AS n, SUM(status IN ('new', 'acknowledged')) AS open, "
                  f"SUM(provenance != 'camera') AS sim FROM alerts WHERE {w} GROUP BY code ORDER BY MIN(rank), code",
            (lo, hi))]
        sections.append({"title": "Alerts by type", "columns": ["Alert", "Code", "Total", "Open", "Simulated"],
                         "rows": rows, "note": "Simulated = fictitious plates or synthetic events of the demo data."})
        closed = db.rows(conn, f"SELECT a.code, a.created_at, a.closed_at, a.status FROM alerts a WHERE {w} AND "
                               f"a.closed_at IS NOT NULL", (lo, hi))
        from .. import clock
        mins = [(clock.parse(c["closed_at"]) - clock.parse(c["created_at"])).total_seconds() / 60 for c in closed]
        sections.append({"title": "Handling", "columns": ["Closed", "Resolved", "False alarms", "Median minutes to close"],
                         "rows": [[len(closed), sum(c["status"] == "resolved" for c in closed),
                                   sum(c["status"] == "false_alarm" for c in closed),
                                   f"{sorted(mins)[len(mins) // 2]:.0f}" if mins else "-"]]})
        lst = db.rows(conn, f"SELECT * FROM alerts WHERE {w} ORDER BY rank, seen_at", (lo, hi))
        sections.append({"title": "Alert list", "columns": ["Seen at", "Alert", "Plate", "Status", "Provenance"],
                         "rows": [[a["seen_at"][:19], title(a["code"]),
                                   a["plate_text"] if not user.masked else mask_plate(a["plate_text"]), a["status"],
                                   a["provenance"]] for a in lst]})
    else:
        site = user.site_id
        ev = db.rows(conn, "SELECT * FROM access_events WHERE site_id = ? AND at BETWEEN ? AND ?", (site, lo, hi))
        by: Dict[str, int] = {}
        for e in ev:
            k = f"{e['decision']} / {e['identity'].replace('_', ' ')}"
            by[k] = by.get(k, 0) + 1
        sections.append({"title": "Gate decisions", "columns": ["Decision / identity", "Count"],
                         "rows": [[k, v] for k, v in sorted(by.items())]})
        from . import gate as gsvc
        sections.append({"title": "Inside now (visitors)", "columns": ["Plate", "Unit", "Entered", "Minutes", "Overstay"],
                         "rows": [[i.plate, i.unit_no, i.entered_at[:16], i.minutes_inside, "yes" if i.overstay else ""]
                                  for i in gsvc.inside(conn, user)]})
    rep = {"title": f"{'Weekly' if weekly else 'Daily'} {kind} report", "period": f"{start} to {end}" if weekly else start,
           "kind": kind, "sections": sections}
    audit(conn, user, "report", "report", kind, {"period": rep["period"], "weekly": weekly})
    return rep


def to_csv(rep: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([rep["title"], rep["period"], "Course project demo - not an official document"])
    for s in rep["sections"]:
        w.writerow([])
        w.writerow([s["title"]])
        w.writerow(s["columns"])
        w.writerows(s["rows"])
        if s.get("note"):
            w.writerow([s["note"]])
    return buf.getvalue()


def to_pdf(rep: dict) -> bytes:
    from .evidence import MARK, pdf_document
    pdf, t = pdf_document(f"{rep['title']} {rep['period']}", MARK)
    F, W = pdf.family_name, pdf.w - 20
    pdf.set_font(F, "B", 16)
    pdf.cell(W, 9, t(f"{rep['title']} · {rep['period']}"), new_x="LMARGIN", new_y="NEXT")
    for s in rep["sections"]:
        pdf.set_font(F, "B", 12)
        pdf.ln(2)
        pdf.cell(W, 7, t(s["title"]), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(F, "", 8.5)
        if s["rows"]:
            with pdf.table(text_align="LEFT", line_height=4.8, first_row_as_headings=True) as tb:
                for r in [s["columns"]] + s["rows"]:
                    row = tb.row()
                    for c in r:
                        row.cell(t("" if c is None else c))
        else:
            pdf.multi_cell(W, 5, t("No data in this period."), new_x="LMARGIN", new_y="NEXT")
        if s.get("note"):
            pdf.set_text_color(90, 90, 98)
            pdf.multi_cell(W, 5, t(s["note"]), new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(20, 20, 24)
    return bytes(pdf.output())
