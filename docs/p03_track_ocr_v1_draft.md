# P03 Track/OCR Data Contract v1

**Draft — Track/OCR requirements confirmed by Hanwen; Alert section proposed (Section 5); Web interface pending team confirmation (Section 6)**

Current branch: `yifei/recognition-checkpoints`. Section 1 describes implemented
exports. Section 2 records the Track/OCR requirements confirmed by Hanwen;
the per-read JSONL export is implemented as an optional stream. P03 remains a Draft until
the outstanding sections and interfaces are resolved.

## 1. Implemented: one vehicle summary in `plates[]`

Both reports use `AlprPipeline.summary()` for camera vehicles. New rows must meet
`log_min_status` (default `CONFIRMED`); undetected/unlogged plates and simulated
entries are not included.

| Saved field | Meaning |
| --- | --- |
| `track_ids` | Sorted, run-local tracker IDs. Multiple IDs mean tracks merged as the same vehicle, e.g. after occlusion/reacquisition, not multiple OCR candidates or global identities. |
| `first_frame`, `last_frame` | First/last recorded frame in 1-based processing-window coordinates, not original source frame indices when frame stepping is used. |
| `first_sec`, `last_sec` | Seconds on the original video timeline, including the selected start offset, rounded to two decimals. |
| `plate`, `compact` | Current voted display text and compact text without display spaces. Neither is a raw OCR reading. |
| `confidence` | Aggregate voting confidence `[0,1]`: weighted-mean OCR confidence times length/character agreement for legal readings; weighted string agreement for non-standard readings. |
| `votes` | Number of retained readings in the result's voting bucket, currently capped at 40. This is not the lifetime OCR-call count. |
| `valid_format` | Supported Malaysian syntax check, not registration validation or proof of correctness. |
| `status` | `ACQUIRING`, `TENTATIVE`, or `CONFIRMED`; initial row admission depends on `log_min_status`. |
| `vtype`, `region` | Vehicle type and plate-prefix-derived registration region/category, when available. |
| `evidence` (optional) | `crop`, `scene` image paths; `frame`, `time_sec`; `vehicle_box`, `plate_box` as `[x1,y1,x2,y2]` pixel coordinates in the processed scene, after any configured frame crop. |

Evidence requires `evidence_dir` and an available best crop/scene pair, not one
image per OCR call. Images precede JSON publication and use immutable filenames.
Missing evidence is allowed; paths are local filesystem paths, not URLs.

The implemented checkpoint envelope is:

| Field | Current meaning |
| --- | --- |
| `complete` | Always `false` for a partial snapshot, including the last snapshot of a normally completed run. |
| `frames_processed` | Number of processing-window frames consumed at this save, including frames skipped by the view gate; not the number of frames analyzed by the models or original source frames skipped by `frame_step`. |
| `source` | Configured source video path (`cfg.source`), preserved as supplied; not necessarily absolute in current CLI use. |
| `profile` | Configured profile name (`cfg.profile`). |
| `fps` | Effective video FPS: source FPS divided by `max(1, int(frame_step))`, with a 25 FPS source fallback when unavailable; not processing speed. |
| `window_sec` | Saved source-video interval `[start_sec, end_sec]`, where `end_sec = round(start_sec + frames_processed / fps, 2)` for positive progress. At zero progress both values are exactly the configured `start_sec`. This is saved coverage, not the requested full duration. |
| `plates` | Camera-vehicle summary rows described above; may be empty even with positive progress. |
| `registry` (when enabled) | Implemented on this branch for initial, periodic and end-of-loop partials: `rules` is the sorted set of actual checker rules; `disclaimer` comes from the same registry metadata as the final report, falling back to an empty string. Omitted when registry checking is disabled. No counts or registry path are included. |

Summary times retain the existing formula
`(int(start_sec * effective_fps) + frame) / effective_fps`, rounded to two decimals.
This can differ slightly from the envelope window formula for non-integral starts.
The separate final `.plates.json` report uses `frames`, has no `complete` field,
and shares `source`, `profile`, `fps` and `window_sec` with the last partial snapshot.

For an output `result.mp4`, the checkpoint is `result.partial.json`. After opening
and seeking the source, the registry checker is resolved when enabled, then an
initial zero-progress snapshot is written before model initialization. The same
checker is reused by the pipeline, so the initial snapshot already has registry
context even if model loading subsequently fails. During the frame loop, after
frame work (including view-gate skips),
the code checks whether approximately 5 seconds of wall-clock time have elapsed
since the previous save check that triggered a successful save. It also forces a
save at normal loop exit. This is **not a guarantee that at most 5 seconds of
results can be lost**: slow frames, initialization or save failures can extend
the interval between valid snapshots. JSON publication uses a temporary file and
atomic replacement; evidence images are published first with immutable filenames.
An unhandled processing exception does not force a fresh snapshot; recovery uses
the last valid checkpoint. Cooperative stop is now implemented on this branch
as described below. Checkpoints still omit `simulated_events`. Registry context
is limited to `registry.rules` and `registry.disclaimer`; final-report format,
alert logic, atomic replacement and evidence-before-JSON publication are unchanged.

### Cooperative stop: implemented on this branch, pending team interface confirmation

Use the optional `--stop-file PATH` argument in `run_alpr.py`, or set `stop_file`
in the configuration. The default empty string disables stopping. Web is expected
to pass an absolute path to a fresh execution's `stop.request`; relative CLI paths
resolve from the process working directory. For example (use a new job directory):

```powershell
python run_alpr.py --profile malaysia_road_anpr --out D:/jobs/run-001/result.mp4 --evidence D:/jobs/run-001/evidence --stop-file D:/jobs/run-001/stop.request
```

The caller creates the request when cancellation is wanted. Recognition only
checks whether that path exists; it never creates or deletes the request and
never removes other runs' outputs. An existing request is checked after
initialization, before reading any frame: the process forces a zero-progress
partial and exits. Initialization still runs and may take time or fail.

The frame loop checks before the next frame and after completed frame work,
including view-gate skips. A request during a frame lets that frame finish,
including its annotated-frame write, then saves available evidence and forces an
atomic partial immediately, regardless of the periodic interval. All metadata,
registry context, saved-frame counts and source-video timestamps are preserved;
`complete` remains `false`. Requests are also checked during normal finalization
and immediately before final-report publication.

A detected stop releases input, output and registry resources, publishes no new
success `.plates.json`, and emits neither the normal completion summary nor a
success end card. A successful stop flush and cleanup exits with code **0**;
save or cleanup errors propagate to the CLI for a **nonzero** exit. Every cleanup
is attempted; multiple failures retain the original error first and additional
cleanup errors in an exception group. An unsuccessful atomic/image save leaves
the previous valid checkpoint and its immutable evidence available.

**Exit code 0 alone does not mean the job succeeded.** Web owns cancellation
status and must wait for process exit. Final-report publication is the recognition
completion boundary: the final existence check and publication are not a shared
atomic transaction with the caller's request. Web must resolve finish/cancel races.
Success end cards are deferred until after final-report publication so a detected
stop never appends one. Video finalization can still fail after a final JSON was
written; a final file alone does not establish success. No request arriving after
that publication boundary is accepted by this recognition run.

Stopping cannot interrupt model initialization, a blocked backend call, frame
work or saving. There is no recognition-side timeout or terminate/kill escalation.
Web's proposed 30-second + 5-second timer and terminal import remain unimplemented.
Use a fresh output directory and stop path for every execution; existing final
files from an earlier execution are not removed or reclassified by this feature.

## 2. Confirmed Track/OCR requirements — optional JSONL implemented

For v1, retain one raw reading from each selected OCR attempt. Do not export
top-k candidates or per-character probabilities. Write per-read records to a
**separate JSONL file, one read event per line**; preserve compatibility with
the existing `plates.json` summary and checkpoint formats.

Overlapping plate boxes may cause multiple backend OCR calls before selection.
Combine those calls into one read event containing the selected result and the
actual backend-call count, rather than emitting one event per crop call.

The confirmed per-read fields are:

| Field | Required meaning / existing source |
| --- | --- |
| `track_id` | Current run-local vehicle tracker ID. |
| `frame` | Current frame in 1-based processing-window coordinates, following Section 1; not the original source frame index when frame stepping is used. |
| `time_sec` | Seconds on the original video timeline, including the selected start offset, using the same effective-FPS formula and two-decimal convention as Section 1. |
| `raw_text` | Selected `OcrRead.raw`, before syntax correction: Fast OCR's unconstrained top-1 text (padding removed), or EasyOCR's joined fragments. Preserve empty or missing readings instead of dropping the event. |
| `voter_input_text` | Selected `OcrRead.text`, the decoded compact/non-standard text available for submission to the voter. Its presence alone does not mean it was submitted. The voter may normalize/parse it further; it is not the raw text or final summary plate. |
| `ocr_valid_format` | Selected `OcrRead.valid`: the OCR syntax verdict, independent of whether the reading was submitted to the voter. |
| `ocr_confidence` | Selected `OcrRead.conf`. Confidence describes the returned text, not necessarily the raw text when syntax decoding changed it. |
| `crop_quality_score` | `PlateQuality.score` (`q.score`), separate from OCR confidence; `sharpness`, `brightness`, and gate flag `ok` are also available. |
| `accepted_for_vote` | Whether this reading was actually submitted to the voter. Record the actual submission decision, not an inference from format validity or later retention in the voting bucket. |
| `reject_reason` | Reason an empty or rejected reading was not submitted to the voter; record it whenever `accepted_for_vote` is `false`. |
| `n_backend_calls` | Actual number of backend OCR calls combined into this event, including calls on overlapping boxes before result selection. Do not use `n_reads` as a substitute. |

Retain empty and rejected readings with `accepted_for_vote: false` and a reason.
An invalid format does **not** automatically mean rejection: a non-standard
reading can still be submitted to the voter. If it was submitted, record
`accepted_for_vote: true` even when `ocr_valid_format` is `false`.

The voter retains bounded accepted readings, not a raw history. Its current quality input is
`q.score * min(1, plate_width / ocr_ref_plate_w)^2`, combined with OCR confidence;
this derived value is distinct from `crop_quality_score` and is not an additional
v1 per-read field.

### Implementation and use

Enable the stream with `--ocr-reads-jsonl PATH` in `run_alpr.py`, or set
`ocr_reads_jsonl` in the configuration. It is disabled by default. The selected
file is overwritten at run initialization; use a new output directory per run.
Each event is appended and the file closed immediately during processing,
without waiting for the final report. Completed lines remain readable after
process termination; an interrupted trailing write is not a complete event.

No event is emitted when no backend OCR call occurs. Existing overlapping-box
selection and empty-result retry behavior are preserved, with all calls counted
in the single event. Logging makes no additional OCR calls. `accepted_for_vote`
records the call to `PlateVoter.add`, not its return value or later bucket retention.
Missing OCR results use JSON `null` for text, format and confidence; successful
submissions use `reject_reason: null`. Rejection reasons are `empty_read`,
`nonstandard_below_min_width`, `quality_gate`, or `read_budget`. The latter two
can occur after candidate OCR when the selected crop does not pass admission.
The existing `.plates.json` structure and voting rules are unchanged.

## 3. Example: illustrative checkpoint in the current format

This explanatory example is arranged in the current implemented format. It
reuses the 212/900-frame interruption's vehicle values with shortened image paths,
and supplies illustrative `source`, `profile`, `fps`, `window_sec` and `registry`
metadata (including an illustrative disclaimer, not a quoted registry value).
Those added fields are **not claimed to be the original output of that older
interruption experiment**. Here the assumed start is 100 seconds, effective FPS
is 30, and saved coverage ends at `round(100 + 212 / 30, 2) = 107.07` seconds.
This is a summary checkpoint with registry checks enabled, not a per-read JSONL
example. Registry metadata and cooperative stop are implemented on this branch;
team interface confirmation is still pending. The example does not claim that
the older interruption experiment used the new stop-file feature.

```json
{
  "complete": false,
  "frames_processed": 212,
  "source": "data/example_video.mp4",
  "profile": "malaysia_road_anpr",
  "fps": 30.0,
  "window_sec": [100.0, 107.07],
  "registry": {
    "rules": ["BLACKLIST", "DUPLICATE", "EXPIRED", "ILLEGAL_FORMAT", "TYPE_MISMATCH", "UNMATCHED"],
    "disclaimer": "SIMULATED registry; illustrative disclaimer."
  },
  "plates": [{
    "track_ids": [2],
    "first_frame": 1, "last_frame": 212,
    "first_sec": 100.03, "last_sec": 107.07,
    "plate": "WTQ 9809", "compact": "WTQ9809",
    "confidence": 0.7169, "votes": 19,
    "valid_format": true, "status": "CONFIRMED",
    "vtype": "Car", "region": "Kuala Lumpur",
    "alerts": [],
    "evidence": {
      "crop": "evidence/example_crop.jpg",
      "scene": "evidence/example_scene.jpg",
      "frame": 211, "time_sec": 107.03,
      "vehicle_box": [721, 230, 1178, 599],
      "plate_box": [872, 484, 965, 514]
    }
  }]
}
```

## 4. Review status and outstanding decisions

1. Section 2 Track/OCR requirements are confirmed and its optional JSONL stream
   is implemented. Existing fields, voting rules and usage remain unchanged.
2. Hanwen's Alert Section 5 is merged for review. Its current-behavior description
   does not implement the CONFIRMED-only Web import follow-up. Checkpoint registry
   metadata (Section 5.6 proposal A) is now implemented on this branch; team approval
   of the interface and review of the Alert semantics remain pending.
3. Shiyu (Toby)'s Web / recognition interface proposal of 11 Nov 2026 is summarized
   in Section 6, **pending team confirmation**. Stop-file behavior, timeout values,
   exit codes and terminal partial import still require team agreement. Recognition
   stop-file checks, forced flush, cleanup and exit behavior are implemented on this
   branch; Web timers/import and cancellation status handling remain unimplemented.
   Branch implementation does not imply team approval of the interface contract.

## 5. Alerts: fields and semantics (proposed by Hanwen, 10 Nov 2026; for review)

This section covers the `alerts` array that the recognition module writes for a vehicle, in `plates[]` and in
`simulated_events[]`. It describes the code as it is on `yifei/recognition-checkpoints`, checked against
[registry](../alpr/registry.py), [pipeline](../alpr/pipeline.py), the web importer (`webapp/importer.py`,
`webapp/alerting.py`), `webapp/services/common.py`, `webapp/sop.yaml` and `webapp/legal.yaml`. v1 changes no
payload. Sections 5.1-5.5 describe current behavior, with the CONFIRMED-only
import follow-up explicitly identified in 5.5. Section 5.6 separates current
checkpoint behavior from future proposals: proposal A is implemented on this branch,
while proposal B remains unimplemented. Team interface confirmation is still pending.
Section 6 summarizes the wider Web interface proposal.

### 5.1 Where alerts come from

- The recognition module raises alerts from one source only: the registry rules of `alpr/registry.py`
  (`PlateChecker.check`), run by `AlprPipeline._check`. They exist only when `registry_enabled` is true. When it is
  false the `alerts` key is absent from every `plates[]` row: absent means "not checked", `[]` means "checked,
  nothing found". Consumers accept both and never require the key.
- An alert belongs to a vehicle (one `plates[]` row, one plate-log entry), never to a read event. The per-read JSONL
  of Section 2 carries no alert fields, by design.
- A camera vehicle is checked only while its reading is `CONFIRMED`, and re-checked whenever its compact text, format
  verdict, vehicle type, colour or twins (other vehicles in view with the same plate) change. A passing misread
  therefore never raises or clears an alert. SIMULATED events are checked once, when they are injected.
- `alerts` is the verdict at the moment the file was written, not a history: between two checkpoints a code can
  appear or disappear as the vote changes, and there is no per-alert timestamp. The pipeline does not clear the list
  when a reading stops being `CONFIRMED`, so a row's alerts are current only while its `status` is `CONFIRMED`;
  consumers read `alerts` together with `status`.
- The rendered dashboard video uses the same list, so the alert pills in the video and the exported alerts agree at
  the time of each save.

### 5.2 Payload (unchanged in v1)

`alerts` is an array of 0 to 4 objects with unique codes, sorted most severe first and then in rule order
(`UNMATCHED`, `EXPIRED`, `ILLEGAL_FORMAT`, `BLACKLIST`, `TYPE_MISMATCH`, `DUPLICATE`). `ILLEGAL_FORMAT` is
exclusive: a text that is not a plate gets that alert only, and no registry lookup follows.

| Field | Required meaning / existing source |
| --- | --- |
| `code` | One of the six identifiers below (`alpr/registry.py: ALL_RULES`). UPPER_SNAKE and stable: consumers key on `code`, never on the message text. |
| `severity` | `high` or `medium`, fixed per code (`SEVERITY`): `BLACKLIST` and `DUPLICATE` are `high`, the other four `medium`. The web maps `high` to rank 1 and `medium` to rank 3 (rank 2, hazard, exists for lane events only). |
| `message` | One English sentence for display, built by the rule. It may embed dates and numbers computed with the pipeline clock (5.3). Not parseable; it may change without notice. |

| `code` | `severity` | Raised when (rule inputs) | Message form |
| --- | --- | --- | --- |
| `ILLEGAL_FORMAT` | medium | The confirmed text is not a legal Malaysian plate (the OCR's `OcrRead.valid`, else `plates.parse_plate`). No lookup follows. | `'<text>' is not a legal Malaysian plate format` |
| `UNMATCHED` | medium | No registry record and, with `registry_misread_tolerance` (default on), no registered plate within one character; a near miss suppresses `UNMATCHED` as a likely OCR misread. There is no exact record for record-based checks; independent `DUPLICATE` checks still run. | `no registration record` |
| `EXPIRED` | medium | The record's road-tax expiry date is before the observation time (5.3). | `road tax expired 2026-03-31 (197 days ago)` |
| `BLACKLIST` | high | The record's status is `blacklisted`. | the record's note, e.g. `SIMULATED: reported stolen`, else `plate is blacklisted` |
| `TYPE_MISMATCH` | medium | The detector class (`vtype`, majority over the track) does not fit the registered body type (`BODY_TYPES`, tolerant on purpose); with `registry_check_colour` also the HSV colour estimate. | `registered Motorcycle, seen as Car`, plus `; registered red, looks white` with colour |
| `DUPLICATE` | high | (a) another vehicle in view, clearly apart from this one, carries the same compact plate: both entries get the alert, also one that has already left the view; or (b) the registry's sightings table has the plate at another camera too far away for the implied speed (`registry_max_speed_kmh`, legs under 2 km ignored). (b) needs `camera_latlon`; without it the travel check is skipped silently. | `same plate on vehicle #7 in view: cloned plate?` / `seen at <place> <HH:MM>, <km> km away (<speed> km/h needed): cloned plate?` |

Example from the committed violations demo (fictitious X-series plates): a camera row and a simulated event.

```json
{"plate": "XSA 1001", "compact": "XSA1001", "status": "CONFIRMED", "vtype": "Car",
 "alerts": [{"code": "EXPIRED", "severity": "medium", "message": "road tax expired 2026-03-31 (197 days ago)"},
            {"code": "TYPE_MISMATCH", "severity": "medium", "message": "registered Motorcycle, seen as Car"}]}
```

```json
{"plate": "XSB 2002", "compact": "XSB2002", "vtype": "Car", "simulated": true, "time_sec": 103.0,
 "alerts": [{"code": "BLACKLIST", "severity": "high", "message": "SIMULATED: reported stolen"}]}
```

### 5.3 Inputs the caller provides (they decide which alerts can exist)

| Setting | Effect on alerts | Who sets it |
| --- | --- | --- |
| `registry_enabled` | off: no `alerts` key at all | the profile; the web worker sets it from the chosen scene (`SCENES[..]["registry"]`) |
| `registry_rules` | the codes that may appear. Demo profiles use all six; uploads use `UPLOAD_RULES = ["ILLEGAL_FORMAT", "DUPLICATE"]`, because the other four require an authoritative registry context that the simulated registry cannot provide for real footage (FR-19) | the profile / `webapp/services/jobs.py` |
| `registry_path` | the SIMULATED registry file (5.4) | the profile / the web settings |
| `video_start_time` | wall-clock time of video second 0. `EXPIRED` and the travel `DUPLICATE` compare against `video_start_time + start_sec + frame / fps`, and the dates in the messages come from it. It must be the same instant as the web's `clock_origin` (the worker passes `job.recorded_at` to both; `webapp/seed/cameras.yaml` does the same for the committed runs), otherwise `seen_at` in the web and the dates in the message disagree. | the profile / the worker |
| `camera_name`, `camera_latlon` | the travel `DUPLICATE` and its message; the worker fills them from the camera row when its lat/lon are known | the profile / the worker |
| `registry_misread_tolerance` | on (default): without an exact record, a one-character near match suppresses `UNMATCHED`; independent `DUPLICATE` checks still run | the profile |
| `registry_check_colour` | adds the colour comparison to `TYPE_MISMATCH` | the profile |

### 5.4 The SIMULATED registry, simulated events and real vehicles

- The registry is simulated (`alpr/registry.py`, `python -m alpr.registry --make-demo`): fictitious vehicles with
  X-series plates (no Malaysian series uses X), a few of them deliberately expired, blacklisted, of the wrong type
  or seen elsewhere, and a neutral placeholder (status ok, no dates, no body type, no colour) for every real plate
  hand-labelled in `alpr/gt`, so those placeholders do not trigger record-based violations. Uploads enable only
  `ILLEGAL_FORMAT` and `DUPLICATE`; these outputs may reflect misreads and are not findings about real vehicles.
  Other profiles with `UNMATCHED` enabled can still flag an unknown real plate unless near-match tolerance applies.
- `registry` (top level, final report, when checks are on): `path`, `rules` (sorted), `disclaimer` (from the
  registry's metadata), `alerts` (count over camera rows) and `simulated_alerts`. The web stores `rules` and
  `disclaimer` per run (`runs.registry_rules`, `runs.registry_disclaimer`) for display.
  Checkpoints now carry only `rules` and `disclaimer` on this branch (5.6).
- `simulated_events[]` (top level, final report only, demo profiles only; the worker sets `registry_sim_events: []`
  for uploads): fictitious sightings injected at `at_sec` to demonstrate the rules. Fields: `plate`, `compact`,
  `vtype`, `simulated: true`, `time_sec` (same convention as `first_sec`) and `alerts` (same payload as above).
  They are not camera vehicles: not in `plates[]`, not counted in the flow. The web imports them as sightings with
  provenance `synthetic` and a rendered plate image.
- Digitally inserted plates (the violations demo): the pipeline cannot tell them apart; the web marks them
  `inserted_plate` from `alpr/simulation.yaml`. No change.
- Public-repository rule: in docs, screenshots and the demo only X-series or simulated vehicles appear as
  violators. An alert on a real plate is a system output, never a finding; the web adds `UNMATCHED_NOTE` and the
  legal notes to every registry alert.

### 5.5 What the web does with them (for the partial-result import)

- `webapp/importer.import_results` calls `webapp/alerting.create_alert` once per (sighting, code): natural key
  `<run code>|p:<first_frame>:<compact>|<code>` (`s:<time_sec>:<compact>` for a simulated event), `source:
  registry`, rank from the severity, `seen_at = clock_origin + first_sec`, `status: new`, plus an `alert_actions`
  row "created". Re-importing the same file updates the data and keeps the workflow state (idempotent); the alerts of a
  sighting that disappears are deleted with it (cascade), but an alert whose code no longer appears for a sighting
  that stays is not removed. Alerts are imported for every row that has them, whatever its `status` (web-side follow-up, Hanwen: create them only for
  `CONFIRMED` rows, matching 5.1).
- Other alert sources are the web's, not the pipeline's: `WATCHLIST` and `BLACKLIST` from the police lists and
  `BANNED_AT_SITE` from a site's ban list are matched by `match_lists` on `CONFIRMED` sightings after the import
  (sources `list` and `gate`); lane violations and hazards come from the `lanes` block (a separate section); gate
  arrivals from the gate. A registry `BLACKLIST` and a list `BLACKLIST` on the same sighting are two rows with two
  sources.
- The workflow (status new / acknowledged / resolved / false_alarm, handler notes, corrected plate) lives in the web
  only; nothing of it appears in any JSON.
- Every code is also a key in `webapp/sop.yaml` (SOP steps), `webapp/legal.yaml` (sections and notes) and
  `ALERT_TITLE` (`alpr/hud.py` and `webapp/services/common.py`). Adding a code means all four plus a test;
  renaming one breaks stored alerts and these files.

### 5.6 Checkpoints (partial results)

- `plates[].alerts` is written to a checkpoint exactly as to the final report (same `summary()`): it is the verdict
  as of that save.
- Proposal A (Yifei), **implemented on this branch; team interface confirmation
  pending**: when registry checking is enabled, every initial, periodic and
  end-of-loop partial includes `registry.rules` (sorted actual checker rules) and
  `registry.disclaimer` (registry metadata, or an empty string, as in the final report).
  Disabled checks omit the block. Checkpoints contain no registry counts, registry
  path or `simulated_events`. The final report retains its existing richer block.
  The importer tolerates older partials without registry metadata, but automatic
  Web cancellation/failure recovery is still unimplemented.
- Proposal B (Shiyu): a partial run imported after a cancel or a failure is a one-off import (no final report
  follows for that job), with idempotent retry of the same snapshot; mark the run incomplete. Natural keys
  include compact text and first frame, so they do not guarantee stable identity
  across evolving snapshots. This is a proposal, not implemented cancellation recovery.

### 5.7 Not in v1 (open for v2 if the web needs it)

- No per-alert timestamp or `raised_frame`, no alert history, and no fields of the registered record in the payload
  (the web has its own registry lookup for police).
- `DUPLICATE` names the twin only in the message (`#7`, a run-local track id). If the web needs the link, v2 adds
  `related_track_ids`.
- Alerts are computed at run time against the registry file of that run; the web does not re-evaluate them when the
  registry changes.

## 6. Web / recognition interface proposal — pending team confirmation

Source: Shiyu (Toby), `P03_Web_Checkpoint_Interface_Proposal_2026-11-11.md`.
This is a proposed v1 agreement for **video uploads**, pending team approval.
Checkpoint registry metadata and recognition cooperative stop are implemented on
this branch. Web recovery and cancellation handling remain unimplemented. Image-job
cancellation is a separate path. Proposed targets are interface confirmation by
15 Nov 2026 and joint testing by 22 Nov 2026.

### 6.1 Current integration gaps

The current Web worker terminates immediately on cancellation and skips imports
on cancellation or failure. The importer reads `frames`, not `frames_processed`,
and does not persist `complete`. The video worker neither explicitly enables
job evidence saving nor passes `evidence=results` to `import_results`.
Consequently, a valid recognition checkpoint alone does not yet provide Web
partial-result recovery. Checkpoint `registry.rules` / `registry.disclaimer`
and recognition cooperative stop are now implemented on this branch. Web recovery,
its timeout/escalation policy and finish/cancel handling still require implementation;
the cross-module interface remains pending team confirmation.

### 6.2 Proposed paths and cooperative stop

Use a fresh job directory and execution identity for every run or retry. Web
would pass absolute paths, including `--stop-file ABSOLUTE_PATH` pointing to
that execution's `stop.request`, and explicitly set `evidence_dir`.

| Item | Proposed path under the job directory |
| --- | --- |
| Stop request | `stop.request` |
| Partial result | `result.partial.json` |
| Final result | `result.plates.json` |
| Optional annotated video | `result.mp4` |
| Evidence images | `evidence/` |
| Optional separate OCR stream | `ocr_reads.jsonl` |

- Cancel a queued job without spawning recognition. For a running job, record
  cancellation and create `stop.request` once; repeated requests are harmless.
- **Recognition behavior implemented on this branch:** check after initialization,
  before the next frame (including view-gate-skipped frames), after frame work and
  before final-report publication. Finish an in-flight frame, save available
  evidence, force an atomic partial, release resources and exit. No new success
  report or end card is published after a detected stop. See Section 1 for the
  final publication boundary and error behavior.
- Proposed graceful-stop timeout: **30 seconds, configurable on the Web side**;
  then terminate, wait up to **another 5 seconds**, and kill and wait if needed.
  Timing must be independent of the blocking stdout iterator. These values need
  Yifei's confirmation and testing on slow machines.
- **Recognition exit behavior implemented on this branch, pending team agreement:**
  0 after successful stop flush and cleanup; nonzero on saving or cleanup failure.
  Web retains its cancellation flag; code 0 alone never means job success. While draining, show "Cancellation requested — saving results";
  set the terminal finish time only after exit.
- If cancellation is accepted before finalization, keep the terminal status
  cancelled; `_done` must not overwrite it. Already finalized jobs reject cancel.

### 6.3 Proposed terminal import, evidence and metadata

- After normal exit with no accepted cancellation and a valid final file,
  import final and mark done. After cancel or failure, **wait for process exit
  and import the last valid partial once**, preserving cancelled/failed even
  if the child returned 0 or import succeeded. A final file left by abnormal
  exit does not override this policy.
- This is terminal recovery, not live imports of evolving snapshots. Retrying
  the same snapshot may be idempotent and preserve workflow state. Existing
  natural keys and alert reconciliation do not guarantee safe evolving imports
  (Section 5.5); no subsequent final import follows a cancelled/failed job in v1.
- Missing, malformed or zero-progress partials show "No saved results" or a
  precise recovery error. Positive-progress `plates: []` is valid and means
  "No plate records saved in the processed portion", not a total traffic count.
- Persist completeness, `frames_processed` and `window_sec` in Web data/API.
  Map saved frames into the partial run's frame count; only show percentages
  when expected frames are known. Partial flow, lane and simulated-event totals
  are unavailable unless supported; exclude partials from complete-run aggregates
  by default.
- Pass the selected result JSON as `evidence` to `import_results` so image
  attachment runs. Use absolute result/evidence paths and support immutable hash
  filenames. Missing images retain the sighting with "Evidence unavailable";
  copy errors must not turn the original job into done. Interrupted MP4 playback
  is not guaranteed; still evidence and role-scoped source playback take priority.
- Keep Web `clock_origin` and recognition `video_start_time` at the same
  video-second-zero instant; do not add window start twice. Keep existing timestamp
  conventions, with non-integral starts and `frame_step` covered by regression tests.
- The proposed `registry.rules` and `registry.disclaimer` subset (Section 5.6
  proposal A) is **implemented on this branch, pending team interface confirmation**.
  Registry-enabled partials include both fields, without counts or a registry path;
  disabled partials omit the block. For older files without metadata, the proposed
  Web behavior is to show it as unavailable, not as checks off or clear.
  Keep `simulated_events` out of upload partials; demo recovery needs separate
  provenance agreement. Keep the current alert payload and propose importing
  registry alerts only for explicitly `CONFIRMED` camera rows. Near-match review
  changes remain separate future work.

### 6.4 Proposed ownership and acceptance

| Owner | Proposed responsibilities |
| --- | --- |
| Yifei | Recognition stop-file option/checks, forced flush and clean exit, registry metadata in partials, updated P03 examples and recognition tests. |
| Shiyu (Toby) | Independent Web cancellation timer, terminal partial import and evidence attachment, completeness/progress persistence and UI, terminal status/race handling and idempotent recovery tests. |
| Hanwen | Confirm Section 5 semantics and CONFIRMED-only import behavior; confirm that near-match changes stay outside this payload revision. |

Joint acceptance should cover normal success with both files, graceful cancel,
crash with an older checkpoint, timeout during a slow frame, interruption during
JSON/image publication, cancellation before the first checkpoint, positive-progress
empty plates, missing evidence, same-snapshot retry, finish/cancel races,
`frame_step` with a nonzero start, registry enabled/disabled, stale alerts on
non-CONFIRMED rows and role-scoped evidence access. Record the integrated SHA,
environment, saved frames, terminal status, evidence links and logs.

**Still pending team confirmation:** the stop option/path and flush semantics,
30-second + 5-second timeout, exit-code convention, checkpoint registry metadata,
terminal one-off import and the responsibilities/acceptance plan above. This
branch implements checkpoint registry metadata and recognition cooperative stop,
including forced flush and cleanup/exit behavior. Web recovery, timeouts,
escalation and terminal status handling remain unimplemented.

The Track/OCR decisions in Section 2 are confirmed and the optional per-read
JSONL export is implemented. The overall P03 document remains a Draft.

Checked against [pipeline](../alpr/pipeline.py), [OCR](../alpr/ocr.py),
[voting](../alpr/voting.py), [quality](../alpr/quality.py), the tracked
[final demo report](../demo/malaysia_road_anpr_demo_30s.plates.json), and the local
`outputs/evidence_interrupt_20261110_053241_442d5a/` checkpoint and run metadata.
The earlier 213/900-frame run did not enable evidence and cannot test image export.
