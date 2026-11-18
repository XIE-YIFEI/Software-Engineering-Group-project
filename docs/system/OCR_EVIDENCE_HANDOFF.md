# OCR evidence handoff

## Scope and dependency

This branch extends PR #4 head `7710ed3` on top of PR #3 `6e539ae`.
It is separate from both pending PRs: do not add these changes to the reviewed PR #4 branch. Merge/review the dependencies first and recheck this branch against the selected integrated revision.

Video uploads enable the existing P03 `ocr_reads_jsonl` option at an absolute per-job `ocr_reads.jsonl` path. After the recognition child exits, both successful final import and cancelled/failed checkpoint recovery import the optional log. Recognition/OCR, registry decisions, tracking/merging and STOPPED logic are unchanged. Hanwen's three defect fixtures and fixes remain separate.

## Association and partial coverage

The adapter keys association by run, run-local track ID, processing-window frame and source-video time. It never matches by plate text. Each event must fall inside the terminal run's saved frame/time cutoff and inside exactly one camera sighting's track/frame/time interval. Unmatched, out-of-coverage and ambiguous events are counted but not guessed into a record. Synthetic events do not receive camera OCR evidence.

Only newline-terminated valid JSONL entries are imported. An interrupted trailing line, malformed events, missing/unreadable log or bounded-import limit is explicit. Retry replaces the same run's linked rows without duplicating them. Limits: 100,000 physical lines, approximately 64 MiB read budget, 16 KiB per line; bounded capture does not promise a full raw OCR history. Each P03 event is the selected reading, not every backend hypothesis. `voter_input_text` is the existing selected candidate passed toward voting, not a new independent decode.

Raw log remains in the private job directory; no public file URL is introduced. Linked text lives in `ocr_read_evidence` and cascades when its sighting is deleted; stream metadata has a run foreign key. The pre-existing private job/upload retention policy is unchanged. No public raw-log download is provided.

## Review and export

The existing plate-detail and alert-review pages show a paginated OCR evidence panel. Readings include raw text, vote candidate, format verdict, OCR confidence, crop quality, vote admission/reason, track/frame and source time, together with completeness and saved coverage.

`GET /api/v1/sightings/{id}/ocr?offset=0&limit=100`

`GET /api/v1/sightings/{id}/ocr?offset=0&limit=100&export=true`

Limit is 1–500, default 100; UI pages contain 50 entries. JSON export exports the specified page with `total`, `has_more`, offset and scope metadata, never silently claims the full stream. HTML/PDF evidence packs and existing CSV exports are unchanged. No raw-only/unassociated track review queue is added here: those readings remain in the private original log and appear only in exclusion counts. That queue needs a separate interface/permission decision.

Every request re-checks current login, camera/site scope and role, and audits view/export. Raw alternatives are available only to unmasked roles within their permitted cameras. Traffic/admin responses null both text fields even after temporary reveal of the final plate, because that reveal does not authorise other raw candidate strings. Metadata remains available; responses are `private, no-store`. Export does not return private file paths or arbitrary unknown payload fields. Existing manual business operations and AI policy remain unchanged.

## Verification

Run in the project environment (no model weights/API key required):

```powershell
python -B -m unittest webapp.tests.test_ocr_evidence -v
python -B webapp/tests/test_partial_jobs.py
python -B webapp/tests/test_web.py
node --check webapp/static/js/app.js
node --check webapp/static/js/pages/plates.js
node --check webapp/static/js/pages/alerts.js
```

The dedicated suite covers P03 writer-to-Web handoff, upload option wiring, final/partial import, ambiguous associations, saved cutoffs, invalid/torn/missing logs, pagination/export audit, scope/role downgrade, masked alternatives and retention. Existing schema receives additive CREATE IF NOT EXISTS tables/index; no destructive migration.

Before shared acceptance: inspect the rendered review pages, run an actual model/video upload and cancelled/failed case, inspect evidence associations and exported JSON at the selected integrated SHA, and obtain module-owner review of the evidence/privacy contract. Unit/API tests do not replace that acceptance or establish GitHub CI success before a push.
