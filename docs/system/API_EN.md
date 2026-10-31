# SECS Vision web system: API overview

[中文版 / Chinese version](API_ZH.md) · System documents: [index](README.md)

The complete, always up-to-date reference is generated from the code: start the server and open
**`http://127.0.0.1:8000/docs`** (interactive OpenAPI / Swagger UI) or `/openapi.json`. This page explains the
conventions and lists every endpoint with the roles that may call it.

## 1. Conventions

| Topic | Rule |
|---|---|
| Base path | JSON endpoints under `/api/v1`; images and videos under `/media` |
| Login | `POST /api/v1/auth/login` `{username, password}` sets the HttpOnly session cookie `secs_session` and returns `{user, csrf_token}`. Five wrong passwords lock the account for 5 minutes. Sessions last 8 hours (setting `session_hours`). |
| CSRF | every `POST` / `PUT` / `PATCH` / `DELETE` must send the session's token in the `X-CSRF-Token` header (`GET /api/v1/auth/me` returns it again) |
| Errors | JSON `{"detail": "..."}`: 401 not logged in · 403 the role may not do this (or wrong CSRF token) · 404 not found · 422 invalid input (e.g. a missing purpose or reason) |
| Pages of results | `page`, `page_size` -> `{items, total, page, page_size}` |
| Privacy | role scoping, plate masking, reveals and the audit log are applied in the **service layer**, so no endpoint can bypass them. Masked roles (traffic, admin) receive `VJF ****` and blurred crops. |
| Purpose | `GET /sightings` (and its CSV) requires `purpose` (Alert follow-up, Investigation (case reference), Patrol check, Gate operations, Incident at site, Traffic study, Data-quality check, Audit), written to the audit log with the filters |
| Simulated data | every observation carries `provenance` (`camera`, `inserted_plate`, `synthetic`) and a readable `provenance_label`; CSV files have a provenance column |
| Times | Malaysia time, ISO 8601 text (`2026-10-14T08:00:23.000`) |

## 2. Endpoints

Roles: **P** police · **G** guard · **T** traffic · **A** admin. "masked" = plates masked for that role.

### Authentication and overview

| Method and path | Roles | What it does |
|---|---|---|
| `POST /auth/login` | all | log in (body `{username, password}`) |
| `POST /auth/logout` | all | end the session |
| `GET /auth/me` | all | the logged-in user and the CSRF token |
| `GET /overview?date=` | P T A | camera cards, the day's flow, open alerts by severity, congestion at the first warning; `date` defaults to the latest day with camera data, `dates` lists the others |
| `GET /cameras` | all (scoped) | cameras this role may see, with counts |
| `GET /runs?camera_id=` | all (scoped) | analysis runs (video URL only where the role may play it) |

### Plates

| Method and path | Roles | What it does |
|---|---|---|
| `GET /sightings?plate=&tolerance=&start=&end=&camera_id=&run_id=&vtype=&status=&provenance=&has_alerts=&purpose=&reference=&reveal_reason=&page=&page_size=` | P, G (own gate), T A masked | fuzzy plate search: look-alike characters cost 0.5, `tolerance` 0–2, `*` / `?` wildcards (not for masked roles); `purpose` required; a masked role searching by plate number must give `reveal_reason` (counts as a reveal, exact match only) |
| `GET /sightings/export.csv?…` | same | the whole result as CSV (masked for masked roles), audit-logged |
| `GET /sightings/{id}?purpose=` | same | one record: alerts, video position, scene frame, SIMULATED registry record (police only) |
| `POST /sightings/{id}/reveal` | T A | "Show full plate": body `{reason_code, note}`; full plate and sharp crop for 15 minutes; audit-logged |
| `GET /plates/{plate}/timeline` | P | cross-camera timeline: legs with distance, time gap, implied speed, impossible-travel flag; near plates (look-alikes) |

### Alerts

| Method and path | Roles | What it does |
|---|---|---|
| `GET /alerts?status=&severity=&code=&camera_id=&include_simulated=&plate=&page=&page_size=` | P, G (own gate), A (read-only, masked) | sorted High / violation -> Hazard -> Medium, newest first |
| `GET /alerts/counts` | same | counts by status and severity |
| `GET /alerts/{id}` | same | detail: history, registry vs observed comparison, speed ± error, allowed next steps |
| `POST /alerts/{id}/transition` | P, G (own gate) | body `{to, note, corrected_plate}`: new -> acknowledged -> resolved / false_alarm (note required to close); `corrected_plate` adds the crop to the correction pool |
| `GET /alerts/{id}/evidence?format=html\|pdf` | P | evidence pack; recorded in `evidence_exports`, the alert history and the audit log |

### Traffic analytics

| Method and path | Roles | What it does |
|---|---|---|
| `GET /traffic/runs` | P T A | runs with a counting line or lanes |
| `GET /traffic/summary?run_id=` | P T A | KPIs, per-lane table, vehicles outside every lane, withheld counts, speed note |
| `GET /traffic/flow?run_id=&by=type\|lane\|time&bin=` | P T A | vehicles per time bin (`bin` in seconds, 1–600, default 10) |
| `GET /traffic/lanes/series?run_id=&metric=occupancy\|density\|speed\|flow\|count` | P T A | one series per lane |
| `GET /traffic/events?run_id=` | P T A (masked) | lane events with video positions |
| `GET /traffic/export.csv?run_id=&what=crossings\|lane_bins\|flow_bins\|lanes\|events` | P T A | CSV |

### Congestion forecast

| Method and path | Roles | What it does |
|---|---|---|
| `GET /forecast/days` | P T A | camera-days with sections, first warning, onsets, time-lapse video |
| `GET /forecast/state?day=&at=HH:MM` | P T A | every section at that minute: now, 5-min mean, vehicles in view, state, +5 / +15 / +30 with error band, status text and warning |
| `GET /forecast/accuracy?day=&section=&horizon=5\|15\|30` | P T A | measured vs the forecast made h minutes earlier; MAE, RMSE, onsets, warning minutes, precision, recall |
| `GET /forecast/whatif?day=&section=&capacity_pct=&mu=&start=` | P T A | projected queue with more discharge capacity, with the assumptions ("Estimate under stated assumptions") |

### Guardhouse (guards of a site)

| Method and path | Roles | What it does |
|---|---|---|
| `GET /gate/identify?plate=` | G | identity colour now: red banned, green resident, teal valid pass, yellow unregistered |
| `GET /gate/replay?run_id=` | G | the demo replay video and one card per plate with its video time |
| `POST /gate/decisions` | G | body `{plate, decision: allowed\|stopped\|exited, note, sighting_id, unit_no}` (a stop needs a note) |
| `GET /gate/inside` | G | visitors inside now, overstays first |
| `GET /gate/access-log?plate=&start=&end=&decision=&identity=` · `GET /gate/access-log.csv` | G | the access log (fuzzy plate search), CSV |
| `GET /passes` · `POST /passes` · `POST /passes/{id}/cancel` | G | visitor passes: body `{plate, unit_no, visitor_label, valid_from, valid_to, hours}` |

### Lists

| Method and path | Roles | What it does |
|---|---|---|
| `GET /lists/{resident\|banned}` · `POST /lists/{resident\|banned}` | G (own site) | residents (unit required) and the site's ban list |
| `GET /lists/{watch\|blacklist}` · `POST /lists/{watch\|blacklist}` | P | watch list and blacklist (reason required); body `{plate, reason, label, unit_no, valid_to}` |
| `POST /lists/entries/{id}/deactivate` | owner role | body `{reason}` |

### Upload & analyse, reports

| Method and path | Roles | What it does |
|---|---|---|
| `GET /jobs/scenes` | P T A | scene profiles for uploads (roadside ANPR, guardhouse gate, …) |
| `POST /jobs` (multipart: `file`, `camera_id`, `scene`, `render_video`, `recorded_at`) | P T A | queue an image or video (max 2 GB) for the background worker |
| `GET /jobs` · `GET /jobs/{id}` · `POST /jobs/{id}/cancel` | P T A | jobs with progress (`612/900 frames · 10.4 FPS · about 0:28 left`) |
| `GET /reports/{daily\|weekly}?date=&kind=traffic\|alerts\|gate&format=json\|csv\|pdf` | traffic: P T A · alerts: P A · gate: G | reports |

### Administration

| Method and path | Roles | What it does |
|---|---|---|
| `GET /users` · `POST /users` · `PATCH /users/{id}` | A | accounts (`{username, display_name, role, password, site_code}`; patch `{active, password, role}` signs the user out) |
| `GET /settings` · `PUT /settings` | A | retention days, overstay hours, reveal minutes, … |
| `GET /audit?username=&action=&start=&end=&limit=` · `GET /audit/verify` | A | audit log; hash-chain check |
| `POST /retention/purge?dry_run=true\|false` | A | preview or run the retention purge |
| `GET /corrections` | A | correction pool |
| `GET /data` | A | runs with source file, SHA-256 and counts; congestion days; forecast runs; registry path; last import |

### Media

| Path | Who | Notes |
|---|---|---|
| `GET /media/runs/{id}/video` | roles that may see the camera; **not** masked roles on plate cameras (their videos show full plates) | HTTP range requests (206), so the player can seek |
| `GET /media/runs/{id}/image` | as above | annotated image of an image run |
| `GET /media/series/{day_id}/video` | P T A | congestion time-lapse |
| `GET /media/sightings/{id}/crop` | scoped | sharp for police, guards and revealed records; blurred on the server otherwise |
| `GET /media/sightings/{id}/scene` | P, G, revealed records | full camera frame of the best crop; audit-logged |

## 3. Example (Python, against a running server)

```python
import httpx

c = httpx.Client(base_url="http://127.0.0.1:8000")
r = c.post("/api/v1/auth/login", json={"username": "police1", "password": "<from webapp/seed/demo_users.yaml>"})
c.headers["X-CSRF-Token"] = r.json()["csrf_token"]          # needed for POST / PUT / PATCH / DELETE

hits = c.get("/api/v1/sightings", params={"plate": "XSD4OO4", "tolerance": 2, "purpose": "Alert follow-up"}).json()
for s in hits["items"]:
    print(s["plate"], s["camera_name"], s["seen_at"], s["provenance"], s["distance"])

alert = c.get("/api/v1/alerts", params={"code": "BLACKLIST"}).json()["items"][0]
c.post(f"/api/v1/alerts/{alert['id']}/transition", json={"to": "acknowledged", "note": "Patrol informed"})
```

## 4. Service functions (for the AI assistant)

The API routers are thin wrappers: authenticate -> call a service function -> return its pydantic model. The service
functions are typed, documented and take the logged-in `User`, so the next task's AI assistant can call them as tools
and inherit the same permissions, masking and audit (an assistant call is logged with `via: assistant`).

| Module (`webapp/services/`) | Functions (all take `conn, user, …`) | Returns |
|---|---|---|
| `overview` | `summary(date)`, `cameras()` | `Overview`, `List[CameraOut]` |
| `plates` | `search(SightingQuery)`, `get(sighting_id, purpose)`, `reveal_sighting(sighting_id, reason, note)`, `timeline_of(plate)`, `export_csv(SightingQuery)` | `Page[SightingOut]`, `SightingDetail`, `RevealOut`, `Timeline`, CSV text |
| `alerts` | `list_alerts(status, severity, code, camera_id, include_simulated, plate, page, page_size)`, `counts()`, `get_alert(alert_id)`, `transition(alert_id, to, note, corrected_plate)` | `Page[AlertOut]`, `AlertCounts`, `AlertDetail` |
| `traffic` | `runs()`, `summary(run_id)`, `flow_series(run_id, by, bin_sec)`, `lane_series(run_id, metric)`, `events(run_id)`, `export_csv(run_id, what)` | `List[RunOut]`, `TrafficSummary`, `Series`, `List[LaneEventOut]` |
| `forecast` | `days()`, `state(day, at)`, `accuracy(day, section, horizon)`, `whatif(day, section, capacity_pct, mu, start)` | `List[ForecastDay]`, `ForecastState`, `Accuracy`, `WhatIfOut` |
| `gate` | `identify(plate)`, `replay(run_id)`, `decide(plate, decision, note, sighting_id, unit_no)`, `inside()`, `access_log(…)`, `passes()`, `issue_pass(…)`, `cancel_pass(pass_id)` | `Identity`, dict, `AccessEventOut`, `List[InsideOut]`, `List[PassOut]` |
| `lists` | `entries(lst)`, `add(lst, plate, reason, label, unit_no, valid_to)`, `deactivate(entry_id, reason)` | `ListEntryOut` |
| `jobs` | `create(stream, filename, camera_id, scene, render_video, recorded_at)`, `list_jobs()`, `get(job_id)`, `cancel(job_id)` | `JobOut` |
| `evidence` | `build(alert_id)`, `export(alert_id, fmt)` | dict, file path |
| `reports` | `build(kind, day, weekly)`, `to_csv(rep)`, `to_pdf(rep)` | dict, CSV text, PDF bytes |
| `admin` | `users()`, `add_user(…)`, `update_user(…)`, `get_settings()`, `put_settings(values)`, `audit_log(…)`, `verify()`, `run_purge(dry_run)`, `corrections()`, `data_status()` | models / dicts |

The models are in `webapp/models.py`; their JSON schemas are in `/openapi.json` (`components.schemas`), ready to be
turned into tool definitions. Write actions (alert transitions, list changes, gate decisions) should stay behind a
confirmation step in the assistant.
