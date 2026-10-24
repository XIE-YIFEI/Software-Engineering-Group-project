# SECS Vision web system: architecture and page layout

[中文版 / Chinese version](ARCHITECTURE_ZH.md) · System documents: [index](README.md)

> **Status: AS BUILT (step 2).** The design below was approved before the build and is now implemented. Every
> **Decision** was confirmed; what changed during the build is listed in section 13. The wireframes of section 8 are
> the approved design: screenshots of the built pages are in [DEMO_WALKTHROUGH_EN.md](DEMO_WALKTHROUGH_EN.md), and the
> numbers on the built pages are those of section 13 where they differ. Related documents:
> [requirements](REQUIREMENTS_EN.md), [database](DATABASE_EN.md), [API](API_EN.md),
> [test plan and report](TEST_PLAN_REPORT_EN.md).

## 1. What we are building

The project already turns camera footage into results files: command-line tools (`run_alpr.py`, `run_image.py`,
`python -m alpr.forecast*`) write `*.plates.json`, `*.forecast.json`, 1-minute CSV series and annotated videos.
Before this step there was no web system, database or CI. This step added them:

- a **database** (SQLite) filled by an **importer** from the committed results, so the system has real data without
  reprocessing anything;
- a **service layer** of plain, typed, documented Python functions (search, alerts, traffic, forecast, gate …) that
  applies the privacy rules (roles, masking, audit) in one place;
- a **JSON API** (FastAPI, OpenAPI docs at `/docs`) and **server-rendered pages** (Jinja2 + a little JavaScript) on
  top of the service layer;
- a **background worker** that analyses uploaded images and videos with the existing pipeline;
- **GitHub Actions CI** running the 86 unit tests of `alpr/` (the 85 earlier ones + 1 for the new evidence output)
  and 22 API tests.

Positioning: *turn camera footage into traffic data that can be searched, raises alerts and forecasts congestion*,
for three Malaysian user groups:

| Users | Role in the system | What they get |
|---|---|---|
| Traffic police / JPJ enforcement | `police` | immediate alerts on flagged plates; search where and when a plate was seen, with evidence; lane violations logged; evidence pack |
| Condo / office guardhouse security | `guard` | gate reads plates; residents / visitors / banned vehicles shown green / yellow / red; one-click entry log; visitor passes and overstays |
| Traffic management (DBKL, CIQ checkpoint operators, highway operators) | `traffic` | per-lane flow, occupancy and speed; congestion forecast up to ~30 min ahead; what-if; reports |
| System administrator | `admin` | users, data import, uploads, retention, audit log |

Out of scope for this step, but designed for (section 11): live RTSP cameras, drawing lanes in the browser, a public
congestion-outlook page without plates, push notifications, and the generative/agentic AI assistant (next task).

## 2. Components and data flow

```
  recorded demo footage, uploads                          committed results (no reprocessing needed)
            |                                             demo/*.plates.json, demo/*.mp4
            v                                             demo/jb_ciq_forecast_timelapse.forecast.json
  +---------------------+  results JSON, annotated mp4,   docs/data/causeway/*.csv, *.calibration.json
  | alpr/ (unchanged    |  opt-in crops + scene frames             |
  | recognition code)   |-------------------------+                |
  | run_alpr.py         |                         v                v
  | run_image.py        |               +--------------------------------+     +------------------------+
  +---------------------+               | webapp/importer.py             |---->| alpr/registry.py       |
            ^                           | (idempotent, records SHA-256   |     | SIMULATED registry     |
            | subprocess                |  of every source file)         |     | data/registry/*.db     |
  +---------------------+  results      +---------------+----------------+     +-----------+------------+
  | webapp/worker.py    |--------------------------------+                                 | RegistryProvider
  | 1 background thread |                                v                                 | (replaceable by an
  +---------------------+               +--------------------------------+                 |  authorised source)
            ^ jobs                      | SQLite  data/web/secs.db       |<----------------+
            |                           | (gitignored)                   |
            |                           +---------------+----------------+
            |                                           | data-access layer (webapp/db.py)
            |                           +---------------v----------------+
            |                           | service layer webapp/services/ |  roles, masking, audit,
            |                           | typed functions (= AI tools)   |  retention, "Simulated" labels
            |                           +-------+----------------+-------+
            |                                   |                |
            +------------------------  JSON API /api/v1     HTML pages (Jinja2)
                                                |                |
                                    future AI assistant     browser: vanilla JS, SVG charts, <video>
```

**Runtime.** One process: `python -m webapp` starts uvicorn on `127.0.0.1:8000` with the FastAPI app, one worker
thread for uploads and a daily retention task. Heavy analysis runs in a **subprocess** of the project venv
(`.venv\Scripts\python run_alpr.py … --headless --backend openvino`), so torch is never loaded into the web process
and a crashed analysis cannot take the server down.

**Technology (Decision).**

| Part | Choice | Why |
|---|---|---|
| Web framework | FastAPI + uvicorn | typed request/response models, automatic OpenAPI docs, TestClient for tests |
| Database | SQLite through `sqlite3` (standard library) and one small data-access module | no server to install, one file, same approach as `alpr/registry.py`; all SQL lives in one place, so a move to PostgreSQL rewrites one module |
| Pages | Jinja2 templates + vanilla JavaScript | students can maintain it without a JS build chain |
| Charts | a small SVG chart helper of our own (`static/js/charts.js`) | no CDN, works offline at the defence |
| PDF | `fpdf2` (pure Python) | evidence packs and reports without system libraries |
| Passwords | `hashlib.scrypt` (standard library), per-user salt | no extra dependency |
| Sessions | random token in an HttpOnly, SameSite=Lax cookie; only its SHA-256 is stored; CSRF token in a header | revocable, nothing secret in the cookie |
| New packages | fastapi, pydantic, uvicorn, jinja2, python-multipart, httpx (tests), fpdf2 | added to `requirements.txt` (ASCII only); CI pins exact versions in `requirements-ci.txt` |

**Code layout (Decision: a new top-level package `webapp/`, next to `alpr/`).**

```
webapp/
  __main__.py            python -m webapp  (uvicorn, 127.0.0.1:8000)
  app.py, deps.py        the FastAPI app, error handlers; request dependencies (session, CSRF)
  settings.py, clock.py  paths and defaults (retention days, overstay hours ...), env overrides; Malaysia time
  schema.sql, db.py      schema + connection + small helpers (the data-access layer)
  models.py              pydantic models of every response (also the future AI tool schemas)
  importer.py            python -m webapp.importer --demo [--demo-users]
  worker.py              background jobs: run_alpr.py / run_image.py in a subprocess, progress, import
  security.py            password hashing, sessions, CSRF, role checks
  privacy.py             plate masking, reveal, audit log (hash chain), retention purge
  alerting.py            create_alert: the one place where alerts are created (future push hook)
  registry_provider.py   RegistryProvider interface + the SIMULATED alpr.registry implementation
  legal.yaml             Road Transport Act 1987 sections verified for the evidence pack, with their sources
  services/              overview, plates, alerts, traffic, forecast, gate, lists, jobs, evidence, reports, admin
  analysis/              fuzzy.py (OCR-aware plate distance, vectorised), timeline.py, whatif.py
  api/                   FastAPI routers: thin wrappers (auth -> service -> JSON)
  web/pages.py           page routes;  templates/;  static/{css,js}
  seed/                  cameras.yaml, demo_lists.yaml, demo_users.yaml (test accounts, local demo only)
  tests/test_web.py      22 API tests (FastAPI TestClient), same runner style as alpr/tests/test_alpr.py
data/web/ (gitignored)   secs.db, media/ (crops, blurred crops, scene frames), uploads/, jobs/, exports/, corrections/
```

**Main flows.**

1. *Import*: `python -m webapp.importer --demo` reads the committed files, creates cameras, runs, sightings, alerts,
   lane statistics, congestion series and forecasts. Re-running it updates rows by natural key (run code +
   first frame + plate), so alert workflow states survive a re-import.
2. *Upload & analyse*: the browser uploads a file -> a `jobs` row -> the worker runs the pipeline in a subprocess and
   reads its progress lines (`[612/900] ...`) -> on success the same importer code loads the results JSON as a new
   run.
3. *Search / view / reveal*: every request goes API -> service; the service checks the role, applies masking, writes
   the audit log (who, when, what, why) and only then returns data.
4. *Alert workflow*: new -> acknowledged -> resolved / false alarm, each step stored with handler, time and note.
5. *Gate decision*: plate card -> Allow / Stop & note -> access log.

## 3. Database schema

All times are Malaysia time (MYT, UTC+8), stored as ISO 8601 text. `compact` is the canonical plate (`VJF1234`),
`plate_text` the display form (`VJF 1234`).

**Provenance.** The development plan asks to keep real observations, digitally modified plates and synthetic
trajectories distinct, so every observation-like row has `provenance`:

| Value | Meaning | Examples in the demo data | UI |
|---|---|---|---|
| `camera` | a real vehicle with its real plate, read from the footage | all plates of the road, gate and lane cameras that were not altered | no pill |
| `inserted_plate` | a real vehicle whose plate was digitally replaced by a fictitious one | the 7 vehicles of `malaysia_road_violations` (X-series and `WXY 12345`) | purple pill "Simulated · fictitious plate inserted" |
| `synthetic` | no vehicle in the footage at all | `simulated_events` of the road demo, purple lane vehicles, the Johor Bahru sighting, simulated gate arrivals | purple pill "Simulated · synthetic" |

**Tables.** 30 tables in seven groups; every column, natural key, trigger and retention rule is described in
[DATABASE_EN.md](DATABASE_EN.md):

| Group | Tables |
|---|---|
| system and users | `meta`, `settings`, `sites`, `users`, `sessions` |
| cameras and runs | `cameras`, `runs` |
| plates and alerts | `plates`, `sightings`, `alerts`, `alert_actions` |
| lanes and flow | `lane_events`, `lanes`, `lane_bins`, `crossings`, `flow_bins`, `withheld` |
| congestion and forecasts | `series_days`, `zone_minutes`, `calibrations`, `forecast_runs`, `forecast_points` |
| lists and the gate | `list_entries`, `visitor_passes`, `access_events` |
| privacy, work queue, exports | `reveals`, `audit_log` (append-only, hash-chained), `jobs`, `corrections`, `evidence_exports` |

The vehicle registry itself is **not** copied into this database: it stays in `data/registry/demo_registry.db`
(SIMULATED, built by `alpr/registry.py`) behind a `RegistryProvider` interface (`lookup`, `near`, `sightings`,
`is_simulated`). An authorised data source would be a second implementation of the same interface, chosen in
`settings.py`; nothing else changes.

## 4. API

JSON under `/api/v1`, OpenAPI documentation at `/docs`. Authentication by session cookie; every POST / PUT / PATCH /
DELETE needs the `X-CSRF-Token` header. Lists are paginated (`page`, `page_size` -> `{items, total, page, page_size}`).
Masking and audit happen in the service layer, so the API cannot bypass them.

59 endpoints (54 paths) in 12 groups (auth, overview, plates, alerts, traffic, forecast, gate, lists, jobs, reports, admin,
media); the list with the roles allowed to call each one is in [API_EN.md](API_EN.md), and the interactive reference
is generated at `/docs`.

**Service functions** (what the API wraps and what the AI assistant will call), e.g.
`plates.search(conn, user, SightingQuery) -> Page[SightingOut]`, `plates.timeline_of(conn, user, plate) -> Timeline`,
`alerts.transition(conn, user, alert_id, to, note, corrected_plate) -> AlertDetail`,
`forecast.state(conn, user, day, at) -> ForecastState`,
`forecast.whatif(conn, user, day, section, capacity_pct) -> WhatIfOut`, `gate.identify(conn, user, plate) -> Identity`.
Each has type hints, a docstring and pydantic output models, so an LLM tool schema can be generated from it; because
they take the user, the assistant inherits the same permissions, masking and audit (logged with `via: assistant`).
The full list is in [API_EN.md](API_EN.md), section 4.

## 5. Roles, privacy and audit

**Who sees what (Decision).**

| Page / action | police | guard | traffic | admin |
|---|---|---|---|---|
| Home after login | Overview | Guardhouse | Overview | Overview |
| Plate search | full plates, road and lane cameras | own site's gate only (in the access log) | masked; searching *by plate number* counts as a reveal and needs a reason | masked |
| Show full plate | — | — | reason required, audit-logged | reason required, audit-logged |
| Videos of plate cameras, scene frames | yes | own gate | no (they show full plates; scene frames only for a revealed record) | no (same) |
| Alert centre | all road / lane alerts, workflow | own gate's alerts, workflow | — (lane events appear in Traffic analytics) | read-only, masked |
| Cross-camera timeline, evidence pack | yes | — | — | — |
| Guardhouse, access log, residents and visitors, site ban list | — | yes | — | — |
| Watch list and blacklist | yes | — | — | — |
| Traffic analytics, congestion forecast | read | — | yes | read |
| Upload & analyse | yes | — | yes | yes |
| Reports | alerts, traffic | gate | traffic | traffic, alerts |
| Users, data import, settings, retention, audit log | — | — | — | yes |

**Masking (addition 1).** Police and guards see full plates and crops. Traffic managers and admins see `VJF ****`
(the letter prefix stays, the rest is always four stars, so the length is not revealed) and a crop that is **blurred
on the server** (a CSS blur would still send the sharp image). "Show full plate" opens a dialog with a required reason
(*accident investigation*, *incident review*, *data-quality check*, *audit*, *other* + note); the reveal is written to
`reveals` and the audit log and lasts 15 minutes for that record. Masked roles get no video of plate cameras (the
annotated videos show every plate in full) and no scene frame unless the record is revealed; lane and congestion
videos stay available to them.

**Audit (who, when, what, why).** Logged: login and logout (also failures), every search (filters and the
*purpose* chosen on the search form), opening a record, viewing a full crop or scene frame, every reveal, every CSV /
PDF export and evidence pack, alert transitions, list and pass changes, gate decisions, uploads, settings, purges and
user changes. Only admins can read the log; the hash chain can be verified from the admin page.

**Retention (Decision: defaults, editable by the admin).** Ordinary sightings (no alert, no list match) are deleted
with their crops **30 days** after they were recorded; sightings tied to an alert are kept until the alert is closed,
then **180 days**; access-log entries **90 days**; the correction pool **30 days**; the audit log **365 days**. A daily
task and an admin "preview / purge now" button do the deletion and log it. (Demo data counts from its import time.)

**Wording on the pages and in the docs:** *"The privacy controls follow the data-minimisation principle of Malaysia's
PDPA 2010: each role sees only what its task needs, ordinary sightings are kept only for a set period, and every
access is recorded."* The PDPA binds private bodies such as the property manager running a guardhouse; it does not
apply to the Federal and State Governments, so the system does **not** claim that police or JPJ use is bound by it —
the same controls are applied to those roles as good practice. Checked in the official text (Laws of Malaysia,
Act 709, online updated text as at 15 June 2016): s. 3(1) (not applicable to the Federal and State Governments),
s. 6(3)(c) (adequate but not excessive) and s. 10(1) (not kept longer than necessary); the 2024 amendment was not
re-checked for these sections.

**"Simulated" everywhere.** Every row with `provenance` other than `camera` carries the purple pill on every page,
in CSV exports (`provenance` column), in PDFs and in the evidence pack. An `UNMATCHED` alert always shows: *"Not in
the SIMULATED registry. This does not mean a real vehicle is unregistered."*

## 6. Demo data: what is real and what is simulated

| Camera (seed) | Run (file) | Clock used | What it gives | Real / simulated |
|---|---|---|---|---|
| KL roadside ANPR (demo), 3.1390, 101.6869 (notional) | `malaysia_road_anpr_demo_30s` (window 100–130 s) | notional 2026-10-14 08:00:00 + second (from `video_start_time`) | 8 plates; 4 simulated events with 5 registry alerts + 1 watch-list hit (`XSE 5005`) | plates real; events synthetic |
| same camera | `malaysia_road_violations_demo` (0–155 s) | same notional clock | 19 plates (18 confirmed, 1 provisional `VCV 1555`), 8 registry alerts + 1 watch-list hit, flow 19 | 7 inserted fictitious plates, 12 real |
| Guardhouse gate (demo condominium) | `malaysia_guardhouse_demo_30s` (66–96 s) | notional 2026-10-14 07:30:00 (seed) | `JPP 3473`, `JMT 8577`; flow 1 | real; simulated arrivals added for the gate demo (section 7.3) |
| Taman Sri Selayang lane camera | `malaysia_selayang_lanes_demo` (0–120 s) | notional 2026-10-14 08:30:00 (seed) | 355 crossings, 8 lanes, 12 events (5 simulated violations / hazard, 7 real lane changes), 10 withheld | real flow; violations synthetic |
| JB CIQ approach, Camera 02 (A) | `jb_ciq_cam02_2023-03-22.csv` + calibration + `jb_ciq_forecast_timelapse.forecast.json` | real on-screen clock, Wed 22 Mar 2023 00:24–09:54 | per-zone fill each minute; LightGBM +5 / +15 / +30 (trained on C) | real measurements |
| JB CIQ approach, IPC camera (C) | `jb_ciq_ipc_2023-03-21.csv` + calibration | real, Tue 21 Mar 2023 02:52–10:06 | per-zone fill; forecasts computed at import (trained on A) | real |
| Causeway approach (B) | `jb_causeway_cam1_2024-01-12.csv` | real, Fri 12 Jan 2024 00:25–11:12 | occupancy only (not calibrated, not used for forecasting) | real |
| Johor Bahru (SIM) | `DEMO_SIGHTINGS` in `alpr/registry.py` | 2026-10-14 07:45 | `XSD 4004` seen ~293 km away | synthetic |

**Data gaps, stated on the pages:** the registry is SIMULATED (no JPJ access); no real vehicle appears at two of our
cameras (different places and dates), so cross-camera chains are demonstrated with simulated sightings; the CIQ
cameras measure how full the road is, not vehicles per minute or speed; the gate camera reads plates on the entry lane
only; real vehicles' lane violations are withheld (counted, not shown); the forecast is validated on 2 days; CPU-only
analysis runs at about 10 FPS. Note: both roadside runs come from the same footage at the same notional time (the
second one with plates replaced), so a real plate can appear in both; each result row names its run.

## 7. The six agreed additions (build order after the base)

### 7.1 Role-based plate masking
Section 5. Implemented once in `privacy.py` and applied by the service layer to every response, CSV, PDF and image.

### 7.2 Click-to-timestamp playback
Clicking any record in plate search or the alert centre seeks the video player to `first_sec − 1` s and plays.
Demo videos are the annotated dashboard renders, which start at the run's `window_sec[0]`, so the player seeks to
`first_sec − window_sec[0] − 1` (stored as `runs.video_offset_sec`). Uploaded videos play the source video at
`first_sec − 1` (or the annotated render, if one was requested). The media endpoint answers HTTP range requests so the
browser can seek. Lane events seek the same way with `time_sec`.

### 7.3 Guardhouse mode (the guard's home page)
Video on the left; on the right a big card with the latest plate and its identity colour, and two big buttons,
**Allow (log entry)** and **Stop & note**, both written to the access log. Identity, in this order:
red = on the site's ban list; green = resident; teal (blue-green) = visitor with a pass valid now; yellow =
unregistered visitor (or a pass outside its window). Demo: the page replays `demo/malaysia_guardhouse_demo_30s.mp4`
and shows each plate's card when the video reaches its `first_sec` (minus the 66 s offset): `JPP 3473` at 0:00
(resident in the SIMULATED demo list, green), `JMT 8577` at 0:09 (not on any list, yellow: the guard can issue a pass
on the spot). Two simulated arrivals with fictitious plates (purple "no vehicle in the video" note) show the other
colours: a pre-registered visitor `XVP 3003` at 0:16 (teal) and a banned vehicle `XBN 4321` at 0:24 (red). In replay
mode a plate counts as arriving when the video reaches it, at the real clock, so passes and overstays use real time.

### 7.4 Time-bounded visitor passes and the overstay list
A pass has a plate, a unit number, an optional label (e.g. *delivery*; no names or IC numbers) and a validity window
(default now -> now + 4 h). Visitors still inside after **N hours** (setting, default 4) or after their pass ended are
pinned to the top of "Inside now". **Limitation:** the guardhouse camera only reads plates on the entry lane (exit-lane
plates are too small and oblique), so exits come from the guard's **Exited** button, or from simulated exits in the
demo seed.

### 7.5 Cross-camera timeline
For a plate seen at several cameras: a time-ordered chain with distance, time gap and implied speed between
consecutive sightings, flagging impossible travel with `alpr.registry.check_travel` (more than 250 km/h, legs
under 2 km ignored). Inputs: the `sightings` table, camera `lat/lon`, and the registry's external sightings. Demo
(all labelled Simulated): `XSD 4004` at Johor Bahru (SIM) 07:45:00 -> KL roadside 08:00:23 (inserted plate):
"293 km in 15 min 24 s: 1141 km/h needed - impossible (more than 250 km/h): cloned plate?" -> KL roadside 08:02:04
(synthetic event), "0 km (same site)".

### 7.6 Evidence pack (HTML and PDF) for an alert
Contents: case summary (alert, plate, time, camera, run, provenance); scene frame; best plate crop; registry
comparison (SIMULATED record vs what the camera saw: body type vs detected type, road-tax expiry vs observation
date, status; for a cloned plate the conflicting sightings); speed ± error when the event has one (`value`,
`error_1sigma`); legal reference; processing details (git commit, profile, SHA-256 of the results JSON); SHA-256 of
every embedded image with the sentence *"These hashes only prove that the image files were not changed after this
pack was exported."*; exported by / at / audit id.
Rules: every page carries a prominent **"Course project demo — not an official document"** mark; no PDRM / JPJ logos
and no imitation of official forms; Road Transport Act 1987 sections are cited only after checking them against the
official text (Attorney General's Chambers, Laws of Malaysia), with the source; a section that cannot be verified is
left out.
**Pipeline change (opt-in):** crops and scene frames are not in the results JSON today. A new option
`--evidence DIR` (config `evidence_dir`, default off) saves each log entry's best plate crop and the full frame it
came from, and adds `evidence: {crop, scene, frame, time_sec, vehicle_box, plate_box}` to each plate row. Off by
default, so normal runs and their JSON are unchanged. Done: the four demo profiles were re-run with and without the
option and compared field by field (every existing field identical; only `evidence` and `processing` are new), and
the 85 earlier tests plus a new one pass (86/86). The images go to `outputs/evidence/` (gitignored); if they are
missing (fresh clone) the pack falls back to a frame of the committed annotated video and says so.
Demonstrated with the registry alerts of the violation demo (fictitious plates): the lane camera cannot read plates and
real vehicles' violations are withheld, so there is no real speeding case with a plate.

### 7.7 What-if "open more lanes" (last)
On the forecast page: a slider **+10 / +20 / +30 %** discharge capacity from the warning time, drawing the projected
queue next to the measured one. Model (deterministic point queue): `Q'(t) = max(0, Q'(t−1) + ΔQ(t) − p·μ)`, where
`ΔQ` is the measured change of the queue (fill × jam capacity of the zones, vehicles in view) and `μ` the discharge
capacity. The CIQ cameras measure only how full the road is, not vehicles per minute (TEAM_GUIDE section 10.6), so
`μ` is either typed in as an assumption or roughly estimated from the fastest sustained clearing of the queue (on day
A 0.83 vehicles in view per minute over the 10 minutes ending 07:33, a lower bound because cars keep arriving while
it clears). Result on day A with +20 % from the first warning (06:56): congested minutes 146 -> 7, peak 38 -> 28
vehicles in view; large because the queue beyond the camera's view is not seen (assumption below).
Labelled **"Estimate under stated assumptions"**, with the assumptions listed on the page: point queue at the head of
the section; arrivals unchanged by the measure; extra capacity fully available from the warning time; `μ` estimated or
assumed as shown; only the part of the queue inside the camera's view (when the queue is longer than the view the
measured fill saturates and the projection overstates the gain); measurement error 22–33 % per frame; validated on
2 days.

## 8. Pages for each role

The wireframes below are the approved design (their numbers are from the draft). The built pages follow them;
screenshots are in [DEMO_WALKTHROUGH_EN.md](DEMO_WALKTHROUGH_EN.md).

Every page has the same shell: left navigation (only the role's pages), a top bar with the user and role pill, a data
notice chip ("Demo data · notional clock" when showing demo runs) and the provenance legend. Style follows the
dashboard in `alpr/hud.py`: dark background `#0c0c0e`, frosted cards, white / grey labels, colour only for status
(green `#30d158` valid, yellow `#ffd60a` reading / unregistered, orange `#ff9f0a` medium / hazard, red `#ff453a` high /
violation, purple `#bf5af2` simulated, blue `#0a84ff` info, light blue `#64d2ff` forecast), Segoe UI Variable, plates
drawn as plate badges (light characters on black). Interface text is in English, like the existing dashboards.

### 8.1 Login
Username, password, sign in. Note: "Course project demo. Test accounts: `webapp/seed/demo_users.yaml` (local demo
only)." Data: `users`, `sessions`.

### 8.2 Overview (police, traffic, admin; the guard lands on the Guardhouse)

```
+- SECS Vision - Overview ------------------------- Demo data - notional clock -- A. Rahman (Police) [Sign out] +
| Overview      | Cameras                                                                                      |
| Plate search  | +-KL roadside ANPR-----+ +-Guardhouse gate----+ +-Selayang lanes-----+ +-JB CIQ cam 02-----+ |
| Alert centre  | | o Replay  2 runs     | | o Replay  1 run    | | o Replay  1 run    | | o Replay 22/3/23  | |
| Watch lists   | | 27 plates (+4 sim.)  | | 2 plates           | | 355 veh  8 lanes   | | fill 20 %         | |
| Upload        | | 13 alerts            | |                    | |                    | |                   | |
| Reports       | +----------------------+ +--------------------+ +--------------------+ +-------------------+ |
|               | Flow on Wed 14 Oct 2026 (demo day)        | Open alerts (18, all simulated)                 |
|               | 375 vehicles   ._.-'-._.-'  per 10 s       | o 10 high  o 1 hazard  o 7 medium               |
|               | Car 246  Motorcycle 111  Truck 17  Bus 1   | High  Tailgating  XTG 5006  Selayang 08:31:28    |
|               |                                            | High  Solid line  XSL 2002  Selayang 08:30:19    |
|               | Congestion - JB CIQ approach (replay of Wed 22 Mar 2023, 06:56)          Open forecast > |
|               | o Free flow now 20 %   +5 min 25 %   +15 min 53 %   +30 min 54 %   "Congestion in ~15 min" |
+---------------+--------------------------------------------------------------------------------------------+
```

| Element | Database | Source file and field |
|---|---|---|
| Camera cards | `cameras`, `runs`, counts of `sightings` / `alerts` | `seed/cameras.yaml`; `*.plates.json`: `source`, `profile`, `window_sec`, `plates[]` |
| Flow of the day | `flow_bins` (+ `lanes` for the lane camera) | `flow.total`, `flow.by_type`, `flow.per_10s[]` of each run (for the roadside camera the whole-clip run) |
| Open alerts | `alerts` where status is new / acknowledged | `plates[].alerts[]`, `simulated_events[].alerts[]`, `lanes.events[]` (violation, hazard) |
| Congestion | `forecast_points`, `calibrations` | `forecast.json`: `measured_fill[]`, `forecast_fill{}`; `congestion_fill` |

Variants: police see road and lane cameras and the alert list first; traffic managers see flow and congestion first
(alerts reduced to lane events, plates masked); admins also get a system card (database size, last import, queued
jobs, next purge, audit entries today).

### 8.3 Plate search (police full; traffic and admin masked)

```
+- Plate search -----------------------------------------------------------------------------------------+
| Plate [XSD 4OO4    ]  OCR tolerance ( )0 ( )1 (*)2   From [14 Oct 07:00]  To [14 Oct 09:00]             |
| Camera [All v]  Type [All v]  Status [Confirmed + provisional v]  Simulated [include v]                 |
| Purpose [Alert follow-up v]  Ref [        ]                                  [Search]  [Export CSV]     |
+------------------------------------------------------+-------------------------------------------------+
| [crop] XSD 4004  =  Car   KL roadside  08:00:23-37   | +-------------- video of the run -------------+ |
|        Cloned plate  Simulated: inserted     > 0:22  | |   seeks to first_sec - offset - 1 s, plays  | |
| [plate] XSD 4004 =  Car   KL roadside  08:02:04      | +---------------------------------------------+ |
|        Cloned plate  Simulated: synthetic    > 0:23  | XSD 4004  Car  confidence 74 %  40 readings      |
|                                                      | Registry (SIMULATED): Hatchback, white, Perodua  |
|                                                      | Myvi, road tax to 2027-01-31, status ok          |
| 2 results                                            | [Timeline across cameras >]                      |
+------------------------------------------------------+-------------------------------------------------+
```

Fuzzy search: OCR-aware edit distance over the distinct plates (look-alike pairs such as O/0, I/1, B/8, S/5, Z/2,
H/R, M/W cost less), tolerance 0–2 characters; each hit shows its distance. Provisional readings (`TENTATIVE`) are
shown in yellow as "Reading" and never raise alerts; `valid_format: false` shows "Non-standard format". For masked
roles the plate column shows `XSD ****` and a blurred crop, with "Show full plate" on the detail pane.

| Element | Database | Source file and field |
|---|---|---|
| Rows | `sightings` | `plates[]`: plate, compact, confidence, votes, status, valid_format, vtype, region, track_ids, first/last_frame, first/last_sec; `simulated_events[]`: plate, compact, vtype, time_sec |
| Time | `sightings.seen_at` | `clock_origin` (seed / `video_start_time`) + `first_sec` |
| Crop | `sightings.crop_path` | new opt-in `evidence.crop`; synthetic rows: the rendered plate image (`alpr.hud.plate_image`) |
| Alerts | `alerts` | `plates[].alerts[]` {code, severity, message} |
| Registry | `RegistryProvider.lookup` | `data/registry/demo_registry.db` (SIMULATED) |
| Video | `runs.video_path`, `video_offset_sec` | `demo/<run>.mp4`, `window_sec[0]` |

### 8.4 Alert centre (police; guard sees its gate's alerts; admin read-only)

```
+- Alert centre ----------------------------- [New 18] [Acknowledged 0] [Resolved 0] [False alarm 0] ----+
| Severity [All v]  Code [All v]  Camera [All v]  [x] include simulated                                    |
+--------------------------------------------------+------------------------------------------------------+
| o High   Tailgating     XTG 5006  Selayang In1 08:31:28 | Blacklisted - "SIMULATED: reported stolen"       |
| o High   Solid line     XSL 2002  Selayang In4 08:30:19 | +-video----------------+ [crop] [scene frame]    |
| ...                                                     | +----------------------+                         |
| o High   Blacklisted    XSB 2002  KL rdside 08:01:46    | Registry (SIMULATED)   vs   observed            |
|          Simulated: inserted plate          New  <-sel. |  Sedan, silver               Car, 08:01:46       |
| o High   Cloned plate   XSD 4004  KL rdside 08:00:23    |  status blacklisted                              |
| o Hazard Stopped in lane XST 3003 Selayang In3 08:31:28 | [Acknowledge] [Resolve] [False alarm...]         |
| o Medium Road tax expired XSA 1001 KL rdside 08:00:00   | [Evidence pack: HTML | PDF]  [Timeline >]        |
| ...                                                     | History: created by import 23 Oct 10:02 ...       |
+---------------------------------------------------------+--------------------------------------------------+
```

Sorting (Decision): **High** (red: blacklisted, cloned plate, wrong way, speeding, solid line, tailgating) ->
**Hazard** (orange: stopped in a live lane) -> **Medium** (orange: road tax expired, not registered, type mismatch,
illegal format, watch-list hit), then newest first. "False alarm" asks for a note and optionally the correct plate;
the plate and its crop go to the correction pool (retention applies).

| Element | Database | Source file and field |
|---|---|---|
| Registry alerts | `alerts` (source registry) | `plates[].alerts[]`, `simulated_events[].alerts[]` |
| Lane violations / hazards | `alerts` (source lane) + `lane_events` | `lanes.events[]`: code, severity, time_sec, lane, from_lane, message, vtype, track_id, plate, simulated, value, error_1sigma |
| List hits | `alerts` (source list) | police watch list / blacklist matched at import or upload |
| Workflow | `alert_actions` | — |

### 8.5 Cross-camera timeline (police)

```
+- Timeline - XSD 4004 ------------------------------------------------- Simulated --------------------+
| 07:45:00  Johor Bahru (SIM)        external sighting (simulated registry)                               |
|    |      293 km in 15 min 23 s  ->  1141 km/h   IMPOSSIBLE (> 250 km/h): cloned plate?                  |
| 08:00:23  KL roadside ANPR (demo)  run "Violation check" - fictitious plate inserted          [> play]  |
|    |      0 km in 1 min 40 s (same camera)                                                              |
| 08:02:04  KL roadside ANPR (demo)  run "Demo window" - synthetic event                        [> play]  |
| Rule: alpr.registry.check_travel (limit 250 km/h, legs under 2 km ignored)                              |
+---------------------------------------------------------------------------------------------------------+
```

Data: `sightings` of the plate, `cameras.lat/lon`, `RegistryProvider.sightings(compact)`.

### 8.6 Guardhouse (guard home)

```
+- Guardhouse - Demo condominium gate ------------------------------------------- 07:31 -- guard1 (Guard) +
| +----------------------------- replay video ---------------------+ +------------------------------------+ |
| |                                                                 | |      [  J M T   8 5 7 7  ]          | |
| |                                                                 | |  o UNREGISTERED VISITOR (yellow)    | |
| |                                                                 | |  Truck - 07:31:15 - entry lane      | |
| |                                                                 | |  not a resident, no valid pass      | |
| +-----------------------------------------------------------------+ |  +--------------+ +---------------+ | |
|  > || <<  0:09 / 0:30   replay of recorded footage                  |  |    ALLOW     | |  STOP & NOTE  | | |
|                                                                     |  | (log entry)  | |               | | |
| Inside now (visitors)                            overstays first    |  +--------------+ +---------------+ | |
| ! XVA 5120  unit B-07-3  in 6 h 12 min  pass ended 2 h ago [Exited] |  [+ Issue visitor pass]             | |
|   XVP 3003  unit A-12-1  in 0 h 03 min  pass to 18:00     [Exited]  +------------------------------------+ |
| Recent: 07:31 JPP 3473 allowed (resident) ...                                                            |
+----------------------------------------------------------------------------------------------------------+
```

| Element | Database | Source file and field |
|---|---|---|
| Replay cards | `sightings` of the gate run (+ simulated arrivals) | `malaysia_guardhouse_demo_30s.plates.json`: plate, vtype, first_sec; `window_sec[0]` = 66 |
| Identity colour | `list_entries`, `visitor_passes` | guard pages; demo entries in `seed/demo_lists.yaml` |
| Buttons, inside now, recent | `access_events` | — |

### 8.7 Access log (guard)
Search by plate (fuzzy), date, decision, identity; columns: time, plate, identity colour, decision, direction, unit /
pass, guard, note, provenance; CSV export. Data: `access_events` (+ `sightings` for the camera reading).

### 8.8 Residents and visitors (guard)
Tabs: **Residents** (plate, unit, label, active; add / deactivate) · **Visitor passes** (plate, unit, label, valid
from–to, status; issue / cancel) · **Inside and overstays** · **Banned at this site** (plate, reason). Data:
`list_entries`, `visitor_passes`, `access_events`. Only unit numbers and plates are stored, no names or IC numbers.

### 8.9 Watch list and blacklist (police)
Table: plate, list, reason (required), added by / at, expiry, active, number of past sightings (link to the search);
the SIMULATED registry status shown beside it. New imports and uploads are matched against the active lists and raise
`WATCHLIST` (medium) or `BLACKLIST` (high, source list) alerts. Data: `list_entries`, `RegistryProvider`.

### 8.10 Traffic analytics (traffic; police and admin read)

```
+- Traffic analytics - Taman Sri Selayang lane camera - 120 s replay ---------------------- [Export CSV v] +
| [355 vehicles] [177.5 veh/min] [occupancy: inbound 15 % - outbound 72 %] [speed: In1 87 km/h ... ]      |
| Flow per 10 s by vehicle type (stacked)             | Per lane                                           |
| ||| ||| ||| ||| ||| ||| ||| ||| ||| ||| |||          | Lane  Dir  Count veh/min Occ   Density  Speed      |
|                                                     | In1   down   42   21.0   11 %   15 /km   87 +-6    |
| Lane metric [Occupancy v]  (one line per lane)      | ...                                                |
|  ~~~ In1  ~~~ In2  ~~~ Out1 ...                      | Out4  up     30   15.0   91 %  254 /km  8.5 +-0.6  |
+-----------------------------------------------------+----------------------------------------------------+
| Lane events:  o 08:30:08 Wrong way In1 XWW **** Simulated [>]   o 08:30:13 Lane change Out4->Out3 [>] ... |
| Withheld (real vehicles, public footage): speeding 7, tailgating 3 - counted, not shown                  |
+-----------------------------------------------------------------------------------------------------------+
```

| Element | Database | Source file and field |
|---|---|---|
| KPI tiles, per-lane table | `lanes` | `lanes.per_lane{}`: count, by_type, veh_per_min, occupancy, density, density_unit, zone_length_m, mean_speed_kmh |
| Flow chart | `flow_bins`, `crossings` | `flow.per_10s[]`; `lanes.crossings[]` (time_sec, lane, vtype, direction) |
| Lane metric chart | `lane_bins` | `lanes.per_10s{lane}[]`: count, veh_per_min, occupancy, mean_vehicles, density_veh_km, mean_speed_kmh |
| Events, withheld | `lane_events`, `withheld` | `lanes.events[]`; `lanes.withheld_violations`, `withheld_note` |
| Speed error | run | `lanes.calibration.note` (6.5 % 1σ, TEAM_GUIDE 5.3) |

The same page shows the counting-line flow of the roadside and gate cameras (`flow.*`).

### 8.11 Congestion forecast (traffic; police and admin read)

```
+- Congestion forecast - JB CIQ approach -------- Day [Wed 22 Mar 2023 - Camera 02 v] -- LightGBM, trained on C +
| Replay clock  [<] 06:56 [>]  ======*=========================  [> play]                                        |
| +- To CIQ ---------------------+ +- TC1 queue head --+ +- TC2 ------+ +- TC3 ------+ +- TC4 ------+            |
| | o Congestion in ~15 min      | | fill ...          | | ...        | | ...        | | ...        |            |
| | now 20 %   +5 min 25 %       | +-------------------+ +------------+ +------------+ +------------+            |
| | +15 min 53 %   +30 min 54 %  |  +- time-lapse video, synced to the clock (05:30-09:50) -+                    |
| | error band +-9 / 15 / 15 pts |  +--------------------------------------------------------+                    |
| +------------------------------+                                                                                 |
| Forecast vs actual   [+5] [+15] [*+30]    MAE this day 0.077 (expected band 0.150)                              |
|  1.0 |            measured ---   forecast made 30 min earlier - - -   congestion 0.5 .....                     |
|  0.5 |....................../~~~\_/~~~~~~~~~~~~~~                                                               |
|  0.0 +---------------------------------------------------- 00:30 ... 09:50 (onsets 07:13, 08:17)              |
| What-if: more capacity  [+0] [+10] [*+20] [+30] %  from 06:56 (first warning)   mu [0.8] veh/min (estimated)    |
|  queue (vehicles in view): measured --- projected ---     peak, congested minutes, time to clear (illustrative)  |
|  Estimate under stated assumptions:  - point queue ...  - arrivals unchanged ...  - only the part in view ...    |
+------------------------------------------------------------------------------------------------------------------+
```

At 06:56 on day A the model first warns (the queue starts at 07:13, 17 minutes later); this is the traffic manager's
demo scenario. Section states: free < 25 % of jam capacity, dense 25–50 %, queue >= 50 % (TEAM_GUIDE 10.2).

| Element | Database | Source file and field |
|---|---|---|
| Section cards, now | `zone_minutes` | CSV `<entity>.fill`, `.occupancy`, `.queue` per `time` |
| +5 / +15 / +30, band | `forecast_points`, `forecast_runs` | `forecast_fill{5,15,30}[]`, `error_band_mae{}`; zone and day-C sections computed at import with `alpr.forecast.LightGBMForecaster` (same recipe as `forecast_demo.py`) |
| Forecast vs actual | `forecast_points` | `measured_fill[]` vs `forecast_fill{h}[i − h]`; onsets from `alpr.forecast.onsets` |
| Vehicles, capacity | `calibrations` | `capacity{}`, `groups{}.loo_mae` |
| Time-lapse video | run | `demo/jb_ciq_forecast_timelapse.mp4` (05:30–09:50, 10 s of camera time per frame) |
| What-if | computed | section 7.7 |

### 8.12 Upload & analyse (police, traffic, admin)

```
+- Upload & analyse ----------------------------------------------------------------------------------+
| File [choose...]  Camera [KL roadside ANPR v | + new]  Scene [Roadside ANPR v]                        |
| Recorded at [2026-10-23 09:00]   [ ] also render the annotated video (about 3x slower)  [Start]       |
| Jobs                                                                                                 |
| #12 video  clip.mp4   ########....  68 %   612/900 frames - 10.4 FPS - about 0:28 left  [Cancel]     |
| #11 image  car.jpg    done - 1 plate                                                  [Open results] |
+------------------------------------------------------------------------------------------------------+
```

Images run `run_image.py`; videos run `run_alpr.py --headless --backend openvino` with the chosen scene's profile,
the recording time as `video_start_time`, and **no simulated events**. Registry checks on uploads use only
`ILLEGAL_FORMAT` and `DUPLICATE`: the registry is simulated, so a real plate "not in the registry" says nothing about
the vehicle and `UNMATCHED` is switched off; the watch / ban lists still apply. Playback: the annotated video if it was
rendered, otherwise the source video (a browser-playable H.264 copy is made with the bundled ffmpeg when needed).
Data: `jobs`, then the same tables as an import.

### 8.13 Reports
Daily or weekly; kind traffic (flow by camera / type / lane, congestion episodes), alerts (by code, status, handling
time) or gate (entries, stops, overstays); CSV or PDF (PDF with the demo mark). Data: aggregates over the tables above.

### 8.14 Admin
**Users** (add, deactivate, reset password, site) · **Data** (cameras, runs with source file and SHA-256, re-import,
forecast runs) · **Jobs** · **Privacy and audit** (audit log with filters and the hash-chain check, retention
settings, purge preview, correction pool size) · **Settings** (overstay hours, purpose codes).

## 9. CI

`.github/workflows/ci.yml`, on every push and pull request: Ubuntu, Python 3.12, pip cache; install the CPU build of
torch 2.3.1 from the PyTorch CPU index (no CUDA download), then a short `requirements-ci.txt` (only what the tests
import: numpy < 2, opencv, pyyaml, pillow, ultralytics, statsmodels, lightgbm, the web packages); run
`python alpr/tests/test_alpr.py` (86 tests, no models or GPU) and `python webapp/tests/test_web.py` (22 API tests
with FastAPI TestClient on a temporary database filled from the committed demo JSONs). The README has the badge.
Rehearsed locally in a fresh venv built the same way: 86/86 and 22/22 (Windows; Linux itself cannot be run on this
laptop: no WSL / Docker, so the first GitHub run is the Linux check).

## 10. Tests and documents (delivered)

- API tests: login and lockout, role scoping, masking and reveal (audit row written), fuzzy search, CSV export, alert
  workflow and correction pool, gate identity colours and overstay, impossible-travel timeline, evidence pack (mark
  and hashes present), what-if (more capacity never lengthens the queue), importer idempotency and counts, CSRF,
  append-only audit log, retention purge.
- Documents in English and Chinese (`docs/system/`, [index](README.md)): requirements specification, this
  architecture, database schema, API overview, test plan and report, demo walkthrough with screenshots of the three
  defence scenarios; section 13 of `TEAM_GUIDE.md` / `TEAM_GUIDE_EN.md`; README link and CI badge.

## 11. Designed for later

- **Live RTSP cameras:** `cameras.stream_url` and `status`; a live ingest worker would write sightings through the
  same importer functions and push them to pages over Server-Sent Events (the guardhouse card already consumes a stream
  of plate cards).
- **Lanes drawn in the browser:** camera geometry (lanes, count line, calibration points) stored per camera and
  exported in the `alpr/config.yaml` profile format.
- **Public congestion outlook without plates:** the forecast services return only aggregated section data; a public
  page needs no new data access.
- **Push notifications:** every alert is created by one function, the single hook point.
- **AI assistant:** service functions as tools (section 4), acting as the logged-in user.

## 12. Build order

1. Base: schema, importer, login and roles, core pages (overview, plate search, alert centre, traffic analytics,
   forecast, upload, lists, privacy and audit, reports), with click-to-timestamp built into search and alerts.
2. Additions in the agreed order: masking -> click-to-timestamp -> guardhouse mode -> visitor passes and overstays ->
   cross-camera timeline -> evidence pack (with the opt-in pipeline output and its regression check) -> what-if.
3. CI, documents, screenshots, summary for review. Nothing is committed or pushed without your approval.

All three steps are done.

## 13. Decisions (confirmed) and changes during the build

The eight decisions below were confirmed before the build.

1. New top-level package `webapp/` (not inside `alpr/`).
2. Plain `sqlite3` with one data-access module, no ORM.
3. Role scoping of section 5: guards see only their site's gate; police see road and lane cameras, not condo gates;
   admins see masked plates; traffic managers' search by plate number counts as a reveal.
4. Retention defaults: 30 days ordinary sightings, 180 days after an alert is closed, 90 days access log, 30 days
   correction pool, 365 days audit log.
5. Gate demo: the real gate plate `JPP 3473` is a *resident* in the SIMULATED demo list (a neutral status);
   `JMT 8577` stays unlisted (yellow); the teal and red cases use simulated arrivals with fictitious X-plates.
6. Uploads: registry rules limited to `ILLEGAL_FORMAT` and `DUPLICATE` (no `UNMATCHED` against a simulated registry).
7. Evidence crops and scene frames stay out of git (`outputs/evidence/`); fresh clones fall back to frames of the
   committed annotated video.
8. CI installs the CPU build of torch (about 200 MB per run, cached) so all 85 tests (now 86) run on GitHub.

Changes during the build:

1. **Police read the congestion cameras too** (overview card and forecast page), as the role table of section 5
   always said; the first build had left the congestion cameras out of the police camera scope.
2. **No plate-camera video for masked roles.** Found while testing: the annotated videos of the roadside and gate
   cameras show every plate in full, so traffic managers and admins get no video of plate cameras (the API answers
   403) and no scene frame unless the record is revealed. Lane and congestion videos stay available to them.
3. **21 alerts in the demo database** instead of the 18 of the wireframes: the police watch list (`XSE 5005`) hits once
   on each roadside run, and the gate's banned arrival raises `BANNED_AT_SITE`. Police see 20 open alerts (10 high,
   1 hazard, 9 medium); the police overview counts 374 vehicles (KL roadside 19 + Selayang 355; the gate is not in the
   police scope).
4. **Overview date picker** (default: the latest day with camera data), because uploads add new days.
5. **Crossings outside every lane** are reported (4 of the 355 Selayang crossings, in the lay-by), so the per-lane
   counts (351) visibly add up to the total.
6. **Speed:** the fuzzy plate search is vectorised with numpy and cached per database version (100,000 distinct
   plates: 34 ms exact, 130–140 ms with tolerance 1–2; about 5 s before); the congestion onsets and first warnings
   are cached the same way (overview 300 -> 34 ms).
7. **CSRF token only in the `X-CSRF-Token` header** (the sign-out form posts to its own page route).
8. **SQL location:** the queries are plain SQL in the service modules, run through the `db.py` helpers, rather than
   all in one module; [DATABASE_EN.md](DATABASE_EN.md) section 9 lists the few SQLite-specific parts a move to
   PostgreSQL would touch.
9. **Legal references** of the evidence pack are in `webapp/legal.yaml`, each section checked in two official texts
   (MOT copy of the Attorney General's Chambers text as at 1 February 2013; JPJ copy as at 15 October 2023);
   penalties are not quoted.
10. **Evidence pack fallback frame** (fresh clone without the opt-in crops): a frame of the committed annotated video
    70 % of the way through the vehicle's track, where the vehicle is fully in view.
