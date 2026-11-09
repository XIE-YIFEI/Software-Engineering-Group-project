# P03 Track/OCR Data Contract v1

**Draft for review with Hanwen**

Current branch: `yifei/recognition-checkpoints`. Per-read fields below are proposals,
not implemented exports.

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

## 2. Available in memory; per-read JSON is proposed

These values are available during processing but **not exported per read**.
Left-column names are proposals.

| Proposed field | Existing source / distinction |
| --- | --- |
| `track_id`, `frame`, `time_sec` | Current vehicle ID and frame; source-video time can be derived using the same offset rule. |
| `raw_text` | `OcrRead.raw`, before syntax correction: Fast OCR's unconstrained top-1 text (padding removed), or EasyOCR's joined fragments. Failed calls can return `None`. |
| `voter_input_text` | `OcrRead.text`, decoded compact/non-standard text submitted only if admission conditions pass. The voter may normalize/parse it further; it is not the raw text or final summary plate. |
| `ocr_confidence`, `ocr_valid_format` | `OcrRead.conf` and `.valid`. Confidence describes the returned text, not necessarily the raw text when syntax decoding changed it. |
| `crop_quality_score` | `PlateQuality.score` (`q.score`), separate from OCR confidence; `sharpness`, `brightness`, and gate flag `ok` are also available. |
| `voting_quality`, `accepted_for_vote` | Current quality input is `q.score * min(1, plate_width / ocr_ref_plate_w)^2`; the voter combines it with OCR confidence. Admission/rejection must be captured explicitly if exported. |

The voter retains bounded accepted readings, not a raw history. `n_reads` is not
an exact backend-call count: overlapping boxes can trigger up to three crop OCR
calls before selection. `OcrRead` returns one result, not top-k text candidates;
Fast OCR's internal slot probabilities would need additional export work.

## 3. Example: existing checkpoint shape

Based on the real 212/900-frame interruption output; image paths are shortened
for illustration. No proposed per-read fields are included.

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

## 4. Questions for Hanwen

1. For v1, is one raw reading per selected OCR attempt sufficient, or are multiple
   candidates from the same attempt required? If multiple, does this mean results
   from different crop boxes, alternative decoded strings, or character probabilities?
2. Should rejected and empty OCR results be retained, or only readings admitted
   to voting? What defines one read event when several backend calls occur?
3. Confirm per-read names, frame/time conventions, and the separation of raw text,
   voter input, confidence, and crop quality.
4. **Alert fields and semantics are for Hanwen to define.** The current writer
   conditionally emits `alerts` when registry checking is enabled; this draft
   does not define or change its payload or propose a new Alert schema.

Checked against [pipeline](../alpr/pipeline.py), [OCR](../alpr/ocr.py),
[voting](../alpr/voting.py), [quality](../alpr/quality.py), the tracked
[final demo report](../demo/malaysia_road_anpr_demo_30s.plates.json), and the local
`outputs/evidence_interrupt_20261110_053241_442d5a/` checkpoint and run metadata.
The earlier 213/900-frame run did not enable evidence and cannot test image export.
