# SECS Vision web system: test plan and report

[中文版 / Chinese version](TEST_PLAN_REPORT_ZH.md) · System documents: [index](README.md)

Report date: 24 October 2026. Everything below was run on the code that is submitted with this document.

## 1. Plan: what is tested, and how

| Level | What | How | Where |
|---|---|---|---|
| Unit tests (recognition) | the `alpr/` package: plate reading and voting, tracking, the plate log, registry rules, lanes, dangerous-driving rules, forecasting, and the new opt-in evidence output | 86 tests on synthetic inputs; no models, no GPU, no network | `alpr/tests/test_alpr.py` |
| API / integration tests | importer, login, sessions, roles, masking and reveals, audit log, fuzzy search, click-to-timestamp, alerts, timeline, evidence pack, guardhouse, forecast, what-if, traffic analytics, retention, uploads, reports, API docs, `requirements.txt` | 22 tests with FastAPI's TestClient on a temporary database filled from the committed demo results; each test works on its own copy | `webapp/tests/test_web.py` |
| Regression of the pipeline | the opt-in `--evidence` output changes no recognition result | the four demo profiles re-run with and without the option (OpenVINO, headless); every field of the committed JSON compared with the new one | section 5 |
| End to end | upload & analyse with the real background worker and models | a running server; an 8 s video and a photo uploaded through the API | section 6 |
| Browser | every page of every role loads and runs without script errors; layout | headless Chrome over the DevTools protocol at 1440 px, JavaScript exceptions and console errors recorded; screenshots reviewed by eye | section 7 |
| Performance | response times; the fuzzy search at scale | 20 requests per endpoint after a warm-up; 100,000 extra fictitious plates for the search | section 8 |
| CI rehearsal | the GitHub workflow's installation and tests | a brand-new virtual environment built with the commands of `.github/workflows/ci.yml` | section 9 |

**Pass criteria:** every automated test passes; the regression shows no difference in any field of the committed
results; no page of any role has a script error; every functional requirement (FR-1 … FR-23 of the
[requirements](REQUIREMENTS_EN.md)) is covered by an automated test or a documented manual check.

## 2. Test environment

| Item | Value |
|---|---|
| Computer | Windows 11 Home (build 26200), CPU only |
| Python | project virtual environment `.venv`, Python 3.12.0b3 (a beta: pip 26 refuses it, so the CI rehearsal skipped the pip upgrade; GitHub uses a stable 3.12) |
| Main packages | torch 2.3.1 (CPU), ultralytics 8.4.171, OpenCV 4.11, numpy 1.26.4, LightGBM 4.7.0, FastAPI 0.161.1, Starlette 1.9.0, uvicorn 0.54.0, Jinja2 3.1.6, fpdf2 2.8.9, httpx 0.28.1 (pinned in `requirements-ci.txt`) |
| Browser | Google Chrome 155 (headless) |
| CI | GitHub Actions, `ubuntu-latest`, Python 3.12 |

## 3. Results at a glance

| Check | Result |
|---|---|
| `alpr` unit tests, project venv | **86 / 86 passed** |
| `alpr` unit tests, fresh CI-style venv | **86 / 86 passed** |
| web API tests, project venv | **22 / 22 passed** |
| web API tests, fresh CI-style venv | **22 / 22 passed** |
| pipeline regression (7 comparisons) | **0 differences** in recognition results (section 5) |
| end-to-end upload (video + photo) | **done**: 5 plates from the video (the same as the demo), 1 from the photo |
| browser check (32 page loads, 4 roles) | **no script errors** |
| performance | median 20–65 ms for the interactive pages' API calls (section 8) |

## 4. The 22 web tests

| Test | Requirement | What it proves |
|---|---|---|
| `test_importer_loads_every_demo_result` | FR-21, FR-13 | 4 runs, 35 sightings (7 inserted plates, 6 synthetic), 13 registry + 5 lane alerts, 2 watch-list and 1 ban hits, **no alert on a real vehicle**, 355 crossings, 12 lane events, 10 withheld, 571 forecast minutes; every source file's SHA-256 recorded; `seen_at` = clock origin + `first_sec` |
| `test_importer_is_idempotent_and_keeps_alert_workflow_state` | FR-21 | importing twice changes no count and keeps an acknowledged alert acknowledged |
| `test_importer_reproduces_the_committed_forecast_and_adds_the_zones` | FR-17, FR-21 | LightGBM recomputed at import = the committed forecast JSON (max difference < 0.01: 0.0000 on Windows, 0.0016 on the Linux CI runner, from floating-point differences between platforms); 9 more sections computed |
| `test_login_lockout_logout_and_csrf` | FR-1 | 5 wrong passwords lock the account (also for the right password) for 5 min; logout without the CSRF header is refused; audit rows for every attempt; only a scrypt hash is stored |
| `test_roles_see_only_their_scope` | FR-2 | a guard sees only its gate; police never the condo gate; traffic has no alert centre; only admins manage users; pages follow the same rules; anonymous visitors are sent to the login |
| `test_masked_roles_see_masked_plates_and_blurred_crops_until_they_reveal_with_a_reason` | FR-8 | masked plates and blurred crops (different bytes); no scene frame and no plate-camera video (403) while lane video works (206); reveal needs a valid reason, then shows the plate and the sharp crop and is logged; a plate-number search needs a reason and matches exactly; the masking rule |
| `test_every_search_view_and_export_is_audited_with_its_purpose` | FR-5, FR-9 | no search without a purpose; the purpose and case reference reach the audit log; views and exports are logged; the CSV has a provenance column with all three values |
| `test_audit_log_is_append_only_and_hash_chained` | FR-9, NFR-9 | `UPDATE` and `DELETE` are refused by the database; a row inserted behind the system's back breaks the hash chain and is reported |
| `test_fuzzy_search_tolerates_ocr_look_alikes` | FR-4 | `XSD 4OO4` finds `XSD 4004` at tolerance 1 (not 0); wildcards; distances of look-alikes; the vectorised index equals the pairwise distance on 400 random plates; the provisional reading is kept apart and never alerts |
| `test_click_to_timestamp_seeks_one_second_before_the_record` | FR-6, FR-11 | every result seeks to `first_sec − offset − 1`; gate cards at 0.03 / 9.2 / 16.0 / 24.0 s of the replay; videos answer range requests (206) |
| `test_alerts_sorted_by_severity_and_workflow_with_correction_pool` | FR-7 | sorted by rank; the `UNMATCHED` disclaimer; closing needs a note; acknowledge -> false alarm with a corrected plate -> correction pool; closed alerts cannot be resolved again; admins cannot change alerts |
| `test_cross_camera_timeline_flags_impossible_travel` | FR-14 | Johor Bahru -> KL: 290–296 km, 1100–1180 km/h, impossible; the same-site leg is not; simulated flag; traffic role refused |
| `test_evidence_pack_marks_every_page_hashes_images_and_cites_only_verified_sections` | FR-15 | the demo mark appears repeatedly; each image's SHA-256 matches its embedded bytes; the hash statement; only sections from `legal.yaml` cited (s. 108(3)(b), (f) for a cloned plate); no PDRM name or logo; no external images; the PDF is valid; both exports recorded; traffic role refused |
| `test_gate_identity_colours_passes_and_overstays` | FR-11, FR-12 | green / teal / red / yellow; an expired pass is not valid; issuing a pass turns a visitor teal; a stop needs a note; allowed -> inside; after 5 h an overstay; overstays first; exited -> gone; the access log in order |
| `test_forecast_state_accuracy_and_warning_match_the_committed_json` | FR-17 | first warning 06:56, onsets 07:13 and 08:17; "Congestion in ~15 min"; forecasts equal the committed JSON; MAE recomputed independently; warned 17 and 3 min ahead (TEAM_GUIDE 10.5) |
| `test_whatif_more_capacity_never_lengthens_the_queue` | FR-18 | +0 % = measured; each step up is never above the previous one; the label and ≥ 5 assumptions; μ estimated by default, assumed when given |
| `test_traffic_analytics_flow_lanes_events_and_csv` | FR-16 | 355 vehicles = lanes + 4 outside every lane; withheld 7 speeding, 3 tailgating; speed error 6.5 %; flow by type sums to 355; 8 lane series without a bin after the clip; 12 events, plates masked, violations only synthetic; CSV of 355 rows + header |
| `test_retention_deletes_ordinary_sightings_but_keeps_open_alerts` | FR-10 | nothing due at first; after 31 days the preview counts exactly the sightings without alerts; the purge keeps every sighting with an open alert; purge logged; hash chain still valid |
| `test_upload_job_is_analysed_by_the_worker_and_imported` | FR-19 | a text file is refused; an image is queued, analysed (a stand-in for `run_image.py`, so no models are needed) and imported with the given recording time; upload logged |
| `test_reports_are_built_per_role_as_csv_and_pdf` | FR-20 | the daily alerts report counts the 20 police alerts; weekly traffic CSV with the demo mark; traffic role refused the alerts report; the guard's gate PDF |
| `test_api_documentation_lists_the_service_endpoints` | FR-22 | the OpenAPI document lists the main service endpoints |
| `test_requirements_cover_every_third_party_import_of_the_web_system` | constraint | every third-party import of `webapp/` is in `requirements.txt`, which is read as ASCII |

Overview page details (camera cards, date picker, flow by type) have no automated test: they were checked in the
browser (section 7) and in the demo walkthrough.

## 5. Regression of the pipeline (`--evidence`)

The only change to the recognition code is the opt-in evidence output. The four demo profiles were re-run on
23 October 2026 (OpenVINO, headless) and each new results file was compared field by field with the committed one
(every committed field must exist in the new file with the same value; new fields are listed).

| Profile | Run | Differences in committed fields | New fields | Rows with evidence |
|---|---|---|---|---|
| `malaysia_road_anpr` | without `--evidence` | 0 | `processing` | 0 / 8 |
| `malaysia_road_anpr` | with `--evidence` | 0 | `processing`, `evidence` | 8 / 8 |
| `malaysia_guardhouse` | without | 0 | `processing` | 0 / 2 |
| `malaysia_guardhouse` | with | 0 | `processing`, `evidence` | 2 / 2 |
| `malaysia_road_violations` | without | 0 | `processing` | 0 / 19 |
| `malaysia_road_violations` | with | 0 | `processing`, `evidence` | 19 / 19 |
| `malaysia_selayang_lanes` | with | 0 | `processing`, `observed_sec` (104 lane bins) | no plates |

`observed_sec` is not from this work: it was added to `alpr/flow.py` by the earlier commits f7b13f9 and d576a9a,
after the Selayang demo JSON was committed (d519842). Apart from these 104 new values every field is identical.
The committed demo JSONs were not regenerated or changed. The new unit test
`test_evidence_output_saves_crop_and_frame_and_changes_no_other_field` checks the same property on synthetic input.

## 6. End-to-end upload with the real worker

Server started with its background worker; logged in as `police1` through the API; two files uploaded to the KL
roadside camera with recording time 2026-10-23 09:00 (24 October 2026):

| File | Scene | Result |
|---|---|---|
| 8 s video, 240 frames, 1920×1080, cut from source seconds 117–125 of the roadside footage | LILIN demo camera (crops the vendor's screen recording) | queued -> running with live progress ("50/240 frames · 2.3 FPS · about 1:23 left" … "200/240 frames · 3.6 FPS · about 0:11 left") -> done after about 70 s, of which about 20 s loading the models: **5 confirmed plates** `WRS 7607`, `CAX 3029`, `WA 9299 M`, `WB 7605 J`, `VCH 1587` — exactly the committed demo's plates in that part of the footage, each first seen at the demo's time minus 117.0 s; every record seeks to 1 s before it; no alerts (uploads only use `ILLEGAL_FORMAT` and `DUPLICATE`) |
| a photo from the plate dataset's validation set | roadside | done about 10 s after it started: **1 plate**, `JYD 9104` |

The 3.6 FPS average of this short clip includes the models' warm-up; TEAM_GUIDE section 11 measures 10.5 FPS for the
roadside scene after 20 warm-up frames.

## 7. Browser checks

- **All pages, all roles:** headless Chrome logged in as each demo account and opened every page of its navigation
  (police 9 incl. the timeline, guard 5, traffic 6, admin 11 = 31 pages, plus the traffic page again for the
  documentation screenshot). JavaScript exceptions and console errors were recorded: **none**.
- **Screenshots reviewed by eye:** the 16 screenshots of the [demo walkthrough](DEMO_WALKTHROUGH_EN.md) one by one,
  and the 31 check screenshots as contact sheets (every page shows its content, no error page; layout, numbers,
  simulated pills, masking).
- **Narrow windows:** the layout switches to fewer columns at 1180 px and 1100 px. Checked at 1024 px (overview, alert
  centre, forecast, guardhouse): camera and section cards reflow to two or three columns, the alert detail moves
  below the list, the guardhouse card below the video; nothing overflows.
- **Media:** videos seek (HTTP 206), the gate replay shows each card at its time, evidence packs open inline.

## 8. Performance (24 October 2026)

Median and 95th percentile of 20 requests after a warm-up, through the full HTTP stack (TestClient), on a copy of
the demo database:

| Call | Median | p95 |
|---|---|---|
| overview | 33.7 ms | 54.3 ms |
| plate search, fuzzy tolerance 2 | 23.9 ms | 40.2 ms |
| plate search, no plate (page of 50) | 22.5 ms | 36.0 ms |
| alert list | 28.2 ms | 41.7 ms |
| cross-camera timeline | 62.5 ms | 74.1 ms |
| forecast state (5 sections) | 34.6 ms | 46.9 ms |
| forecast vs actual | 42.9 ms | 68.5 ms |
| what-if | 22.2 ms | 30.2 ms |
| traffic summary | 21.2 ms | 53.7 ms |
| evidence pack (PDF, 335 KB) | 1.5 s (one request) | — |
| fuzzy search with **100,000** extra distinct plates: tolerance 0 / 1 / 2 | 34 / 136 / 131 ms | 37 / 145 / 137 ms |

Two speed-ups came out of these measurements: the fuzzy search was vectorised with numpy and cached per database
version (before: about 5 s over 100,000 plates), and the congestion onsets / first warnings are now cached (before:
overview 302 ms, forecast state 183 ms, what-if 93 ms median).

## 9. CI rehearsal

GitHub's Linux runner cannot be reproduced on this laptop (no WSL or Docker), so the workflow was rehearsed on
Windows in a brand-new virtual environment built with the workflow's own commands: CPU torch 2.3.1 / torchvision
0.18.1 from the PyTorch CPU index, then `requirements-ci.txt`. The first attempt failed on a transient download error;
the second completed (2.6 GB installed). Result on 24 October 2026: **86 / 86** (`alpr`, 26 s) and **22 / 22**
(web, 32 s). The first run on GitHub is therefore also the first test on Linux; the workflow installs `libgl1`,
`libglib2.0` and DejaVu fonts for OpenCV and the PDF.

## 10. Defects found by testing and fixed

| Found by | Defect | Fix |
|---|---|---|
| browser check | the timeline page returned 500: the registry's SQLite connection was used from another thread | one registry connection per thread |
| privacy review of screenshots | masked roles could play the plate cameras' annotated videos, which show every plate in full | no plate-camera video or scene frame for masked roles (403), test added |
| browser check | the word "null" appeared in the gate card and the lane table | empty values filtered out before rendering |
| browser check | opening an alert link (`/alerts#12`) from another page did not select the alert | the page also listens for hash changes |
| browser check | charts drawn while hidden were squashed; time labels overlapped | charts redraw on resize; tick count follows the width |
| API test | the lane series showed an empty bin after the end of the clip | bins starting after the clip are left out |
| API test | the per-lane counts (351) did not add up to the total (355) | crossings outside every lane are counted and shown (4) |
| performance run | the overview took about 300 ms because congestion onsets were recomputed on every request | cached per forecast data version |
| performance run | fuzzy search took about 5 s over 100,000 plates | numpy-vectorised distance, cached index |
| browser check | the evidence pack downloaded instead of opening | served inline |
| browser check | the police overview had no congestion card | police camera scope includes congestion cameras (read only) |
| review of the PDF | justified text left wide gaps | left-aligned |
| review of the evidence pack | the plate box was drawn light blue while the caption says yellow (RGB / BGR mix-up) | colour fixed, screenshot retaken |
| legal check | the Malay title of s. 23 was misspelt ("berkenaan" instead of "berkaitan") | corrected after checking the JPJ text |
| code review | the API also accepted the CSRF token from the URL | header only |

## 11. Not covered (known gaps)

- **Linux:** not run locally; the first GitHub Actions run is the Linux check.
- **Browser tests in CI:** the page checks are a scripted local procedure, not part of CI; the JavaScript has no unit
  tests.
- **Load:** no test with many simultaneous users (SQLite with one writer, one analysis worker).
- **Real cameras:** no live RTSP stream (not built); uploads were tested with one short video and one photo.
- **Registry:** simulated, so alerts on real plates cannot be tested; all alert tests use fictitious plates.
- **CI uses a stand-in for the analysis subprocess** (no models on the runner); the real subprocess is covered by the
  end-to-end test (section 6).
- **PDF appearance** is checked by eye (fonts, page breaks), not automatically.
- **Accessibility:** every chart has a table view and colours are never the only signal, but no screen-reader test
  was done.
- **Local Python is a 3.12 beta**; CI uses a stable 3.12.
