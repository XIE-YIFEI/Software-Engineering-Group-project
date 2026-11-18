"""Run the copilot evaluation set (questions.yaml) and grade it programmatically.

    python -m webapp.copilot.eval.run_eval --list                # the cases, as a Markdown table (for review)
    python -m webapp.copilot.eval.run_eval --dry-run oracle      # runner + grader self-test, no API: expect 100 %
    python -m webapp.copilot.eval.run_eval --dry-run null        # empty answers, no API: expect 0 %
    python -m webapp.copilot.eval.run_eval --pilot 3             # REAL model, first 3 cases (cost pilot)
    python -m webapp.copilot.eval.run_eval                       # REAL model, every case (costs money: ask first)

Each run imports the committed demo data into a fresh scratch database (never data/web), asks every question through
the real entry point (webapp.copilot.agent.ask: the same prompt, tools, permissions and number check as the panel),
computes the expected answers from that database with the service functions, and grades each answer with the checks
listed in questions.yaml. Output (outputs/ is gitignored): <out>/<variant>/results.jsonl (one row per case and rep,
written as it completes; a re-run resumes), traces/<id>_rep<k>.json (the full exchange), errors.jsonl (attempts that
failed before an answer could be graded), summary.md. A paid run refuses to start until the harness (this file, the
cases, the prompt and the tools) has been approved once with --approve-harness, and again after any of them changes.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
CASES = HERE / "questions.yaml"
HARNESS = [Path(__file__), CASES, HERE.parent / "prompts.py", HERE.parent / "tools.py", HERE.parent / "agent.py"]
DAY = "jb_ciq_cam02_2023-03-22"
MALAY = ("yang", "dan", "untuk", "tidak", "ialah", "adalah", "dengan", "pada", "kenderaan", "dari", "ini", "boleh",
         "sebanyak", "minit", "ramalan", "kereta")
CANNED = ["verify and confirm by an officer", "withheld: counted only", "banned: refuse entry", "the Federal government",
          "intact", "no data for that day", "please choose a reason to reveal the full plate", "assumptions",
          "only your own gate", "evidence pack", "the copilot is read-only", "add it on the Watch list & blacklist page",
          "change it in the Alert centre", "issue it on the Guardhouse or Residents & visitors page",
          "use Show full plate in the Plate search"]


def load_cases() -> List[Dict[str, Any]]:
    with open(CASES, encoding="utf-8") as f:
        cases = yaml.safe_load(f)
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    for c in cases:
        assert c["role"] in ("police", "guard", "traffic", "admin") and c["lang"] in ("en", "zh", "ms"), c["id"]
        assert c.get("values") or c.get("keywords") or c.get("card") or c.get("tools"), f"{c['id']} checks nothing"
        assert not c.get("read_only") or (c.get("unchanged") and c.get("keywords")), f"{c['id']}: read_only needs both"
    return cases


# ---------------------------------------------------------------------------- the scratch database
class Env:
    """A fresh import of the demo data, the four demo users, and the expected-value functions."""

    def __init__(self, data_dir: Path, computed_forecasts: bool = True):
        os.environ["SECS_WEB_DATA"] = str(data_dir / "web")
        os.environ["SECS_REGISTRY"] = str(data_dir / "registry" / "demo_registry.db")
        sys.path.insert(0, str(ROOT))
        from webapp import db
        from webapp.importer import import_demo
        from webapp.settings import load_settings, use
        self.st = load_settings()
        use(self.st)
        self.st.ensure_dirs()
        conn = db.connect(self.st.db_path)
        if not db.scalar(conn, "SELECT COUNT(*) FROM users"):
            import_demo(conn, self.st, demo_users=True, computed_forecasts=computed_forecasts, log=lambda *_: None)
        conn.close()

    def conn(self):
        from webapp import db
        return db.connect(self.st.db_path)

    def user(self, conn, role: str, via: str = "web"):
        from webapp import db
        from webapp.security import User
        r = db.row(conn, "SELECT * FROM users WHERE role = ? AND demo = 1 ORDER BY id LIMIT 1", (role,))
        return User(r["id"], r["username"], r["display_name"], r["role"], r["site_id"], via=via)

    def call(self, conn, role: str, tool: str, **args) -> Dict[str, Any]:
        """A tool call made by the grader (audit rows say via=eval)."""
        from webapp.copilot import tools
        r = tools.execute(tools.Ctx(conn, self.user(conn, role, "eval")), tool, args)
        if r.is_error:
            raise RuntimeError(f"expected-value call {tool}({args}) failed: {r.payload}")
        return r.payload

    def resolve(self, conn, value: Any) -> Any:
        """Placeholders of the `ideal` calls: {alert:CODE}, {lane_run}, {day}, {today}."""
        from webapp import clock, db
        if not isinstance(value, str) or not value.startswith("{"):
            return value
        key = value.strip("{}")
        if key.startswith("alert:"):
            code = key.split(":")[1]
            if code == "DUPLICATE_TOGETHER":
                return db.scalar(conn, "SELECT id FROM alerts WHERE code = 'DUPLICATE' AND message LIKE 'same plate%' ORDER BY id")
            return db.scalar(conn, "SELECT id FROM alerts WHERE code = ? ORDER BY id", (code,))
        return {"lane_run": lambda: db.scalar(conn, "SELECT r.id FROM runs r JOIN cameras c ON c.id = r.camera_id "
                                                    "WHERE c.kind = 'lane' ORDER BY r.id"),
                "day": lambda: DAY, "today": lambda: clock.now_iso()[:10]}[key]()


def _lane(env: Env, conn) -> Dict[str, Any]:
    return env.call(conn, "traffic", "traffic_summary", run_id=env.resolve(conn, "{lane_run}"))


def _acc(env: Env, conn, h: int) -> Dict[str, Any]:
    return next(x for x in env.call(conn, "traffic", "forecast_accuracy", day=DAY)["by_horizon"] if x["horizon_min"] == h)


def _explain(env: Env, conn, code: str) -> Dict[str, Any]:
    return env.call(conn, "police", "explain_alert", alert_id=env.resolve(conn, "{alert:%s}" % code))


def _value(f: Dict[str, Any], what: str) -> Any:
    return next(v["value"] for v in f["values"] if v["what"] == what)


def _episodes(env: Env, conn) -> Dict[str, Any]:
    return env.call(conn, "traffic", "congestion_episodes", day=DAY)


def _timeline_leg(env: Env, conn) -> Dict[str, Any]:
    return next(l for l in env.call(conn, "police", "plate_timeline", plate="XSD4004")["legs"] if l["impossible"])


EXPECTED: Dict[str, Callable[[Env, Any], Any]] = {
    "open_alerts": lambda e, c: e.call(c, "police", "list_alerts", limit=1)["counts"]["open"],
    "open_alerts_simulated": lambda e, c: e.call(c, "police", "list_alerts", limit=1)["counts"]["open_simulated"],
    "blacklisted_plate": lambda e, c: e.call(c, "police", "list_alerts", status="all", code="BLACKLIST")["alerts"][0]["plate"],
    "clone_speed_kmh": lambda e, c: _timeline_leg(e, c)["speed_kmh"],
    "clone_distance_km": lambda e, c: _timeline_leg(e, c)["km"],
    "speed_limit_kmh": lambda e, c: _value(_explain(e, c, "SPEEDING"), "Speed limit used (km/h)"),
    "speed_lower_95": lambda e, c: _explain(e, c, "SPEEDING")["derived"]["Lower end of the 95 % interval (km/h)"],
    "road_tax_expiry": lambda e, c: _value(_explain(e, c, "EXPIRED"), "Road tax valid to"),
    "days_expired": lambda e, c: _explain(e, c, "EXPIRED")["derived"]["Days expired when seen"],
    "lane_vehicles": lambda e, c: _lane(e, c)["vehicles"],
    "lane_car_share": lambda e, c: _lane(e, c)["by_type_share_pct"]["Car"],
    "withheld_speeding": lambda e, c: _lane(e, c)["withheld_real_violations"]["SPEEDING"],
    "provisional_plate": lambda e, c: e.call(c, "police", "search_plates", purpose="Data-quality check",
                                             reading="TENTATIVE")["records"][0]["plate"],
    "inside_count": lambda e, c: e.call(c, "guard", "gate_inside")["count"],
    "overstay_plate": lambda e, c: next(v["plate"] for v in e.call(c, "guard", "gate_inside")["visitors"] if v["overstay"]),
    "valid_pass_plate": lambda e, c: e.call(c, "guard", "gate_passes", valid_now_only=True)["passes"][0]["plate"],
    "allowed_today": lambda e, c: e.call(c, "guard", "gate_access_log", start=e.resolve(c, "{today}"))["by_decision"].get("allowed", 0),
    "visitor_unit": lambda e, c: e.call(c, "guard", "gate_identify", plate="XVP 3003")["unit_no"],
    "day_vehicles": lambda e, c: e.call(c, "traffic", "overview")["vehicles"],
    "day_cars": lambda e, c: e.call(c, "traffic", "overview")["by_type"]["Car"],
    "onset_1": lambda e, c: _episodes(e, c)["entities"]["To CIQ"]["episodes"][0]["start"],
    "onset_1_end": lambda e, c: _episodes(e, c)["entities"]["To CIQ"]["episodes"][0]["end"],
    "onset_2": lambda e, c: _episodes(e, c)["entities"]["To CIQ"]["episodes"][1]["start"],
    "lead_30_first": lambda e, c: list(_acc(e, c, 30)["warning_lead_min"].values())[0],
    "lead_30_second": lambda e, c: list(_acc(e, c, 30)["warning_lead_min"].values())[1],
    "whatif20_baseline": lambda e, c: e.call(c, "traffic", "what_if", day=DAY, capacity_pct=20)["minutes_over_threshold_baseline"],
    "whatif20_projected": lambda e, c: e.call(c, "traffic", "what_if", day=DAY, capacity_pct=20)["minutes_over_threshold_projected"],
    "longest_zone": lambda e, c: _episodes(e, c)["longest_queueing_zone"],
    "longest_zone_minutes": lambda e, c: _episodes(e, c)["longest_queueing_zone_minutes"],
    "mae_15": lambda e, c: _acc(e, c, 15)["mae_pct_points"],
    "busiest_lane": lambda e, c: max(_lane(e, c)["lanes"], key=lambda l: l["vehicles"])["lane"],
    "busiest_lane_occupancy": lambda e, c: max(_lane(e, c)["lanes"], key=lambda l: l["vehicles"])["occupancy_pct"],
    "admin_high_alerts": lambda e, c: e.call(c, "admin", "list_alerts", limit=1)["counts"]["open_by_severity"]["High"],
    "copilot_retention": lambda e, c: e.call(c, "admin", "privacy_rules")["retention_days"]["assistant conversations"],
    "reveal_minutes": lambda e, c: e.call(c, "admin", "privacy_rules")["reveal_minutes"],
}


# ---------------------------------------------------------------------------- grading
NUM = re.compile(r"(?<![A-Za-z0-9_.:])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
TIME = re.compile(r"^\d{1,2}:\d{2}$")
ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
MS_MONTHS = ["Januari", "Februari", "Mac", "April", "Mei", "Jun", "Julai", "Ogos", "September", "Oktober", "November",
             "Disember"]


def present(value: Any, answer: str) -> bool:
    """Whether an expected value appears in the answer: numbers rounded to the answer's precision, plates compared
    without spaces, times with or without a leading zero, dates in the usual written forms."""
    if isinstance(value, bool):
        return str(value).lower() in answer.lower()
    if isinstance(value, (int, float)):
        for m in NUM.finditer(answer):
            tok = m.group(0).replace(",", "").replace("−", "-")
            d = len(tok.split(".")[1]) if "." in tok else 0
            if abs(abs(float(tok)) - abs(float(value))) <= 0.5 * 10 ** (-d) + 1e-9:
                return True
        return False
    s = str(value)
    if TIME.match(s):
        h, mi = s.split(":")
        return re.search(rf"(?<!\d)0?{int(h)}:{mi}(?!\d)", answer) is not None
    m = ISO_DATE.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        forms = [s, f"{d} {MONTHS[mo - 1]} {y}", f"{d} {MONTHS[mo - 1][:3]} {y}", f"{MONTHS[mo - 1]} {d}, {y}",
                 f"{y}年{mo}月{d}日", f"{d} {MS_MONTHS[mo - 1]} {y}", f"{d}/{mo}/{y}", f"{d:02d}/{mo:02d}/{y}"]
        return any(f.lower() in answer.lower() for f in forms)
    if re.fullmatch(r"[A-Z]{1,3} ?\d{1,4} ?[A-Z]{0,2}", s):
        return re.sub(r"[^A-Z0-9]", "", s) in re.sub(r"[^A-Z0-9]", "", answer.upper())
    return s.lower() in answer.lower()


def language_ok(answer: str, lang: str) -> bool:
    cjk = len(re.findall(r"[一-鿿]", answer))
    latin = len(re.findall(r"[A-Za-z]", answer))
    if lang == "zh":
        return cjk >= 8 and cjk / max(1, cjk + latin / 4) >= 0.3
    words = set(re.findall(r"[a-z]+", answer.lower()))
    if lang == "ms":
        return cjk == 0 and len(words & set(MALAY)) >= 3
    return cjk == 0 and len(words & set(MALAY) - {"minit", "kereta"}) <= 2


PLATE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,3}) ?(\d{1,4})(?: ?([A-Z]{1,2}))?(?![A-Za-z0-9*])")


def grade(case: Dict[str, Any], expected: Dict[str, Any], display: Dict[str, Any], cards: List[str],
          counts: Tuple[int, int]) -> Tuple[Dict[str, float], Dict[str, str]]:
    answer = display.get("text") or ""
    used = [s["tool"] for s in display.get("steps", [])]
    g: Dict[str, float] = {}
    why: Dict[str, str] = {}
    if case.get("tools"):
        g["tools"] = float(any(t in used for t in case["tools"]))
        why["tools"] = f"called {used or 'nothing'}; expected one of {case['tools']}"
    if expected:
        hits = {k: present(v, answer) for k, v in expected.items()}
        g["values"] = sum(hits.values()) / len(hits)
        why["values"] = "; ".join(f"{k}={v!r}: {'found' if hits[k] else 'MISSING'}" for k, v in expected.items())
    if case.get("keywords"):
        miss = [k for k in case["keywords"] if not re.search(k, answer, re.I)]
        g["keywords"] = float(not miss)
        why["keywords"] = f"missing {miss}" if miss else "all found"
    check = display.get("check") or {}
    g["number_check"] = float(bool(answer) and bool(check.get("ok")))
    why["number_check"] = json.dumps({k: check.get(k) for k in ("unmatched", "plates_unmatched", "unknown_refs")},
                                     ensure_ascii=False)
    if case.get("cite"):
        cited = re.findall(r"\[\[([A-Z]:\d+)\]\]", answer)
        g["cite"] = float(bool(cited) and not check.get("unknown_refs"))
        why["cite"] = f"cited {cited}"
    if case.get("card"):
        g["card"] = float(case["card"] in cards and counts[0] == counts[1])
        why["card"] = f"cards {cards}; {case.get('unchanged')} rows {counts[0]} -> {counts[1]}"
    if case.get("read_only"):
        g["read_only"] = float(not cards and counts[0] == counts[1])
        why["read_only"] = f"cards {cards or 'none'}; {case['unchanged']} rows {counts[0]} -> {counts[1]}"
    bad = [p for p in case.get("forbid", []) if re.search(p, answer, re.I)]
    if case.get("masked"):
        asked = re.sub(r"[^A-Z0-9]", "", case["question"].upper())
        bad += [m.group(0) for m in PLATE.finditer(answer) if len(m.group(2)) >= 2
                and "".join(x for x in m.groups() if x) not in asked]
    g["guards"] = float(not bad)
    why["guards"] = f"violations {bad}" if bad else "none"
    g["lang"] = float(language_ok(answer, case["lang"]))
    g["pass"] = float(all(v == 1.0 for v in g.values()))
    return {"pass": g.pop("pass"), **g}, why


# ---------------------------------------------------------------------------- the stand-in models (dry runs)
def oracle_script(case: Dict[str, Any], env: Env, expected: Dict[str, Any]):
    """What an ideal assistant would do: the `ideal` tool calls, then an answer with the expected values."""
    from webapp.copilot.llm import text, tool

    def script(messages, tool_defs):
        if messages[-1]["role"] == "system":
            conn = env.conn()
            try:
                calls = [tool(c["tool"], **{k: env.resolve(conn, v) for k, v in c["args"].items()})
                         for c in case.get("ideal", [])]
            finally:
                conn.close()
            return calls or [text(answer_text([]))]
        payloads = [json.loads(b["content"]) for b in messages[-1]["content"] if b.get("type") == "tool_result"]
        return [text(answer_text(payloads))]

    def answer_text(payloads):
        refs = re.findall(r'"ref":"([A-Z]:\d+)"', json.dumps(payloads, separators=(",", ":")))
        parts = [{"en": "According to the system's records:", "zh": "根据系统的记录，答案如下：",
                  "ms": "Berdasarkan rekod sistem, ini ialah jawapan untuk soalan dan data yang ada:"}[case["lang"]]]
        parts += [str(v) for v in expected.values()]
        for k in case.get("keywords", []):
            parts.append(next((c for c in CANNED if re.search(k, c, re.I)), ""))
        if case.get("cite") and refs:
            parts.append(f"[[{refs[0]}]]")
        return " ".join(p for p in parts if p)
    return script


# ---------------------------------------------------------------------------- running
def harness_sha() -> str:
    h = hashlib.sha256()
    for p in HARNESS:
        h.update(p.read_bytes())
    return h.hexdigest()


def trace(conn, conv_id: Optional[int], role: str, question: str, events: List[Tuple[str, Any]]) -> List[Dict[str, Any]]:
    """The exchange as {role, content} turns: system, user, tool_call, tool_result, assistant."""
    from webapp.copilot import prompts, store
    turns = [{"role": "system", "content": "\n\n".join(b["text"] for b in prompts.system_blocks(role))}]
    msgs = store.transcript(conn, conv_id) if conv_id else []
    if not msgs:
        turns.append({"role": "user", "content": question})
        turns.append({"role": "assistant", "content": "".join(v for k, v in events if k == "text") or
                      json.dumps([e for e in events if e[0] in ("error", "offline")], default=str)})
        return turns
    for m in msgs:
        if m["role"] == "system":
            turns.append({"role": "system", "content": m["content"]})
            continue
        for b in m["content"]:
            t = b.get("type")
            if t == "text":
                turns.append({"role": m["role"], "content": b["text"]})
            elif t == "tool_use":
                turns.append({"role": "tool_call", "name": b["name"], "content": json.dumps(b["input"], indent=1, ensure_ascii=False)})
            elif t == "tool_result":
                turns.append({"role": "tool_result", "content": b["content"]})
    return turns


def run_case(env: Env, case: Dict[str, Any], rep: int, make_llm: Callable[[Dict[str, Any], Dict[str, Any]], Any],
             timeout_s: float) -> Dict[str, Any]:
    from webapp import db
    from webapp.copilot import agent
    conn = env.conn()
    try:
        expected = {k: EXPECTED[k](env, conn) for k in case.get("values", [])}
        table = case.get("unchanged")
        before = db.scalar(conn, f"SELECT COUNT(*) FROM {table}") if table else 0
        u = env.user(conn, case["role"])
        llm = make_llm(case, expected)
        t0 = time.monotonic()
        events = []
        for ev in agent.ask(conn, u, case["question"], llm=llm):
            events.append(ev)
            if time.monotonic() - t0 > timeout_s:
                raise TimeoutError(f"over {timeout_s:.0f} s")
        latency = time.monotonic() - t0
        done = next((v for k, v in events if k == "done"), None)
        err = next((v for k, v in events if k in ("error", "offline")), None)
        if done is None:
            raise RuntimeError(f"no answer: {err}")
        d = done["display"]
        after = db.scalar(conn, f"SELECT COUNT(*) FROM {table}") if table else 0
        kinds = [r["kind"] for r in db.rows(conn, f"SELECT kind FROM copilot_actions WHERE id IN "
                                                  f"({','.join('?' * len(d['cards'])) or 'NULL'})", d["cards"])]
        g, why = grade(case, expected, d, kinds, (before, after))
        status = "truncated" if (d.get("problem") or "").startswith("The answer was cut off") else "ok"
        return {"prompt_id": case["id"], "rep": rep, "prompt": case["question"], "tags": [case["role"], case["lang"]] + case.get("tags", []),
                "status": status, "stop_reason": "refusal" if "declined" in (d.get("problem") or "") else "end_turn",
                "grade": g, "explanation": why, "expected": expected, "answer": d.get("text", ""),
                "model": d.get("model"), "fallback": d.get("fallback"), "usage": d.get("usage", {}),
                "cost_usd": d.get("cost_usd", 0.0), "latency_s": round(latency, 2), "tool_calls": len(d.get("steps", [])),
                "problem": d.get("problem"),
                "_trace": trace(conn, done.get("conversation_id"), case["role"], case["question"], events)}
    finally:
        conn.close()


def wilson(p: float, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, c - h), min(1.0, c + h)


def summarise(rows: List[Dict[str, Any]], errors: int, title: str) -> str:
    ok = [r for r in rows if r["status"] == "ok"]
    n = len(ok)
    rate = sum(r["grade"]["pass"] for r in ok) / n if n else 0.0
    lo, hi = wilson(rate, n)
    cost = [r.get("cost_usd") or 0.0 for r in rows]
    lat = [r.get("latency_s") or 0.0 for r in rows]
    toks = [sum((r.get("usage") or {}).values()) for r in rows]
    metrics = sorted({k for r in ok for k in r["grade"] if k != "pass"})
    out = [f"# {title}", "", f"**Pass rate {rate * 100:.0f} %** ({int(sum(r['grade']['pass'] for r in ok))}/{n} cases; 95 % CI "
           f"{lo * 100:.0f}-{hi * 100:.0f} %) · {errors} errored attempts · {sum(r['status'] == 'truncated' for r in rows)} truncated",
           "", f"Cost {sum(cost):.4f} USD in all; per case median {statistics.median(cost) if cost else 0:.4f}, max "
           f"{max(cost, default=0):.4f} USD · tokens per case median {statistics.median(toks) if toks else 0:.0f} "
           f"(min {min(toks, default=0)}, max {max(toks, default=0)}) · latency median "
           f"{statistics.median(lat) if lat else 0:.1f} s", "",
           "| Metric | Mean over the cases where it applies |", "|---|---|"]
    for m in metrics:
        vals = [r["grade"][m] for r in ok if m in r["grade"]]
        out.append(f"| {m} | {sum(vals) / len(vals) * 100:.0f} % ({len(vals)} cases) |")
    out += ["", "| Case | Role | Lang | Pass | Failed checks | Cost USD | s | Trace |", "|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["tags"][0], r["prompt_id"])):
        failed = ", ".join(k for k, v in r["grade"].items() if k != "pass" and v < 1) or "-"
        out.append(f"| {r['prompt_id']} | {r['tags'][0]} | {r['tags'][1]} | {'yes' if r['grade']['pass'] else 'NO'} | "
                   f"{failed} | {r.get('cost_usd', 0):.4f} | {r.get('latency_s', 0):.1f} | traces/{r['prompt_id']}_rep{r['rep']}.json |")
    return "\n".join(out) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="print the cases as a Markdown table and stop")
    ap.add_argument("--dry-run", choices=("oracle", "null"), help="no API: an ideal stand-in or empty answers")
    ap.add_argument("--pilot", type=int, help="only the first N cases")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--variant", default="baseline", help="baseline or v<N>")
    ap.add_argument("--model", help="model id (default: the copilot's configured model)")
    ap.add_argument("--effort", default=None, choices=("low", "medium", "high"))
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--timeout-s", type=float, default=240.0, help="wall-clock ceiling per case")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "outputs" / "copilot_eval"))
    ap.add_argument("--approve-harness", action="store_true", help="for the user: approve the current harness")
    ap.add_argument("--no-computed-forecasts", action="store_true", help=argparse.SUPPRESS)   # quicker import (tests)
    args = ap.parse_args(argv)
    cases = load_cases()
    if args.list:
        print("| # | Case | Role | Lang | Question | Checks |\n|---|---|---|---|---|---|")
        for i, c in enumerate(cases, 1):
            checks = [f"tools {'/'.join(c['tools'])}" if c.get("tools") else "", f"values {', '.join(c.get('values', []))}"
                      if c.get("values") else "", "keywords" if c.get("keywords") else "", "cite" if c.get("cite") else "",
                      f"card {c['card']}" if c.get("card") else "",
                      f"read-only ({c['unchanged']} unchanged)" if c.get("read_only") else "",
                      "masked" if c.get("masked") else "",
                      "forbid" if c.get("forbid") else ""]
            print(f"| {i} | {c['id']} | {c['role']} | {c['lang']} | {c['question']} | {'; '.join(x for x in checks if x)} |")
        return 0
    if not re.fullmatch(r"baseline|v\d+", args.variant):
        ap.error("--variant must be baseline or v<N>")
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    if args.pilot:
        cases = cases[:args.pilot]
    out_root = Path(args.out)
    variant = f"dry-{args.dry_run}" if args.dry_run else args.variant
    out = out_root / variant
    (out / "traces").mkdir(parents=True, exist_ok=True)
    if not args.dry_run:                                       # paid run: the harness must be approved by the user
        state_path = out_root / "_state.json"
        state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {}
        sha = harness_sha()
        if args.approve_harness:
            state.update(harness_sha=sha, harness_paths=[str(p.relative_to(ROOT)) for p in HARNESS])
            state_path.write_text(json.dumps(state, indent=2), "utf-8")
        elif state.get("harness_sha") != sha:
            print("The harness (runner, cases, prompt or tools) is new or has changed. Review it, then run once with "
                  "--approve-harness (that approval is the user's to give).")
            return 2
    env = Env(Path(tempfile.mkdtemp(prefix="copilot_eval_")), not args.no_computed_forecasts)
    from webapp.copilot import config
    from webapp.copilot.llm import AnthropicLLM, FakeLLM, text
    if args.dry_run == "oracle":
        make = lambda case, exp: FakeLLM(oracle_script(case, env, exp))          # noqa: E731
    elif args.dry_run == "null":
        make = lambda case, exp: FakeLLM(lambda m, t: [text("")])               # noqa: E731
    else:
        ok, why = config.credentials()
        if not ok:
            print(f"No credentials: {why}")
            return 2
        conn = env.conn()
        cfg = config.load(conn)
        conn.close()
        model, effort = args.model or cfg.model, args.effort or cfg.effort
        make = lambda case, exp: AnthropicLLM(model, effort)                     # noqa: E731
    results_path, errors_path = out / "results.jsonl", out / "errors.jsonl"
    done = set()
    if results_path.exists():
        done = {(r["prompt_id"], r["rep"]) for r in map(json.loads, results_path.read_text("utf-8").splitlines()) if r}
    todo = [(c, k) for c in cases for k in range(args.reps) if (c["id"], k) not in done]
    print(f"{len(todo)} attempts to run ({len(done)} already done) -> {out}")

    def attempt(case, rep):
        tries = 0
        while True:
            tries += 1
            try:
                return run_case(env, case, rep, make, args.timeout_s), tries
            except Exception as e:                                               # noqa: BLE001
                from webapp.copilot.llm import LLMUnavailable
                transient = isinstance(e, LLMUnavailable) or "not available" in str(e)
                if transient and tries < 3:
                    time.sleep(5 * tries + random.random() * 3)
                    continue
                kind = "timeout" if isinstance(e, TimeoutError) else "serving" if transient else "harness"
                return {"prompt_id": case["id"], "rep": rep, "error": f"{type(e).__name__}: {e}", "class": kind}, tries
    with cf.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(attempt, c, k): (c, k) for c, k in todo}
        for fut in cf.as_completed(futures):
            row, tries = fut.result()
            if "error" in row:
                with open(errors_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({**row, "attempts": tries}, ensure_ascii=False) + "\n")
                print(f"  ERROR {row['prompt_id']}: {row['error'][:160]}")
                continue
            tr = row.pop("_trace")
            (out / "traces" / f"{row['prompt_id']}_rep{row['rep']}.json").write_text(
                json.dumps(tr, ensure_ascii=False, indent=1), "utf-8")
            row["attempts"] = tries
            with open(results_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            print(f"  {'pass' if row['grade']['pass'] else 'FAIL'}  {row['prompt_id']}  "
                  f"{', '.join(k for k, v in row['grade'].items() if v < 1 and k != 'pass') or ''}")
    rows = [json.loads(x) for x in results_path.read_text("utf-8").splitlines() if x] if results_path.exists() else []
    rows = [r for r in rows if r["prompt_id"] in {c["id"] for c in cases}]
    errs = len(errors_path.read_text("utf-8").splitlines()) if errors_path.exists() else 0
    summary = summarise(rows, errs, f"SECS Copilot evaluation · {variant} · {date.today().isoformat()}")
    (out / "summary.md").write_text(summary, "utf-8")
    print(summary.split("\n\n")[1])
    print(f"Summary: {out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
