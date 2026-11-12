"""Tests of SECS Copilot (webapp/copilot): tool schemas, permission inheritance and masking, the chat loop with a
deterministic fake model, the read-only policy (the four actions refused, nothing changed, no plate revealed), the
dormant action cards (switched on for their tests: nothing written before Confirm), audit entries (via=assistant), the
number check, the SOP and legal sources, generated documents, offline mode, refusals, limits, retention, the optional
image checks, and the request the real Anthropic SDK sends (against a mock transport). No network, no API key.

    python webapp/tests/test_copilot.py          (also runs under pytest)
"""
from __future__ import annotations

import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_web as W  # noqa: E402  (shares the temporary demo database and the login helper)

from webapp import clock, db  # noqa: E402
from webapp.copilot import (actions, agent, config, documents, explain, numbers, offline, prompts, sop,  # noqa: E402
                            store, tools, vlm)
from webapp.copilot.llm import FakeLLM, LLMUnavailable, ModelTurn, clean_blocks, text, tool, turn_cost  # noqa: E402
from webapp.security import Forbidden, User  # noqa: E402

ROOT = W.ROOT
DAY = "jb_ciq_cam02_2023-03-22"


def user(conn, name: str) -> User:
    r = db.row(conn, "SELECT * FROM users WHERE username = ?", (name,))
    return User(r["id"], r["username"], r["display_name"], r["role"], r["site_id"])


def setup():
    st, app = W.fresh()
    from webapp.settings import use
    use(st)
    config.reset_availability()
    return st, app, W.conn_of(st)


def last_tool_result(messages):
    m = messages[-1]
    assert m["role"] == "user" and m["content"][0]["type"] == "tool_result", m
    return json.loads(m["content"][0]["content"])


def first_call(messages) -> bool:
    return messages[-1]["role"] == "system"


def ask(conn, u, question, script, conv=None, page=None):
    fake = FakeLLM(script)
    out = agent.collect(agent.ask(conn, u, question, conv, page, llm=fake))
    return out, fake


@contextmanager
def actions_on():
    """Switch the dormant action cards back on for one test (the copilot is read-only: config.ACTIONS_ENABLED)."""
    saved = config.ACTIONS_ENABLED
    config.ACTIONS_ENABLED = True
    try:
        yield
    finally:
        config.ACTIONS_ENABLED = saved


# ---------------------------------------------------------------------------- tools: schemas and permissions
def test_tool_schemas_are_valid_and_offered_per_role():
    names = set()
    for role in tools.ALL:
        defs = tools.definitions(role)
        assert defs and json.loads(json.dumps(defs)) == defs
        for d in defs:
            names.add(d["name"])
            s = d["input_schema"]
            assert re.fullmatch(r"[a-z_]{3,40}", d["name"]) and len(d["description"]) > 40
            assert s["type"] == "object" and s.get("additionalProperties") is False
            assert set(s.get("required", [])) <= set(s.get("properties", {}))
            assert "title" not in json.dumps(s) and "anyOf" not in json.dumps(s)
        assert len(json.dumps(defs)) < 16000                           # the cached prefix stays small
    by = tools.tools_by_role()
    assert "plate_timeline" in by["police"] and "plate_timeline" not in by["traffic"]
    assert "list_alerts" not in by["traffic"] and "evidence_pack_link" in by["police"]
    assert {n for n in by["guard"] if n.startswith("gate_")} == {"gate_identify", "gate_inside", "gate_access_log", "gate_passes"}
    assert not any(n.startswith("gate_") for n in by["police"] + by["traffic"] + by["admin"])
    # read-only: no role is offered an action tool
    assert not any(n.startswith("prepare_") for n in names) and not any(n.startswith("prepare_") for r in by for n in by[r])
    # the dormant action tools, if they were switched back on: who would get which
    with actions_on():
        on = tools.tools_by_role()
    assert {n for r in on for n in on[r] if n.startswith("prepare_")} == {
        "prepare_visitor_pass", "prepare_list_entry", "prepare_alert_status_change", "prepare_reveal_request"}
    assert "prepare_reveal_request" in on["traffic"] and "prepare_reveal_request" not in on["police"]
    assert not any(n.startswith("prepare_") for n in on["admin"] if n != "prepare_reveal_request")
    # the purpose list of the search is the system's own list
    from webapp.settings import PURPOSES
    s = next(d for d in tools.definitions("police") if d["name"] == "search_plates")["input_schema"]
    assert s["properties"]["purpose"]["enum"] == PURPOSES and "purpose" in s["required"]


def test_tools_inherit_permissions_masking_and_audit():
    st, app, c = setup()
    traffic, police, guard = (replace(user(c, n), via="assistant") for n in ("traffic1", "police1", "guard1"))
    t = tools.Ctx(c, traffic)
    r = tools.execute(t, "search_plates", {"purpose": "Traffic study", "limit": 50})
    assert not r.is_error and r.payload["plates_masked"]
    assert all(x["plate"].endswith("****") for x in r.payload["records"])
    assert all(v["masked"] and v["plate"].endswith("****") for v in t.citations.values() if v["kind"] == "sighting")
    # a search by plate number needs a reveal with a reason: the tool says so and does not search
    r = tools.execute(t, "search_plates", {"purpose": "Traffic study", "plate": "WTQ 9809"})
    assert r.payload.get("needs_reveal") and "records" not in r.payload
    # a tool outside the role is refused even when the model asks for it
    for name in ("plate_timeline", "list_alerts", "gate_inside", "evidence_pack_link"):
        r = tools.execute(t, name, {"plate": "XSD4004"} if name == "plate_timeline" else {})
        assert r.is_error and "not available" in r.payload["error"]
    # guards see only their own gate
    g = tools.Ctx(c, guard)
    plates = {x["plate"] for x in tools.execute(g, "search_plates", {"purpose": "Gate operations", "limit": 50}).payload["records"]}
    assert plates and plates <= {"JPP 3473", "JMT 8577", "XVP 3003", "XBN 4321"}
    # invalid input goes back to the model as an error, nothing runs
    r = tools.execute(tools.Ctx(c, police), "search_plates", {"plate": "XSD 4004", "purpose": "Because"})
    assert r.is_error and "INVALID_JSON" in r.payload
    # every call is in the audit log, marked as made through the assistant, under the signed-in user
    rows = db.rows(c, "SELECT username, action, details FROM audit_log WHERE action IN ('search', 'reveal')")
    via = [json.loads(x["details"]).get("via") for x in rows]
    assert rows and set(via) == {"assistant"} and {x["username"] for x in rows} >= {"traffic1", "guard1"}


def test_withheld_violations_of_real_vehicles_are_never_detailed():
    st, app, c = setup()
    police = replace(user(c, "police1"), via="assistant")
    ctx = tools.Ctx(c, police)
    run = next(r for r in tools.execute(ctx, "traffic_runs", {}).payload["runs"] if r["camera_kind"] == "lane")
    s = tools.execute(ctx, "traffic_summary", {"run_id": run["run_id"]}).payload
    assert s["withheld_real_violations"] == {"SPEEDING": 7, "TAILGATING": 3} and s["withheld_total"] == 10
    ev = tools.execute(ctx, "lane_events", {"run_id": run["run_id"]}).payload
    # violations and hazards shown are only simulated vehicles; real vehicles appear only as lane changes
    assert all(e["simulated"] for e in ev["events"] if e["severity"] in ("violation", "hazard"))
    assert all("plate" not in e for e in ev["events"] if not e["simulated"])
    # the prompt carries the guards
    core = prompts.CORE
    for phrase in ("withheld", "Never reveal a plate yourself", "another role or account", "data-minimisation principle",
                   "does not apply to the Federal and State Governments", "not a finding that an offence happened",
                   "Never invent or add steps", "Do not do arithmetic", "The copilot is read-only",
                   "you cannot prepare a change"):
        assert phrase in core, phrase
    assert all(prompts.system_blocks(r)[-1].get("cache_control") for r in tools.ALL)


# ---------------------------------------------------------------------------- the chat loop
def test_chat_streams_tool_steps_citations_and_stores_an_append_only_transcript():
    st, app, c = setup()
    police = user(c, "police1")

    def script(messages, tool_defs):
        if first_call(messages):
            return [tool("search_plates", purpose="Investigation (case reference)", plate="XSD 4004")]
        res = last_tool_result(messages)
        r = res["records"][-1]
        return [text(f"The system has {res['total']} sightings of {r['plate']}; the first was at {r['seen_at'][11:19]} "
                     f"[[{r['ref']}]]. It is simulated (a fictitious X-series plate).")]
    out, fake = ask(c, police, "Where was XSD 4004 seen?", script, page={"path": "/plates"})
    kinds = [k for k, _ in out["events"]]
    assert kinds[0] == "meta" and "step" in kinds and "step_done" in kinds and "cite" in kinds and kinds[-1] == "done"
    assert kinds.index("step") < kinds.index("text") < kinds.index("done")
    d = out["done"]["display"]
    assert d["check"]["ok"], d["check"]
    ref = re.search(r"\[\[(S:\d+)\]\]", d["text"]).group(1)
    assert d["citations"][ref]["plate"] == "XSD 4004" and d["citations"][ref]["time"]
    assert d["steps"][0]["tool"] == "search_plates" and d["ai"] and out["done"]["message_id"]
    # the request: frozen system prompt with the cache breakpoint, the role's tools, the question and the context
    call = fake.calls[0]
    assert call["system"] == prompts.system_blocks("police") and [t["name"] for t in call["tools"]] == [
        t["name"] for t in tools.definitions("police")]
    assert call["messages"][-1]["role"] == "system" and "Session context" in call["messages"][-1]["content"]
    # a second question replays the stored transcript verbatim (the cached prefix and earlier reasoning stay valid)
    conv = out["done"]["conversation_id"]
    stored = store.transcript(c, conv)
    assert [m["role"] for m in stored] == ["user", "system", "assistant", "user", "assistant"]
    out2, fake2 = ask(c, police, "Thanks. And how many open alerts?", [[text("I would need to check the alert list.")]], conv=conv)
    sent = fake2.calls[0]["messages"]
    assert sent[:len(stored)] == stored and sent[len(stored)]["content"][0]["text"].startswith("Thanks")
    assert fake2.calls[0]["system"] == call["system"] and fake2.calls[0]["tools"] == call["tools"]
    h = store.history(c, police, conv)
    assert [i["role"] for i in h["items"]] == ["user", "assistant", "user", "assistant"]
    # cost per question is logged and linked to the stored answer; the question is audited (no question text)
    rows = db.rows(c, "SELECT * FROM copilot_usage WHERE conversation_id = ?", (conv,))
    assert len(rows) == 3 and {r["message_id"] for r in rows} == {out["done"]["message_id"], out2["done"]["message_id"]}
    a = db.rows(c, "SELECT * FROM audit_log WHERE action = 'copilot_question'")
    assert len(a) == 2 and all(json.loads(x["details"])["via"] == "assistant" for x in a)
    assert "XSD" not in a[0]["details"]
    # another user cannot open the conversation
    try:
        store.history(c, user(c, "traffic1"), conv)
        raise AssertionError("another user read the conversation")
    except LookupError:
        pass


def test_number_check_flags_invented_numbers_and_accepts_rounding():
    data = [{"total": 3, "speed": 113.56, "share": 0.645, "at": "2026-10-14T08:00:23.500", "msg": "1141 km/h needed",
             "plate": "XSD 4004", "lead": {"07:13": 17}}]
    ok = numbers.check("3 sightings of XSD 4004 [[S:12]]: 114 km/h (113.6), 64.5 % of the vehicles, at 08:00:23 on "
                       "2026-10-14; 1,141 km/h needed; warning 17 min before 07:13.", data)
    assert ok["ok"], ok
    bad = numbers.check("1. There were 47 sightings at 09:45 of XSD 4005, about 1,100 km/h.", data)
    assert not bad["ok"] and {u["text"] for u in bad["unmatched"]} == {"47", "09:45", "1,100"}
    assert bad["plates_unmatched"] == ["XSD 4005"]
    assert numbers.check("You asked about 20 % more capacity.", data, question="what if +20%?")["ok"]
    assert numbers.check("Lane In1 and zone TC1; [[A:5]].", data)["ok"]                 # names and refs are not numbers
    # the chat flags an invented number and an unknown citation
    st, app, c = setup()
    police = user(c, "police1")

    def script(messages, tool_defs):
        if first_call(messages):
            return [tool("list_alerts", status="open", limit=3)]
        return [text("There are 99 open alerts, see [[A:999]].")]
    d = ask(c, police, "How many open alerts?", script)[0]["done"]["display"]
    assert not d["check"]["ok"] and d["check"]["unknown_refs"] == ["A:999"]
    assert [u["text"] for u in d["check"]["unmatched"]] == ["99"]


# ---------------------------------------------------------------------------- action cards (dormant: switched on here)
@actions_on()
def test_visitor_pass_card_writes_nothing_until_the_guard_confirms():
    st, app, c = setup()
    guard = user(c, "guard1")
    before = db.scalar(c, "SELECT COUNT(*) FROM visitor_passes")

    def script(messages, tool_defs):
        if first_call(messages):
            return [tool("prepare_visitor_pass", plate="XVK 2201", unit_no="B-05-1", visitor_label="Delivery", hours=3)]
        return [text("I prepared the pass; please check and press Confirm.")]
    out, _ = ask(c, guard, "Register a delivery van XVK 2201 for unit B-05-1, three hours", script)
    card = out["cards"][0]
    assert card["kind"] == "visitor_pass" and card["status"] == "pending"
    assert db.scalar(c, "SELECT COUNT(*) FROM visitor_passes") == before          # nothing written yet
    assert not db.scalar(c, "SELECT COUNT(*) FROM audit_log WHERE action = 'pass_issue'")
    # only the person it was prepared for can confirm it, through the API with the CSRF header
    gc, pc = W.client(app, "guard1"), W.client(app, "police1")
    assert pc.post(f"/api/v1/copilot/actions/{card['id']}/confirm", json={"fields": {}}).status_code == 404
    gc2 = W.TestClient(app)
    gc2.cookies.update(gc.cookies)
    assert gc2.post(f"/api/v1/copilot/actions/{card['id']}/confirm", json={"fields": {}}).status_code == 403   # no CSRF
    r = gc.post(f"/api/v1/copilot/actions/{card['id']}/confirm", json={"fields": {"visitor_label": "Parcel delivery"}})
    assert r.status_code == 200 and r.json()["status"] == "confirmed", r.text
    p = db.row(c, "SELECT * FROM visitor_passes WHERE compact = 'XVK2201'")
    assert p and p["unit_no"] == "B-05-1" and p["visitor_label"] == "Parcel delivery" and p["created_by"] == guard.id
    a = db.row(c, "SELECT * FROM audit_log WHERE action = 'pass_issue'")
    assert a["username"] == "guard1" and json.loads(a["details"])["via"] == "assistant"
    assert db.scalar(c, "SELECT COUNT(*) FROM audit_log WHERE action = 'copilot_action'") == 1
    assert gc.post(f"/api/v1/copilot/actions/{card['id']}/confirm", json={"fields": {}}).status_code == 422   # once only
    assert gc.get("/api/v1/gate/identify", params={"plate": "XVK 2201"}).json()["colour"] == "teal"
    # a card expires
    late = actions.prepare(c, guard, "visitor_pass", {"plate": "XVK 2202", "unit_no": "B-05-2"})
    clock.set_now(clock.now() + timedelta(minutes=31))
    try:
        assert gc.post(f"/api/v1/copilot/actions/{late['id']}/confirm", json={"fields": {}}).status_code == 422
    finally:
        clock.set_now(None)
    assert db.scalar(c, "SELECT status FROM copilot_actions WHERE id = ?", (late["id"],)) == "expired"


@actions_on()
def test_alert_and_reveal_cards_need_the_users_note_and_reason():
    st, app, c = setup()
    police, traffic = user(c, "police1"), user(c, "traffic1")
    aid = db.scalar(c, "SELECT id FROM alerts WHERE code = 'BLACKLIST' ORDER BY id LIMIT 1")
    card = actions.prepare(c, police, "alert_status", {"alert_id": aid, "to": "resolved"})
    assert [f["name"] for f in card["fields"] if f["required"]] == ["note"]
    pc = W.client(app, "police1")
    assert pc.post(f"/api/v1/copilot/actions/{card['id']}/confirm", json={"fields": {}}).status_code == 422
    assert db.scalar(c, "SELECT status FROM alerts WHERE id = ?", (aid,)) == "new"
    r = pc.post(f"/api/v1/copilot/actions/{card['id']}/confirm", json={"fields": {"note": "Checked the crop; patrol informed"}})
    assert r.status_code == 200 and db.scalar(c, "SELECT status FROM alerts WHERE id = ?", (aid,)) == "resolved"
    h = db.row(c, "SELECT * FROM audit_log WHERE action = 'alert_resolved'")
    assert h["username"] == "police1" and json.loads(h["details"])["via"] == "assistant"
    # a transition the alert does not allow is refused when preparing
    try:
        actions.prepare(c, police, "alert_status", {"alert_id": aid, "to": "resolved"})
        raise AssertionError("prepared an impossible transition")
    except ValueError:
        pass
    # reveal: the masked user picks the reason on the card; afterwards searching by that plate works for 15 min
    card = actions.prepare(c, traffic, "reveal", {"plate": "WTQ 9809"})
    tc = W.client(app, "traffic1")
    assert tc.post(f"/api/v1/copilot/actions/{card['id']}/confirm", json={"fields": {}}).status_code == 422
    assert W.search(tc, plate="WTQ 9809").status_code == 403
    r = tc.post(f"/api/v1/copilot/actions/{card['id']}/confirm", json={"fields": {"reason": "Incident review"}})
    assert r.status_code == 200, r.text
    rev = db.row(c, "SELECT * FROM audit_log WHERE action = 'reveal' AND username = 'traffic1'")
    assert rev["reason"] == "Incident review" and json.loads(rev["details"])["via"] == "assistant"
    res = W.search(tc, plate="WTQ 9809", purpose="Traffic study")
    assert res.status_code == 200 and {s["plate"] for s in res.json()["items"]} == {"WTQ 9809"}
    n = db.scalar(c, "SELECT COUNT(*) FROM reveals WHERE user_id = ?", (traffic.id,))
    W.search(tc, plate="WTQ 9809", purpose="Traffic study")
    assert db.scalar(c, "SELECT COUNT(*) FROM reveals WHERE user_id = ?", (traffic.id,)) == n    # not silently extended
    # police already see full plates: no reveal cards for them
    try:
        actions.prepare(c, police, "reveal", {"plate": "WTQ 9809"})
        raise AssertionError("police got a reveal card")
    except Exception as e:                                                     # noqa: BLE001
        assert "already sees full plates" in str(e)


# ---------------------------------------------------------------------------- the read-only policy (plan Rev2)
def test_copilot_is_read_only_the_four_actions_are_refused_and_nothing_changes():
    st, app, c = setup()
    assert not config.ACTIONS_ENABLED and config.status(c)["actions_enabled"] is False
    business = ("visitor_passes", "list_entries", "alert_actions", "reveals", "corrections")
    writes = ("pass_issue", "list_add", "alert_acknowledged", "alert_resolved", "alert_false_alarm", "reveal")

    def counts():
        return ({t: db.scalar(c, f"SELECT COUNT(*) FROM {t}") for t in business},
                db.scalar(c, f"SELECT COUNT(*) FROM audit_log WHERE action IN ({','.join('?' * len(writes))})", writes),
                db.rows(c, "SELECT id, status FROM alerts ORDER BY id"))
    before = counts()
    aid = db.scalar(c, "SELECT id FROM alerts WHERE code = 'BLACKLIST' ORDER BY id LIMIT 1")
    attempts = [("guard1", "prepare_visitor_pass", "visitor_pass", {"plate": "XVK 2201", "unit_no": "B-05-1", "hours": 3}),
                ("police1", "prepare_list_entry", "list_entry", {"list": "watch", "plate": "XSN 7310", "reason": "case IP/2026/114"}),
                ("police1", "prepare_alert_status_change", "alert_status", {"alert_id": aid, "to": "resolved", "note": "checked"}),
                ("traffic1", "prepare_reveal_request", "reveal", {"plate": "WTQ 9809"})]
    for name, tool_name, kind, args in attempts:
        u = user(c, name)
        assert tool_name not in [d["name"] for d in tools.definitions(u.role)]          # not offered to the model

        def script(messages, tool_defs, tool_name=tool_name, args=args):
            if first_call(messages):
                assert tool_name not in [d["name"] for d in tool_defs]
                return [tool(tool_name, **args)]                                    # a model that calls it anyway
            assert "read-only" in last_tool_result(messages)["error"]
            return [text("The copilot is read-only: please do this on the page itself.")]
        out, _ = ask(c, u, "Please do it for me.", script)
        steps = out["done"]["display"]["steps"]
        assert not out["cards"] and steps[0]["tool"] == tool_name and steps[0]["error"]
        try:                                                                         # the service refuses too
            actions.prepare(c, u, kind, args)
            raise AssertionError(f"a {kind} card was prepared")
        except Forbidden as e:
            assert "read-only" in str(e)
    # a card prepared before the change cannot be confirmed, through the API or directly; cancelling it still works
    with actions_on():
        old = actions.prepare(c, user(c, "guard1"), "visitor_pass", {"plate": "XVK 2203", "unit_no": "B-05-3"})
    gc = W.client(app, "guard1")
    r = gc.post(f"/api/v1/copilot/actions/{old['id']}/confirm", json={"fields": {}})
    assert r.status_code == 403 and "read-only" in r.json()["detail"]
    try:
        actions.confirm(c, user(c, "guard1"), old["id"])
        raise AssertionError("a card was confirmed while read-only")
    except Forbidden:
        pass
    assert db.scalar(c, "SELECT status FROM copilot_actions WHERE id = ?", (old["id"],)) == "pending"
    assert gc.post(f"/api/v1/copilot/actions/{old['id']}/cancel").json()["status"] == "cancelled"
    # a masked role gets no full plate: the search says where to ask for one, and the plate stays masked
    t = tools.Ctx(c, replace(user(c, "traffic1"), via="assistant"))
    r = tools.execute(t, "search_plates", {"purpose": "Traffic study", "plate": "WTQ 9809"})
    assert r.payload["needs_reveal"] and "records" not in r.payload and "Show full plate" in r.payload["message"]
    assert "prepare_" not in r.payload["message"] and W.search(W.client(app, "traffic1"), plate="WTQ 9809").status_code == 403
    # nothing changed: no business row, no write in the audit log, every alert keeps its status
    assert counts() == before
    # what the model and the panel are told
    assert not any("prepare_" in b["text"] for r_ in tools.ALL for b in prompts.system_blocks(r_))
    s = W.client(app, "police1").get("/api/v1/copilot/status").json()
    assert s["actions_enabled"] is False and not any(n.startswith("prepare_") for n in s["tools"])
    # audit rows may be added (the questions and searches are logged) and the chain still verifies
    assert db.scalar(c, "SELECT COUNT(*) FROM audit_log WHERE action = 'copilot_question'") >= len(attempts)
    assert W.client(app, "admin1").get("/api/v1/audit/verify").json()["ok"]


# ---------------------------------------------------------------------------- explanations, SOP, legal
def test_sop_file_is_complete_bilingual_and_marked_as_a_draft_until_reviewed():
    assert sop.validate() == []
    data = sop.load()
    from webapp.services.common import ALERT_TITLE
    police_codes = set(ALERT_TITLE) - {"BANNED_AT_SITE", "LANE_CHANGE"}
    assert police_codes <= set(data["sops"]), police_codes - set(data["sops"])
    assert all("police" in data["sops"][c] for c in police_codes) and "guard" in data["sops"]["BANNED_AT_SITE"]
    s = sop.steps("SPEEDING", "police")
    assert s["found"] and len(s["en"]) == len(s["zh"]) >= 3 and s["status"] in ("draft", "reviewed")
    assert sop.steps("BLACKLIST", "admin")["scope"] == "role default"
    assert not sop.steps("SPEEDING", "traffic")["found"]
    text_all = json.dumps(data, ensure_ascii=False).lower()
    for word in ("guilty", "convict", "highly likely", "高度疑似"):
        assert word not in text_all


def test_explain_alert_uses_the_stored_values_the_sop_and_only_verified_legal_sections():
    st, app, c = setup()
    police, admin = user(c, "police1"), user(c, "admin1")
    aid = db.scalar(c, "SELECT id FROM alerts WHERE code = 'SPEEDING'")
    ev = db.row(c, "SELECT * FROM lane_events WHERE code = 'SPEEDING'")
    f = explain.facts(c, police, aid)
    assert f["derived"]["lower_95_kmh"] == round(ev["value"] - 2 * ev["error_1sigma"], 1)
    assert f["derived"]["lower_95_above_limit_kmh"] == round(ev["value"] - 2 * ev["error_1sigma"] - 80.0, 1)
    assert any("ASSUMED" in x["en"] for x in f["caveats"]) and any("Simulated" in x["en"] for x in f["caveats"])
    assert f["sop"]["en"] == sop.steps("SPEEDING", "police")["en"]
    legal = W.yaml.safe_load((ROOT / "webapp" / "legal.yaml").read_text("utf-8"))
    assert [s["section"] for s in f["legal"]["sections"]] == [legal["sections"][k]["section"] for k in legal["alerts"]["SPEEDING"]["sections"]]
    # an admin sees the alert (masked) but gets the admin SOP default and no registry record
    blk = db.scalar(c, "SELECT id FROM alerts WHERE code = 'BLACKLIST' ORDER BY id LIMIT 1")
    fa = explain.facts(c, admin, blk)
    assert fa["alert"]["plate"].endswith("****") and fa["sop"]["scope"] == "role default"
    assert not any(v["source"] == "SIMULATED registry" for v in fa["values"])
    # the travel facts of a cloned plate are the timeline's numbers
    dup = db.scalar(c, "SELECT id FROM alerts WHERE code = 'DUPLICATE' AND message LIKE 'seen at%' ORDER BY id LIMIT 1")
    fd = explain.facts(c, police, dup)
    assert fd["derived"]["speed_needed_kmh"] > 250 and fd["derived"]["max_speed_kmh"] == 250
    # offline: the template explains from the same facts and passes the number check; with a model, its text is checked
    os.environ["SECS_COPILOT_OFFLINE"] = "1"
    try:
        r = documents.collect(documents.explain_alert(c, police, aid, "zh"))
    finally:
        os.environ.pop("SECS_COPILOT_OFFLINE")
    assert r["done"]["offline"] and r["done"]["check"]["ok"] and "如何计算" in r["text"]
    fake = FakeLLM([[text(f"The speed was {ev['value']:.0f} km/h with a lower bound of 98.9 km/h, above the assumed "
                          f"80 km/h limit; it means the driver is guilty of 42 offences.")]])
    r = documents.collect(documents.explain_alert(c, police, aid, "en", llm=fake))
    assert r["done"]["ai"] and [u["text"] for u in r["done"]["check"]["unmatched"]] == ["42"]
    sent = json.dumps(fake.calls[0]["messages"])                  # the SOP and the legal text are not the model's to write
    assert "steps_en" not in sent and "Exceeding speed limit" not in sent and sop.steps("SPEEDING", "police")["en"][0] not in sent
    assert db.scalar(c, "SELECT COUNT(*) FROM copilot_documents WHERE kind = 'alert_explanation'") == 2


# ---------------------------------------------------------------------------- generated documents
def test_case_summary_is_added_to_the_evidence_pack_and_marked():
    st, app, c = setup()
    police = user(c, "police1")
    aid = db.scalar(c, "SELECT id FROM alerts WHERE code = 'DUPLICATE' AND provenance = 'inserted_plate' ORDER BY id LIMIT 1")
    f = documents.case_facts(c, police, aid)
    assert f["simulated"] and f["timeline"]["legs"] and f["legal_sections"]
    fake = FakeLLM([[text("## Summary\n- Alert #%d: %s, %s.\n## What the system computed\n- %s" % (
        aid, f["alert"]["plate"], f["alert"]["seen_at"][:19], f["timeline"]["legs"][0]))]])
    r = documents.collect(documents.case_summary(c, police, aid, "en", llm=fake))
    assert r["done"]["ai"] and r["done"]["check"]["ok"], r["done"]["check"]
    pc = W.client(app, "police1")
    html = pc.get(f"/api/v1/alerts/{aid}/evidence", params={"format": "html"}).text
    assert "AI-generated case summary (draft, not checked by a person)" in html and "every number was found" in html
    assert "not a finding that an offence happened" in html and html.count("COURSE PROJECT DEMO") >= 2
    assert pc.get(f"/api/v1/alerts/{aid}/evidence", params={"format": "pdf"}).content.startswith(b"%PDF")
    tc = W.client(app, "traffic1")
    assert tc.post(f"/api/v1/copilot/alerts/{aid}/case-summary", json={"lang": "en"}).status_code == 403


def test_daily_review_facts_template_number_check_and_pdf():
    st, app, c = setup()
    traffic = user(c, "traffic1")
    f = documents.review_facts(c, traffic)
    assert f["flows"]["vehicles"] == 374 and abs(sum(f["flows"]["by_type_share_pct"].values()) - 100) < 0.5
    cong = f["congestion"]["by_section"]["To CIQ"]
    assert [e["start"] for e in cong["episodes"]] == ["07:13", "08:17"]          # = the forecast page's onsets
    assert f["congestion"]["longest_queueing_zone"] == "TC1"
    h30 = next(h for h in f["forecast"]["by_horizon"] if h["horizon_min"] == 30)
    assert list(h30["warning_lead_min"].values()) == [17, 3]                    # TEAM_GUIDE.md 10.5
    assert [w["capacity_increase_pct"] for w in f["what_if"]] == [10.0, 20.0, 30.0]
    assert all(w["minutes_over_threshold_saved"] >= 0 for w in f["what_if"]) and f["what_if"][0]["assumptions"]
    os.environ["SECS_COPILOT_OFFLINE"] = "1"
    try:
        r = documents.collect(documents.daily_review(c, traffic, lang="en"))
    finally:
        os.environ.pop("SECS_COPILOT_OFFLINE")
    d = r["done"]
    assert d["offline"] and d["check"]["ok"], d["check"]
    assert "Suggestions for tomorrow" in d["text"] and "Suggestion (estimate under stated assumptions)" in d["text"]
    tc = W.client(app, "traffic1")
    pdf = tc.get(f"/api/v1/copilot/documents/{d['id']}.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert W.client(app, "guard1").get(f"/api/v1/copilot/documents/{d['id']}.pdf").status_code == 404


# ---------------------------------------------------------------------------- offline mode, refusals, limits
def test_offline_mode_answers_quick_questions_with_labelled_templates():
    st, app, c = setup()
    config._state.update(checked=1e18, creds=(False, "no API key: set ANTHROPIC_API_KEY"))   # as without a key
    try:
        police = user(c, "police1")
        out = agent.collect(agent.ask(c, police, "Where was XSD 4004 seen?"))
        assert out["offline"] and "no API key" in out["offline"]["reason"] and out["offline"]["quick"]
        assert not db.scalar(c, "SELECT COUNT(*) FROM copilot_usage")
        for name in ("police1", "guard1", "traffic1", "admin1"):
            u = user(c, name)
            for q in offline.quick_questions(u.role):
                for lang in ("en", "zh"):
                    a = offline.answer(c, u, q["id"], lang)
                    assert a["text"] and not a["problem"] and not a["ai"] and a["offline"], (name, q["id"])
                    assert a["label"] == offline.LABEL[lang]
                    for ref in re.findall(r"\[\[([A-Z]:\d+)\]\]", a["text"]):
                        assert ref in a["citations"], (q["id"], ref)
        a = offline.answer(c, user(c, "traffic1"), "lane_violations", "en")
        assert "withheld" in a["text"] and all(v["masked"] for v in a["citations"].values() if v["plate"])
        tc = W.client(app, "traffic1")
        s = tc.get("/api/v1/copilot/status").json()
        assert not s["online"] and {q["id"] for q in s["quick"]} >= {"flow_today", "congestion", "whatif20"}
        r = tc.post("/api/v1/copilot/chat", json={"message": "hello"})
        assert r.status_code == 200 and "event: offline" in r.text
        assert tc.post("/api/v1/copilot/quick", json={"id": "audit_check"}).status_code == 404     # admin only
    finally:
        config.reset_availability()
    # forced offline and an exhausted budget are offline too
    config._state.update(checked=1e18, creds=(True, ""))
    try:
        assert config.status(c)["online"]
        os.environ["SECS_COPILOT_OFFLINE"] = "1"
        assert not config.status(c)["online"]
        os.environ.pop("SECS_COPILOT_OFFLINE")
        store.log_usage(c, user(c, "police1"), "chat", "claude-opus-5-5", {}, 999.0)
        assert "budget" in config.status(c)["reason"]
    finally:
        os.environ.pop("SECS_COPILOT_OFFLINE", None)
        config.reset_availability()


def test_refusals_and_failures_never_corrupt_the_transcript():
    st, app, c = setup()
    police = user(c, "police1")
    ok, _ = ask(c, police, "How many open alerts?", lambda m, t: [tool("list_alerts")] if first_call(m) else
                [text(f"{last_tool_result(m)['counts']['open']} open alerts.")])
    conv = ok["done"]["conversation_id"]
    n = len(store.transcript(c, conv))
    out, _ = ask(c, police, "Tell me about the real vehicles that were speeding.", [[{"type": "refusal"}]], conv=conv)
    assert out["error"] and "declined" in out["error"]["message"] and len(store.transcript(c, conv)) == n

    class Down(FakeLLM):
        def stream(self, **kw):
            raise LLMUnavailable("the model service could not be reached")
            yield  # noqa
    out = agent.collect(agent.ask(c, police, "Anything new?", conv, llm=Down([])))
    assert "not available" in out["error"]["message"] and len(store.transcript(c, conv)) == n
    # a model that keeps calling tools is stopped after max_steps, and nothing half-done is stored
    out, fake = ask(c, police, "Loop", lambda m, t: [tool("privacy_rules")], conv=conv)
    assert len(fake.calls) == config.MAX_STEPS and "steps" in out["done"]["display"]["problem"]
    assert len(store.transcript(c, conv)) == n
    # the next question still works on a clean transcript
    out, fake = ask(c, police, "Thanks", [[text("You are welcome.")]], conv=conv)
    assert out["done"]["message_id"] and fake.calls[0]["messages"][:n] == store.transcript(c, conv)[:n]
    # a new conversation whose first question failed is not kept
    before = db.scalar(c, "SELECT COUNT(*) FROM copilot_conversations")
    agent.collect(agent.ask(c, police, "x", llm=Down([])))
    assert db.scalar(c, "SELECT COUNT(*) FROM copilot_conversations") == before


def test_rate_limit_cost_logging_and_prices():
    st, app, c = setup()
    traffic = user(c, "traffic1")
    db.set_setting(c, "copilot_questions_per_hour", 2)
    for _ in range(2):
        out, _ = ask(c, traffic, "Hello", [[text("Hello.")]])
        assert out["done"]["message_id"]
    out, fake = ask(c, traffic, "Hello again", [[text("Hello.")]])
    assert "limit of 2" in out["error"]["message"] and not fake.calls
    assert store.requests_last_hour(c, traffic) == 2
    # prices: Opus 5.5 is 4 / 20 USD per million tokens, cache reads 0.20, 5-minute cache writes 5.00
    u = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 2000}
    assert abs(config.cost_usd("claude-opus-5-5", u) - (1000 * 4 + 500 * 20 + 5000 * 0.2 + 2000 * 5) / 1e6) < 1e-12
    assert config.cost_usd("claude-haiku-5-5", u) < config.cost_usd("claude-sonnet-5-5", u) < config.cost_usd("claude-opus-5-5", u)
    # after a refusal fallback each attempt is billed at the price of the model that ran it
    usage = {"input_tokens": 100, "output_tokens": 10, "iterations": [
        {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 0},
        {"type": "fallback_message", "model": "claude-opus-5", "input_tokens": 1000, "output_tokens": 100}]}
    assert abs(turn_cost("claude-opus-5", usage) - (1000 * 4 + 1000 * 5 + 100 * 25) / 1e6) < 1e-12
    # thinking blocks before a fallback switch point are not replayed
    blocks = [{"type": "thinking", "thinking": "", "signature": "a"}, {"type": "text", "text": "partial", "citations": None},
              {"type": "fallback", "from": {"model": "x"}, "to": {"model": "y"}}, {"type": "thinking", "thinking": "", "signature": "b"},
              {"type": "text", "text": "answer"}]
    assert clean_blocks(blocks) == [{"type": "text", "text": "partial"}, {"type": "thinking", "thinking": "", "signature": "b"},
                                    {"type": "text", "text": "answer"}]


def test_conversation_and_card_retention():
    st, app, c = setup()
    police = user(c, "police1")
    out, _ = ask(c, police, "Hi", [[text("Hello.")]])
    conv = out["done"]["conversation_id"]
    admin = W.client(app, "admin1")
    assert admin.post("/api/v1/retention/purge", params={"dry_run": True}).json()["copilot"] == 0
    clock.set_now(clock.now() + timedelta(days=31))
    try:
        admin = W.client(app, "admin1")
        rep = admin.post("/api/v1/retention/purge", params={"dry_run": False}).json()
        assert rep["copilot"] >= 1 and not db.scalar(c, "SELECT COUNT(*) FROM copilot_conversations WHERE id = ?", (conv,))
        assert not db.scalar(c, "SELECT COUNT(*) FROM copilot_messages WHERE conversation_id = ?", (conv,))
        assert db.scalar(c, "SELECT COUNT(*) FROM copilot_usage")              # cost rows (no personal data) stay longer
    finally:
        clock.set_now(None)
    assert admin.get("/api/v1/audit/verify").json()["ok"]


# ---------------------------------------------------------------------------- optional image checks
def test_image_checks_are_off_by_default_police_only_audited_and_neutral():
    import cv2
    import numpy as np
    st, app, c = setup()
    police = user(c, "police1")
    sid = db.scalar(c, "SELECT id FROM sightings WHERE status = 'TENTATIVE'")
    pc, ac = W.client(app, "police1"), W.client(app, "admin1")
    assert pc.post(f"/api/v1/copilot/vlm/second-reading/{sid}").status_code == 403          # off by default
    assert W.client(app, "traffic1").put("/api/v1/copilot/settings", json={"vlm_enabled": True}).status_code == 403
    assert ac.put("/api/v1/copilot/settings", json={"vlm_enabled": True}).json()["vlm_enabled"]
    assert db.row(c, "SELECT * FROM audit_log WHERE action = 'copilot_settings'")["username"] == "admin1"
    # CI has no camera crops: give the provisional sighting and a TYPE_MISMATCH vehicle a saved image
    crop = np.full((40, 120, 3), 200, np.uint8)
    cv2.imwrite(str(st.media_dir / "test_crop.jpg"), crop)
    c.execute("UPDATE sightings SET crop_path = 'test_crop.jpg' WHERE id = ?", (sid,))
    fake = FakeLLM([[text(json.dumps({"plate_visible": True, "reading": "VCV 1555", "confidence": "medium",
                                      "uncertain_characters": "the last 5 may be a 6", "why_hard": "motion blur"}))]])
    r = vlm.second_reading(c, police, sid, llm=fake)
    assert r["status"] == "proposed" and r["result"]["reading"] == "VCV 1555" and len(r["image_sha256"]) == 64
    sent = fake.calls[0]["messages"][0]["content"]
    assert sent[0]["type"] == "image" and fake.calls[0]["output_schema"] == vlm.READING_SCHEMA
    a = db.row(c, "SELECT * FROM audit_log WHERE action = 'vlm_second_reading'")
    assert json.loads(a["details"])["sent_image_sha256"] == r["image_sha256"] and "base64" not in a["details"]
    assert "data" not in db.scalar(c, "SELECT result FROM copilot_vlm WHERE id = ?", (r["id"],))
    d = vlm.decide(c, police, r["id"], True, "VCV 1556")
    assert d["corrected_plate"] == "VCV1556" and db.row(c, "SELECT * FROM corrections")["corrected_compact"] == "VCV1556"
    # appearance check: the system compares the model's description with the SIMULATED record; neutral wording
    aid = db.scalar(c, "SELECT id FROM alerts WHERE code = 'TYPE_MISMATCH' AND provenance = 'inserted_plate'")
    s2 = db.scalar(c, "SELECT sighting_id FROM alerts WHERE id = ?", (aid,))
    cv2.imwrite(str(st.media_dir / "test_scene.jpg"), np.full((200, 300, 3), 120, np.uint8))
    c.execute("UPDATE sightings SET scene_path = 'test_scene.jpg', scene_info = ? WHERE id = ?",
              (json.dumps({"vehicle_box": [50, 40, 250, 180]}), s2))
    fake = FakeLLM([[text(json.dumps({"vehicle_visible": True, "vehicle_type": "Car", "body_style": "hatchback",
                                      "colour": "Silver", "make_model_guess": "Perodua Myvi", "confidence": "medium",
                                      "notes": ""}))]])
    r = vlm.appearance(c, police, aid, llm=fake)
    res = r["result"]
    assert res["verdict"] == "differs" and res["note"]["zh"] == "外观与登记不符，需人工核实。"
    assert {x["field"] for x in res["comparison"]} >= {"Vehicle type", "Colour", "Make / model"}
    assert "高度疑似" not in json.dumps(res, ensure_ascii=False) and "cloned" not in res["note"]["en"].lower()
    assert db.scalar(c, "SELECT COUNT(*) FROM audit_log WHERE action = 'vlm_appearance'") == 1
    synth = db.scalar(c, "SELECT id FROM alerts WHERE code = 'TYPE_MISMATCH' AND provenance = 'synthetic'")
    try:
        vlm.appearance(c, police, synth, llm=fake)
        raise AssertionError("sent a synthetic event")
    except ValueError as e:
        assert "synthetic" in str(e)
    assert W.client(app, "traffic1").post(f"/api/v1/copilot/vlm/appearance/{aid}").status_code == 403


# ---------------------------------------------------------------------------- the API and the pages
def test_copilot_api_pages_and_streamed_chat():
    st, app, c = setup()
    for name in ("police1", "guard1", "traffic1", "admin1"):
        cl = W.client(app, name)
        assert cl.get("/assistant", headers={"accept": "text/html"}).status_code == 200
        assert 'src="/static/js/copilot.js"' in cl.get("/assistant", headers={"accept": "text/html"}).text
        s = cl.get("/api/v1/copilot/status").json()
        assert s["model"] == "claude-opus-5-5" and not s["vlm_enabled"] and s["tools"] == tools.tools_by_role()[cl.get(
            "/api/v1/auth/me").json()["user"]["role"]]
    assert W.client(app, "police1").get("/api/v1/copilot/settings").status_code == 403
    st_admin = W.client(app, "admin1").get("/api/v1/copilot/settings").json()
    assert [m["id"] for m in st_admin["models"]] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    assert W.client(app, "admin1").put("/api/v1/copilot/settings", json={"model": "gpt-4"}).status_code == 422
    # streamed chat through the API with the fake model standing in for Claude
    fake = FakeLLM(lambda m, t: [tool("gate_inside")] if first_call(m) else
                   [text(f"{last_tool_result(m)['count']} visitors inside, {last_tool_result(m)['overstays']} overstaying.")])
    real_make, real_creds = agent.make_llm, config.credentials
    agent.make_llm, config.credentials = (lambda cfg: fake), (lambda: (True, ""))
    try:
        g = W.client(app, "guard1")
        r = g.post("/api/v1/copilot/chat", json={"message": "Who is inside?", "page": {"path": "/gate", "evil": "x"}})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = [(m.group(1), json.loads(m.group(2))) for m in re.finditer(r"event: (\w+)\ndata: (.*)\n\n", r.text)]
        kinds = [k for k, _ in events]
        assert kinds[0] == "meta" and kinds[-1] == "done" and "step" in kinds
        done = events[-1][1]
        assert done["display"]["check"]["ok"] and "visitors inside" in done["display"]["text"]
        assert done["display"]["citations"] and all(k.startswith("G:") for k in done["display"]["citations"])
        assert "evil" not in json.dumps(fake.calls[0]["messages"][-1])
        conv = done["conversation_id"]
        assert [x["id"] for x in g.get("/api/v1/copilot/conversations").json()] == [conv]
        assert W.client(app, "police1").get(f"/api/v1/copilot/conversations/{conv}").status_code == 404
        assert W.client(app, "police1").delete(f"/api/v1/copilot/conversations/{conv}").status_code == 404
        assert g.delete(f"/api/v1/copilot/conversations/{conv}").json()["ok"]
        assert g.post("/api/v1/copilot/chat", json={"message": ""}).text.count("event: error") == 1
    finally:
        agent.make_llm, config.credentials = real_make, real_creds
        config.reset_availability()


def test_gate_inside_includes_usable_citations():
    st, app, c = setup()
    guard = user(c, "guard1")
    ctx = tools.Ctx(c, replace(guard, via="assistant"))
    res = tools.execute(ctx, "gate_inside", {})
    assert not res.is_error and res.payload["count"] >= 1 and res.payload["visitors"]
    for v in res.payload["visitors"]:
        ref = v.get("ref")
        assert ref and ref.startswith("G:") and ref in ctx.citations
        cit = ctx.citations[ref]
        assert cit["kind"] == "access" and cit["id"] > 0 and cit["plate"] == v["plate"] and cit["link"] == "/gate/log"
    q = W.client(app, "guard1").post("/api/v1/copilot/quick", json={"id": "inside", "lang": "en"}).json()
    assert q["citations"] and all(k.startswith("G:") and v["plate"] and v["link"] == "/gate/log"
                                  for k, v in q["citations"].items())
    assert all(f"[[{k}]]" in q["text"] for k in q["citations"])


def test_conversation_history_enforces_current_permissions_after_reveal_expiry_and_role_downgrade():
    st, app, c = setup()
    # 1. Temporary reveal expiry: traffic1 reveals WTQ 9809, asks Copilot, then the reveal expires.
    tc = W.client(app, "traffic1")
    rev = W.search(tc, plate="WTQ 9809", purpose="Incident review", reveal_reason="Incident review")
    assert rev.status_code == 200 and {x["plate"] for x in rev.json()["items"]} == {"WTQ 9809"}
    traffic = user(c, "traffic1")

    def t_script(messages, tool_defs):
        if first_call(messages):
            return [tool("search_plates", purpose="Traffic study", plate="WTQ 9809")]
        rec = last_tool_result(messages)["records"][0]
        return [text(f"Sighting [[{rec['ref']}]] shows {rec['plate']}.")]
    out, _ = ask(c, traffic, "Where was WTQ 9809 seen?", t_script)
    conv_t = out["done"]["conversation_id"]
    ref_t = next(iter(out["done"]["display"]["citations"]))
    # While the reveal is still active, the conversation shows the full plate.
    h_active = tc.get(f"/api/v1/copilot/conversations/{conv_t}").json()
    assert h_active["items"][-1]["citations"][ref_t]["plate"] == "WTQ 9809"
    assert not h_active["items"][-1]["citations"][ref_t]["masked"]
    # Expire the temporary reveal and check normal query + conversation history + citations + transcript replay.
    clock.set_now(clock.now() + timedelta(minutes=20))
    try:
        assert W.search(tc, plate="WTQ 9809", purpose="Incident review").status_code == 403
        convs = tc.get("/api/v1/copilot/conversations").json()
        assert "9809" not in json.dumps(convs) and "WTQ ****" in convs[0]["title"]
        h_exp = tc.get(f"/api/v1/copilot/conversations/{conv_t}").json()
        raw_exp = json.dumps(h_exp)
        assert "WTQ 9809" not in raw_exp and "9809" not in raw_exp
        cit_exp = h_exp["items"][-1]["citations"][ref_t]
        assert cit_exp["plate"] == "WTQ ****" and cit_exp["masked"] is True
        assert "?v=full" not in (cit_exp.get("crop_url") or "")
        tr_exp = store.transcript(c, conv_t)
        assert "WTQ 9809" not in json.dumps(tr_exp) and "9809" not in json.dumps(tr_exp)
        out_follow, fake_follow = ask(c, traffic, "Can you summarize the earlier sighting?",
                                      [[text(f"Earlier sighting [[{ref_t}]] is masked.")]], conv=conv_t)
        assert "WTQ 9809" not in json.dumps(fake_follow.calls[0]["messages"])
        assert out_follow["done"]["display"]["citations"][ref_t]["plate"] == "WTQ ****"
    finally:
        clock.set_now(None)

    # 2. Role downgrade: police1 asks about XSD 4004, then is downgraded to traffic and logs in again.
    police = user(c, "police1")
    aid = db.scalar(c, "SELECT id FROM alerts WHERE compact = 'XSD4004' ORDER BY id LIMIT 1")

    def p_script(messages, tool_defs):
        if first_call(messages):
            return [tool("search_plates", purpose="Alert follow-up", plate="XSD 4004"),
                    tool("get_alert", alert_id=aid)]
        rec = last_tool_result(messages)["records"][0] if "records" in last_tool_result(messages) else \
            json.loads(messages[-2]["content"][0]["content"])["records"][0]
        return [text(f"Plate XSD 4004 was seen in [[{rec['ref']}]] with alert [[A:{aid}]].")]
    out_p, _ = ask(c, police, "Check XSD 4004 and its alert", p_script)
    conv_p = out_p["done"]["conversation_id"]
    ac = W.client(app, "admin1")
    r_down = ac.patch(f"/api/v1/users/{police.id}", json={"role": "traffic"})
    assert r_down.status_code == 200 and r_down.json()["role"] == "traffic" and r_down.json()["masked"] is True
    pc_new = W.client(app, "police1")
    convs_p = pc_new.get("/api/v1/copilot/conversations").json()
    assert "XSD 4004" not in json.dumps(convs_p) and "4004" not in json.dumps(convs_p)
    h_down = pc_new.get(f"/api/v1/copilot/conversations/{conv_p}").json()
    raw_down = json.dumps(h_down)
    assert "XSD 4004" not in raw_down and "4004" not in raw_down and "registry" not in raw_down
    for cit in h_down["items"][-1]["citations"].values():
        assert cit["plate"] == "XSD ****" and cit["masked"] is True
        assert "?v=full" not in (cit.get("crop_url") or "") and cit.get("link") is None
    # Even if a caller passes the stale in-memory police User object or calls transcript without user:
    assert "XSD 4004" not in json.dumps(store.history(c, police, conv_p))
    assert "XSD 4004" not in json.dumps(store.transcript(c, conv_p))


# ---------------------------------------------------------------------------- the evaluation set
def test_eval_runner_and_grader_pass_an_oracle_and_fail_empty_answers():
    import tempfile
    from webapp.copilot.eval import run_eval
    saved = {k: os.environ.get(k) for k in ("SECS_WEB_DATA", "SECS_REGISTRY")}
    out = Path(tempfile.mkdtemp(prefix="eval_", dir=W._TMP))
    try:
        cases = run_eval.load_cases()
        assert len(cases) >= 30 and {c["role"] for c in cases} == set(tools.ALL) and {c["lang"] for c in cases} == {"en", "zh", "ms"}
        assert all(k in run_eval.EXPECTED for c in cases for k in c.get("values", []))
        for kind, want in (("oracle", 1.0), ("null", 0.0)):
            assert run_eval.main(["--dry-run", kind, "--out", str(out), "--no-computed-forecasts", "--concurrency", "2"]) == 0
            rows = [json.loads(x) for x in (out / f"dry-{kind}" / "results.jsonl").read_text("utf-8").splitlines()]
            assert len(rows) == len(cases) and not (out / f"dry-{kind}" / "errors.jsonl").exists()
            bad = [(r["prompt_id"], r["explanation"]) for r in rows if r["grade"]["pass"] != want]
            assert not bad, bad
        # the grader's value matching: rounding, plates without spaces, times, written dates
        assert run_eval.present(1140.6, "about 1141 km/h") and not run_eval.present(1140.6, "about 1100 km/h")
        assert run_eval.present("XSD 4004", "plate XSD4004") and run_eval.present("07:13", "at 7:13")
        assert run_eval.present("2026-03-31", "valid to 31 March 2026") and run_eval.present("2026-03-31", "2026年3月31日")
        # a paid run refuses to start until the user approves the harness
        assert run_eval.main(["--pilot", "1", "--out", str(out)]) == 2
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------------------- the real SDK request (mock transport)
def test_the_anthropic_sdk_request_and_stream_parsing():
    try:
        import anthropic
        import httpx2
    except ImportError:
        print("  (anthropic not installed: skipped)")
        return
    from webapp.copilot.llm import AnthropicLLM, run
    seen = []
    events = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                                                 "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                                                                 "stop_sequence": None, "usage": {
                                                                     "input_tokens": 120, "output_tokens": 1,
                                                                     "cache_creation_input_tokens": 0,
                                                                     "cache_read_input_tokens": 4800}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-1"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Checking "}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "the plate."}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("content_block_start", {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search_plates", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"purpose\": \"Alert follow-up\", "}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "\"plate\": \"XSD 4004\"}"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 2}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 42}}),
        ("message_stop", {"type": "message_stop"})]

    def handler(request):
        seen.append(request)
        body = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)
        return httpx2.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_1"}, content=body.encode())
    client = anthropic.Anthropic(api_key="test-key", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    llm = AnthropicLLM("claude-opus-5-5", "medium", client=client)
    msgs = [{"role": "user", "content": [{"type": "text", "text": "Where was XSD 4004 seen?"}]},
            {"role": "system", "content": "Session context from the system: test"}]
    defs = tools.definitions("police")
    out, turn = run(llm.stream(system=prompts.system_blocks("police"), messages=msgs, tools=defs))
    body = json.loads(seen[0].content)
    assert "/v1/messages" in str(seen[0].url) and seen[0].headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default" and body["max_tokens"] == config.MAX_TOKENS
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {"effort": "medium"}
    assert body["cache_control"] == {"type": "ephemeral"} and body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert all(t["eager_input_streaming"] for t in body["tools"]) and [t["name"] for t in body["tools"]] == [d["name"] for d in defs]
    assert body["messages"] == msgs and "temperature" not in body
    assert out == "Checking the plate." and turn.stop_reason == "tool_use" and turn.model == "claude-opus-5-5"
    assert turn.tool_uses == [{"type": "tool_use", "id": "toolu_1", "name": "search_plates",
                               "input": {"purpose": "Alert follow-up", "plate": "XSD 4004"}}]
    assert turn.content[0] == {"type": "thinking", "thinking": "", "signature": "sig-1"}       # replayed unchanged
    assert turn.usage["cache_read_input_tokens"] == 4800 and turn.usage["output_tokens"] == 42 and turn.request_id == "req_1"
    assert abs(turn.cost_usd - config.cost_usd("claude-opus-5-5", turn.usage)) < 1e-12
    # Haiku 5.5 has no server-side fallback: no beta header, no fallbacks parameter
    seen.clear()
    run(AnthropicLLM("claude-haiku-5-5", "low", client=client).stream(system=prompts.system_blocks("police"), messages=msgs))
    body = json.loads(seen[0].content)
    assert "fallbacks" not in body and "anthropic-beta" not in seen[0].headers and "tools" not in body

    def failing(request):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
    bad = anthropic.Anthropic(api_key="bad", max_retries=0,
                              http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(failing)))
    try:
        run(AnthropicLLM("claude-opus-5-5", client=bad).stream(system=[], messages=msgs))
        raise AssertionError("no error")
    except LLMUnavailable as e:
        assert "rejected" in str(e)
    finally:
        config.reset_availability()
    # an account without the fallback beta: the request is repeated once without it, and later ones leave it out
    bodies = []

    def picky(request):
        b = json.loads(request.content)
        bodies.append(b)
        if "fallbacks" in b:
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                          "message": "fallbacks is not enabled for this organization"}})
        return handler(request)
    cl = anthropic.Anthropic(api_key="k", max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(picky)))
    try:
        out, _ = run(AnthropicLLM("claude-opus-5-5", client=cl).stream(system=[], messages=msgs))
        assert out == "Checking the plate." and "fallbacks" in bodies[0] and "fallbacks" not in bodies[1]
        run(AnthropicLLM("claude-opus-5-5", client=cl).stream(system=[], messages=msgs))
        assert len(bodies) == 3 and "fallbacks" not in bodies[2]
    finally:
        AnthropicLLM.no_fallbacks = False


if __name__ == "__main__":
    tests = {k: v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)}
    failed = 0
    for name, fn in tests.items():
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:                                           # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            clock.set_now(None)
            config.reset_availability()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    import shutil
    shutil.rmtree(W._TMP, ignore_errors=True)
    sys.exit(1 if failed else 0)
