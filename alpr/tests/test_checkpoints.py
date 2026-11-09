"""Model-free checkpoint tests, including forced termination of a video worker."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alpr import pipeline
from alpr.config import AlprConfig
from alpr.pipeline import AlprPipeline, LogEntry
from alpr.voting import CONFIRMED, PlateResult


def _initialize(self, cfg, fps):
    self.cfg, self.fps = cfg, fps
    self.log = []
    self.checker = self.counter = None
    self._vehicle_args = {}


def _process_frame(self, frame, idx):
    if not self.log:
        result = PlateResult("WA 1234 B", "WA1234B", 0.95, 10, CONFIRMED)
        self.log.append(LogEntry(result, "Car", "", idx, idx, {1}))
    self.log[0].last_frame = idx
    return frame, []


def _run_video(source, out, marker=None, stop_mode=None, gate=False, interval=0):
    cfg = AlprConfig(source=str(source), out=str(out), headless=True, start_sec=1.2, frame_step=2)
    original_dump = json.dump

    def process(self, frame, idx):
        if idx == 3 and stop_mode == "processing":
            Path(marker).touch()
            time.sleep(120)
        return _process_frame(self, frame, idx)

    def dump(report, handle, **kwargs):
        if report.get("frames_processed") == 3 and stop_mode == "writing":
            handle.write('{"complete":')
            handle.flush()
            Path(marker).touch()
            time.sleep(120)
        return original_dump(report, handle, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(AlprPipeline, "__init__", _initialize))
        stack.enter_context(patch.object(AlprPipeline, "process_frame", process))
        stack.enter_context(patch.object(AlprPipeline, "lane_report", return_value=None))
        stack.enter_context(patch.object(pipeline, "PARTIAL_SAVE_INTERVAL_SEC", interval))
        stack.enter_context(patch.object(pipeline.json, "dump", dump))
        if gate:
            cfg.view_gate = {"template": "unused"}
            stack.enter_context(patch.object(pipeline.ViewGate, "from_config", return_value=lambda frame: False))
        return pipeline.run(cfg)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source, self.out = self.root / "source.avi", self.root / "result.mp4"
        writer = cv2.VideoWriter(str(self.source), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
        self.assertTrue(writer.isOpened())
        try:
            for _ in range(30):
                writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
        finally:
            writer.release()
        self.partial = self.out.with_suffix(".partial.json")
        self.final = self.out.with_suffix(".plates.json")

    def test_empty_checkpoint_exists_before_models_load(self):
        self.partial.write_text('{"complete": true, "plates": ["stale"]}', encoding="utf-8")
        capture = cv2.VideoCapture(str(self.source))
        self.addCleanup(capture.release)
        cfg = AlprConfig(source=str(self.source), out=str(self.out), headless=True)
        with patch.object(pipeline.cv2, "VideoCapture", return_value=capture), \
                patch.object(AlprPipeline, "__init__", side_effect=RuntimeError("model load stopped")):
            with self.assertRaisesRegex(RuntimeError, "model load stopped"):
                pipeline.run(cfg)
        self.assertEqual(json.loads(self.partial.read_text(encoding="utf-8")),
                         {"complete": False, "frames_processed": 0, "plates": []})
        self.assertFalse(self.final.exists())

    def _terminate_worker(self, mode):
        marker = self.root / "ready"
        with (self.root / "worker.log").open("w", encoding="utf-8") as log:
            worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker",
                                       str(self.source), str(self.out), str(marker), mode],
                                      stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 60
                while not marker.exists() and worker.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), "Worker did not reach the interruption point")
                worker.terminate()
                worker.wait(timeout=10)
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=10)
        report = json.loads(self.partial.read_text(encoding="utf-8"))
        self.assertIs(report["complete"], False)
        self.assertEqual(report["frames_processed"], 2)
        row, = report["plates"]
        self.assertEqual(row["compact"], "WA1234B")
        self.assertEqual((row["first_sec"], row["last_sec"]), (1.4, 1.6))
        self.assertFalse(self.final.exists())

    def test_forced_stop_preserves_last_checkpoint(self):
        self._terminate_worker("processing")

    def test_forced_stop_during_write_preserves_readable_checkpoint(self):
        self._terminate_worker("writing")
        self.assertTrue(list(self.root.glob("result.partial.json.*.tmp")))

    def test_replace_failure_preserves_previous_checkpoint(self):
        pipeline._write_json_atomic(str(self.partial), {"complete": False, "frames_processed": 2, "plates": []})
        previous = self.partial.read_bytes()
        with patch.object(pipeline.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                pipeline._write_json_atomic(str(self.partial), {"frames_processed": 3})
        self.assertEqual(self.partial.read_bytes(), previous)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_success_keeps_final_report_compatible_and_flushes_partial(self):
        # A long interval ensures the last snapshot comes from the forced final save.
        self.assertEqual(_run_video(self.source, self.out, interval=float("inf")), 0)
        report = json.loads(self.final.read_text(encoding="utf-8"))
        partial = json.loads(self.partial.read_text(encoding="utf-8"))
        self.assertEqual(set(report), {"source", "profile", "fps", "frames", "window_sec", "plates", "processing"})
        self.assertEqual((report["fps"], report["frames"], report["window_sec"]), (5.0, 9, [1.2, 3.0]))
        self.assertEqual(partial, {"complete": False, "frames_processed": 9, "plates": report["plates"]})
        self.assertEqual(report["plates"][0]["last_sec"], 3.0)

    def test_gated_frames_still_update_progress(self):
        self.assertEqual(_run_video(self.source, self.out, gate=True), 0)
        self.assertEqual(json.loads(self.partial.read_text(encoding="utf-8")),
                         {"complete": False, "frames_processed": 9, "plates": []})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        sys.exit(_run_video(*sys.argv[2:]))
    unittest.main()
