# SECS Vision web system: requirements specification

[中文版 / Chinese version](REQUIREMENTS_ZH.md) · Part of the system documents: [index](README.md)

## 1. Purpose and scope

The recognition code of this project (`alpr/`) turns camera footage into results files: plates read in images and video,
alerts from a simulated vehicle registry, traffic flow per vehicle type and lane, dangerous-driving events and
peak-congestion forecasts. This specification covers the **web system** built on top of those results (`webapp/`):
a database, an API, pages for four roles, a background worker for new uploads, and continuous integration.

Positioning: *turn camera footage into traffic data that can be searched, raises alerts and forecasts congestion*.

**In scope:** login and roles; overview; plate search and log; alert centre; traffic analytics; congestion forecast;
upload & analyse; lists; privacy and audit; reports; and the six agreed additions (role-based masking,
click-to-timestamp playback, guardhouse mode, visitor passes and overstays, cross-camera timeline, evidence pack) plus
the what-if "open more lanes" scenario.
**Designed for, not built now:** live RTSP cameras, drawing lanes in the browser, a public congestion-outlook page
without plates, push notifications, and the generative / agentic AI assistant (the next task).

## 2. Users and the value they get

| Users | Their problem today | What the system gives them |
|---|---|---|
| **Traffic police / JPJ enforcement** (role `police`) | flagged vehicles pass cameras unnoticed; finding where and when a plate was seen means scrubbing video | alerts on blacklisted, cloned, unregistered, expired or wrongly typed plates and on lane violations, sorted by severity; fuzzy plate search with the video at the right second; a cross-camera timeline; an evidence pack |
| **Condominium / office guardhouse security** (role `guard`) | guards write plates in a book; visitors overstay unnoticed | the gate card shows each arriving plate in green / teal / yellow / red; one button logs the entry; visitor passes with a validity window; overstays pinned to the top; a searchable access log |
| **Traffic management** (DBKL, CIQ checkpoint operators, highway operators; role `traffic`) | queues are noticed when they already exist | per-lane flow, occupancy, density and speed; a congestion forecast up to ~30 minutes ahead with a warning; forecast vs actual so the model's accuracy is visible; a what-if for extra capacity; reports |
| **System administrator** (role `admin`) | — | accounts, data import status, retention, the audit log |

## 3. Roles and permissions

| Page / action | police | guard | traffic | admin |
|---|---|---|---|---|
| Home after login | Overview | Guardhouse | Overview | Overview |
| Plate search | full plates; road and lane cameras | own site's gate only (access log) | masked; a search by plate number is a reveal and needs a reason | masked |
| "Show full plate" | — | — | with a reason, audit-logged, 15 min | with a reason, audit-logged, 15 min |
| Videos of plate cameras | yes | own gate | no (they show full plates) | no |
| Alert centre | road and lane alerts, workflow | own gate's alerts, workflow | — (lane events in Traffic analytics) | read-only, masked |
| Cross-camera timeline, evidence pack | yes | — | — | — |
| Guardhouse, access log, residents and visitors, site ban list | — | yes | — | — |
| Watch list and blacklist | yes | — | — | — |
| Traffic analytics, congestion forecast | read | — | yes | read |
| Upload & analyse | yes | — | yes | yes |
| Reports | alerts, traffic | gate | traffic | traffic, alerts |
| Users, data, settings, retention, audit log | — | — | — | yes |

## 4. Use cases

| ID | Actor | Use case | Main flow (alternatives) |
|---|---|---|---|
| UC-1 | all | Log in and out | username + password -> role home; 5 wrong passwords lock the account for 5 minutes |
| UC-2 | police, traffic, admin | See the overview | camera status, the day's flow, open alerts, current congestion; pick another day |
| UC-3 | police (traffic, admin masked; guard at own gate) | Search a plate | type a plate (look-alike tolerant), choose the purpose, filter by time / camera / type; click a record -> the video plays from 1 s before it; export CSV |
| UC-4 | traffic, admin | Reveal a masked plate | "Show full plate" -> choose a reason -> the plate and sharp crop are shown for 15 minutes; logged |
| UC-5 | police (guard for the gate) | Handle an alert | open the alert (video, crop, scene, registry comparison) -> acknowledge -> resolve or mark a false alarm with a note (optionally the correct plate -> correction pool) |
| UC-6 | police | Trace a plate across cameras | timeline: sightings in time order with distance, time gap and implied speed; impossible travel flagged |
| UC-7 | police | Export an evidence pack | HTML or PDF with the demo mark, images and their SHA-256, registry comparison, legal reference |
| UC-8 | guard | Decide at the gate | the card shows the plate and its colour -> Allow (log entry) or Stop & note; issue a visitor pass on the spot; mark exits |
| UC-9 | guard, police | Manage lists | residents / site ban list (guards), watch list / blacklist (police), each with a reason |
| UC-10 | traffic (police, admin read) | Analyse traffic | flow per 10 s by vehicle type, per-lane occupancy / density / speed, lane events with video; CSV |
| UC-11 | traffic (police, admin read) | Read the congestion forecast | replay clock; per section now, +5 / +15 / +30 min with error band and a warning; forecast vs actual; what-if +10 / 20 / 30 % capacity |
| UC-12 | police, traffic, admin | Upload & analyse | upload an image or video, choose camera and scene -> the worker analyses it with progress -> results searchable, video playable |
| UC-13 | by role | Produce a report | daily or weekly; traffic, alerts or gate; preview, CSV, PDF |
| UC-14 | admin | Administer | users, settings, retention preview and purge, audit log with hash-chain check, correction pool, data status |

## 5. Functional requirements

Each requirement has an acceptance criterion and the automated test that checks it (`webapp/tests/test_web.py` unless
noted; `alpr/tests/test_alpr.py` for the pipeline).

| ID | Requirement | Acceptance criterion | Test |
|---|---|---|---|
| FR-1 | Session login with hashed passwords; logout; lockout | scrypt hashes only; 5 failures -> locked 5 min; every attempt audit-logged; state-changing calls need the CSRF token | `test_login_lockout_logout_and_csrf` |
| FR-2 | Four roles with scoped pages and data (section 3) | a guard sees only its gate; police never the gate; traffic has no alert centre; only admins manage users | `test_roles_see_only_their_scope` |
| FR-3 | Overview per role | camera cards, flow of a chosen day, open alerts by severity, congestion at the first warning | manual (walkthrough) |
| FR-4 | Fuzzy plate search | look-alike characters (O/0, B/8 ...) cost 0.5; tolerance 0–2; `*` / `?` wildcards; filters; purpose required; provisional readings kept apart | `test_fuzzy_search_tolerates_ocr_look_alikes` |
| FR-5 | CSV export of searches | provenance column; masked for masked roles; audit-logged | `test_every_search_view_and_export_is_audited_with_its_purpose` |
| FR-6 | Click-to-timestamp | the player seeks to `first_sec − window_sec[0] − 1` on demo runs and `first_sec − 1` on uploads; range requests | `test_click_to_timestamp_seeks_one_second_before_the_record` |
| FR-7 | Alert centre | six registry alerts, list hits and lane violations / hazards sorted High > Hazard > Medium, newest first; workflow with handler and notes; corrected plate to the correction pool; simulated items labelled | `test_alerts_sorted_by_severity_and_workflow_with_correction_pool` |
| FR-8 | Role-based masking | `VJF ****`, server-blurred crops, no plate-camera video; reveal needs a reason, lasts 15 min, is logged | `test_masked_roles_see_masked_plates_and_blurred_crops_until_they_reveal_with_a_reason` |
| FR-9 | Audit log | every search, view, reveal, export and change with who / when / what / why; append-only; hash chain verifiable | `test_every_search_view_and_export_is_audited_with_its_purpose`, `test_audit_log_is_append_only_and_hash_chained` |
| FR-10 | Retention | ordinary sightings deleted after N days; sightings with open alerts kept; preview before purge; purge logged | `test_retention_deletes_ordinary_sightings_but_keeps_open_alerts` |
| FR-11 | Guardhouse mode | red banned > green resident > teal valid pass > yellow unregistered; Allow and Stop & note write the access log; replay cards appear at `first_sec − 66 s` | `test_gate_identity_colours_passes_and_overstays`, `test_click_to_timestamp_...` |
| FR-12 | Visitor passes and overstays | pass = plate + unit + window; visitors inside > N h or past their pass pinned first; exits by the guard | `test_gate_identity_colours_passes_and_overstays` |
| FR-13 | Lists | resident / ban (site) and watch / blacklist (police) with reasons; imports and uploads matched | `test_importer_loads_every_demo_result` (2 watch-list, 1 ban alerts) |
| FR-14 | Cross-camera timeline | legs with km, time gap and implied km/h; > 250 km/h flagged impossible (`alpr.registry.check_travel`, legs under 2 km ignored); simulated sightings labelled | `test_cross_camera_timeline_flags_impossible_travel` |
| FR-15 | Evidence pack | HTML + PDF; demo mark on every page; SHA-256 of every embedded image; only verified Road Transport Act sections; no logos; export recorded | `test_evidence_pack_marks_every_page_hashes_images_and_cites_only_verified_sections` |
| FR-16 | Traffic analytics | flow by type / lane / time, per-lane occupancy, density, speed ± error; lane events; withheld counts; CSV | `test_traffic_analytics_flow_lanes_events_and_csv` |
| FR-17 | Congestion forecast | per section now, +5 / +15 / +30 with band and the dashboard's status wording; forecast vs actual with MAE; warning times as in TEAM_GUIDE 10.5 | `test_forecast_state_accuracy_and_warning_match_the_committed_json` |
| FR-18 | What-if | +0 / 10 / 20 / 30 % capacity from the warning time; more capacity never lengthens the queue; assumptions listed; labelled "Estimate under stated assumptions" | `test_whatif_more_capacity_never_lengthens_the_queue` |
| FR-19 | Upload & analyse | images and videos queued, analysed in a background subprocess with progress, imported as a run; no simulated events; `UNMATCHED` off | `test_upload_job_is_analysed_by_the_worker_and_imported` (+ end-to-end run, test report) |
| FR-20 | Reports | daily / weekly; traffic, alerts, gate; CSV and PDF with the demo mark | `test_reports_are_built_per_role_as_csv_and_pdf` |
| FR-21 | Importer | loads every committed result; idempotent (keeps alert workflow states); SHA-256 of every source; reproduces the committed forecast | `test_importer_*` (3 tests) |
| FR-22 | Documented API, reusable service functions | OpenAPI at `/docs`; typed service functions (for the AI assistant) | `test_api_documentation_lists_the_service_endpoints` |
| FR-23 | Opt-in evidence output of the pipeline | `--evidence DIR` saves each logged plate's best crop and its frame and adds their paths; every existing JSON field unchanged | `test_evidence_output_saves_crop_and_frame_and_changes_no_other_field` (alpr) + re-runs of the four demo profiles (test report) |

## 6. Non-functional requirements

| ID | Requirement | How it is met / measured |
|---|---|---|
| NFR-1 Privacy | Follow the data-minimisation principle of Malaysia's PDPA 2010 (personal data adequate but not excessive for the purpose, s. 6(3)(c); not kept longer than necessary for the purpose, s. 10(1)). The PDPA does not apply to the Federal and State Governments (s. 3(1)), so the system does not claim that police or JPJ use is bound by it; the same controls are applied to them as good practice. | role scoping; masking; reveal with reason; retention; audit; guard data limited to plates, unit numbers and short labels (no names or IC numbers). Sections checked in the official text (Laws of Malaysia, Act 709, online updated text as at 15 June 2016); the 2024 amendment was not re-checked for these sections. |
| NFR-2 Security | passwords never stored in clear; sessions revocable; no cross-site requests | scrypt; only the SHA-256 of the session token is stored; HttpOnly SameSite=Lax cookie; CSRF header; lockout; server listens on 127.0.0.1 by default and warns when demo accounts are active on another address |
| NFR-3 Honesty | real and simulated data never mixed | `provenance` on every observation (camera / inserted_plate / synthetic); purple "Simulated" pill on every page, CSV and PDF; disclaimers on `UNMATCHED`, the registry and the what-if |
| NFR-4 Performance | interactive pages on the demo data; search usable at scale | measured (test report): median 20–65 ms for the overview, searches, alerts, timeline and forecast panels; fuzzy search over 100,000 distinct plates 34 ms (exact) / 130–140 ms (tolerance 1–2); evidence pack PDF about 1.5 s |
| NFR-5 Reliability | an analysis cannot take the server down | the worker runs the pipeline in a subprocess; failures are recorded on the job; jobs still running when the server stops are marked failed on restart |
| NFR-6 Maintainability | students can extend it | layers (data access -> services -> API / pages); typed models; plain SQL in one place; no front-end build chain; 86 + 22 automated tests; CI on every push |
| NFR-7 Portability | runs on a team laptop | Windows 11 + Python 3.12 venv (tested); Ubuntu in CI; CPU only; no internet needed at run time (no CDN) |
| NFR-8 Usability | one consistent look; role-specific navigation | the dark style and colours of the existing dashboard (`alpr/hud.py`); English interface; every chart has a table view |
| NFR-9 Auditability | the log cannot be edited unnoticed | append-only triggers; each row carries the hash of the previous row; admin "Verify hash chain" |

## 7. Data and its limits (data gaps)

| Data | Real or simulated | Limit |
|---|---|---|
| Plates of the roadside, gate and lane footage | real (public YouTube footage) | demo windows of 30–155 s; notional clock (the footage has no recording time) |
| Vehicle registry | **simulated** (`alpr/registry.py`) | no JPJ access; real plates are neutral placeholders; `UNMATCHED` means "not in the simulated registry" |
| Violators | **fictitious X-series plates** (and the malformed `WXY 12345`), inserted into the footage or synthetic | real vehicles' violations are withheld (counted, never shown) |
| Cross-camera sightings | **simulated** (XSD 4004 at Johor Bahru) | our cameras are different places and dates: no real vehicle appears twice |
| CIQ congestion | real measurements (2 days, 2 cameras, 1 road) | occupancy / fill only — no vehicles per minute or speed (TEAM_GUIDE 10.6); forecast validated on 2 days |
| Gate exits | manual (guard's "Exited") or simulated | the exit lane's plates are too small and oblique to read |

## 8. Limitations (stated honestly)

- The registry is simulated (no JPJ access); alerts on real plates cannot be produced, so every alert in the demo is
  about a fictitious plate.
- CPU-only analysis runs at about 10 FPS (OpenVINO, headless); real time needs the GPU (TEAM_GUIDE section 11).
- The demo footage is public video, so real vehicles' violations are withheld; speeding evidence with a plate cannot be
  shown (the lane camera cannot read plates); the evidence pack is demonstrated on the violation demo's fictitious plates.
- The what-if is a deterministic point-queue model under stated assumptions, not a traffic simulation; its discharge
  capacity is a rough lower-bound estimate or an assumption.
- The evidence pack is a course demo, not an official document; its hashes only prove the images did not change after export.
- Local deployment: SQLite, one server, plain HTTP on 127.0.0.1; the published demo accounts must be disabled before
  other computers can reach the server.
- Uploaded videos play in the browser only as H.264 MP4 (other formats get an H.264 copy made with the bundled ffmpeg).
