"""Read P03 OCR events and report event statistics; standard library only."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys

FIELDS = {"track_id", "frame", "time_sec", "raw_text", "voter_input_text",
          "ocr_valid_format", "ocr_confidence", "crop_quality_score",
          "accepted_for_vote", "reject_reason", "n_backend_calls"}


class EventError(ValueError):
    """Invalid JSONL event, with its one-based file line number."""


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate(event, line):
    def fail(message):
        raise EventError(f"line {line}: {message}")

    if not isinstance(event, dict):
        fail("expected a JSON object")
    missing = FIELDS - event.keys()
    if missing:
        fail("missing fields: " + ", ".join(sorted(missing)))
    for key, minimum in (("track_id", 0), ("frame", 1), ("n_backend_calls", 1)):
        if type(event[key]) is not int or event[key] < minimum:
            fail(f"{key} must be an integer >= {minimum}")
    if not _number(event["time_sec"]) or event["time_sec"] < 0:
        fail("time_sec must be a finite non-negative number")
    if not _number(event["crop_quality_score"]) or not 0 <= event["crop_quality_score"] <= 1:
        fail("crop_quality_score must be a finite number in [0, 1]")
    for key in ("raw_text", "voter_input_text"):
        if event[key] is not None and not isinstance(event[key], str):
            fail(f"{key} must be a string or null")
    if event["ocr_valid_format"] is not None and type(event["ocr_valid_format"]) is not bool:
        fail("ocr_valid_format must be a boolean or null")
    confidence = event["ocr_confidence"]
    if confidence is not None and (not _number(confidence) or not 0 <= confidence <= 1):
        fail("ocr_confidence must be null or a finite number in [0, 1]")
    accepted, reason = event["accepted_for_vote"], event["reject_reason"]
    if type(accepted) is not bool:
        fail("accepted_for_vote must be a boolean")
    if accepted and reason is not None:
        fail("accepted events must have a null reject_reason")
    if not accepted and (not isinstance(reason, str) or not reason.strip()):
        fail("rejected events must have a non-empty reject_reason")
    return event


def read_events(path, *, start_sec, source_fps, frame_step):
    """Yield (original event, expected zero-based source frame) for CFR input."""
    if not _number(start_sec) or start_sec < 0:
        raise ValueError("start_sec must be a finite non-negative number")
    if not _number(source_fps) or source_fps <= 0:
        raise ValueError("source_fps must be a finite positive number")
    if type(frame_step) is not int or frame_step < 1:
        raise ValueError("frame_step must be a positive integer")
    product = start_sec * source_fps
    if not _number(product):
        raise ValueError("start_sec * source_fps must be finite")
    offset = int(product)
    with Path(path).open("rb") as stream:
        for line_no, line in enumerate(stream, 1):
            try:
                line = line.decode("utf-8")
                event = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid constant {value}")))
            except UnicodeDecodeError as exc:
                raise EventError(f"line {line_no}: invalid UTF-8: {exc}") from exc
            except ValueError as exc:
                raise EventError(f"line {line_no}: invalid JSON: {exc}") from exc
            validate(event, line_no)
            yield event, offset + (event["frame"] - 1) * frame_step


def summarize(events):
    """Summarize read events, not accuracy or the total number of vehicles."""
    count = accepted = 0
    tracks, reasons = set(), Counter()
    for event, _ in events:
        count += 1
        tracks.add(event["track_id"])
        if event["accepted_for_vote"]:
            accepted += 1
        else:
            reasons[event["reject_reason"]] += 1
    return {"kind": "OCR read event statistics (not accuracy or total vehicle count)",
            "event_count": count, "accepted_for_vote_count": accepted,
            "rejection_reasons": dict(sorted(reasons.items())),
            "distinct_track_id_count": len(tracks)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--start-sec", type=float, required=True)
    parser.add_argument("--source-fps", type=float, required=True, help="Original CFR video FPS, not report effective FPS")
    parser.add_argument("--frame-step", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = summarize(read_events(args.jsonl, start_sec=args.start_sec,
                                       source_fps=args.source_fps, frame_step=args.frame_step))
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
