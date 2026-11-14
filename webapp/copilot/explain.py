"""How an alert was computed, from the STORED values: the facts behind "AI explain" (and the explain_alert tool).

Everything here is deterministic code: the rule that raised the alert, the stored values (alert message, sighting,
lane event, the SIMULATED registry record for police, the thresholds of the camera's profile in alpr/config.yaml)
and the numbers derived from them (e.g. the lower end of a speed's 95 % interval). The model only puts these facts
into words; the SOP steps (sop.yaml) and the legal references (legal.yaml) are shown next to its text verbatim.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date
from functools import lru_cache
from typing import Any, Dict, List, Optional

from .. import db
from ..registry_provider import get_provider
from ..security import User
from ..services import alerts as alert_svc
from ..services import plates as plate_svc
from ..services.evidence import legal_refs
from ..settings import active
from . import sop

NOT_A_FINDING = {
    "en": "An alert is a rule that fired on measured data. It is not a finding that an offence happened: a person must "
          "verify it.",
    "zh": "告警只是规则在测量数据上被触发，并不表示违法行为已经发生：必须由人工核实。"}
SIMULATED = {
    "synthetic": {"en": "Simulated: a synthetic event with a fictitious X-series plate; there is no vehicle in the video.",
                  "zh": "模拟数据：合成事件，使用虚构的 X 开头车牌；视频中没有这辆车。"},
    "inserted_plate": {"en": "Simulated: a real vehicle whose plate was digitally replaced by a fictitious X-series plate "
                             "for the demo.",
                       "zh": "模拟数据：真实车辆，但其车牌被数字替换为虚构的 X 开头车牌，仅用于演示。"}}
REGISTRY = {"en": "The vehicle registry is SIMULATED (no JPJ data).", "zh": "车辆登记库是模拟的（没有 JPJ 数据）。"}

RULES = {
    "BLACKLIST": ("The plate's registry record has the status 'blacklisted' (for example reported stolen); "
                  "alpr.registry.check_blacklist raises the alert whenever such a plate is read.",
                  "该车牌的登记记录状态为“黑名单”（例如报失窃）；只要读到这样的车牌，alpr.registry.check_blacklist 就会告警。"),
    "EXPIRED": ("The road-tax (motor vehicle licence) expiry date in the registry record is before the day the plate "
                "was seen; alpr.registry.check_expiry compares the two dates.",
                "登记记录中的路税（机动车执照）到期日早于看到该车牌的日期；alpr.registry.check_expiry 比较这两个日期。"),
    "UNMATCHED": ("The plate is not in the registry and no registered plate is one character away (one character "
                  "away is more likely an OCR misread of that plate, so it does not alert).",
                  "该车牌不在登记库中，且没有只差一个字符的已登记车牌（只差一个字符更可能是 OCR 读错，因此不告警）。"),
    "ILLEGAL_FORMAT": ("The text read does not follow a legal Malaysian plate format (alpr.plates.parse_plate: a "
                       "prefix of 1-3 letters, a number of 1-4 digits, an optional suffix letter).",
                       "读出的文字不符合马来西亚车牌的法定格式（alpr.plates.parse_plate：1-3 个字母的前缀、1-4 位数字、可选的后缀字母）。"),
    "TYPE_MISMATCH": ("The vehicle type the detector saw does not fit the body type in the registry record "
                      "(alpr.registry.check_type with its table of the detector classes each body type may show up as).",
                      "检测器看到的车型与登记记录中的车身类型不符（alpr.registry.check_type，按每种车身类型可能被识别成的检测类别对照表判断）。"),
    "DUPLICATE_TRAVEL": ("The same plate was seen at two places too far apart to drive between in the time between "
                         "the sightings: the implied speed is above the system's maximum (alpr.registry.check_travel; "
                         "legs under 2 km never count).",
                         "同一车牌在两个地点被看到，两次目击之间的时间不足以驾车往返：所需速度超过系统设定的上限"
                         "（alpr.registry.check_travel；2 公里以内的路段不计）。"),
    "DUPLICATE_TOGETHER": ("Two vehicles carrying the same plate were tracked in view at the same time "
                           "(alpr.pipeline: two tracks in view together for at least 5 frames are never merged).",
                           "两辆挂着相同车牌的车同时出现在画面中被跟踪（alpr.pipeline：同时出现在画面中至少 5 帧的两条轨迹不会被合并）。"),
    "WATCHLIST": ("The plate is on the police watch list (an active entry); the import matches every new sighting "
                  "against the active lists.", "该车牌在警方关注名单上（有效条目）；导入时每条新目击都会与有效名单比对。"),
    "BANNED_AT_SITE": ("The plate is on this site's ban list; the gate check raises the alert when the plate is read "
                       "at the gate.", "该车牌在本站点的禁入名单上；门岗读到该车牌时会告警。"),
    "SPEEDING": ("Speed is the slope of a straight-line fit of road position against time on a calibrated camera. "
                 "The alert needs 3 estimates in a row whose lower 95 % bound (speed minus 2σ) is above the limit "
                 "(alpr.behaviour).",
                 "速度是在已标定摄像头上对路面位置随时间做直线拟合得到的斜率。需要连续 3 次估计的 95% 下限（速度减 2σ）都高于限速才告警（alpr.behaviour）。"),
    "TAILGATING": ("The time headway to the vehicle in front at the count line (front to front, same lane) is below "
                   "the threshold, while the follower's speed minus 2σ is at least the minimum speed (alpr.behaviour).",
                   "在计数线处与前车的车头时距（车头到车头，同一车道）低于阈值，且后车速度减 2σ 不低于最低速度（alpr.behaviour）。"),
    "STOPPED": ("The vehicle stood still in a lane for at least the stopped time while the rest of its carriageway kept "
                "moving (standing in a queue is congestion, not an event; alpr.behaviour).",
                "车辆在车道内静止达到设定时长，而同方向其余车道仍在通行（排队等候属于拥堵，不算事件；alpr.behaviour）。"),
    "WRONG_WAY": ("The vehicle moved against its lane's direction of travel by at least the set distance (in box "
                  "heights, so it holds near and far from the camera) within the set time (alpr.behaviour).",
                  "车辆在设定时间内逆着车道行驶方向移动了至少设定的距离（以检测框高度计，远近都适用；alpr.behaviour）。"),
    "SOLID_LINE": ("The vehicle's reference point moved to the other side of a solid line (or into a hatched area) and "
                   "stayed there (alpr.behaviour).", "车辆参考点越过实线（或进入斑马线区域）并停留在另一侧（alpr.behaviour）。"),
}


DERIVED = {
    "days_expired_when_seen": ("Days expired when seen", "看到时已过期天数"),
    "longest_digit_run": ("Digits in the number part", "数字部分的位数"),
    "most_digits_allowed": ("Most digits allowed", "最多允许的位数"),
    "vehicles_with_this_plate_in_the_run": ("Vehicles with this plate in the run", "本段视频中挂此车牌的车辆数"),
    "sightings": ("Sightings", "目击记录"),
    "max_speed_kmh": ("Fastest possible travel (km/h)", "最快可能的行驶速度（公里/小时）"),
    "distance_km": ("Distance (km)", "距离（公里）"),
    "minutes_between": ("Minutes between the sightings", "两次目击相隔（分钟）"),
    "speed_needed_kmh": ("Speed needed (km/h)", "所需速度（公里/小时）"),
    "timeline_places": ("Places in time order", "按时间排列的地点"),
    "error_95_kmh": ("95 % error, 2σ (km/h)", "95% 误差，2σ（公里/小时）"),
    "lower_95_kmh": ("Lower end of the 95 % interval (km/h)", "95% 区间下限（公里/小时）"),
    "upper_95_kmh": ("Upper end of the 95 % interval (km/h)", "95% 区间上限（公里/小时）"),
    "estimates_in_a_row": ("Estimates in a row needed", "需要连续估计的次数"),
    "lower_95_above_limit_kmh": ("Lower end above the limit by (km/h)", "下限高出限速（公里/小时）"),
    "below_threshold_by_s": ("Below the threshold by (s)", "低于阈值（秒）"),
}


@lru_cache(maxsize=8)
def profile_params(profile: str) -> Dict[str, Any]:
    """Thresholds of a pipeline profile (alpr/config.yaml) as the run used them."""
    from alpr.config import load_config
    try:
        c = load_config(profile)
    except Exception:                                                  # noqa: BLE001 - an upload's ad-hoc profile
        return {}
    keys = ("speed_limit_kmh", "tailgate_headway_sec", "tailgate_min_kmh", "stopped_sec", "wrong_way_sec",
            "wrong_way_min_disp", "registry_max_speed_kmh")
    return {k: getattr(c, k) for k in keys if hasattr(c, k)}


def _v(en: str, zh: str, value: Any, source: str) -> Dict[str, Any]:
    return {"label": {"en": en, "zh": zh}, "value": value, "source": source}


def _digits_run(compact: str) -> int:
    return max((len(m) for m in re.findall(r"\d+", compact or "")), default=0)


def facts(conn: sqlite3.Connection, user: User, alert_id: int) -> Dict[str, Any]:
    """The explanation facts of one alert, visible to this user (alert_svc.get_alert checks the role and masks)."""
    a = alert_svc.get_alert(conn, user, alert_id)
    raw = db.row(conn, "SELECT * FROM alerts WHERE id = ?", (alert_id,))
    s = db.row(conn, "SELECT * FROM sightings WHERE id = ?", (raw["sighting_id"],)) if raw["sighting_id"] else None
    ev = db.row(conn, "SELECT * FROM lane_events WHERE id = ?", (raw["lane_event_id"],)) if raw["lane_event_id"] else None
    run = db.row(conn, "SELECT * FROM runs WHERE id = ?", (raw["run_id"],)) if raw["run_id"] else None
    params = profile_params(run["profile"]) if run and run["profile"] else {}
    code = a.code
    values: List[Dict[str, Any]] = [_v("Alert message (stored)", "告警消息（存储值）", a.message, "alerts.message")]
    derived: Dict[str, Any] = {}
    caveats: List[Dict[str, str]] = []
    rule_key = code
    rec = get_provider(str(active().registry_path)).lookup(raw["compact"]) if (
        user.role == "police" and raw["compact"]) else None
    if s:
        values.append(_v("Vehicle type seen", "看到的车型", s["vtype"] or "unknown", "sightings.vtype"))
        values.append(_v("Plate reading", "车牌读取状态", "provisional" if s["status"] == "TENTATIVE" else "confirmed",
                         "sightings.status"))
    if code == "BLACKLIST" and rec:
        values.append(_v("Registry status", "登记状态", rec.get("status"), "SIMULATED registry"))
        values.append(_v("Registry note", "登记备注", rec.get("note") or "", "SIMULATED registry"))
    elif code == "EXPIRED":
        exp = (rec or {}).get("road_tax_expiry") or (re.search(r"\d{4}-\d{2}-\d{2}", a.message) or [None])[0]
        if exp and s:
            seen = s["seen_at"][:10]
            values += [_v("Road tax valid to", "路税有效期至", exp, "SIMULATED registry" if rec else "alerts.message"),
                       _v("Seen on", "看到日期", seen, "sightings.seen_at")]
            derived["days_expired_when_seen"] = (date.fromisoformat(seen) - date.fromisoformat(exp)).days
    elif code == "UNMATCHED":
        caveats.append({"en": "Not in the SIMULATED registry. This does not mean a real vehicle is unregistered.",
                        "zh": "不在模拟登记库中。这并不表示真实车辆未登记。"})
    elif code == "ILLEGAL_FORMAT" and raw["compact"]:
        derived["longest_digit_run"] = _digits_run(raw["compact"])
        derived["most_digits_allowed"] = 4
    elif code == "TYPE_MISMATCH":
        from alpr.registry import BODY_TYPES
        body = (rec or {}).get("body_type") or (re.search(r"registered (\w+)", a.message) or [None, None])[1]
        if body:
            values.append(_v("Registered body type", "登记车身类型", body, "SIMULATED registry" if rec else "alerts.message"))
            values.append(_v("Detector classes that fit it", "与之相符的检测类别", ", ".join(BODY_TYPES.get(body, (body,))),
                             "alpr.registry.BODY_TYPES"))
        caveats.append({"en": "The detector's classes are coarse (SUVs, pickups and vans are often seen as trucks).",
                        "zh": "检测器的类别很粗（SUV、皮卡和厢式车常被识别为卡车）。"})
    elif code == "DUPLICATE":
        if a.message.startswith("same plate on vehicle"):
            rule_key = "DUPLICATE_TOGETHER"
            twins = db.rows(conn, "SELECT id, first_sec, last_sec FROM sightings WHERE run_id = ? AND compact = ? "
                                  "ORDER BY first_sec", (raw["run_id"], raw["compact"]))
            derived["vehicles_with_this_plate_in_the_run"] = len(twins)
            derived["sightings"] = [{"ref": f"S:{t['id']}", "first_sec": t["first_sec"], "last_sec": t["last_sec"]}
                                    for t in twins]
        else:
            rule_key = "DUPLICATE_TRAVEL"
            derived["max_speed_kmh"] = float(db.get_setting(conn, "max_speed_kmh"))
            if user.role == "police" and raw["compact"]:
                tl = plate_svc.timeline_of(conn, user, raw["compact"])
                bad = [l for l in tl.legs if l.impossible]
                if bad:
                    leg = bad[0]
                    derived.update(distance_km=leg.km, minutes_between=round(leg.seconds / 60.0, 1),
                                   speed_needed_kmh=leg.speed_kmh)
                derived["timeline_places"] = [p.place for p in tl.points]
        caveats.append({"en": "Rule out an OCR misread and a wrong camera clock before treating it as one plate on two "
                              "vehicles.", "zh": "在认定同一车牌挂在两辆车上之前，先排除 OCR 读错和摄像头时钟错误。"})
    elif code in ("WATCHLIST", "BANNED_AT_SITE"):
        lst = "watch" if code == "WATCHLIST" else "banned"
        e = db.row(conn, "SELECT * FROM list_entries WHERE list = ? AND compact = ? ORDER BY active DESC, id DESC",
                   (lst, raw["compact"]))
        if e:
            values.append(_v("List entry reason", "名单条目原因", e["reason"], "list_entries.reason"))
            values.append(_v("List entry simulated", "名单条目为模拟", bool(e["simulated"]), "list_entries.simulated"))
    elif ev is not None:
        if ev["lane"]:
            values.append(_v("Lane", "车道", ev["lane"], "lane_events.lane"))
        if code == "SPEEDING" and ev["value"] is not None:
            v, sd, lim = ev["value"], ev["error_1sigma"] or 0.0, params.get("speed_limit_kmh")
            values += [_v("Measured speed (km/h)", "测得速度（公里/小时）", round(v, 2), "lane_events.value"),
                       _v("1σ error (km/h)", "1σ 误差（公里/小时）", round(sd, 2), "lane_events.error_1sigma")]
            derived.update(error_95_kmh=round(2 * sd, 1), lower_95_kmh=round(v - 2 * sd, 1), upper_95_kmh=round(v + 2 * sd, 1),
                           estimates_in_a_row=3)
            if lim:
                values.append(_v("Speed limit used (km/h)", "所用限速（公里/小时）", lim, f"alpr/config.yaml {run['profile']}"))
                derived["lower_95_above_limit_kmh"] = round(v - 2 * sd - lim, 1)
                caveats.append({"en": f"The {lim:.0f} km/h limit is an ASSUMED demo value, not the posted limit of the road.",
                                "zh": f"{lim:.0f} 公里/小时的限速是演示用的假设值，不是该路段的标示限速。"})
            caveats.append({"en": "Camera speeds carry a 6.5 % (1σ) calibration error and are not readings of an approved "
                                  "speed-enforcement device.",
                            "zh": "摄像头测速带有 6.5%（1σ）的标定误差，不是经认可的测速执法设备的读数。"})
        elif code == "TAILGATING" and ev["value"] is not None:
            thr, vmin = params.get("tailgate_headway_sec"), params.get("tailgate_min_kmh")
            values.append(_v("Time headway (s)", "车头时距（秒）", ev["value"], "lane_events.value"))
            if thr:
                values.append(_v("Headway threshold (s)", "车头时距阈值（秒）", thr, f"alpr/config.yaml {run['profile']}"))
                derived["below_threshold_by_s"] = round(thr - ev["value"], 2)
            if vmin:
                values.append(_v("Minimum follower speed (km/h)", "后车最低速度（公里/小时）", vmin,
                                 f"alpr/config.yaml {run['profile']}"))
            caveats.append({"en": "The Act sets no fixed minimum headway; whether the driving was careless is for a court "
                                  "to decide.", "zh": "法令没有规定固定的最小车距；驾驶是否疏忽由法院判断。"})
        elif code == "STOPPED" and ev["value"] is not None:
            values.append(_v("Stood still (s)", "静止时长（秒）", ev["value"], "lane_events.value"))
            if params.get("stopped_sec"):
                values.append(_v("Stopped time that raises it (s)", "触发所需静止时长（秒）", params["stopped_sec"],
                                 f"alpr/config.yaml {run['profile']}"))
        elif code == "WRONG_WAY":
            for k, en, zh in (("wrong_way_min_disp", "Distance against the lane direction (box heights)", "逆向移动距离（检测框高度）"),
                              ("wrong_way_sec", "Within (s)", "时间窗口（秒）")):
                if params.get(k) is not None:
                    values.append(_v(en, zh, params[k], f"alpr/config.yaml {run['profile']}"))
    if a.provenance in SIMULATED:
        caveats.append(SIMULATED[a.provenance])
    if a.source == "registry":
        caveats.append(REGISTRY)
    caveats.append(NOT_A_FINDING)
    rule = RULES.get(rule_key) or RULES.get(code) or ("The rule's details are not documented for this alert code.",
                                                      "该告警代码的规则细节没有文档。")
    return {
        "alert": {"ref": f"A:{a.id}", "id": a.id, "code": code, "title": a.title, "severity": a.severity,
                  "status": a.status, "plate": a.plate, "masked": a.masked, "seen_at": a.seen_at,
                  "camera": a.camera_name, "lane": a.lane, "run": a.run_label, "provenance": a.provenance,
                  "provenance_label": a.provenance_label, "simulated": a.provenance != "camera",
                  "sighting_ref": f"S:{a.sighting_id}" if a.sighting_id else None,
                  "lane_event_ref": f"E:{a.lane_event_id}" if a.lane_event_id else None},
        "rule": {"en": rule[0], "zh": rule[1]}, "values": values, "derived": derived,
        "derived_labels": {k: {"en": DERIVED.get(k, (k, k))[0], "zh": DERIVED.get(k, (k, k))[1]} for k in derived},
        "caveats": caveats, "sop": sop.steps(code, user.role), "legal": legal_refs(code),
    }


def for_model(f: Dict[str, Any]) -> Dict[str, Any]:
    """The facts the model writes from: English labels; no SOP steps or legal text (shown verbatim by the panel)."""
    return {"alert": f["alert"], "rule": f["rule"]["en"],
            "values": [{"what": v["label"]["en"], "value": v["value"], "source": v["source"]} for v in f["values"]],
            "derived": {f["derived_labels"][k]["en"]: v for k, v in f["derived"].items()},
            "caveats": [c["en"] for c in f["caveats"]]}


def template(f: Dict[str, Any], lang: str = "en") -> str:
    """The explanation without a model (offline mode): the same facts as fixed sentences."""
    zh = lang == "zh"
    lab = "zh" if zh else "en"
    vals = "; ".join(f"{v['label'][lab]}: {v['value']}" for v in f["values"])
    der = "; ".join(f"{f['derived_labels'][k][lab]}: {v}" for k, v in f["derived"].items() if not isinstance(v, list))
    head = "如何计算：" if zh else "How it was computed: "
    body = [head + f["rule"][lab], ("存储的数值：" if zh else "Stored values: ") + vals + ("。" if zh else ".")]
    if der:
        body.append(("推导出的数值：" if zh else "Derived: ") + der + ("。" if zh else "."))
    body.append(("它不能说明什么：" if zh else "What it does not show: ") + " ".join(c[lab] for c in f["caveats"]))
    return "\n\n".join(body)
