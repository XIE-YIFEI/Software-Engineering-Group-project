# Video cancellation and partial recovery

Implemented on the Web integration branch based on recognition commit
6e539aecb3a5aadc0936c088dd6dbfa718717a2c. Recognition and Web must be deployed together.

Each video execution gets a fresh `job_<id>` directory. Web configures absolute
`stop_file` and `evidence_dir` paths. Cancelling a queued job prevents launch;
cancelling a running job records `cancel_requested`, leaves it running while
saving, and creates `stop.request`. Repeating cancellation is harmless.

A separate stdout reader allows the worker to poll cancellation even if the
recognition process is silent. The configurable defaults are:

- `SECS_CANCEL_TIMEOUT_SEC=30`: allow recognition to finish its frame/save.
- `SECS_TERMINATE_TIMEOUT_SEC=5`: wait after terminate, then kill if necessary.

For recognition, cancellation creates `stop.request` and waits up to the
graceful cancellation timeout (30 seconds by default) for frame completion,
checkpoint saving and exit. If recognition is still running, Web calls
terminate, waits up to the termination timeout (5 seconds by default), then
kills and waits for that child if necessary.

Playback conversion uses the same child-process monitor and a separate
`playback.log`. FFmpeg cannot honor recognition's stop file, so cancellation
or server shutdown terminates that execution's transcoder immediately, then
uses the configured termination timeout before kill and wait. Conversion also
retains its 3600-second overall limit. A forced zero exit is not a completed
copy; failed conversion falls back to the source. Accepted cancellation still
recovers the partial result and remains cancelled after the child is reaped.

Web waits for exit. An accepted cancel always ends as `cancelled`, including
recognition exit code 0. A nonzero exit or missing/invalid final output ends as
`failed`. Both paths attempt one import of the last valid `result.partial.json`.
Normal success imports `result.plates.json`. Finalization and cancel requests
are serialized in the database; a finalized successful job cannot be cancelled.

Partial results persist `runs.complete=0`, saved frame count and source time
window. Job APIs expose `cancel_requested`, `result_complete`, `saved_frames`,
`saved_window`, and nullable `expected_frames`. The upload page links to saved
plate records, marks them incomplete and hides traffic analytics. Plate rows
also retain the incomplete run label. No percentage is claimed for a recovered
snapshot with an unknown total. Partial runs are not primary traffic inputs;
their unsupported flow/lane/simulated blocks are ignored and direct traffic
analytics requests reject them rather than reporting zero traffic.

Registry rules/disclaimer are preserved when present. Registry alerts are
imported only for explicitly CONFIRMED camera rows. Evidence is attached from
the selected JSON; missing/unavailable image pairs retain the sighting and are
reported in the job message. Partial video playback uses the original source,
subject to existing media permissions, not the potentially incomplete MP4.
Browser compatibility of the source codec is not guaranteed.

Evidence directory creation, copying and read failures degrade to unavailable
evidence without discarding valid sightings. Copied crop/scene images must
both decode before either is attached. An unavailable pair clears attachment
paths and records an `unavailable_reason` in the sighting's `scene_info`; job
messages include the affected count and reasons on both final and partial
imports. Retrying the snapshot can restore a healthy pair. Database errors
and malformed imports still propagate to the existing transaction rollback.

Missing, malformed and zero-progress checkpoints never create a successful
empty run. Positive-progress checkpoints with no plates are valid; they say
no plate records were saved, not that no vehicles existed. A failed import
rolls back database changes. Retrying the same terminal snapshot is idempotent;
live evolving imports into a running job are out of scope.

Existing databases receive additive columns automatically. Original runs default
to complete, preserving existing behavior. Images keep their original processing
path; they do not acquire video checkpoint support.

Graceful server shutdown requests stop and waits for the worker within the
configured limits. Abrupt server death is different: a new server cannot prove
that the previous child exited, so it marks interrupted jobs terminal and leaves
their files for inspection instead of automatically importing an active file.
Automatic orphan-process recovery is not implemented.

## Verification

Run `python webapp/tests/test_partial_jobs.py` for cancellation, silent-child
timeout, exit-code-0 cancellation, final/cancel ordering, partial import,
evidence, metadata, transaction rollback, migration and stale-directory checks.
Run the existing Web tests and recognition checkpoint/OCR suites as well.
The new suite is mandatory in CI. Model-free tests do not replace the planned
real-model joint acceptance run by 22 November.

Local validation on 13 November 2026 (Windows, Python 3.12.10): Web regression
22/22, partial recovery 20/20, recognition checkpoints 22/22, OCR events 13/13,
diagnostics 4/4 and baseline ALPR 86/86. The recovery suite includes the actual
recognition loop writing checkpoints, Web cancellation and recovery, evidence
attachment and role access checks; inference is replaced with deterministic
outputs. JavaScript syntax and Git whitespace checks passed. Browser layout
and actual model/source-video playback have not been manually validated.

Follow-up recovery fixes passed local model-free validation: partial recovery
31/31 (including 11 added regression tests), Web regression 22/22, recognition
checkpoints 22/22 and OCR events 13/13. Transcoder cancellation uses a controlled
Python child; kill escalation also uses a controlled process fixture. These
results do not establish real FFmpeg transcoding, real-model integration or
browser acceptance. Those checks remain pending joint testing.
