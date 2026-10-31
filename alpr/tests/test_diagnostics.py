"""Model-free tests for FR03 uncertain-plate diagnostic outputs."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np

from alpr.diagnostics import collect_uncertain_tracks, write_uncertain_diagnostics


def _state(reads, crop=None):
    return SimpleNamespace(n_reads=reads, first_frame=2, last_frame=40, best_crop=crop)


def _vote(status, plate, votes):
    return SimpleNamespace(status=status, compact=plate, confidence=0.55, votes=votes)


class _Voter:
    def __init__(self, results):
        self.results = results

    def result(self, track_id):
        return self.results.get(track_id)


class UncertainPlateDiagnosticTests(unittest.TestCase):
    def setUp(self):
        crop = np.full((16, 80, 3), 240, dtype=np.uint8)
        self.states = {
            1: _state(5),
            2: _state(9, crop),
            3: _state(0),
            4: _state(3),
            5: _state(8),
        }
        self.voter = _Voter({
            1: _vote("CONFIRMED", "VJF1234", 5),
            2: _vote("TENTATIVE", "WA1284", 9),
            4: _vote("ACQUIRING", "JWT4811", 2),
            5: _vote("TENTATIVE", "VCV155", 8),
        })

    def test_collects_unconfirmed_only(self):
        rows = collect_uncertain_tracks(self.states, self.voter, start_frame=300, fps=30)
        self.assertEqual([r["track_id"] for r in rows], [2, 4, 5])
        self.assertEqual(rows[0]["first_sec"], 10.07)
        self.assertEqual(rows[0]["ocr_attempts"], 9)

    def test_excludes_tracks_already_in_official_log(self):
        log = [SimpleNamespace(track_ids={2}, simulated=False),
               SimpleNamespace(track_ids={1}, simulated=True)]
        rows = collect_uncertain_tracks(self.states, self.voter, log)
        self.assertEqual([r["track_id"] for r in rows], [4, 5])

    def test_writes_separate_json_and_best_crop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_uncertain_diagnostics(self.states, self.voter, [], directory)
            with open(path, encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertTrue(report["diagnostic_only"])
            self.assertEqual(len(report["unconfirmed_tracks"]), 3)
            self.assertEqual(report["unconfirmed_tracks"][0]["best_crop"], "track_000002.jpg")
            self.assertTrue(os.path.isfile(os.path.join(directory, "track_000002.jpg")))
            self.assertNotIn("best_crop", report["unconfirmed_tracks"][1])

    def test_rejects_nonpositive_fps(self):
        with self.assertRaises(ValueError):
            collect_uncertain_tracks(self.states, self.voter, fps=0)


if __name__ == "__main__":
    unittest.main()
