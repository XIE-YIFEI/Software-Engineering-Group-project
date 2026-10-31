"""Tests of the web system: importer, login and roles, masking and audit, search, alerts, gate, timeline, evidence
pack, forecast and what-if, retention, uploads. FastAPI TestClient on a temporary database filled from the committed
demo results; no models, no GPU, no network.

    python webapp/tests/test_web.py          (also runs under pytest)
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp(prefix="secs_web_test_"))
os.environ["SECS_WEB_DATA"] = str(_TMP / "base")
os.environ["SECS_REGISTRY"] = str(_TMP / "registry" / "demo_registry.db")

import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from webapp import clock, db  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.importer import import_demo  # noqa: E402
from webapp.settings import Settings, load_settings  # noqa: E402

USERS = {u["username"]: u for u in yaml.safe_load((ROOT / "webapp" / "seed" / "demo_users.yaml").read_text("utf-8"))["users"]}
_BASE = None


def base_settings() -> Settings:
    """The demo data imported once (without the computed zone forecasts, which test_importer_* checks separately)."""
    global _BASE
    if _BASE is None:
        st = load_settings()
        conn = db.connect(st.db_path)
        import_demo(conn, st, demo_users=True, computed_forecasts=False, log=lambda *_: None)
        conn.close()
        _BASE = st
    return _BASE


def fresh():
    """A private copy of the imported demo database (tests that change data do not disturb each other)."""
    src = base_settings()
    dst = Path(tempfile.mkdtemp(prefix="case_", dir=_TMP))
    shutil.copytree(src.data_dir, dst, dirs_exist_ok=True)
    st = Settings(data_dir=dst, registry_path=src.registry_path)
    return st, create_app(st, start_worker=False, retention=False)


def client(app, username: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": username, "password": USERS[username]["password"]})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def search(c, **params):
    params.setdefault("purpose", "Alert follow-up")
    return c.get("/api/v1/sightings", params=params)


def conn_of(st: Settings) -> sqlite3.Connection:
    return db.connect(st.db_path)


# ---------------------------------------------------------------------------- importer
def test_importer_loads_every_demo_result():
    st, _ = fresh()
    c = conn_of(st)
    n = lambda sql, *p: db.scalar(c, sql, p)          # noqa: E731
    assert n("SELECT COUNT(*) FROM runs") == 4
    assert n("SELECT COUNT(*) FROM sightings") == 35          # 8 + 4 synthetic, 19, 2 + 2 synthetic gate arrivals
    assert n("SELECT COUNT(*) FROM sightings WHERE provenance = 'inserted_plate'") == 7
    assert n("SELECT COUNT(*) FROM sightings WHERE provenance = 'synthetic'") == 6
    assert n("SELECT COUNT(*) FROM alerts WHERE source = 'registry'") == 13
    assert n("SELECT COUNT(*) FROM alerts WHERE source = 'lane'") == 5
    assert n("SELECT COUNT(*) FROM alerts WHERE code = 'WATCHLIST'") == 2           # XSE 5005 on the police watch list
    assert n("SELECT COUNT(*) FROM alerts WHERE code = 'BANNED_AT_SITE'") == 1      # XBN 4321 at the gate
    assert n("SELECT COUNT(*) FROM alerts WHERE provenance = 'camera'") == 0     # no alert ever names a real vehicle
    assert n("SELECT COUNT(*) FROM crossings") == 355
    assert n("SELECT COUNT(*) FROM lane_events") == 12
    assert n("SELECT SUM(count) FROM withheld") == 10
    assert n("SELECT COUNT(DISTINCT minute) FROM zone_minutes z JOIN series_days d ON d.id = z.day_id "
             "WHERE d.code = 'jb_ciq_cam02_2023-03-22'") == 571
    assert n("SELECT COUNT(*) FROM forecast_points") == 571
    # every source file is recorded with its SHA-256
    for r in db.rows(c, "SELECT results_path, results_sha256 FROM runs"):
        assert hashlib.sha256((ROOT / r["results_path"]).read_bytes()).hexdigest() == r["results_sha256"]
    # seen_at = notional clock origin + first_sec (XSD 4004 of the violation check at 23.5 s)
    assert n("SELECT seen_at FROM sightings WHERE compact = 'XSD4004' AND provenance = 'inserted_plate'") == \
        "2026-10-14T08:00:23.500"


def test_importer_is_idempotent_and_keeps_alert_workflow_state():
    st, app = fresh()
    police = client(app, "police1")
    aid = police.get("/api/v1/alerts", params={"code": "BLACKLIST"}).json()["items"][0]["id"]
    assert police.post(f"/api/v1/alerts/{aid}/transition", json={"to": "acknowledged"}).status_code == 200
    c = conn_of(st)
    before = {t: db.scalar(c, f"SELECT COUNT(*) FROM {t}") for t in ("sightings", "alerts", "lane_events", "crossings")}
    import_demo(c, st, computed_forecasts=False, log=lambda *_: None)
    after = {t: db.scalar(c, f"SELECT COUNT(*) FROM {t}") for t in before}
    assert before == after
    assert db.scalar(c, "SELECT status FROM alerts WHERE id = ?", (aid,)) == "acknowledged"


def test_importer_reproduces_the_committed_forecast_and_adds_the_zones():
    st, _ = fresh()
    c = conn_of(st)
    from webapp.importer import SEED_DIR, compute_forecasts, load_yaml
    logs = []
    n = compute_forecasts(c, load_yaml(SEED_DIR / "cameras.yaml"), logs.append)
    assert n == 9                                      # 4 zones of day A + direction and 4 zones of day C
    m = re.search(r"max difference ([0-9.]+)", " ".join(logs))
    # LightGBM recomputed = the committed JSON, up to floating-point differences between platforms: 0.0000 on the
    # Windows machine that made it, 0.0016 on the Linux CI runner (fill 0.01 is about 0.4 vehicles on camera A)
    assert m and float(m.group(1)) < 0.01, logs


# ---------------------------------------------------------------------------- login, sessions, roles
def test_login_lockout_logout_and_csrf():
    st, app = fresh()
    c = TestClient(app)
    for _ in range(5):
        assert c.post("/api/v1/auth/login", json={"username": "police1", "password": "wrong"}).status_code == 401
    r = c.post("/api/v1/auth/login", json={"username": "police1", "password": USERS["police1"]["password"]})
    assert r.status_code == 401 and "too many" in r.json()["detail"]
    clock.set_now(clock.now() + timedelta(minutes=6))
    try:
        r = c.post("/api/v1/auth/login", json={"username": "police1", "password": USERS["police1"]["password"]})
        assert r.status_code == 200
        csrf = r.json()["csrf_token"]
        # state-changing calls need the CSRF token
        assert c.post("/api/v1/auth/logout").status_code == 403
        assert c.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert c.get("/api/v1/auth/me").status_code == 401
    finally:
        clock.set_now(None)
    audit = db.rows(conn_of(st), "SELECT action FROM audit_log WHERE username = 'police1'")
    assert [a["action"] for a in audit].count("login_failed") == 5 and {"login_locked", "login", "logout"} <= {a["action"] for a in audit}
    stored = db.scalar(conn_of(st), "SELECT password_hash FROM users WHERE username = 'police1'")
    assert stored.startswith("scrypt$") and USERS["police1"]["password"] not in stored


def test_roles_see_only_their_scope():
    st, app = fresh()
    guard, traffic, police, admin = (client(app, u) for u in ("guard1", "traffic1", "police1", "admin1"))
    # guards: only their own gate
    plates = {s["plate"] for s in search(guard, page_size=200).json()["items"]}
    assert plates and plates <= {"JPP 3473", "JMT 8577", "XVP 3003", "XBN 4321"}
    assert guard.get("/api/v1/alerts").json()["items"][0]["code"] == "BANNED_AT_SITE"
    # police: road / lane cameras, never the condominium gate
    plates = {s["plate"] for s in search(police, page_size=200).json()["items"]}
    assert "JPP 3473" not in plates and "XSB 2002" in plates
    assert police.get("/api/v1/gate/inside").status_code == 403
    # traffic managers have no alert centre; only admins manage users
    assert traffic.get("/api/v1/alerts").status_code == 403
    assert police.get("/api/v1/users").status_code == 403 and admin.get("/api/v1/users").status_code == 200
    # pages follow the same rule
    assert traffic.get("/admin/users", headers={"accept": "text/html"}).status_code == 403
    assert guard.get("/gate", headers={"accept": "text/html"}).status_code == 200
    assert TestClient(app).get("/plates", headers={"accept": "text/html"}, follow_redirects=False).status_code == 303


# ---------------------------------------------------------------------------- masking, reveal, audit
def test_masked_roles_see_masked_plates_and_blurred_crops_until_they_reveal_with_a_reason():
    st, app = fresh()
    traffic, police = client(app, "traffic1"), client(app, "police1")
    items = search(traffic, page_size=200).json()["items"]
    assert items and all(s["masked"] and s["plate"].endswith("****") for s in items)
    sid = next(s["id"] for s in items if s["crop_url"] and s["plate"].startswith("XSB"))
    blurred = traffic.get(f"/media/sightings/{sid}/crop").content
    sharp = police.get(f"/media/sightings/{sid}/crop").content
    assert blurred != sharp
    assert traffic.get(f"/media/sightings/{sid}/scene").status_code == 403
    # the plate cameras' videos show full plates: none for masked roles; lane-camera videos are fine
    assert all(s["video"] is None for s in items)
    assert traffic.get("/media/runs/1/video").status_code == 403 and police.get("/media/runs/1/video").status_code == 200
    lane_run = next(r["id"] for r in traffic.get("/api/v1/traffic/runs").json() if r["camera_kind"] == "lane")
    assert traffic.get(f"/media/runs/{lane_run}/video", headers={"Range": "bytes=0-9"}).status_code == 206
    # reveal: a reason is required, then the plate and the sharp crop are shown, and it is logged
    assert traffic.post(f"/api/v1/sightings/{sid}/reveal", json={"reason_code": "Because"}).status_code == 422
    r = traffic.post(f"/api/v1/sightings/{sid}/reveal", json={"reason_code": "Accident investigation", "note": "case 12"})
    assert r.status_code == 200 and r.json()["plate"] == "XSB 2002"
    assert traffic.get(f"/media/sightings/{sid}/crop").content == sharp
    rows = db.rows(conn_of(st), "SELECT * FROM audit_log WHERE action = 'reveal' AND username = 'traffic1'")
    assert rows and rows[-1]["reason"] == "Accident investigation"
    # searching by a plate number counts as a reveal: refused without a reason, exact match only with one
    assert search(traffic, plate="WTQ 9809").status_code == 403
    r = search(traffic, plate="WTQ 9809", reveal_reason="Incident review", tolerance=2)
    assert r.status_code == 200 and {s["plate"] for s in r.json()["items"]} == {"WTQ 9809"}
    # the masking rule itself
    from webapp.privacy import mask_plate
    assert (mask_plate("VJF 1234"), mask_plate("WA 9299 M"), mask_plate("1M4U 123")) == ("VJF ****", "WA ****", "****")


def test_every_search_view_and_export_is_audited_with_its_purpose():
    st, app = fresh()
    police = client(app, "police1")
    assert search(police, purpose="").status_code == 422                 # no purpose, no search
    assert search(police, plate="XSB2002", purpose="Investigation (case reference)", reference="IP/123").status_code == 200
    sid = search(police, plate="XSB2002").json()["items"][0]["id"]
    assert police.get(f"/api/v1/sightings/{sid}", params={"purpose": "Alert follow-up"}).status_code == 200
    r = police.get("/api/v1/sightings/export.csv", params={"purpose": "Audit"})
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert r.status_code == 200 and rows and "provenance" in rows[0]
    assert {x["provenance"] for x in rows} >= {"camera", "inserted_plate", "synthetic"}
    log = db.rows(conn_of(st), "SELECT action, reason, details FROM audit_log WHERE username = 'police1'")
    s = next(x for x in log if x["action"] == "search" and x["reason"] == "Investigation (case reference)")
    assert json.loads(s["details"])["reference"] == "IP/123"
    assert any(x["action"] == "view" for x in log) and any(x["action"] == "export" and x["reason"] == "Audit" for x in log)


def test_audit_log_is_append_only_and_hash_chained():
    st, app = fresh()
    admin = client(app, "admin1")
    assert admin.get("/api/v1/audit/verify").json()["ok"]
    c = conn_of(st)
    for sql in ("UPDATE audit_log SET reason = 'x'", "DELETE FROM audit_log"):
        try:
            c.execute(sql)
            raise AssertionError(f"{sql} was allowed")
        except sqlite3.DatabaseError as e:
            assert "append-only" in str(e)
    # a row inserted behind the system's back breaks the chain
    c.execute("INSERT INTO audit_log (at, username, role, action, details, reason, prev_hash, row_hash) "
              "VALUES ('2026-10-23T00:00:00.000', 'mallory', 'admin', 'view', '{}', '', 'x', 'y')")
    r = admin.get("/api/v1/audit/verify").json()
    assert not r["ok"] and r["first_bad_id"]


# ---------------------------------------------------------------------------- search and playback
def test_fuzzy_search_tolerates_ocr_look_alikes():
    st, app = fresh()
    police = client(app, "police1")
    items = search(police, plate="XSD 4OO4", tolerance=1).json()["items"]          # letter O typed for zeros
    assert {s["plate"] for s in items} == {"XSD 4004"} and all(s["distance"] == 1.0 for s in items)
    assert search(police, plate="XSD 4OO4", tolerance=0).json()["total"] == 0
    assert {s["plate"] for s in search(police, plate="XS*", page_size=200).json()["items"]} >= {"XSB 2002", "XSD 4004"}
    from webapp.analysis.fuzzy import PlateIndex, plate_distance
    assert plate_distance("VJF1234", "VJF1234") == 0 and plate_distance("B8", "8B") == 1.0
    assert plate_distance("WTQ9809", "WTQ98O9") == 0.5 and plate_distance("WTQ9809", "WTQ9X09") == 1.0
    # the vectorised index gives exactly the pairwise distances (random plates of different lengths, look-alikes)
    import random
    rng = random.Random(7)
    alphabet = "ABDGHIOSWZ0125689"
    plates = sorted({"".join(rng.choice(alphabet) for _ in range(rng.randint(2, 9))) for _ in range(400)})
    index = PlateIndex(plates)
    for q in ("WTQ98O9", "B8", "XSD4004", "OO1", ""):
        d = index.distances(q)
        assert all(abs(float(d[i]) - plate_distance(q, p)) < 1e-6 for i, p in enumerate(plates)), q
    # provisional readings are kept apart and never alert
    tent = search(police, status="TENTATIVE").json()["items"]
    assert [s["plate"] for s in tent] == ["VCV 1555"] and not tent[0]["alerts"]


def test_click_to_timestamp_seeks_one_second_before_the_record():
    st, app = fresh()
    police, guard = client(app, "police1"), client(app, "guard1")
    for s in search(police, page_size=200).json()["items"]:
        run = police.get("/api/v1/runs").json()
        off = next(r["video_offset_sec"] for r in run if r["id"] == s["run_id"])
        assert s["video"]["seek_sec"] == round(max(0.0, s["first_sec"] - off - 1.0), 2)
    cards = guard.get("/api/v1/gate/replay").json()["cards"]
    assert [round(c["video_time"], 2) for c in cards] == [0.03, 9.2, 16.0, 24.0]     # first_sec - 66 s
    assert police.get("/media/runs/1/video", headers={"Range": "bytes=0-99"}).status_code == 206   # seekable


# ---------------------------------------------------------------------------- alerts
def test_alerts_sorted_by_severity_and_workflow_with_correction_pool():
    st, app = fresh()
    police = client(app, "police1")
    items = police.get("/api/v1/alerts", params={"page_size": 200}).json()["items"]
    ranks = [a["rank"] for a in items]
    assert ranks == sorted(ranks) and ranks[0] == 1
    a = next(x for x in items if x["code"] == "UNMATCHED")
    d = police.get(f"/api/v1/alerts/{a['id']}").json()
    assert "does not mean a real vehicle is unregistered" in d["note"]
    assert police.post(f"/api/v1/alerts/{a['id']}/transition", json={"to": "resolved"}).status_code == 422  # needs a note
    assert police.post(f"/api/v1/alerts/{a['id']}/transition", json={"to": "acknowledged"}).status_code == 200
    r = police.post(f"/api/v1/alerts/{a['id']}/transition",
                    json={"to": "false_alarm", "note": "plate misread", "corrected_plate": "xse 5006"})
    assert r.status_code == 200 and r.json()["status"] == "false_alarm"
    assert [h["action"] for h in r.json()["history"]][-2:] == ["acknowledge", "false_alarm"]
    corr = db.row(conn_of(st), "SELECT * FROM corrections")
    assert corr["corrected_compact"] == "XSE5006" and corr["ocr_compact"] == "XSE5005"
    assert police.post(f"/api/v1/alerts/{a['id']}/transition", json={"to": "resolved", "note": "x"}).status_code == 422
    assert client(app, "admin1").post(f"/api/v1/alerts/{a['id']}/transition", json={"to": "acknowledged"}).status_code == 403


def test_cross_camera_timeline_flags_impossible_travel():
    st, app = fresh()
    t = client(app, "police1").get("/api/v1/plates/XSD4004/timeline").json()
    assert [p["place"] for p in t["points"]] == ["Johor Bahru (SIM)", "KL roadside ANPR (demo)", "KL roadside ANPR (demo)"]
    assert t["impossible"] and t["simulated"]
    leg = t["legs"][0]
    assert leg["impossible"] and 290 < leg["km"] < 296 and 1100 < leg["speed_kmh"] < 1180
    assert not t["legs"][1]["impossible"]                             # same camera: never impossible
    assert client(app, "traffic1").get("/api/v1/plates/XSD4004/timeline").status_code == 403


def test_evidence_pack_marks_every_page_hashes_images_and_cites_only_verified_sections():
    st, app = fresh()
    police = client(app, "police1")
    aid = next(a["id"] for a in police.get("/api/v1/alerts", params={"code": "DUPLICATE"}).json()["items"]
               if a["provenance"] == "inserted_plate")
    html = police.get(f"/api/v1/alerts/{aid}/evidence", params={"format": "html"}).text
    assert html.count("COURSE PROJECT DEMO") >= 2 and "not an official document" in html.lower()
    for data, digest in re.findall(r'data:image/jpeg;base64,([A-Za-z0-9+/=]+)".*?SHA-256 <code>([0-9a-f]{64})', html, re.S):
        import base64
        assert hashlib.sha256(base64.b64decode(data)).hexdigest() == digest
    assert "only prove that the image files were not changed" in html
    legal = yaml.safe_load((ROOT / "webapp" / "legal.yaml").read_text("utf-8"))
    cited = set(re.findall(r"Section (\d+\(\d\)(?:\([a-z]\))?)", html))
    assert cited == {"108(3)(f)", "108(3)(b)"} and cited <= {s["section"] for s in legal["sections"].values()}
    text = re.sub(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+", "", html)     # base64 can contain any word
    assert "PDRM" not in text and "logo" not in text.lower()
    assert all(src.startswith("data:image/") for src in re.findall(r'<img src="([^"]+)"', html))   # nothing external
    pdf = police.get(f"/api/v1/alerts/{aid}/evidence", params={"format": "pdf"})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    ex = db.rows(conn_of(st), "SELECT * FROM evidence_exports WHERE alert_id = ?", (aid,))
    assert len(ex) == 2 and all(len(e["file_sha256"]) == 64 for e in ex)
    assert client(app, "traffic1").get(f"/api/v1/alerts/{aid}/evidence").status_code == 403


# ---------------------------------------------------------------------------- gate
def test_gate_identity_colours_passes_and_overstays():
    st, app = fresh()
    guard = client(app, "guard1")
    ident = lambda p: guard.get("/api/v1/gate/identify", params={"plate": p}).json()        # noqa: E731
    assert (ident("JPP 3473")["colour"], ident("XVP 3003")["colour"], ident("XBN 4321")["colour"],
            ident("JMT 8577")["colour"]) == ("green", "teal", "red", "yellow")
    assert "expired" in ident("XVA 5120")["detail"]                     # a pass outside its window is not valid
    # a new pass turns an unregistered visitor teal; allow -> inside; stop needs a note; exited -> gone
    r = guard.post("/api/v1/passes", json={"plate": "JMT 8577", "unit_no": "A-05-2", "visitor_label": "Delivery"})
    assert r.status_code == 200 and r.json()["valid_now"]
    assert ident("JMT 8577")["colour"] == "teal"
    assert guard.post("/api/v1/gate/decisions", json={"plate": "XBN 4321", "decision": "stopped"}).status_code == 422
    assert guard.post("/api/v1/gate/decisions", json={"plate": "JMT 8577", "decision": "allowed"}).status_code == 200
    inside = {i["plate"]: i for i in guard.get("/api/v1/gate/inside").json()}
    assert "JMT 8577" in inside and not inside["JMT 8577"]["overstay"]
    assert inside["XVA 5120"]["overstay"]                                # seeded: inside 6 h, pass ended
    first = guard.get("/api/v1/gate/inside").json()[0]
    assert first["overstay"]                                             # overstays are pinned to the top
    clock.set_now(clock.now() + timedelta(hours=5))
    try:
        assert {i["plate"]: i for i in guard.get("/api/v1/gate/inside").json()}["JMT 8577"]["overstay"]
    finally:
        clock.set_now(None)
    guard.post("/api/v1/gate/decisions", json={"plate": "JMT 8577", "decision": "exited"})
    assert "JMT 8577" not in {i["plate"] for i in guard.get("/api/v1/gate/inside").json()}
    log = guard.get("/api/v1/gate/access-log", params={"plate": "JMT8577"}).json()
    assert [e["decision"] for e in log] == ["exited", "allowed"]


# ---------------------------------------------------------------------------- forecast and what-if
def test_forecast_state_accuracy_and_warning_match_the_committed_json():
    st, app = fresh()
    traffic = client(app, "traffic1")
    day = next(d for d in traffic.get("/api/v1/forecast/days").json() if d["code"] == "jb_ciq_cam02_2023-03-22")
    assert day["first_warning"].endswith("06:56") and [o[11:] for o in day["onsets"]] == ["07:13", "08:17"]
    s = traffic.get("/api/v1/forecast/state", params={"day": day["code"], "at": "06:56"}).json()["sections"][0]
    assert s["section"] == "To CIQ" and s["status"] == "Congestion in ~15 min" and s["warning"] and s["state"] == "free"
    fc = json.loads((ROOT / "demo" / "jb_ciq_forecast_timelapse.forecast.json").read_text("utf-8"))
    i = fc["minutes"].index("2023-03-22T06:56")
    assert abs(s["forecast"]["15"] - fc["forecast_fill"]["15"][i]) < 1e-9
    acc = traffic.get("/api/v1/forecast/accuracy", params={"day": day["code"], "horizon": 30}).json()
    errs = [abs(fc["forecast_fill"]["30"][k - 30] - fc["measured_fill"][k]) for k in range(30, len(fc["minutes"]))
            if fc["measured_fill"][k] is not None and fc["forecast_fill"]["30"][k - 30] is not None]
    assert abs(acc["mae"] - sum(errs) / len(errs)) < 1e-4
    assert acc["warning_min"] == [17, 3]                                 # TEAM_GUIDE.md 10.5: LightGBM, A, +30


def test_whatif_more_capacity_never_lengthens_the_queue():
    st, app = fresh()
    traffic = client(app, "traffic1")
    prev = None
    for pct in (0, 10, 20, 30):
        w = traffic.get("/api/v1/forecast/whatif", params={"day": "jb_ciq_cam02_2023-03-22", "capacity_pct": pct}).json()
        assert all(p is None or b is None or p <= b + 1e-9 for p, b in zip(w["projected"], w["baseline"]))
        if pct == 0:
            assert w["projected"] == w["baseline"]
        if prev is not None:
            assert all(a is None or b is None or a <= b + 1e-9 for a, b in zip(w["projected"], prev))
        prev = w["projected"]
        assert w["label"] == "Estimate under stated assumptions" and len(w["assumptions"]) >= 5
    assert w["start_minute"].endswith("06:56") and w["mu_source"] == "estimated"
    w = traffic.get("/api/v1/forecast/whatif", params={"day": "jb_ciq_cam02_2023-03-22", "capacity_pct": 20, "mu": 2.0}).json()
    assert w["mu_source"] == "assumed" and w["mu"] == 2.0


def test_traffic_analytics_flow_lanes_events_and_csv():
    st, app = fresh()
    traffic = client(app, "traffic1")
    run = next(r for r in traffic.get("/api/v1/traffic/runs").json() if r["camera_kind"] == "lane")
    s = traffic.get("/api/v1/traffic/summary", params={"run_id": run["id"]}).json()
    assert s["total"] == 355 and s["withheld"] == {"SPEEDING": 7, "TAILGATING": 3}
    assert sum(l["count"] for l in s["lanes"]) + s["outside_lanes"] == 355 and s["outside_lanes"] == 4
    assert all(l["speed_error_kmh"] == round(0.065 * l["mean_speed_kmh"], 1) for l in s["lanes"] if l["mean_speed_kmh"])
    flow = traffic.get("/api/v1/traffic/flow", params={"run_id": run["id"], "by": "type", "bin": 10}).json()
    assert sum(sum(v) for v in flow["series"].values()) == 355 and set(flow["series"]) == {"Car", "Motorcycle", "Truck", "Bus"}
    occ = traffic.get("/api/v1/traffic/lanes/series", params={"run_id": run["id"], "metric": "occupancy"}).json()
    assert len(occ["series"]) == 8 and max(occ["x"]) < 120                         # no bin after the clip ended
    ev = traffic.get("/api/v1/traffic/events", params={"run_id": run["id"]}).json()
    assert len(ev) == 12 and all(e["plate"].endswith("****") for e in ev if e["plate"])       # masked for traffic
    assert all(e["provenance"] == "synthetic" for e in ev if e["severity"] == "violation")    # real ones withheld
    csv_text = traffic.get("/api/v1/traffic/export.csv", params={"run_id": run["id"], "what": "crossings"}).text
    assert len(csv_text.strip().splitlines()) == 356


# ---------------------------------------------------------------------------- retention, uploads, reports
def test_retention_deletes_ordinary_sightings_but_keeps_open_alerts():
    st, app = fresh()
    admin = client(app, "admin1")
    c = conn_of(st)
    total = db.scalar(c, "SELECT COUNT(*) FROM sightings")
    with_alerts = db.scalar(c, "SELECT COUNT(DISTINCT sighting_id) FROM alerts WHERE sighting_id IS NOT NULL")
    assert admin.post("/api/v1/retention/purge", params={"dry_run": True}).json()["sightings"] == 0
    clock.set_now(clock.now() + timedelta(days=31))
    try:
        admin = client(app, "admin1")                                     # the old login has expired by then
        rep = admin.post("/api/v1/retention/purge", params={"dry_run": True}).json()
        assert rep["sightings"] == total - with_alerts and db.scalar(c, "SELECT COUNT(*) FROM sightings") == total
        rep = admin.post("/api/v1/retention/purge", params={"dry_run": False}).json()
        assert db.scalar(c, "SELECT COUNT(*) FROM sightings") == with_alerts      # open alerts keep their sightings
    finally:
        clock.set_now(None)
    assert admin.get("/api/v1/audit", params={"action": "purge"}).json()
    assert admin.get("/api/v1/audit/verify").json()["ok"]


def test_upload_job_is_analysed_by_the_worker_and_imported():
    st, app = fresh()
    police = client(app, "police1")
    cam = next(c["id"] for c in police.get("/api/v1/cameras").json() if c["code"] == "kl-roadside-anpr")
    assert police.post("/api/v1/jobs", files={"file": ("notes.txt", b"hello")}, data={"camera_id": cam}).status_code == 422
    import cv2
    import numpy as np
    ok, png = cv2.imencode(".png", np.zeros((60, 120, 3), np.uint8))
    job = police.post("/api/v1/jobs", files={"file": ("car.png", png.tobytes())},
                      data={"camera_id": cam, "scene": "roadside", "recorded_at": "2026-10-23T09:00:00"}).json()
    assert job["status"] == "queued" and job["kind"] == "image"
    from webapp import worker as W

    class FakeWorker(W.Worker):                     # stands in for run_image.py: no models in CI
        def _run(self, conn, job, cmd, log_path, total_hint=None):
            out = Path(cmd[cmd.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            img = cmd[cmd.index("--source") + 1]
            cv2.imwrite(str(out / Path(img).name), cv2.imread(img))
            (out / "results.json").write_text(json.dumps({"images": [{"image": img, "plates": [
                {"box": [10, 10, 100, 40], "det_conf": 0.9, "text": "XSB 2002", "compact": "XSB2002", "ocr_conf": 0.97,
                 "valid": True, "region": "Unknown", "raw": "XSB2002", "alerts": []}]}]}), encoding="utf-8")
            log_path.write_text("ok", encoding="utf-8")
            return 0
    c = conn_of(st)
    w = FakeWorker(st)
    w.run_job(c, w._claim(c))
    done = police.get(f"/api/v1/jobs/{job['id']}").json()
    assert done["status"] == "done" and done["run_id"]
    rows = search(police, run_id=done["run_id"]).json()["items"]
    assert [r["plate"] for r in rows] == ["XSB 2002"] and rows[0]["seen_at"].startswith("2026-10-23T09:00")
    assert any(a["action"] == "upload" for a in db.rows(c, "SELECT action FROM audit_log"))


def test_reports_are_built_per_role_as_csv_and_pdf():
    st, app = fresh()
    police, traffic, guard = client(app, "police1"), client(app, "traffic1"), client(app, "guard1")
    rep = police.get("/api/v1/reports/daily", params={"date": "2026-10-14", "kind": "alerts"}).json()
    assert sum(r[2] for r in rep["sections"][0]["rows"]) == 20
    t = traffic.get("/api/v1/reports/weekly", params={"date": "2026-10-14", "kind": "traffic", "format": "csv"})
    assert t.status_code == 200 and "not an official document" in t.text and "Taman Sri Selayang" in t.text
    assert traffic.get("/api/v1/reports/daily", params={"date": "2026-10-14", "kind": "alerts"}).status_code == 403
    assert guard.get("/api/v1/reports/daily", params={"date": "2026-10-23", "kind": "gate", "format": "pdf"}).content[:4] == b"%PDF"


def test_api_documentation_lists_the_service_endpoints():
    st, app = fresh()
    spec = TestClient(app).get("/openapi.json").json()
    for p in ("/api/v1/sightings", "/api/v1/alerts/{alert_id}/transition", "/api/v1/plates/{plate}/timeline",
              "/api/v1/forecast/whatif", "/api/v1/gate/decisions", "/api/v1/alerts/{alert_id}/evidence"):
        assert p in spec["paths"], p


def test_requirements_cover_every_third_party_import_of_the_web_system():
    root = ROOT
    with open(root / "requirements.txt", encoding="ascii") as f:          # ASCII only: pip reads it as GBK
        listed = {re.split(r"[<>=!~\[\s]", s)[0].lower() for s in (ln.split("#")[0].strip() for ln in f) if s}
    dist = {"cv2": "opencv-python", "yaml": "pyyaml", "fpdf": "fpdf2", "multipart": "python-multipart", "PIL": "pillow",
            "imageio_ffmpeg": "imageio-ffmpeg"}
    missing = set()
    for path in (root / "webapp").rglob("*.py"):
        for ln in path.read_text("utf-8").splitlines():
            m = re.match(r"\s*(?:from\s+(\w+)[\w.]*\s+import\s|import\s+(\w+))", ln)
            mod = m and (m.group(1) or m.group(2))
            if (mod and mod not in sys.stdlib_module_names and mod not in ("webapp", "alpr", "__future__")
                    and dist.get(mod, mod).lower() not in listed):
                missing.add(f"{mod} ({path.name})")
    assert not missing, f"imported but not in requirements.txt: {sorted(missing)}"
    # the upload form needs python-multipart, the tests httpx: neither is imported by name
    assert {"python-multipart", "httpx", "fastapi", "uvicorn", "jinja2", "fpdf2"} <= listed


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
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1 if failed else 0)
