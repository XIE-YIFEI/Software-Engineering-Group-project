"""Tests use synthetic data only."""
import json
from pathlib import Path
import tempfile
import unittest

from reliability.ocr_jsonl import EventError, read_events, summarize

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_ocr_events.jsonl"


class SyntheticEventsTest(unittest.TestCase):
    def read(self, path=FIXTURE, **kwargs):
        return list(read_events(path, **({"start_sec": 2, "source_fps": 50, "frame_step": 2} | kwargs)))

    def test_statistics_and_source_frames(self):
        events = self.read()
        self.assertEqual([frame for _, frame in events], [100, 104, 106, 112, 114])
        self.assertEqual(events[1][0]["time_sec"], 2.12)
        self.assertIsNone(events[1][0]["raw_text"])
        self.assertEqual(events[4][0]["raw_text"], "")
        self.assertFalse(events[3][0]["ocr_valid_format"])
        self.assertTrue(events[3][0]["accepted_for_vote"])
        summary = summarize(events)
        self.assertEqual(summary["event_count"], 5)
        self.assertEqual(summary["accepted_for_vote_count"], 2)
        self.assertEqual(summary["distinct_track_id_count"], 3)
        self.assertEqual(summary["rejection_reasons"], {"empty_read": 2, "nonstandard_below_min_width": 1})

    def test_fractional_start_truncates_before_frame_offset(self):
        events = self.read(start_sec=2.35, source_fps=10, frame_step=3)
        self.assertEqual([frame for _, frame in events], [23, 29, 32, 41, 44])

    def test_invalid_lines_have_line_number(self):
        event = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[0])
        cases = []
        missing = event.copy(); del missing["frame"]; cases.append((missing, "missing fields: frame"))
        wrong = event.copy(); wrong["frame"] = True; cases.append((wrong, "frame must"))
        wrong = event.copy(); wrong["ocr_confidence"] = "0.9"; cases.append((wrong, "ocr_confidence"))
        wrong = event.copy(); wrong["time_sec"] = 10 ** 400; cases.append((wrong, "time_sec"))
        wrong = event.copy(); wrong["accepted_for_vote"] = False; cases.append((wrong, "reject_reason"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic_invalid.jsonl"
            for value, message in cases:
                with self.subTest(message=message):
                    path.write_text(json.dumps(event) + "\n" + json.dumps(value) + "\n", encoding="utf-8")
                    with self.assertRaisesRegex(EventError, "line 2: .*" + message):
                        self.read(path)
            path.write_text(json.dumps(event) + "\n{\n", encoding="utf-8")
            with self.assertRaisesRegex(EventError, "line 2: invalid JSON"):
                self.read(path)
            path.write_bytes((json.dumps(event) + "\n").encode("utf-8") + b"\xff\n")
            with self.assertRaisesRegex(EventError, "line 2: invalid UTF-8"):
                self.read(path)

    def test_invalid_mapping_parameters(self):
        for kwargs in ({"start_sec": -1}, {"source_fps": 0}, {"frame_step": 0}, {"frame_step": True},
                       {"start_sec": 1e308, "source_fps": 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.read(**kwargs)


if __name__ == "__main__":
    unittest.main()
