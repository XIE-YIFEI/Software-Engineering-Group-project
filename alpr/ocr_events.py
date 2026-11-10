"""Streaming P03 read events, separate from the vehicle summary JSON."""
import json
from pathlib import Path


class OcrEventWriter:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One file per run; never append a new run to an old event stream.
        self.path.write_text("", encoding="utf-8")

    def write(self, *, track_id, frame, time_sec, read, crop_quality_score,
              accepted_for_vote, reject_reason, n_backend_calls):
        event = {
            "track_id": int(track_id),
            "frame": int(frame),
            "time_sec": round(time_sec, 2),
            "raw_text": read.raw if read is not None else None,
            "voter_input_text": read.text if read is not None else None,
            "ocr_valid_format": bool(read.valid) if read is not None else None,
            "ocr_confidence": float(read.conf) if read is not None else None,
            "crop_quality_score": float(crop_quality_score),
            "accepted_for_vote": bool(accepted_for_vote),
            "reject_reason": reject_reason,
            "n_backend_calls": int(n_backend_calls),
        }
        # Close after each event so completed lines survive process termination
        # without depending on a final export or shutdown callback.
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
