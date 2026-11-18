"""Offline mode: quick questions answered by predefined tool queries and fixed templates - no model involved.

They work without a key or network (and when the daily budget is used up), so the demo and teammates can always run
the panel. Each answer is labelled "Offline: templates, not AI". The queries are the copilot's own tools, so the
permissions, masking, citations and audit are exactly those of the chat.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import clock, db
from ..security import User
from . import tools

LABEL = {"en": "Offline: templates, not AI", "zh": "离线：模板回答，非 AI 生成"}
Fn = Callable[["Run"], str]


class Run:
    """One quick question: runs tools like the chat does and remembers the steps."""

    def __init__(self, conn: sqlite3.Connection, user: User, lang: str):
        self.ctx = tools.Ctx(conn, replace(user, via="assistant"))
        self.lang = "zh" if lang == "zh" else "en"
        self.steps: List[Dict[str, Any]] = []

    def call(self, name: str, **inp: Any) -> Dict[str, Any]:
        r = tools.execute(self.ctx, name, inp)
        self.steps.append({"tool": name, "input": inp, "summary": r.summary(), "error": r.is_error})
        if r.is_error:
            raise ValueError(r.payload.get("error"))
        return r.payload

    def t(self, en: str, zh: str) -> str:
        return zh if self.lang == "zh" else en


def _sim(x: Dict[str, Any], r: Run) -> str:
    return r.t(" (simulated)", "（模拟）") if x.get("simulated") else ""


# ---------------------------------------------------------------------------- the quick questions
def open_alerts(r: Run) -> str:
    p = r.call("list_alerts", status="open", limit=8)
    c = p["counts"]
    sev = c["open_by_severity"]
    head = r.t(f"{c['open']} open alerts ({c['open_simulated']} of them simulated): High {sev.get('High', 0)}, "
               f"Hazard {sev.get('Hazard', 0)}, Medium {sev.get('Medium', 0)}. The most severe:",
               f"未处理告警 {c['open']} 条（其中模拟 {c['open_simulated']} 条）：高 {sev.get('High', 0)}、危险 "
               f"{sev.get('Hazard', 0)}、中 {sev.get('Medium', 0)}。最严重的几条：")
    lines = [f"- [[{a['ref']}]] {a['title']} · {a['plate'] or '-'} · {a['camera']}{_sim(a, r)}" for a in p["alerts"]]
    return "\n".join([head] + lines)


def cloned_plate(r: Run) -> str:
    alerts = r.call("list_alerts", status="all", code="DUPLICATE", limit=20)["alerts"]
    travel = next((a for a in alerts if not a["message"].startswith("same plate")), None)
    if travel is None:
        return r.t("There is no cloned-plate alert from impossible travel.", "没有因“不可能的行程”触发的套牌告警。")
    t = r.call("plate_timeline", plate=travel["plate"])
    lines = [r.t(f"Alert [[{travel['ref']}]]: {travel['plate']}{_sim(travel, r)}. Sightings in time order:",
                 f"告警 [[{travel['ref']}]]：{travel['plate']}{_sim(travel, r)}。按时间排列的目击：")]
    for p in t["points"]:
        ref = f"[[{p['ref']}]] " if p.get("ref") else ""
        lines.append(f"- {ref}{p['seen_at'][:19].replace('T', ' ')} · {p['place']}{_sim(p, r)}")
    for leg in t["legs"]:
        if leg["impossible"]:
            lines.append(r.t(f"Impossible travel: {leg['message']}", f"不可能的行程：{leg['message']}"))
    lines.append(r.t("A rule fired on measured data; a person must verify it (OCR misread, camera clock).",
                     "这是规则在测量数据上触发的结果；必须人工核实（OCR 读错、摄像头时钟）。"))
    return "\n".join(lines)


def provisional(r: Run) -> str:
    p = r.call("search_plates", purpose="Data-quality check", reading="TENTATIVE", limit=10)
    if not p["total"]:
        return r.t("No provisional readings.", "没有临时读取结果。")
    lines = [r.t(f"{p['total']} provisional readings (not confirmed; they never raise alerts):",
                 f"临时读取结果 {p['total']} 条（未确认，不会触发告警）：")]
    lines += [f"- [[{s['ref']}]] {s['plate']} · {s['camera']} · {s['seen_at'][11:19]}" for s in p["records"]]
    return "\n".join(lines)


def _lane_run(r: Run) -> Optional[Dict[str, Any]]:
    return next((x for x in r.call("traffic_runs")["runs"] if x["camera_kind"] == "lane"), None)


def lane_violations(r: Run) -> str:
    run = _lane_run(r)
    if run is None:
        return r.t("There is no lane-camera run.", "没有车道摄像头的分析结果。")
    ev = r.call("lane_events", run_id=run["run_id"])
    bad = [e for e in ev["events"] if e["severity"] in ("violation", "hazard")]
    lines = [r.t(f"{run['label']} ({run['camera']}): {len(bad)} violations and hazards shown, all of SIMULATED vehicles; "
                 f"{ev['withheld_total']} violations of real vehicles are withheld (counted only).",
                 f"{run['label']}（{run['camera']}）：显示的违规和危险 {len(bad)} 起，全部是模拟车辆；真实车辆的违规 "
                 f"{ev['withheld_total']} 起不予显示（只计数）。")]
    lines += [f"- [[{e['ref']}]] {e['title']} · {e.get('lane') or '-'} · {e['message']}" for e in bad]
    return "\n".join(lines)


def inside(r: Run) -> str:
    p = r.call("gate_inside")
    lines = [r.t(f"{p['count']} visitors inside, {p['overstays']} overstaying (more than {p['overstay_limit_hours']:g} h "
                 f"or past their pass):", f"场内访客 {p['count']} 位，其中超时 {p['overstays']} 位（超过 "
                 f"{p['overstay_limit_hours']:g} 小时或通行证已过期）：")]
    for v in p["visitors"]:
        flag = r.t(" · OVERSTAY", " · 超时") if v["overstay"] else ""
        lines.append(f"- [[{v['ref']}]] {v['plate']} · {r.t('unit', '单位')} {v['unit'] or '-'} · {v['hours_inside']:g} h{flag}{_sim(v, r)}")
    return "\n".join(lines)


def gate_today(r: Run) -> str:
    today = clock.now_iso()[:10]
    p = r.call("gate_access_log", start=today, limit=10)
    by = ", ".join(f"{k} {v}" for k, v in sorted(p["by_decision"].items())) or r.t("none", "无")
    lines = [r.t(f"Gate decisions today ({today}): {p['total']} ({by}).", f"今天（{today}）的门岗决定：{p['total']} 条（{by}）。")]
    lines += [f"- [[{e['ref']}]] {e['at'][11:16]} · {e['plate']} · {e['decision']} · {e['identity'].replace('_', ' ')}"
              for e in p["events"]]
    return "\n".join(lines)


def passes_now(r: Run) -> str:
    p = r.call("gate_passes", valid_now_only=True)
    lines = [r.t(f"{p['valid_now']} visitor passes valid now:", f"当前有效的访客通行证 {p['valid_now']} 张：")]
    lines += [f"- [[{x['ref']}]] {x['plate']} · {r.t('unit', '单位')} {x['unit']} · {r.t('until', '至')} "
              f"{x['valid_to'][11:16]}{_sim(x, r)}" for x in p["passes"]]
    return "\n".join(lines)


def flow_today(r: Run) -> str:
    p = r.call("overview")
    types = ", ".join(f"{k} {v} ({p['by_type_share_pct'][k]} %)" for k, v in sorted(p["by_type"].items(), key=lambda kv: -kv[1]))
    lines = [r.t(f"{p['date']}: {p['vehicles']} vehicles counted at the counting lines: {types}.",
                 f"{p['date']}：计数线共统计 {p['vehicles']} 辆车：{types}。")]
    for cam, n in p["by_camera"].items():
        lines.append(f"- {cam}: {n}")
    for lr in p["lane_runs"]:
        lines.append(r.t(f"Lane camera {lr['camera']}: {lr['vehicles']} vehicles in {lr['minutes']:g} min "
                         f"({lr['veh_per_min']:g} per minute), busiest lane {lr['busiest_lane']}.",
                         f"车道摄像头 {lr['camera']}：{lr['minutes']:g} 分钟内 {lr['vehicles']} 辆（每分钟 "
                         f"{lr['veh_per_min']:g} 辆），最繁忙车道 {lr['busiest_lane']}。"))
    if p["demo_day"]:
        lines.append(r.t("Demo footage on a notional clock.", "演示视频使用名义时钟。"))
    return "\n".join(lines)


def _congestion_day(r: Run) -> Optional[Dict[str, Any]]:
    days = r.call("forecast_days")["days"]
    return next((d for d in days if d["calibrated"] and d["first_warning"]), None)


def congestion(r: Run) -> str:
    d = _congestion_day(r)
    if d is None:
        return r.t("No calibrated congestion day with a forecast.", "没有带预测的已标定拥堵日。")
    e = r.call("congestion_episodes", day=d["day"])
    whole = e["entities"].get("To CIQ", {})
    eps = "; ".join(f"{x['start']}-{x['end']} ({x['minutes']} min)" for x in whole.get("episodes", [])) or r.t("none", "无")
    return "\n".join([
        r.t(f"{d['label']}, {d['camera']}: congestion on the approach to the CIQ: {eps}.",
            f"{d['label']}，{d['camera']}：通往 CIQ 的路段拥堵时段：{eps}。"),
        r.t(f"First warning {d['first_warning']}, {d['first_warning_lead_min']} min before the first onset "
            f"({d['onsets'][0]}); model {d['model']}.",
            f"首次预警 {d['first_warning']}，比第一次拥堵开始（{d['onsets'][0]}）提前 {d['first_warning_lead_min']} 分钟；模型 {d['model']}。"),
        r.t(f"Longest-queueing zone: {e['longest_queueing_zone']} ({e['longest_queueing_zone_minutes']} congested minutes).",
            f"排队最久的区段：{e['longest_queueing_zone']}（拥堵 {e['longest_queueing_zone_minutes']} 分钟）。")])


def accuracy(r: Run) -> str:
    d = _congestion_day(r)
    if d is None:
        return r.t("No calibrated congestion day with a forecast.", "没有带预测的已标定拥堵日。")
    a = r.call("forecast_accuracy", day=d["day"])
    lines = [r.t(f"Forecast accuracy, {d['label']} ({a['section']}, model {a['model']}, trained on {a['trained_on']}):",
                 f"预测精度，{d['label']}（{a['section']}，模型 {a['model']}，训练数据 {a['trained_on']}）：")]
    for h in a["by_horizon"]:
        leads = ", ".join(f"{o}: {w} min" for o, w in h["warning_lead_min"].items())
        lines.append(r.t(f"- +{h['horizon_min']} min: MAE {h['mae_pct_points']} points of fill, warning before the onsets: {leads}",
                         f"- +{h['horizon_min']} 分钟：平均绝对误差 {h['mae_pct_points']} 个百分点，拥堵开始前的预警：{leads}"))
    return "\n".join(lines)


def whatif20(r: Run) -> str:
    d = _congestion_day(r)
    if d is None:
        return r.t("No calibrated congestion day with a forecast.", "没有带预测的已标定拥堵日。")
    w = r.call("what_if", day=d["day"], capacity_pct=20)
    return "\n".join([
        r.t(f"{w['label']}: with {w['capacity_increase_pct']:g} % more discharge capacity from {w['from']}, the queue "
            f"would be at or above the congestion threshold for {w['minutes_over_threshold_projected']} minutes instead of "
            f"{w['minutes_over_threshold_baseline']} ({w['minutes_over_threshold_saved']} fewer); the peak queue "
            f"{w['peak_queue_projected_veh']} instead of {w['peak_queue_baseline_veh']} vehicles.",
            f"{w['label']}：如果从 {w['from']} 起疏散能力提高 {w['capacity_increase_pct']:g}%，排队达到或超过拥堵阈值的时间将是 "
            f"{w['minutes_over_threshold_projected']} 分钟而不是 {w['minutes_over_threshold_baseline']} 分钟（少 "
            f"{w['minutes_over_threshold_saved']} 分钟）；排队峰值 {w['peak_queue_projected_veh']} 辆而不是 "
            f"{w['peak_queue_baseline_veh']} 辆。"),
        r.t("Assumptions:", "假设：")] + [f"- {x}" for x in w["assumptions"]])


def audit_check(r: Run) -> str:
    s = r.call("admin_status")
    ok = r.t("intact", "完好") if s["audit_chain_ok"] else r.t(f"BROKEN at row {s['audit_first_bad_id']}",
                                                               f"在第 {s['audit_first_bad_id']} 行被破坏")
    return r.t(f"Audit log hash chain: {ok} ({s['audit_rows_checked']} rows checked). Last import: {s['last_import']}.",
               f"审计日志哈希链：{ok}（检查了 {s['audit_rows_checked']} 行）。最近一次导入：{s['last_import']}。")


def usage(r: Run) -> str:
    s = r.call("admin_status")
    c = s["assistant_cost_per_question_usd"]
    return r.t(f"Assistant, last 30 days: {s['assistant_questions_30_days']} questions, {s['assistant_cost_usd_30_days']} "
               f"USD in all; per question mean {c['mean']}, median {c['median']}, max {c['max']} USD.",
               f"AI 助手近 30 天：{s['assistant_questions_30_days']} 个问题，共 {s['assistant_cost_usd_30_days']} 美元；"
               f"每个问题平均 {c['mean']}、中位数 {c['median']}、最多 {c['max']} 美元。")


QUICK: Dict[str, Tuple[Tuple[str, ...], str, str, Fn]] = {
    "open_alerts": (("police", "guard", "admin"), "Open alerts by severity", "按严重程度列出未处理告警", open_alerts),
    "cloned_plate": (("police",), "Trace the cloned-plate alert", "追踪套牌告警", cloned_plate),
    "provisional": (("police",), "Provisional plate readings", "临时（未确认）车牌读取", provisional),
    "lane_violations": (("police", "traffic", "admin"), "Lane violations and hazards", "车道违规与危险事件", lane_violations),
    "inside": (("guard",), "Who is inside now? (overstays first)", "现在场内有谁？（超时优先）", inside),
    "gate_today": (("guard",), "Today's gate decisions", "今天的门岗决定", gate_today),
    "passes_now": (("guard",), "Visitor passes valid now", "当前有效的访客通行证", passes_now),
    "flow_today": (("police", "traffic", "admin"), "Vehicles by type on the latest day", "最近一天各类车辆数", flow_today),
    "congestion": (("police", "traffic", "admin"), "Congestion and the first warning", "拥堵时段与首次预警", congestion),
    "accuracy": (("traffic", "police", "admin"), "Forecast accuracy (+5 / +15 / +30 min)", "预测精度（+5/+15/+30 分钟）", accuracy),
    "whatif20": (("traffic", "police", "admin"), "What if capacity +20 % from the first warning?", "如果从首次预警起通行能力提高 20%？", whatif20),
    "audit_check": (("admin",), "Audit-log hash-chain check", "审计日志哈希链检查", audit_check),
    "usage": (("admin",), "Assistant usage and cost", "AI 助手用量与费用", usage),
}


def quick_questions(role: str) -> List[Dict[str, str]]:
    return [{"id": k, "en": en, "zh": zh} for k, (roles, en, zh, _) in QUICK.items() if role in roles]


def answer(conn: sqlite3.Connection, user: User, qid: str, lang: str = "en") -> Dict[str, Any]:
    """A quick question's answer in the same shape as a chat answer (display), labelled as a template."""
    if qid not in QUICK or user.role not in QUICK[qid][0]:
        raise LookupError("no such quick question for this role")
    run = Run(conn, user, lang)
    try:
        text = QUICK[qid][3](run)
        problem = None
    except ValueError as e:
        text, problem = "", str(e)
    from ..privacy import audit
    audit(conn, run.ctx.user, "copilot_quick", "copilot", qid, {"tools": [s["tool"] for s in run.steps]})
    title = QUICK[qid][2] if run.lang == "zh" else QUICK[qid][1]
    return {"question": title, "text": text, "steps": run.steps, "citations": run.ctx.citations,
            "record_data": run.ctx.citations, "records": [], "cards": [], "panels": run.ctx.panels, "ai": False,
            "offline": True, "label": LABEL[run.lang], "problem": problem, "model": "offline-template",
            "check": {"ok": True, "template": True, "checked": 0, "unmatched": [], "plates_unmatched": []},
            "at": clock.now_iso()}
