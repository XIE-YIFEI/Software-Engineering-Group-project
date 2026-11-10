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
    if self.cfg.evidence_dir:
        self.log[0].crop = np.full((12, 40, 3), idx * 10, dtype=np.uint8)
        self.log[0].scene = {"jpg": cv2.imencode(".jpg", frame)[1].tobytes(), "frame": idx,
                             "vehicle_box": [0, 0, 64, 48], "plate_box": [1, 2, 41, 14]}
    return frame, []


def _run_video(source, out, marker=None, stop_mode=None, gate=False, interval=0, evidence=False):
    cfg = AlprConfig(source=str(source), out=str(out), headless=True, start_sec=1.2, frame_step=2,
                     profile="checkpoint_test")
    if evidence or stop_mode == "evidence_writing":
        cfg.evidence_dir = str(Path(out).parent / "evidence")
    original_dump = json.dump
    original_image_write = pipeline._write_evidence_image
    image_writes = 0

    def write_image(path, data):
        nonlocal image_writes
        image_writes += 1
        if image_writes == 6 and stop_mode == "evidence_writing":
            # The third snapshot has saved its new crop but not its scene or JSON.
            Path(marker).touch()
            time.sleep(120)
        return original_image_write(path, data)

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
        stack.enter_context(patch.object(pipeline, "_write_evidence_image", write_image))
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

    def _metadata(self, end):
        return {"source": str(self.source), "profile": "checkpoint_test", "fps": 5.0,
                "window_sec": [1.2, end]}

    def test_empty_checkpoint_exists_before_models_load(self):
        self.partial.write_text('{"complete": true, "plates": ["stale"]}', encoding="utf-8")
        capture = cv2.VideoCapture(str(self.source))
        self.addCleanup(capture.release)
        cfg = AlprConfig(source=str(self.source), out=str(self.out), headless=True,
                         start_sec=1.234, frame_step=2, profile="checkpoint_test")
        with patch.object(pipeline.cv2, "VideoCapture", return_value=capture), \
                patch.object(AlprPipeline, "__init__", side_effect=RuntimeError("model load stopped")):
            with self.assertRaisesRegex(RuntimeError, "model load stopped"):
                pipeline.run(cfg)
        self.assertEqual(json.loads(self.partial.read_text(encoding="utf-8")),
                         {**self._metadata(1.234), "window_sec": [1.234, 1.234],
                          "complete": False, "frames_processed": 0, "plates": []})
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
        for key, value in self._metadata(1.6).items():
            self.assertEqual(report[key], value)
        self.assertIsInstance(report["fps"], float)
        self.assertIsInstance(report["source"], str)
        self.assertIsInstance(report["profile"], str)
        row, = report["plates"]
        self.assertEqual(row["compact"], "WA1234B")
        self.assertEqual((row["first_sec"], row["last_sec"]), (1.4, 1.6))
        self.assertFalse(self.final.exists())
        return report

    def test_forced_stop_preserves_last_checkpoint(self):
        self._terminate_worker("processing")

    def test_forced_stop_during_write_preserves_readable_checkpoint(self):
        self._terminate_worker("writing")
        self.assertTrue(list(self.root.glob("result.partial.json.*.tmp")))

    def test_forced_stop_during_next_image_pair_preserves_old_evidence(self):
        report = self._terminate_worker("evidence_writing")
        evidence = report["plates"][0]["evidence"]
        self.assertEqual((evidence["frame"], evidence["time_sec"]), (2, 1.6))
        crop = cv2.imread(evidence["crop"])
        scene = cv2.imread(evidence["scene"])
        self.assertEqual(crop.shape, (12, 40, 3))
        self.assertEqual(scene.shape, (48, 64, 3))
        self.assertAlmostEqual(float(crop.mean()), 20, delta=1)

    def test_partial_references_images_only_after_successful_save(self):
        original_write = pipeline._write_evidence_image
        writes = 0

        def fail_scene(path, data):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("scene save failed")
            original_write(path, data)

        with patch.object(pipeline, "_write_evidence_image", side_effect=fail_scene):
            with self.assertRaisesRegex(OSError, "scene save failed"):
                _run_video(self.source, self.out, evidence=True)
        self.assertEqual(json.loads(self.partial.read_text(encoding="utf-8")),
                         {**self._metadata(1.2), "complete": False, "frames_processed": 0, "plates": []})

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
        self.assertEqual(partial, {**self._metadata(3.0), "complete": False,
                                   "frames_processed": 9, "plates": report["plates"]})
        for key in ("source", "profile", "fps", "window_sec"):
            self.assertEqual(partial[key], report[key])
            self.assertIs(type(partial[key]), type(report[key]))
        self.assertEqual(report["plates"][0]["last_sec"], 3.0)

    def test_gated_frames_still_update_progress(self):
        self.assertEqual(_run_video(self.source, self.out, gate=True), 0)
        self.assertEqual(json.loads(self.partial.read_text(encoding="utf-8")),
                         {**self._metadata(3.0), "complete": False, "frames_processed": 9, "plates": []})

    def test_periodic_windows_follow_saved_progress(self):
        snapshots = []
        original_write = pipeline._write_json_atomic

        def capture(path, report):
            original_write(path, report)
            snapshots.append(json.loads(self.partial.read_text(encoding="utf-8")))

        with patch.object(pipeline, "_write_json_atomic", side_effect=capture):
            self.assertEqual(_run_video(self.source, self.out), 0)
        self.assertEqual([s["frames_processed"] for s in snapshots], [0, *range(1, 10), 9])
        self.assertEqual([s["window_sec"] for s in snapshots],
                         [[1.2, end] for end in [1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.4, 2.6, 2.8, 3.0, 3.0]])
        for snapshot in snapshots:
            self.assertEqual(snapshot["fps"], 5.0)  # 10 source FPS / frame_step 2
            self.assertEqual(snapshot["source"], str(self.source))
            self.assertEqual(snapshot["profile"], "checkpoint_test")
            self.assertIs(snapshot["complete"], False)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        sys.exit(_run_video(*sys.argv[2:]))
    unittest.main()
