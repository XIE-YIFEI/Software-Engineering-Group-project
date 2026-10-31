# FR03 — Optional uncertain-plate diagnostics

This adds an **opt-in, development-only** diagnostic output for tracks that have
OCR candidates but have not entered the official confirmed plate log.

## Run

From the repository root (after installing dependencies and obtaining the video
and model weights documented in `TEAM_GUIDE.md`):

```powershell
.\.venv\Scripts\python.exe run_alpr.py --profile malaysia_road_anpr --headless --uncertain-dir outputs/fr03_uncertain
```

Output is written to `outputs/fr03_uncertain/uncertain_plates.json` and, when a
best crop exists, `outputs/fr03_uncertain/track_<ID>.jpg`.

The new option is **disabled by default**. It does not add unconfirmed readings
to the official `*.plates.json` result, mark them as confirmed, or trigger
registry alerts. The regular recognition thresholds and logic are unchanged.

## What is included

A track is included when it has OCR voting evidence and its last state is
`ACQUIRING` or `TENTATIVE`, **unless it already belongs to a regular plate-log
entry**. Tracks without any usable OCR candidate are excluded.

The JSON contains the track ID, candidate text (not validated), status,
confidence, number of accepted votes, OCR attempt count, frame/time range,
and optionally a best-crop image filename. Image filenames are relative to the
diagnostic directory. This first version does **not** save frame-by-frame OCR
history or explain why a particular vote failed; those can be researched later.

**Privacy:** diagnostic data may contain real license plates. Use a local
`outputs/` directory (ignored by git), do not commit crops or diagnostic JSON,
and do not use uncertain candidates to accuse drivers of offenses.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s alpr/tests -p test_diagnostics.py -v
.\.venv\Scripts\python.exe alpr/tests/test_alpr.py
```

The four diagnostic tests cover filtering, excluding previously logged tracks,
output files, and invalid frame rate. Full pipeline regression against original
video/models is still required before claiming zero changes to existing model
results; the new feature is gated off unless explicitly requested.
