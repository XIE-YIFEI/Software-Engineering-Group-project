"""The copilot's tools: thin wrappers of webapp/services, called with the signed-in user (via="assistant").

Permissions are inherited, not re-implemented: a role is offered only the tools it may use (`definitions(role)`), a
call of any other tool is refused (`execute`), and each tool calls the same service function as the pages - which
checks the role again, scopes cameras and sites, masks plates and writes the audit row (marked via=assistant). The
assistant has no SQL of its own. Read tools return compact JSON with a `ref` for every record (S: sighting, A: alert,
E: lane event, G: gate access event, P: visitor pass); the refs become the citation chips of the panel. The
prepare_* tools never write: they create action cards (actions.py) that the user confirms or cancels. While the
copilot is read-only (config.ACTIONS_ENABLED is False, plan Rev2) they are offered to no role and a call to one is
refused, so the model can only read.

Inputs are validated against each tool's pydantic model before anything runs (the request streams tool input
eagerly, so the API does not validate it); an invalid input goes back to the model as an error result to retry.
"""
from __future__ import annotations

import json
import re
import sqlite3
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import clock, db
from ..analysis import fuzzy
from ..models import SightingOut, SightingQuery
from ..privacy import can_see_full
from ..security import Forbidden, User
from ..services import admin as admin_svc
from ..services import alerts as alert_svc
from ..services import forecast as forecast_svc
from ..services import gate as gate_svc
from ..services import overview as overview_svc
from ..services import plates as plate_svc
from ..services import reports as report_svc
from ..services import traffic as traffic_svc
from ..services.common import ALERT_TITLE
from ..settings import PURPOSES, REVEAL_REASONS
from . import actions, config, derived, explain

Purpose = Literal[tuple(PURPOSES)]                          # type: ignore[valid-type]
AlertCode = Literal[tuple(ALERT_TITLE)]                     # type: ignore[valid-type]
ALL = ("police", "guard", "traffic", "admin")
ROAD = ("police", "traffic", "admin")
ALERT_ROLES = ("police", "guard", "admin")


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Ctx:
    """One question's tool context: the user acting through the assistant, and what the panel will show."""
    conn: sqlite3.Connection
    user: User
    conversation_id: Optional[int] = None
    citations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    cards: List[Dict[str, Any]] = field(default_factory=list)
    panels: List[Dict[str, Any]] = field(default_factory=list)       # shown verbatim: SOP steps, legal references


@dataclass
class Tool:
    name: str
    description: str
    model: Type[In]
    roles: Tuple[str, ...]
    run: Callable[[Ctx, Any], Dict[str, Any]]
    action: bool = False

    def definition(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": schema(self.model)}


TOOLS: Dict[str, Tool] = {}


def tool(name: str, roles: Tuple[str, ...], description: str, model: Type[In], action: bool = False):
    def wrap(fn):
        TOOLS[name] = Tool(name, " ".join(description.split()), model, roles, fn, action)
        return fn
    return wrap


def schema(model: Type[BaseModel]) -> Dict[str, Any]:
    """The JSON schema of an input model, simplified for the tool definition (no titles; Optional[X] -> X)."""
    def clean(s: Any) -> Any:
        if isinstance(s, dict):
            s = {k: clean(v) for k, v in s.items() if k != "title"}
            alts = s.get("anyOf")
            if alts and any(a.get("type") == "null" for a in alts):
                rest = [a for a in alts if a.get("type") != "null"]
                if len(rest) == 1:
                    s = {**{k: v for k, v in s.items() if k != "anyOf"}, **rest[0]}
                    if s.get("default", "x") is None:
                        s.pop("default")
            return s
        if isinstance(s, list):
            return [clean(v) for v in s]
        return s
    return clean(model.model_json_schema())


def offered(t: Tool, role: str) -> bool:
    """The role may use the tool, and it is not an action tool while the copilot is read-only."""
    return role in t.roles and (config.ACTIONS_ENABLED or not t.action)


def definitions(role: str) -> List[Dict[str, Any]]:
    """The tools offered to a role, in a fixed order (the tool list is part of the cached prompt prefix)."""
    return [t.definition() for t in TOOLS.values() if offered(t, role)]


@dataclass
class Result:
    payload: Dict[str, Any]
    is_error: bool = False

    @property
    def text(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"), default=str)

    def summary(self) -> str:
        p = self.payload
        if self.is_error:
            return "error: " + str(p.get("error", ""))[:120]
        for k in ("total", "count"):
            if isinstance(p.get(k), int):
                return f"{p[k]} found"
        if p.get("card"):
            return "card prepared - waiting for your confirmation"
        if p.get("needs_reveal"):
            return "needs a reveal with a reason"
        return "done"


def execute(ctx: Ctx, name: str, raw: Any) -> Result:
    t = TOOLS.get(name)
    if t is None or ctx.user.role not in t.roles:
        return Result({"error": f"the tool {name} is not available to the {ctx.user.role} role"}, True)
    if t.action and not config.ACTIONS_ENABLED:                    # read-only: refused even if the model calls it
        return Result({"error": config.READ_ONLY}, True)
    try:
        args = t.model.model_validate(raw if isinstance(raw, dict) else {})
    except ValidationError as e:
        return Result({"error": "invalid input", "INVALID_JSON": json.dumps(raw, ensure_ascii=False, default=str),
                       "problems": [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]}, True)
    try:
        return Result(t.run(ctx, args))
    except Forbidden as e:
        return Result({"error": f"not allowed for this role: {e}"}, True)
    except (KeyError, IndexError) as e:                            # a bug, not a missing record
        traceback.print_exc()
        return Result({"error": f"internal error ({type(e).__name__})"}, True)
    except LookupError as e:
        return Result({"error": f"not found: {str(e).strip(chr(39))}"}, True)
    except ValueError as e:
        return Result({"error": f"invalid request: {e}"}, True)
    except Exception as e:                                         # noqa: BLE001 - keep the conversation alive
        traceback.print_exc()
        return Result({"error": f"internal error ({type(e).__name__})"}, True)


# ---------------------------------------------------------------------------- citations
def _sim(provenance: str, label: str) -> Any:
    return label if provenance != "camera" else False


def cite_sighting(ctx: Ctx, s: SightingOut, purpose: str = "Alert follow-up") -> str:
    ref = f"S:{s.id}"
    ctx.citations[ref] = {"ref": ref, "kind": "sighting", "id": s.id, "plate": s.plate, "masked": s.masked,
                          "time": s.seen_at, "label": s.camera_name, "simulated": s.provenance != "camera",
                          "provenance_label": s.provenance_label, "crop_url": s.crop_url,
                          "video": s.video.model_dump() if s.video else None, "purpose": purpose}
    return ref


def _sighting(ctx: Ctx, s: SightingOut, purpose: str) -> Dict[str, Any]:
    d = {"ref": cite_sighting(ctx, s, purpose), "plate": s.plate,
         "reading": "provisional" if s.status == "TENTATIVE" else "confirmed", "vehicle_type": s.vtype,
         "camera": s.camera_name, "seen_at": s.seen_at, "until": s.last_seen_at,
         "simulated": _sim(s.provenance, s.provenance_label)}
    if s.distance:
        d["match_distance"] = s.distance
    if s.alerts:
        d["alerts"] = [{"ref": cite_alert_brief(ctx, a.id), "code": a.code, "title": a.title, "status": a.status}
                       for a in s.alerts]
    return d


def cite_alert_brief(ctx: Ctx, alert_id: int) -> str:
    """Register an alert chip (details are filled from the alert row, masked per role)."""
    ref = f"A:{alert_id}"
    if ref not in ctx.citations:
        a = db.row(ctx.conn, "SELECT * FROM alerts WHERE id = ?", (alert_id,))
        if a:
            full = can_see_full(ctx.conn, ctx.user, a["sighting_id"], a["compact"]) if a["compact"] else True
            from ..privacy import mask_plate
            ctx.citations[ref] = {"ref": ref, "kind": "alert", "id": alert_id, "time": a["seen_at"],
                                  "plate": (a["plate_text"] or "") if full else mask_plate(a["plate_text"]),
                                  "masked": not full, "label": ALERT_TITLE.get(a["code"], a["code"]),
                                  "simulated": a["provenance"] != "camera",
                                  "link": f"/alerts#{alert_id}" if ctx.user.role in ALERT_ROLES else None}
    return ref


def _alert(ctx: Ctx, a) -> Dict[str, Any]:
    ref = f"A:{a.id}"
    ctx.citations[ref] = {"ref": ref, "kind": "alert", "id": a.id, "plate": a.plate, "masked": a.masked,
                          "time": a.seen_at, "label": a.title, "simulated": a.provenance != "camera",
                          "provenance_label": a.provenance_label, "crop_url": a.crop_url,
                          "video": a.video.model_dump() if a.video else None,
                          "link": f"/alerts#{a.id}" if ctx.user.role in ALERT_ROLES else None}
    d = {"ref": ref, "code": a.code, "title": a.title, "severity": a.severity, "status": a.status, "plate": a.plate,
         "seen_at": a.seen_at, "camera": a.camera_name, "message": a.message,
         "simulated": _sim(a.provenance, a.provenance_label)}
    if a.lane:
        d["lane"] = a.lane
    if a.sighting_id:
        d["record"] = f"S:{a.sighting_id}"
    if a.lane_event_id:
        d["lane_event"] = f"E:{a.lane_event_id}"
    return d


def _event(ctx: Ctx, e) -> Dict[str, Any]:
    ref = f"E:{e.id}"
    ctx.citations[ref] = {"ref": ref, "kind": "event", "id": e.id, "plate": e.plate, "masked": e.plate.endswith("****"),
                          "time": e.seen_at, "label": e.title + (f" · {e.lane}" if e.lane else ""),
                          "simulated": e.provenance != "camera", "provenance_label": e.provenance_label,
                          "video": e.video.model_dump() if e.video else None,
                          "link": f"/alerts#{e.alert_id}" if e.alert_id and ctx.user.role in ALERT_ROLES else None}
    d = {"ref": ref, "code": e.code, "title": e.title, "severity": e.severity, "seen_at": e.seen_at,
         "video_second": e.time_sec, "lane": e.lane, "message": e.message, "vehicle_type": e.vtype,
         "simulated": _sim(e.provenance, e.provenance_label)}
    if e.plate:
        d["plate"] = e.plate
    if e.from_lane:
        d["from_lane"] = e.from_lane
    if e.value is not None:
        d["value"] = e.value
    if e.error_1sigma is not None:
        d["error_1sigma"] = e.error_1sigma
    if e.alert_id:
        d["alert"] = f"A:{e.alert_id}"
    return d


def _access(ctx: Ctx, e) -> Dict[str, Any]:
    ref = f"G:{e.id}"
    ctx.citations[ref] = {"ref": ref, "kind": "access", "id": e.id, "plate": e.plate, "masked": False, "time": e.at,
                          "label": f"{e.decision} · {e.identity.replace('_', ' ')}", "simulated": e.simulated,
                          "link": "/gate/log"}
    return {"ref": ref, "at": e.at, "plate": e.plate, "direction": e.direction, "decision": e.decision,
            "identity": e.identity, "unit": e.unit_no, "guard": e.guard, "note": e.note, "simulated": e.simulated}


def _inside(ctx: Ctx, r) -> Dict[str, Any]:
    ref = f"G:{r.id}"
    ctx.citations[ref] = {"ref": ref, "kind": "access", "id": r.id, "plate": r.plate, "masked": False,
                          "time": r.entered_at, "label": f"allowed · {r.identity.replace('_', ' ')}",
                          "simulated": r.simulated, "link": "/gate/log"}
    return {"ref": ref, "plate": r.plate, "identity": r.identity, "unit": r.unit_no, "entered_at": r.entered_at,
            "minutes_inside": r.minutes_inside, "hours_inside": round(r.minutes_inside / 60.0, 1),
            "pass_valid_to": r.pass_valid_to, "pass_expired": r.pass_expired, "overstay": r.overstay,
            "simulated": r.simulated, "note": r.note}


def _pass(ctx: Ctx, p) -> Dict[str, Any]:
    ref = f"P:{p.id}"
    ctx.citations[ref] = {"ref": ref, "kind": "pass", "id": p.id, "plate": p.plate, "masked": False,
                          "time": p.valid_to, "label": f"pass · unit {p.unit_no}", "simulated": p.simulated,
                          "link": "/gate/visitors"}
    return {"ref": ref, "plate": p.plate, "unit": p.unit_no, "label": p.visitor_label, "valid_from": p.valid_from,
            "valid_to": p.valid_to, "status": p.status, "valid_now": p.valid_now, "simulated": p.simulated}


def _more(total: int, shown: int) -> Dict[str, Any]:
    return {"not_shown": total - shown} if total > shown else {}


# ---------------------------------------------------------------------------- plates
class SearchIn(In):
    purpose: Purpose = Field(description="Why the user searches (recorded in the audit log)")
    plate: str = Field("", description="Plate or part of a plate, e.g. 'XSD 4004'. Empty = any plate. * and ? "
                                       "wildcards work for police and guards only")
    tolerance: float = Field(1.0, ge=0, le=2, description="OCR tolerance: weighted edit distance, look-alike "
                                                          "characters (O/0, B/8 ...) cost 0.5. Masked roles: exact only")
    start: str = Field("", description="From, ISO date or date-time (Malaysia time)")
    end: str = Field("", description="To, ISO date or date-time")
    camera_id: Optional[int] = None
    vehicle_type: Optional[Literal["Car", "Motorcycle", "Truck", "Bus"]] = None
    reading: Optional[Literal["CONFIRMED", "TENTATIVE"]] = Field(None, description="TENTATIVE = provisional reading")
    provenance: Optional[Literal["camera", "simulated"]] = None
    with_alerts: Optional[bool] = None
    reference: str = Field("", description="Case reference, if the user gave one")
    limit: int = Field(15, ge=1, le=50)


@tool("search_plates", ALL, """Search plate sightings (vehicles read by the cameras this user may see), newest first or
best match first, with OCR tolerance for look-alike characters. Use it whenever the user asks where or when a plate
was seen, which vehicles passed a camera, which records are provisional or simulated. For roles with masked plates a
search by plate number needs a reveal with a reason: the result then says needs_reveal.""", SearchIn)
def search_plates(ctx: Ctx, a: SearchIn) -> Dict[str, Any]:
    q = SightingQuery(plate=a.plate, tolerance=0.0 if ctx.user.masked else a.tolerance, start=a.start or None,
                      end=a.end or None, camera_id=a.camera_id, vtype=a.vehicle_type, status=a.reading,
                      provenance=a.provenance, has_alerts=a.with_alerts, purpose=a.purpose, reference=a.reference,
                      page_size=a.limit)
    try:
        page = plate_svc.search(ctx.conn, ctx.user, q)
    except Forbidden as e:
        if ctx.user.masked and fuzzy.compact(a.plate):
            return {"needs_reveal": True, "plate": fuzzy.compact(a.plate),
                    "message": f"{e}. This role sees masked plates and the copilot is read-only: tell the user to "
                               f"use 'Show full plate' on the Plate search page (searching there by the plate number "
                               f"asks for a reason and is logged). Do not search another way."}
        raise
    return {"total": page.total, "shown": len(page.items), "plates_masked": ctx.user.masked,
            "records": [_sighting(ctx, s, a.purpose) for s in page.items], **_more(page.total, len(page.items))}


class RecordIn(In):
    sighting_id: int = Field(description="The number of a sighting ref, e.g. 12 for S:12")
    purpose: Purpose = "Alert follow-up"


@tool("get_record", ALL, """One sighting in detail: reading status and confidence, camera, time, alerts, how often the
plate was seen elsewhere, and for police the SIMULATED registry record. Use it when the user asks about one specific
record.""", RecordIn)
def get_record(ctx: Ctx, a: RecordIn) -> Dict[str, Any]:
    s = plate_svc.get(ctx.conn, ctx.user, a.sighting_id, a.purpose)
    d = _sighting(ctx, s, a.purpose)
    d.update(confidence=s.confidence, readings=s.votes, valid_format=s.valid_format, region=s.region, run=s.run_label,
             other_sightings_of_plate=s.other_sightings, can_reveal=s.can_reveal)
    if s.registry:
        d["registry"] = {**s.registry.model_dump(), "source": s.registry_name}
    elif ctx.user.role == "police":
        d["registry"] = {"record": None, "note": s.registry_note}
    if s.video:
        d["video_seek_sec"] = s.video.seek_sec
    return d


class TimelineIn(In):
    plate: str


@tool("plate_timeline", ("police",), """Cross-camera timeline of one plate: every sighting in time order (also the
SIMULATED registry's external sightings), the distance, time and speed between consecutive sightings, and whether
the travel was impossible (a cloned-plate check). Use it to trace a vehicle or check a cloned-plate alert.""", TimelineIn)
def plate_timeline(ctx: Ctx, a: TimelineIn) -> Dict[str, Any]:
    t = plate_svc.timeline_of(ctx.conn, ctx.user, a.plate)
    pts = []
    for p in t.points:
        d = {"seen_at": p.seen_at, "place": p.place, "source": p.source,
             "simulated": _sim(p.provenance, p.provenance_label or "SIMULATED registry sighting")}
        if p.sighting_id:                             # police only: full plates
            d["ref"] = f"S:{p.sighting_id}"
            ctx.citations[d["ref"]] = {"ref": d["ref"], "kind": "sighting", "id": p.sighting_id, "plate": t.plate,
                                       "masked": False, "time": p.seen_at, "label": p.place,
                                       "simulated": p.provenance != "camera", "provenance_label": p.provenance_label,
                                       "crop_url": f"/media/sightings/{p.sighting_id}/crop?v=full",
                                       "video": p.video.model_dump() if p.video else None,
                                       "purpose": "Investigation (case reference)"}
        pts.append(d)
    legs = [{"from": i + 1, "to": i + 2, "km": l.km, "minutes": round(l.seconds / 60.0, 1), "speed_kmh": l.speed_kmh,
             "impossible": l.impossible, "message": l.message} for i, l in enumerate(t.legs)]
    return {"plate": t.plate, "count": len(pts), "points": pts, "legs": legs, "impossible_travel": t.impossible,
            "rule": t.rule, "max_speed_kmh": float(db.get_setting(ctx.conn, "max_speed_kmh")),
            "simulated": t.simulated, "look_alike_plates": t.near_plates}


# ---------------------------------------------------------------------------- alerts
class AlertsIn(In):
    status: Literal["open", "new", "acknowledged", "resolved", "false_alarm", "all"] = "open"
    code: Optional[AlertCode] = None
    severity: Optional[Literal["high", "violation", "hazard", "medium"]] = None
    include_simulated: bool = True
    plate: str = ""
    limit: int = Field(15, ge=1, le=50)


@tool("list_alerts", ALERT_ROLES, """Alerts the user may see (police: road and lane cameras; guard: their gate; admin:
read only), sorted by severity then newest, with the counts by status. Use it for questions about open alerts,
alerts of a type (BLACKLIST, DUPLICATE, SPEEDING ...) or of a plate.""", AlertsIn)
def list_alerts(ctx: Ctx, a: AlertsIn) -> Dict[str, Any]:
    page = alert_svc.list_alerts(ctx.conn, ctx.user, None if a.status == "all" else a.status, a.severity, a.code,
                                 None, a.include_simulated, a.plate, 1, a.limit)
    c = alert_svc.counts(ctx.conn, ctx.user)
    return {"total": page.total, "shown": len(page.items), "alerts": [_alert(ctx, x) for x in page.items],
            "counts": {"open": c.total_open, "open_simulated": c.simulated, "by_status": c.by_status,
                       "open_by_severity": c.by_rank}, **_more(page.total, len(page.items))}


class AlertIn(In):
    alert_id: int = Field(description="The number of an alert ref, e.g. 5 for A:5")


@tool("get_alert", ALERT_ROLES, """One alert in detail: message, status and history, the registry-vs-observed
comparison (police), the measured value with its error for lane events, and the status changes this user may make.""",
      AlertIn)
def get_alert(ctx: Ctx, a: AlertIn) -> Dict[str, Any]:
    d = alert_svc.get_alert(ctx.conn, ctx.user, a.alert_id)
    out = _alert(ctx, d)
    out.update(history=[{"at": h.at, "user": h.user, "action": h.action, "to": h.to_status, "note": h.note}
                        for h in d.history],
               allowed_status_changes=d.allowed_transitions, measured=d.speed_text, note=d.note)
    if d.comparison:
        out["registry_vs_observed"] = [c.model_dump() for c in d.comparison]
    if d.registry:
        out["registry"] = d.registry.model_dump()
    return out


class ExplainIn(In):
    alert_id: int


@tool("explain_alert", ALERT_ROLES, """How an alert was computed from its stored values (the rule, the values, the
derived numbers, the caveats), the next steps from the system's reviewed SOP file for this user's role, and the
Road Transport Act 1987 sections it relates to (verified list). Call it whenever the user asks why an alert fired,
what it means or what to do about it; the panel also shows the SOP steps and the legal references verbatim.""",
      ExplainIn)
def explain_alert(ctx: Ctx, a: ExplainIn) -> Dict[str, Any]:
    f = explain.facts(ctx.conn, ctx.user, a.alert_id)
    _alert(ctx, alert_svc.get_alert(ctx.conn, ctx.user, a.alert_id))
    ctx.panels.append({"type": "sop", "alert": f["alert"]["ref"], "title": f["alert"]["title"], "sop": f["sop"],
                       "legal": f["legal"]})
    out = explain.for_model(f)
    s = f["sop"]
    out["sop"] = {"steps_en": s["en"], "steps_zh": s["zh"], "status": s["status"], "source": s["source"],
                  "note": "Quote these steps; do not add any. The SOP file has English and Chinese; for Malay, give the "
                          "English steps." + (" The SOP is a DRAFT, not yet reviewed: say so." if s["status"] != "reviewed" else "")}
    L = f["legal"]
    out["legal"] = ({"act": "Road Transport Act 1987 (Act 333)", "checked": L["checked"],
                     "sections": [{"section": x["section"], "title": x["title"], "summary": x["text"]} for x in L["sections"]],
                     "note": L.get("note", ""), "not_a_finding": "The alert relates to these sections; it is not a "
                                                                "finding that an offence happened."}
                    if L else {"sections": [], "note": "No section of the Act is cited for this kind of alert."})
    return out


# ---------------------------------------------------------------------------- gate (guards)
class PlateIn(In):
    plate: str


@tool("gate_identify", ("guard",), """Identity of a plate at this guard's gate right now: banned (red), resident
(green), visitor with a pass valid now (teal) or unregistered visitor (yellow), with the unit and the pass.""", PlateIn)
def gate_identify(ctx: Ctx, a: PlateIn) -> Dict[str, Any]:
    i = gate_svc.identify(ctx.conn, ctx.user, a.plate)
    return {**i.model_dump(), "simulated_list": i.simulated_list}


class EmptyIn(In):
    pass


@tool("gate_inside", ("guard",), """Visitors inside the site now (their last access event is an allowed entry), with
minutes inside, the end of their pass, and overstays first (inside longer than the overstay limit, or past their
pass).""", EmptyIn)
def gate_inside(ctx: Ctx, a: EmptyIn) -> Dict[str, Any]:
    rows = gate_svc.inside(ctx.conn, ctx.user)
    hours = float(db.get_setting(ctx.conn, "overstay_hours"))
    return {"count": len(rows), "overstays": sum(r.overstay for r in rows), "overstay_limit_hours": hours,
            "visitors": [_inside(ctx, r) for r in rows]}


class AccessIn(In):
    plate: str = ""
    start: str = ""
    end: str = ""
    decision: Optional[Literal["allowed", "stopped", "exited"]] = None
    identity: Optional[Literal["resident", "visitor_pre", "visitor_unregistered", "banned"]] = None
    limit: int = Field(20, ge=1, le=100)


@tool("gate_access_log", ("guard",), """The site's access log (entries, stops, exits), newest first; the plate search
is fuzzy. Use it for who came in or was stopped, when a plate entered or left, and counts per decision.""", AccessIn)
def gate_access_log(ctx: Ctx, a: AccessIn) -> Dict[str, Any]:
    evs = gate_svc.access_log(ctx.conn, ctx.user, a.plate, a.start, a.end, a.decision or "", a.identity or "", 500)
    by: Dict[str, int] = {}
    for e in evs:
        by[e.decision] = by.get(e.decision, 0) + 1
    return {"total": len(evs), "by_decision": by, "events": [_access(ctx, e) for e in evs[:a.limit]],
            **_more(len(evs), min(len(evs), a.limit))}


class PassesIn(In):
    valid_now_only: bool = False


@tool("gate_passes", ("guard",), """The site's visitor passes (plate, unit, validity window, whether valid now).""",
      PassesIn)
def gate_passes(ctx: Ctx, a: PassesIn) -> Dict[str, Any]:
    ps = [p for p in gate_svc.passes(ctx.conn, ctx.user) if p.valid_now or not a.valid_now_only]
    return {"total": len(ps), "valid_now": sum(p.valid_now for p in ps), "passes": [_pass(ctx, p) for p in ps[:30]]}


# ---------------------------------------------------------------------------- traffic
@tool("traffic_runs", ROAD, """The analysed recordings with traffic counts (run id, camera, date and window). Call it
first to find the run_id for traffic_summary, traffic_flow and lane_events.""", EmptyIn)
def traffic_runs(ctx: Ctx, a: EmptyIn) -> Dict[str, Any]:
    rs = traffic_svc.runs(ctx.conn, ctx.user)
    return {"count": len(rs), "runs": [{"run_id": r.id, "label": r.label, "camera": r.camera_name,
                                        "camera_kind": r.camera_kind, "starts": r.clock_origin,
                                        "seconds": round((r.window_end_sec or 0) - (r.window_start_sec or 0), 1),
                                        "vehicles": r.flow_total, "note": r.provenance_note} for r in rs]}


class RunIn(In):
    run_id: int


@tool("traffic_summary", ROAD, """Traffic figures of one run: vehicles and vehicles per minute, by type with shares,
per lane (count, flow, occupancy, density, speed with its error), per direction, vehicles outside the lanes, and the
COUNT of withheld violations of real vehicles (never their details).""", RunIn)
def traffic_summary(ctx: Ctx, a: RunIn) -> Dict[str, Any]:
    s = traffic_svc.summary(ctx.conn, ctx.user, a.run_id)
    return {"run": s.run.label, "camera": s.run.camera_name, "starts": s.run.clock_origin, "vehicles": s.total,
            "seconds": s.duration_sec, "minutes": round(s.duration_sec / 60.0, 1), "veh_per_min": s.veh_per_min,
            "by_type": s.by_type, "by_type_share_pct": {k: derived.share(v, s.total) for k, v in s.by_type.items()},
            "lanes": [{"lane": l.name, "direction": l.group, "vehicles": l.count, "veh_per_min": l.veh_per_min,
                       "occupancy_pct": derived.pct(l.occupancy), "density": l.density, "density_unit": l.density_unit,
                       "speed_kmh": l.mean_speed_kmh, "speed_error_kmh_1sigma": l.speed_error_kmh} for l in s.lanes],
            "lane_count": len(s.lanes),
            "directions": {g: {"vehicles": v["count"], "mean_occupancy_pct": derived.pct(v["occupancy"]),
                               "speed_kmh": v["speed_kmh"]} for g, v in s.groups.items()},
            "outside_lanes": s.outside_lanes, "withheld_real_violations": s.withheld,
            "withheld_total": sum(s.withheld.values()), "withheld_note": s.withheld_note, "speed_note": s.speed_note}


class FlowIn(In):
    run_id: int
    by: Literal["type", "lane", "time"] = "type"
    bin_sec: float = Field(10.0, ge=1, le=600)


@tool("traffic_flow", ROAD, """Vehicles per time bin of one run, split by vehicle type or lane (or the total): totals
per series, the busiest bin, and the bins themselves when there are few.""", FlowIn)
def traffic_flow(ctx: Ctx, a: FlowIn) -> Dict[str, Any]:
    s = traffic_svc.flow_series(ctx.conn, ctx.user, a.run_id, a.by, a.bin_sec)
    totals = {k: int(sum(v or 0 for v in vs)) for k, vs in s.series.items()}
    per_bin = [int(sum((vs[i] or 0) for vs in s.series.values())) for i in range(len(s.x))]
    peak = max(range(len(per_bin)), key=lambda i: per_bin[i]) if per_bin else None
    out = {"run_id": a.run_id, "split_by": a.by, "bin_sec": a.bin_sec, "bins": len(s.x), "totals": totals,
           "vehicles": sum(totals.values()),
           "busiest_bin": {"starts_at_sec": s.x[peak], "vehicles": per_bin[peak]} if peak is not None else None}
    if len(s.x) <= 24:
        out["series"] = {"bin_start_sec": s.x, **s.series}
    return out


@tool("lane_events", ROAD, """Lane events of a lane-camera run in time order: lane changes, hazards (stopped in a lane)
and violations. Every violation shown is a SIMULATED vehicle with a fictitious plate; violations of real vehicles are
withheld and only counted (traffic_summary).""", RunIn)
def lane_events(ctx: Ctx, a: RunIn) -> Dict[str, Any]:
    evs = traffic_svc.events(ctx.conn, ctx.user, a.run_id)
    by: Dict[str, int] = {}
    for e in evs:
        by[e.code] = by.get(e.code, 0) + 1
    w = {r["code"]: r["count"] for r in db.rows(ctx.conn, "SELECT code, count FROM withheld WHERE run_id = ?", (a.run_id,))}
    return {"total": len(evs), "by_code": by, "events": [_event(ctx, e) for e in evs],
            "withheld_real_violations": w, "withheld_total": sum(w.values()),
            "withheld_note": "Violations of real vehicles are withheld: counted only, never shown or tied to a plate."}


# ---------------------------------------------------------------------------- forecast
@tool("forecast_days", ROAD, """The congestion recordings of the JB CIQ approach (one per camera-day): date, sections,
whether calibrated, the forecasting model, the first warning, the congestion onsets and the minutes between the first
warning and the first onset, and the count error of the measurement.""", EmptyIn)
def forecast_days(ctx: Ctx, a: EmptyIn) -> Dict[str, Any]:
    days = derived.lead_times(ctx.conn, ctx.user)
    return {"count": len(days), "days": days}


class StateIn(In):
    day: str = Field(description="Day code from forecast_days, e.g. jb_ciq_cam02_2023-03-22")
    at: str = Field("", description="HH:MM clock minute; empty = the day's first warning")


@tool("forecast_state", ROAD, """The state of every section of a congestion day at one minute: fill now and the
5-minute mean (% of the jam capacity), vehicles in view, free / dense / queue, the forecasts made at that minute for
+5, +15 and +30 min with their error band, and the dashboard's status line (e.g. "Congestion in ~15 min").""", StateIn)
def forecast_state(ctx: Ctx, a: StateIn) -> Dict[str, Any]:
    st = forecast_svc.state(ctx.conn, ctx.user, a.day, a.at or None)
    return {"day": st.day, "at": st.at, "threshold_fill_pct": derived.pct(st.threshold), "sections": [
        {"section": s.section, "label": s.label, "fill_now_pct": derived.pct(s.now), "mean5_pct": derived.pct(s.mean5),
         "vehicles": s.vehicles, "jam_capacity_veh": s.capacity, "state": s.state, "status": s.status,
         "warning": s.warning, "forecast_pct": {f"+{h} min": derived.pct(v) for h, v in s.forecast.items()},
         "error_band_pct_points": {f"+{h} min": derived.pct(v) for h, v in s.band.items()}, "model": s.model}
        for s in st.sections]}


class AccuracyIn(In):
    day: str
    section: str = "To CIQ"


@tool("forecast_accuracy", ROAD, """How good the congestion forecast was on a day: for +5, +15 and +30 min, the mean
absolute error and RMSE (percentage points of fill), precision and recall of "congested", and for each congestion
onset the warning lead time (minutes of warning before it).""", AccuracyIn)
def forecast_accuracy(ctx: Ctx, a: AccuracyIn) -> Dict[str, Any]:
    return derived.accuracy(ctx.conn, ctx.user, a.day, a.section)


class DayIn(In):
    day: str


@tool("congestion_episodes", ROAD, """Congested episodes of a congestion day for every zone and the whole approach:
start, end, minutes, total congested minutes, peak fill and when, and the longest-queueing zone.""", DayIn)
def congestion_episodes(ctx: Ctx, a: DayIn) -> Dict[str, Any]:
    return derived.episodes(ctx.conn, ctx.user, a.day)


class WhatIfIn(In):
    day: str
    capacity_pct: float = Field(20.0, ge=0, le=100, description="Extra discharge capacity, % (e.g. opening lanes)")
    section: str = "To CIQ"
    start: str = Field("", description="HH:MM when the extra capacity starts; empty = the first warning")
    mu: Optional[float] = Field(None, gt=0, description="Assumed discharge capacity, vehicles per minute; empty = "
                                                        "estimated from the fastest clearing")


@tool("what_if", ROAD, """What-if "open more lanes": the projected queue if the section's discharge capacity were raised
by capacity_pct % from a start minute: the minutes the queue is at or above the congestion threshold and the peak
queue, baseline vs projected, the minutes saved, and the model's assumptions. An estimate under stated assumptions,
not a prediction.""", WhatIfIn)
def what_if(ctx: Ctx, a: WhatIfIn) -> Dict[str, Any]:
    return derived.whatif(ctx.conn, ctx.user, a.day, a.section, a.capacity_pct, a.mu, a.start or None)


# ---------------------------------------------------------------------------- overview, reports, evidence, rules
class OverviewIn(In):
    date: str = Field("", description="YYYY-MM-DD; empty = the latest day with camera data")


@tool("overview", ROAD, """The day at a glance: vehicles counted by type (with shares) and by camera, lane-camera
figures, the other days with data, open alerts by severity, and the congestion status.""", OverviewIn)
def overview(ctx: Ctx, a: OverviewIn) -> Dict[str, Any]:
    f = derived.flows(ctx.conn, ctx.user, a.date or None)
    ov = overview_svc.summary(ctx.conn, ctx.user, a.date or None)
    f["open_alerts"] = {"total": ov.alerts.total_open, "by_severity": ov.alerts.by_rank, "simulated": ov.alerts.simulated}
    f["cameras"] = [{"id": c.id, "name": c.name, "kind": c.kind, "status": c.status} for c in ov.cameras]
    if ov.congestion:
        f["congestion"] = {"day": ov.congestion.day, "at": ov.congestion.at,
                           "sections": [{"section": s.section, "status": s.status, "fill_pct": derived.pct(s.now)}
                                        for s in ov.congestion.sections]}
    return f


class ReportIn(In):
    kind: Literal["traffic", "alerts", "gate"]
    date: str = Field(description="YYYY-MM-DD")
    weekly: bool = False


@tool("report_data", ALL, """The tables of a daily or weekly report (traffic: police, traffic, admin; alerts: police,
admin; gate: guard), as the Reports page shows them.""", ReportIn)
def report_data(ctx: Ctx, a: ReportIn) -> Dict[str, Any]:
    rep = report_svc.build(ctx.conn, ctx.user, a.kind, a.date, a.weekly)
    return {"title": rep["title"], "period": rep["period"], "sections": [
        {"title": s["title"], "columns": s["columns"], "rows": s["rows"][:30], "rows_total": len(s["rows"]),
         "note": s.get("note", "")} for s in rep["sections"]]}


@tool("evidence_pack_link", ("police",), """Links to the evidence pack (HTML or PDF) of an alert, and whether an AI case
summary has been added to it. The pack is exported (and logged) only when the user opens a link.""", AlertIn)
def evidence_pack_link(ctx: Ctx, a: AlertIn) -> Dict[str, Any]:
    d = alert_svc.get_alert(ctx.conn, ctx.user, a.alert_id)
    _alert(ctx, d)
    has = db.scalar(ctx.conn, "SELECT COUNT(*) FROM copilot_documents WHERE kind = 'case_summary' AND alert_id = ?",
                    (a.alert_id,))
    return {"alert": f"A:{a.alert_id}", "html": f"/api/v1/alerts/{a.alert_id}/evidence?format=html",
            "pdf": f"/api/v1/alerts/{a.alert_id}/evidence?format=pdf", "ai_case_summary_added": bool(has),
            "note": "Course project demo, not an official document. Opening a link exports the pack and records it."}


@tool("privacy_rules", ALL, """The system's privacy rules and settings: who sees masked plates, how long a reveal lasts,
the search purposes and reveal reasons, retention periods, and the PDPA wording.""", EmptyIn)
def privacy_rules(ctx: Ctx, a: EmptyIn) -> Dict[str, Any]:
    g = lambda k: db.get_setting(ctx.conn, k)                                   # noqa: E731
    return {"your_plates": "masked" if ctx.user.masked else "full",
            "masked_roles": ["traffic manager", "administrator"],
            "masking": "the letter prefix stays, the rest becomes **** (e.g. VJF ****); crops are blurred on the server",
            "reveal": f"'Show full plate' needs a reason, is recorded in the audit log and lasts {g('reveal_minutes')} "
                      f"minutes for that record or plate", "reveal_minutes": g("reveal_minutes"),
            "reveal_reasons": REVEAL_REASONS, "search_purposes": PURPOSES,
            "retention_days": {"ordinary sightings": g("retention_days"), "sightings with alerts after closing":
                               g("retention_alert_days"), "gate access log": g("retention_access_days"),
                               "audit log": g("retention_audit_days"), "assistant conversations": g("copilot_retention_days")},
            "pdpa": "The controls follow the data-minimisation principle of Malaysia's PDPA 2010. The PDPA binds private "
                    "bodies (e.g. a property manager running a guardhouse); it does not apply to the Federal and State "
                    "Governments.", "audit": "every search, view, reveal, export and assistant action is logged"}


@tool("admin_status", ("admin",), """For administrators: the imported data (runs, sightings, alerts, congestion days),
the audit log's hash-chain check and the assistant's usage and cost over the last 30 days.""", EmptyIn)
def admin_status(ctx: Ctx, a: EmptyIn) -> Dict[str, Any]:
    from . import store
    d = admin_svc.data_status(ctx.conn, ctx.user)
    v = admin_svc.verify(ctx.conn, ctx.user)
    u = store.usage_summary(ctx.conn)
    return {"runs": [{"label": r["label"], "camera": r["camera"], "sightings": r["sightings"], "alerts": r["alerts"]}
                     for r in d["runs"]], "congestion_days": len(d["series_days"]), "last_import": d["last_import"],
            "audit_chain_ok": v["ok"], "audit_rows_checked": v["rows_checked"], "audit_first_bad_id": v["first_bad_id"],
            "assistant_cost_usd_30_days": u["total_usd"], "assistant_questions_30_days": u["questions"],
            "assistant_cost_per_question_usd": u["cost_per_question"]}


# ---------------------------------------------------------------------------- prepared actions (cards; off while read-only)
def _card(ctx: Ctx, kind: str, params: Dict[str, Any]) -> Dict[str, Any]:
    c = actions.prepare(ctx.conn, ctx.user, kind, params, ctx.conversation_id)
    ctx.cards.append(c)
    return {"card": c["id"], "status": "prepared, NOT done: waiting for the user to check and press Confirm",
            "shows": c["lines"], "fields_for_the_user": [f["name"] for f in c["fields"] if f.get("required") and not f.get("value")]}


class PassIn(In):
    plate: str
    unit_no: str = Field("", description="Unit number, e.g. B-05-1")
    visitor_label: str = Field("", description="e.g. Guest, Delivery, Contractor (no names or IC numbers)")
    valid_from: str = Field("", description="ISO date-time; empty = now")
    valid_to: str = Field("", description="ISO date-time; empty = from + hours")
    hours: Optional[float] = Field(None, gt=0, le=72)


@tool("prepare_visitor_pass", ("guard",), """Prepare a visitor pass for the user to confirm (nothing is written until they
press Confirm): plate, unit, label, validity window. Convert times the user gave ("until 6 pm") to ISO using the
current time from the session context.""", PassIn, action=True)
def prepare_visitor_pass(ctx: Ctx, a: PassIn) -> Dict[str, Any]:
    return _card(ctx, "visitor_pass", a.model_dump())


class ListIn(In):
    list: Literal["watch", "blacklist", "resident", "banned"] = Field(description="watch / blacklist: police; resident / "
                                                                                 "banned (site ban list): guards")
    plate: str
    reason: str = Field("", description="Only what the user said; leave empty otherwise (they complete it on the card)")
    label: str = ""
    unit_no: str = ""
    valid_to: str = ""


@tool("prepare_list_entry", ("police", "guard"), """Prepare adding a plate to a list for the user to confirm (nothing
is written until they press Confirm): police manage the watch list and blacklist, guards their site's residents and ban
list.""", ListIn, action=True)
def prepare_list_entry(ctx: Ctx, a: ListIn) -> Dict[str, Any]:
    return _card(ctx, "list_entry", a.model_dump())


class StatusIn(In):
    alert_id: int
    to: Literal["acknowledged", "resolved", "false_alarm"]
    note: str = Field("", description="Only the user's own words about what they checked; empty if they gave none")
    corrected_plate: str = Field("", description="False alarm only: the correct plate, if the user gave it")


@tool("prepare_alert_status_change", ("police", "guard"), """Prepare an alert status change (acknowledge, resolve,
false alarm) for the user to confirm; closing needs the user's note, which they can complete on the card.""",
      StatusIn, action=True)
def prepare_alert_status_change(ctx: Ctx, a: StatusIn) -> Dict[str, Any]:
    _alert(ctx, alert_svc.get_alert(ctx.conn, ctx.user, a.alert_id))
    return _card(ctx, "alert_status", a.model_dump())


class RevealIn(In):
    sighting_id: Optional[int] = Field(None, description="Reveal one record (S:<id>)")
    plate: str = Field("", description="Or reveal one plate number the user asked about (then searching by it works)")
    note: str = ""


@tool("prepare_reveal_request", ("traffic", "admin"), """Prepare a "Show full plate" request for a role with masked
plates: one record, or one plate number the user wants to search. The user must choose the reason on the card;
confirming records the reveal in the audit log, and it lasts a limited time. Never choose the reason yourself.""",
      RevealIn, action=True)
def prepare_reveal_request(ctx: Ctx, a: RevealIn) -> Dict[str, Any]:
    if not a.sighting_id and not fuzzy.compact(a.plate):
        raise ValueError("give the sighting id or the plate")
    return _card(ctx, "reveal", a.model_dump())


def tools_by_role() -> Dict[str, List[str]]:
    return {r: [t.name for t in TOOLS.values() if offered(t, r)] for r in ALL}
