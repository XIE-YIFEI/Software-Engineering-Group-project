"""Generated documents: the "AI explain" text of an alert, the case summary for a police evidence pack, and the
daily traffic and congestion review for traffic managers.

Each is written from FACTS that the system computes first (the stored values and the service functions' results,
derived numbers included); the model only writes them up, and offline a fixed template does. Every text is stored
(copilot_documents) with its facts, the model that wrote it and the number check (every number of the text must be
found in the facts), and is marked AI-generated (or "template") wherever it is shown.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .. import clock, db
from ..privacy import audit
from ..security import Forbidden, User
from ..services import evidence as evidence_svc
from ..services import forecast as forecast_svc
from . import config, derived, explain, numbers, prompts, store
from .llm import LLMError, LLMUnavailable

Event = Tuple[str, Any]
LANGS = ("en", "zh", "ms")
AI_LABEL = {"en": "AI-generated: check it against the records", "zh": "AI 生成：请对照记录核实",
            "ms": "Dijana oleh AI: semak dengan rekod"}
TEMPLATE_LABEL = {"en": "Offline: templates, not AI", "zh": "离线：模板生成，非 AI", "ms": "Luar talian: templat, bukan AI"}


def _display(row: Dict[str, Any]) -> Dict[str, Any]:
    offline_doc = row["model"] == "offline-template"
    lang = row["lang"]
    return {"id": row["id"], "kind": row["kind"], "alert_id": row["alert_id"], "text": row["text"], "lang": lang,
            "model": row["model"], "ai": not offline_doc, "offline": offline_doc,
            "label": (TEMPLATE_LABEL if offline_doc else AI_LABEL).get(lang, AI_LABEL["en"]),
            "check": json.loads(row["number_check"]), "created_at": row["created_at"],
            "params": json.loads(row["params"] or "{}")}


def latest(conn: sqlite3.Connection, kind: str, alert_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    r = db.row(conn, "SELECT * FROM copilot_documents WHERE kind = ? AND " + ("alert_id = ?" if alert_id else "1 = 1")
               + " ORDER BY id DESC LIMIT 1", (kind, alert_id) if alert_id else (kind,))
    return _display(r) if r else None


def generate(conn: sqlite3.Connection, user: User, kind: str, system_text: str, facts: Dict[str, Any],
             model_facts: Dict[str, Any], template: Callable[[str], str], lang: str = "en",
             alert_id: Optional[int] = None, params: Optional[Dict[str, Any]] = None, llm: Any = None,
             extra: Optional[Dict[str, Any]] = None) -> Iterator[Event]:
    """Write one document (streamed), store it and yield ("done", display)."""
    lang = lang if lang in LANGS else "en"
    cfg = config.load(conn)
    note = None
    if llm is None:
        st = config.status(conn, cfg)
        if st["online"]:
            from .agent import make_llm
            llm = make_llm(cfg)
        else:
            note = f"The AI service is not available ({st['reason']}): a fixed template was used."
    if llm is not None:
        try:
            store.check_rate(conn, user, cfg.questions_per_hour)
        except Forbidden as e:
            llm, note = None, f"{e}. A fixed template was used."
    text, model = "", "offline-template"
    if llm is not None:
        system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
        msg = {"role": "user", "content": [{"type": "text", "text": f"Write in {prompts.LANG_NAME[lang]}.\n\nFACTS:\n"
                                                                    + json.dumps(model_facts, ensure_ascii=False, indent=1,
                                                                                 default=str)}]}
        try:
            turn = None
            for k, v in llm.stream(system=system, messages=[msg], max_tokens=cfg.max_tokens):
                if k == "text":
                    text += v
                    yield "text", v
                else:
                    turn = v
            store.log_usage(conn, user, kind, turn.model, turn.usage, turn.cost_usd, turn.stop_reason, turn.request_id)
            if turn.stop_reason in ("refusal", "max_tokens"):
                note = ("The model declined; " if turn.stop_reason == "refusal" else "The text was cut off; ") + \
                       "a fixed template was used instead."
                text = ""
            else:
                model = turn.model
        except (LLMUnavailable, LLMError) as e:
            note, text = f"The AI service could not answer ({e}): a fixed template was used.", ""
    if not text:
        text = template(lang)
        yield "text", text
    check = numbers.check(text, [facts])
    via = replace(user, via="assistant")
    did = db.insert(conn, "copilot_documents", {
        "kind": kind, "alert_id": alert_id, "user_id": user.id, "lang": lang,
        "params": json.dumps(params or {}, ensure_ascii=False), "facts": json.dumps(facts, ensure_ascii=False, default=str),
        "text": text, "model": model, "number_check": json.dumps(check, ensure_ascii=False), "created_at": clock.now_iso()})
    audit(conn, via, "copilot_document", kind, did, {"alert": alert_id, "model": model, "lang": lang,
                                                     "number_check": "ok" if check["ok"] else f"flagged {len(check['unmatched'])}"})
    out = _display(db.row(conn, "SELECT * FROM copilot_documents WHERE id = ?", (did,)))
    out["note"] = note
    out.update(extra or {})
    yield "done", out


# ---------------------------------------------------------------------------- "AI explain" of an alert
def explain_alert(conn: sqlite3.Connection, user: User, alert_id: int, lang: str = "en", llm: Any = None) -> Iterator[Event]:
    f = explain.facts(conn, user, alert_id)
    lang_t = "zh" if lang == "zh" else "en"
    yield "facts", {"alert": f["alert"], "sop": f["sop"], "legal": f["legal"], "values": f["values"],
                    "derived": f["derived"], "derived_labels": f["derived_labels"], "caveats": f["caveats"],
                    "rule": f["rule"]}
    yield from generate(conn, user, "alert_explanation", prompts.EXPLAIN, explain.for_model(f), explain.for_model(f),
                        lambda lg: explain.template(f, lang_t), lang, alert_id, {"code": f["alert"]["code"]}, llm,
                        extra={"sop": f["sop"], "legal": f["legal"], "alert": f["alert"]})


# ---------------------------------------------------------------------------- case summary (evidence pack)
def case_facts(conn: sqlite3.Connection, user: User, alert_id: int) -> Dict[str, Any]:
    pack = evidence_svc.build(conn, user, alert_id)                 # police only; the same data as the pack
    a, s, run = pack["alert"], pack["sighting"], pack["run"]
    facts: Dict[str, Any] = {
        "alert": {"id": a["id"], "code": a["code"], "title": a["title"], "severity": a["severity"], "status": a["status"],
                  "message": a["message"], "plate": a["plate"], "seen_at": a["seen_at"],
                  "provenance": pack["provenance_label"]},
        "camera": {"name": run["camera_name"], "location": run["location_note"], "run": run["label"],
                   "clock_note": pack["clock_note"]},
        "simulated": a["provenance"] != "camera", "registry_source": pack["registry_name"],
        "registry_vs_observed": [{"field": c["field"], "registered": c["registered"], "observed": c["observed"],
                                  "match": c["match"]} for c in pack["comparison"]],
        "measurement": pack["speed_text"],
        "legal_sections": [f"Section {x['section']}: {x['title']}" for x in (pack["legal"] or {}).get("sections", [])],
        "legal_note": (pack["legal"] or {}).get("note", ""), "disclaimer": pack["disclaimer"]}
    if s:
        facts["sighting"] = {"ref": f"S:{s['id']}", "first_seen": s["seen_at"], "last_seen": s["last_seen_at"],
                             "vehicle_type": s["vtype"], "reading": "provisional" if s["status"] == "TENTATIVE" else "confirmed",
                             "confidence": s["confidence"], "readings": s["votes"], "video_seconds": [s["first_sec"], s["last_sec"]]}
    if pack["travel"]:
        facts["timeline"] = {"points": [{"seen_at": p["seen_at"], "place": p["place"],
                                         "source": p["provenance_label"] or p["source"]} for p in pack["travel"]["points"]],
                             "legs": [l["message"] for l in pack["travel"]["legs"]], "rule": pack["travel"]["rule"]}
    return facts


def case_template(f: Dict[str, Any], lang: str) -> str:
    zh = lang == "zh"
    a = f["alert"]
    s = f.get("sighting") or {}
    L: List[str] = ["## " + ("摘要" if zh else "Summary"),
                    (f"告警 #{a['id']}（{a['code']}，{a['title']}），车牌 {a['plate']}，{a['seen_at'][:19]}，{f['camera']['name']}。"
                     if zh else f"Alert #{a['id']} ({a['code']}, {a['title']}) for plate {a['plate']}, "
                                f"{a['seen_at'][:19]}, {f['camera']['name']}."), f"- {a['message']}",
                    "## " + ("摄像头记录了什么" if zh else "What the cameras recorded")]
    if s:
        L.append(f"- {s['first_seen'][:19]} – {s['last_seen'][:19]} · {s['vehicle_type']} · {s['reading']}")
    for p in (f.get("timeline") or {}).get("points", []):
        L.append(f"- {p['seen_at'][:19]} · {p['place']} · {p['source']}")
    L.append("## " + ("系统计算了什么" if zh else "What the system computed"))
    L += [f"- {x}" for x in (f.get("timeline") or {}).get("legs", [])]
    if f.get("measurement"):
        L.append(f"- {f['measurement']}")
    for c in f["registry_vs_observed"]:
        L.append(f"- {c['field']}: {c['registered']} / {c['observed']}")
    L.append("## " + ("模拟数据与局限" if zh else "Simulated data and limits"))
    L.append(f"- {a['provenance']}")
    L.append(f"- {f['disclaimer']}")
    L.append("## " + ("需要警员核实的要点" if zh else "Points for the officer to verify"))
    L.append("- " + ("对照截图和现场画面核对车牌读取；在权威系统中核实登记信息。" if zh else
                     "Check the plate reading against the crop and the scene frame; verify the registration in the "
                     "system of record."))
    if f["legal_sections"]:
        L.append("- " + ("该告警涉及：" if zh else "The alert relates to: ") + "; ".join(f["legal_sections"]))
    return "\n".join(L)


def case_summary(conn: sqlite3.Connection, user: User, alert_id: int, lang: str = "en", llm: Any = None) -> Iterator[Event]:
    if user.role != "police":
        raise Forbidden("only police officers add case summaries to evidence packs")
    f = case_facts(conn, user, alert_id)
    yield from generate(conn, user, "case_summary", prompts.CASE_SUMMARY, f, f,
                        lambda lg: case_template(f, "zh" if lg == "zh" else "en"), lang, alert_id, {}, llm)


# ---------------------------------------------------------------------------- daily traffic and congestion review
def review_facts(conn: sqlite3.Connection, user: User, date: Optional[str] = None,
                 congestion_day: Optional[str] = None) -> Dict[str, Any]:
    flows = derived.flows(conn, user, date or None)
    days = [d for d in forecast_svc.days(conn, user) if d.calibrated and d.first_warning]
    day = congestion_day or (days[0].code if days else None)
    facts: Dict[str, Any] = {"flows": flows}
    if day:
        ep = derived.episodes(conn, user, day)
        facts["congestion"] = {"day": day, "date": ep["date"], "camera": ep["camera"], "definition": ep["definition"],
                               "longest_queueing_zone": ep["longest_queueing_zone"],
                               "longest_queueing_zone_minutes": ep["longest_queueing_zone_minutes"],
                               "by_section": ep["entities"]}
        facts["forecast"] = derived.accuracy(conn, user, day)
        facts["what_if"] = [derived.whatif(conn, user, day, "To CIQ", p) for p in (10.0, 20.0, 30.0)]
        facts["data_note"] = (f"The flows come from the demo footage of {flows['date']} (notional clock); the congestion "
                              f"comes from the CIQ recording of {ep['date']}: two different recordings.")
    return facts


def review_template(f: Dict[str, Any], lang: str) -> str:
    zh = lang == "zh"
    fl = f["flows"]
    types = ", ".join(f"{k} {v} ({fl['by_type_share_pct'][k]} %)" for k, v in sorted(fl["by_type"].items(), key=lambda kv: -kv[1]))
    L = ["## " + ("交通流量" if zh else "Traffic flow"),
         (f"{fl['date']}：计数线共 {fl['vehicles']} 辆：{types}。" if zh else f"{fl['date']}: {fl['vehicles']} vehicles at "
                                                                              f"the counting lines: {types}.")]
    L += [f"- {cam}: {n}" for cam, n in fl["by_camera"].items()]
    for lr in fl["lane_runs"]:
        L.append(f"- {lr['camera']}: {lr['vehicles']} · {lr['veh_per_min']:g} veh/min · {lr['busiest_lane']}")
    if "congestion" in f:
        c, fc = f["congestion"], f["forecast"]
        L.append("## " + ("拥堵" if zh else "Congestion"))
        L.append(f"- {f['data_note']}")
        for name, e in c["by_section"].items():
            if e.get("episodes"):
                L.append(f"- {name}: " + "; ".join(f"{x['start']}-{x['end']} ({x['minutes']} min)" for x in e["episodes"]))
        L.append(("- 排队最久的区段：" if zh else "- Longest-queueing zone: ") +
                 f"{c['longest_queueing_zone']} ({c['longest_queueing_zone_minutes']} min)")
        L.append("## " + ("预测精度与预警" if zh else "Forecast accuracy and warning"))
        L.append(f"- {fc['model']} · first warning {fc['first_warning']} · onsets {', '.join(fc['onsets'])}")
        for h in fc["by_horizon"]:
            leads = ", ".join(f"{o}: {w} min" for o, w in h["warning_lead_min"].items())
            L.append(f"- +{h['horizon_min']} min: MAE {h['mae_pct_points']} · {leads}")
        L.append("## " + ("明天的建议" if zh else "Suggestions for tomorrow"))
        for w in f["what_if"]:
            L.append(("- 建议（模拟估算）：" if zh else "- Suggestion (estimate under stated assumptions): ") +
                     f"+{w['capacity_increase_pct']:g} % from {w['from']}: {w['minutes_over_threshold_baseline']} -> "
                     f"{w['minutes_over_threshold_projected']} min over the threshold")
        L.append(("- 假设：" if zh else "- Assumptions: ") + " ".join(f["what_if"][0]["assumptions"]))
    return "\n".join(L)


def daily_review(conn: sqlite3.Connection, user: User, date: Optional[str] = None, congestion_day: Optional[str] = None,
                 lang: str = "en", llm: Any = None) -> Iterator[Event]:
    if user.role not in ("traffic", "police", "admin"):
        raise Forbidden("the daily traffic review is for traffic managers (police and admins may read it)")
    f = review_facts(conn, user, date, congestion_day)
    yield from generate(conn, user, "daily_review", prompts.DAILY_REVIEW, f, f,
                        lambda lg: review_template(f, "zh" if lg == "zh" else "en"), lang, None,
                        {"date": f["flows"]["date"], "congestion_day": (f.get("congestion") or {}).get("day")}, llm)


def collect(events: Iterator[Event]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"text": "", "done": None, "facts": None}
    for k, v in events:
        if k == "text":
            out["text"] += v
        elif k in ("done", "facts"):
            out[k] = v
    return out
