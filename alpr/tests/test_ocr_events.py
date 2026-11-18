"""P03 event capture tests: exercise the existing selection and voting path without models."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from alpr.config import AlprConfig
from alpr.ocr import OcrRead
from alpr.ocr_events import OcrEventWriter
from alpr.pipeline import AlprPipeline, TrackState
from alpr.plates import parse_plate
from alpr.quality import PlateQuality
from alpr.voting import PlateVoter


FIELDS = {"track_id", "frame", "time_sec", "raw_text", "voter_input_text",
          "ocr_valid_format", "ocr_confidence", "crop_quality_score",
          "accepted_for_vote", "reject_reason", "n_backend_calls"}
GOOD = PlateQuality(100, 128, 0.75, True)
BAD = PlateQuality(0, 128, 0.2, False)
LEGAL = OcrRead("WA1234B", 0.9, "WA123AB", parse_plate("WA1234B", 0))
NONSTD = OcrRead("WXY12345", 0.8, "WXY12345", None)


class OcrEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "reads.jsonl"
        self.pipe = AlprPipeline.__new__(AlprPipeline)
        self.pipe.cfg = AlprConfig(start_sec=48.09)
        self.pipe.fps = 15.0  # effective FPS, e.g. 30 FPS source / frame_step 2
        self.pipe.ocr_events = OcrEventWriter(self.path)
        self.pipe.voter = PlateVoter()
        self.pipe.voter.add = Mock(wraps=self.pipe.voter.add)
        self.pipe.ocr = Mock()
        self.state = TrackState(1, 1)
        self.frame = np.full((200, 300, 3), 128, dtype=np.uint8)

    def run_event(self, reads, width=100, overlap=False, quality=GOOD):
        self.pipe.ocr.read_plate.side_effect = reads
        candidates = [((10, 10, 10 + width, 40), 0.9)]
        if overlap:
            candidates.append(((10, 10, 10 + width + 2, 42), 0.8))
        with patch("alpr.pipeline.assess", side_effect=quality if isinstance(quality, list) else None,
                   return_value=quality):
            self.pipe._read_plate(self.frame, {"track_id": 7, "box": (0, 0, 300, 200)},
                                  self.state, 2, (0, 0, 300, 200), candidates)
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_normal_vote_raw_text_and_time(self):
        event, = self.run_event([LEGAL])
        self.assertEqual(set(event), FIELDS)
        self.assertEqual((event["track_id"], event["frame"], event["time_sec"]), (7, 2, 48.2))
        self.assertEqual(event["raw_text"], "WA123AB")
        self.assertEqual(event["voter_input_text"], "WA1234B")
        self.assertTrue(event["accepted_for_vote"])
        self.assertIsNone(event["reject_reason"])
        self.assertEqual(event["crop_quality_score"], 0.75)
        self.assertEqual(event["ocr_confidence"], 0.9)
        self.assertEqual(event["n_backend_calls"], 1)
        self.assertEqual(self.pipe.voter.votes(7), 1)
        self.pipe.voter.add.assert_called_once_with(7, "WA1234B", 0.9, 0.75, valid=True)

    def test_empty_read(self):
        event, = self.run_event([None])
        self.assertIsNone(event["raw_text"])
        self.assertIsNone(event["voter_input_text"])
        self.assertIsNone(event["ocr_confidence"])
        self.assertIsNone(event["ocr_valid_format"])
        self.assertFalse(event["accepted_for_vote"])
        self.assertEqual(event["reject_reason"], "empty_read")
        self.pipe.voter.add.assert_not_called()

    def test_nonstandard_small_crop_not_submitted(self):
        event, = self.run_event([NONSTD], width=50)
        self.assertFalse(event["accepted_for_vote"])
        self.assertEqual(event["reject_reason"], "nonstandard_below_min_width")
        self.assertEqual(event["raw_text"], NONSTD.raw)
        self.pipe.voter.add.assert_not_called()

    def test_invalid_format_can_be_submitted(self):
        event, = self.run_event([NONSTD])
        self.assertFalse(event["ocr_valid_format"])
        self.assertTrue(event["accepted_for_vote"])
        self.assertIsNone(event["reject_reason"])
        self.pipe.voter.add.assert_called_once()
        self.assertEqual(self.pipe.voter.result(7).compact, NONSTD.text)

    def test_overlapping_calls_one_selected_event(self):
        event, = self.run_event([NONSTD, LEGAL], overlap=True)
        self.assertEqual(event["n_backend_calls"], 2)
        self.assertEqual(event["raw_text"], LEGAL.raw)
        self.assertEqual(self.pipe.ocr.read_plate.call_count, 2)
        self.pipe.voter.add.assert_called_once()

    def test_empty_overlap_preserves_existing_retry(self):
        event, = self.run_event([None, None, None], overlap=True)
        self.assertEqual(event["n_backend_calls"], 3)
        self.assertEqual(event["reject_reason"], "empty_read")
        self.assertEqual(self.pipe.ocr.read_plate.call_count, 3)
        self.pipe.voter.add.assert_not_called()

    def test_three_overlapping_candidates_one_event(self):
        candidates = [((10, 10, 110 + i, 40 + i), 0.9 - i * 0.1) for i in range(3)]
        self.pipe.ocr.read_plate.side_effect = [NONSTD, LEGAL, NONSTD]
        with patch("alpr.pipeline.assess", return_value=GOOD):
            self.pipe._read_plate(self.frame, {"track_id": 7, "box": (0, 0, 300, 200)},
                                  self.state, 2, (0, 0, 300, 200), candidates)
        event, = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(event["n_backend_calls"], 3)
        self.assertEqual(event["voter_input_text"], LEGAL.text)
        self.assertEqual(self.pipe.ocr.read_plate.call_count, 3)
        self.pipe.voter.add.assert_called_once()

    def test_overlap_selected_read_budget_reject(self):
        self.state.n_reads = self.pipe.cfg.ocr_max_readings
        self.state.best_read_w = 60
        self.state.best_ocr_q = 0.0
        # First crop's quality improvement permits candidate OCR; selected crop's does not.
        event, = self.run_event([NONSTD, LEGAL], width=50, overlap=True,
                                quality=[GOOD, PlateQuality(100, 128, 0.05, True)])
        self.assertTrue(event["ocr_valid_format"])
        self.assertFalse(event["accepted_for_vote"])
        self.assertEqual(event["reject_reason"], "read_budget")
        self.assertEqual(event["n_backend_calls"], 2)
        self.pipe.voter.add.assert_not_called()

    def test_overlap_selected_quality_rejects_even_legal_read(self):
        # First candidate permits OCR, but the selected second candidate fails quality.
        event, = self.run_event([NONSTD, LEGAL], overlap=True, quality=[GOOD, BAD])
        self.assertTrue(event["ocr_valid_format"])
        self.assertFalse(event["accepted_for_vote"])
        self.assertEqual(event["reject_reason"], "quality_gate")
        self.assertEqual(event["n_backend_calls"], 2)
        self.pipe.voter.add.assert_not_called()

    def test_no_ocr_no_event(self):
        self.assertEqual(self.run_event([], quality=BAD), [])
        self.pipe.ocr.read_plate.assert_not_called()

    def test_read_budget_no_ocr_no_event(self):
        self.state.n_reads = self.pipe.cfg.ocr_max_readings
        self.state.best_read_w = 60
        self.state.best_ocr_q = 1.0
        self.assertEqual(self.run_event([], width=50), [])
        self.pipe.ocr.read_plate.assert_not_called()

    def test_logging_does_not_change_votes_or_backend_calls(self):
        self.run_event([NONSTD, LEGAL], overlap=True)
        before = self.pipe.voter.result(7)
        self.pipe.voter = PlateVoter()
        self.pipe.ocr.reset_mock()
        self.pipe.ocr_events = None
        self.state = TrackState(1, 1)
        self.run_event([NONSTD, LEGAL], overlap=True)
        self.assertEqual(self.pipe.voter.result(7), before)
        self.assertEqual(self.pipe.ocr.read_plate.call_count, 2)

    def test_process_kill_keeps_completed_lines_without_final_export(self):
        ready = Path(self.tmp.name) / "ready"
        code = """
import sys, time
from pathlib import Path
from alpr.ocr_events import OcrEventWriter
w = OcrEventWriter(sys.argv[1])
for frame in range(1, 4):
    w.write(track_id=7, frame=frame, time_sec=frame/30, read=None,
            crop_quality_score=0.5, accepted_for_vote=False,
            reject_reason='empty_read', n_backend_calls=1)
Path(sys.argv[2]).write_text('ready')
time.sleep(30)
"""
        child = subprocess.Popen([sys.executable, "-c", code, str(self.path), str(ready)],
                                 cwd=Path(__file__).resolve().parents[2])
        try:
            import time
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            # Read while the producer is still running, then terminate without cleanup.
            live = self.path.read_bytes()
            child.kill()
            child.wait(timeout=5)
            self.assertEqual(self.path.read_bytes(), live)
            self.assertTrue(live.endswith(b"\n"))
            self.assertEqual([json.loads(line)["frame"] for line in live.splitlines()], [1, 2, 3])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
