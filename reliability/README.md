# OCR JSONL reader

This standalone, standard-library reader follows the P03 per-read contract at
`6e539aecb3a5aadc0936c088dd6dbfa718717a2c`. It does not run OCR or voting.
The included fixture is **synthetic**. From the repository root in PowerShell:

```powershell
python -m unittest discover -s alpr/tests -p test_ocr_jsonl.py
python -m reliability.ocr_jsonl alpr/tests/fixtures/synthetic_ocr_events.jsonl --start-sec 2 --source-fps 50 --frame-step 2
```

The JSON output is **event statistics**, not recognition accuracy or a total
vehicle count. A track ID is local to a run; multiple IDs can refer to one
vehicle, and vehicles without read events are absent. Unknown nonempty rejection
reasons are retained for forward compatibility. Extra event fields are preserved.
Blank lines and incomplete JSON are errors, reported with their physical line.

For a non-negative start and constant-frame-rate (CFR) source, `read_events`
yields `(event, expected_source_frame)` pairs. The source index is zero-based:
`int(start_sec * source_fps) + (frame - 1) * frame_step`. The event's original
`time_sec` is preserved and is not used to reconstruct the frame index. Supply
the original source FPS explicitly: report `fps` is effective FPS, not source FPS.
The result is an expected index under these assumptions, not verification of seek
or decode behavior. Variable-frame-rate video is outside this mapping contract.

The writer is not present on this base branch. Compatibility is checked against
the referenced PR's draft contract and writer, not against a local integration run.
