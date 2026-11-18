# Synthetic OCR evidence and pagination fixture

`web_ocr_synthetic_events.jsonl` is hand-constructed synthetic test data for
Toby's Web OCR Evidence Panel and paginated JSON export integration. It does
not come from a video, smoke test, real vehicle, plate, or held-out dataset.
Most non-null text uses deliberately nonstandard `SYNTHETIC...` labels.
The single format-valid token `XXX0000` is invented for this fixture: `X` is
outside the parser's known first-letter prefixes and its numeric part is all
zeroes, which the parser's comments say registration numbers do not start
with. The parser permits both. A syntax verdict does not check registration;
this token is not based on any observed vehicle or lookup. One line represents
one event in one fictional run; track IDs are local to that run.

There are 103 events across 6 tracks: 81 accepted and 22 rejected. Track 1
alone has 72 events (60 accepted and 12 rejected), including 30 accepted
`SYNTHETICALPHA` and 30 accepted `SYNTHETICBETA` reads. This is a tie:
aggregation reports `tie_count: 2` and `top_candidate_count: 30`. Separately,
its deterministic `top_candidate` is `SYNTHETICALPHA`, the lexicographically
smaller tied value; it is not a unique winner. Track 6 includes one accepted
`ocr_valid_format: true` event for `XXX0000`. Empty reads use null OCR fields;
other rejections cover `nonstandard_below_min_width`, `quality_gate`, and
`read_budget`. Accepted nonstandard readings intentionally have format=false.
Confidence, crop quality and backend-call counts vary.

## Schema

Every record has exactly these 11 required keys. Nullable means the key is
still present. The Reader allows extra fields and unknown nonempty rejection
reasons, but this fixture uses only the Writer's fields and known reasons.

| Field | Type and constraints |
| --- | --- |
| `track_id` | Integer >= 0, run-local track identifier |
| `frame` | Integer >= 1, processing-window frame number |
| `time_sec` | Finite number >= 0, original timeline seconds |
| `raw_text` | String or null, raw selected read |
| `voter_input_text` | String or null, selected text offered to the voter |
| `ocr_valid_format` | Boolean or null, OCR format verdict |
| `ocr_confidence` | Finite number in [0, 1] or null |
| `crop_quality_score` | Finite number in [0, 1] |
| `accepted_for_vote` | Boolean; whether submitted to the voter |
| `reject_reason` | Null for accepted; nonempty string for rejected |
| `n_backend_calls` | Integer >= 1, calls combined in the event |

The Writer inspected is at Git commit
`6e539aecb3a5aadc0936c088dd6dbfa718717a2c` (`alpr/ocr_events.py`,
`alpr/pipeline.py`, and `docs/p03_track_ocr_v1_draft.md`); this branch's
Reader is `reliability/ocr_jsonl.py`. Reader constraints above are validation
rules, rather than claims that the Writer independently enforces every range.
Synthetic times follow the Writer formula for start 0, effective 25 FPS, and
frame step 1. The Reader's expected source-frame mapping is zero-based.

## Validate from the repository root

```powershell
python -m reliability.ocr_jsonl alpr/tests/fixtures/web_ocr_synthetic_events.jsonl --start-sec 0 --source-fps 25 --frame-step 1
python -m reliability.track_aggregation alpr/tests/fixtures/web_ocr_synthetic_events.jsonl --start-sec 0 --source-fps 25 --frame-step 1
python -m unittest discover -s alpr/tests -p test_ocr_jsonl.py -v
python -m unittest discover -s alpr/tests -p test_track_aggregation.py -v
```

To verify the syntax-valid token, run:

```powershell
python -c "from alpr.plates import parse_plate; p = parse_plate('XXX0000', max_corrections=0); assert p and p.compact == 'XXX0000' and p.corrections == 0 and p.trimmed == 0; print('format valid without corrections')"
```

The following simulates 50-item pages and checks a JSON round trip; it does
not exercise the Web application or its export endpoint:

```powershell
python -c "import json; from reliability.ocr_jsonl import read_events; e = [x for x, _ in read_events('alpr/tests/fixtures/web_ocr_synthetic_events.jsonl', start_sec=0, source_fps=25, frame_step=1)]; pages = [e[i:i+50] for i in range(0, len(e), 50)]; t = [x for x in e if x['track_id'] == 1]; assert json.loads(json.dumps([x for page in pages for x in page])) == e; print('pages:', [len(p) for p in pages], 'track 1:', [len(t[i:i+50]) for i in range(0, len(t), 50)], 'round trip: identical')"
```

Observed result: `pages: [50, 50, 3] track 1: [50, 22] round trip: identical`.

The aggregation module is a separate worktree change and must be available to
run its commands. The existing plate-search page uses 50 rows per page; this
fixture exceeds that size both overall and within track 1. It does not establish
the page size or export shape of the not-yet-present OCR Evidence Panel. Once
integrated, check page boundaries, stable ordering, all 103 exported events,
and equality of exported fields and values against the source. If that panel's
configured page size exceeds 72, adjust the fixture for per-track pagination.
