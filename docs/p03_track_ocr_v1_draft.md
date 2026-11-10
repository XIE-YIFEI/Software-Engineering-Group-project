# P03 Track/OCR Data Contract v1

**Draft — Track/OCR requirements confirmed by Hanwen; Alert section pending**

Current branch: `yifei/recognition-checkpoints`. Section 1 describes implemented
exports. Section 2 records the Track/OCR requirements confirmed by Hanwen;
the per-read JSONL export is **not yet implemented**. P03 remains a Draft until
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

The checkpoint envelope contains `complete: false`, `frames_processed`, and
`plates`. Its frame counter includes frames skipped by the view gate. Effective
FPS is source FPS divided by `frame_step`; summary times use
`(int(start_sec * effective_fps) + frame) / effective_fps`. Partial files remain
incomplete even after a normal finish; the existing final report is separate.

## 2. Confirmed Track/OCR requirements — JSONL not yet implemented

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

These are confirmed requirements, not a description of an existing JSONL writer.
Some values already exist in memory, but event capture, rejection reasons and
backend-call accounting still require implementation. The voter retains bounded
accepted readings, not a raw history. Its current quality input is
`q.score * min(1, plate_width / ocr_ref_plate_w)^2`, combined with OCR confidence;
this derived value is distinct from `crop_quality_score` and is not an additional
v1 per-read field.

## 3. Example: existing checkpoint shape

Based on the real 212/900-frame interruption output; image paths are shortened
for illustration. This is an implemented checkpoint example, not a per-read
JSONL example; no per-read fields are included.

```json
{
  "complete": false,
  "frames_processed": 212,
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

## 4. Outstanding Alert section and interface discussion

1. **Alert fields and semantics are for Hanwen to add.** The current writer
   conditionally emits `alerts` when registry checking is enabled; this draft
   does not define or change its payload or propose a new Alert schema.
2. **Stop signals and timeout values remain open for the interface discussion
   with Shiyu.** No stop-signal mechanism or timeout value is agreed in this
   draft.

The Track/OCR decisions in Section 2 are confirmed. The per-read JSONL export
remains to be implemented, and the overall P03 document remains a Draft.

Checked against [pipeline](../alpr/pipeline.py), [OCR](../alpr/ocr.py),
[voting](../alpr/voting.py), [quality](../alpr/quality.py), the tracked
[final demo report](../demo/malaysia_road_anpr_demo_30s.plates.json), and the local
`outputs/evidence_interrupt_20261110_053241_442d5a/` checkpoint and run metadata.
The earlier 213/900-frame run did not enable evidence and cannot test image export.
