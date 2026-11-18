"""Model-free checkpoint tests, including forced termination of a video worker."""
import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alpr import pipeline
from alpr.config import AlprConfig
from alpr.pipeline import AlprPipeline, LogEntry
from alpr.registry import PlateChecker, Registry
from alpr.voting import CONFIRMED, PlateResult


def _initialize(self, cfg, fps, checker=None):
    self.cfg, self.fps = cfg, fps
    self.log = []
    self.checker, self.counter = checker, None
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


def _run_video(source, out, marker=None, stop_mode=None, gate=False, interval=0, evidence=False,
               registry_config=None, initialize=_initialize):
    cfg = AlprConfig(source=str(source), out=str(out), headless=True, start_sec=1.2, frame_step=2,
                     profile="checkpoint_test", **(registry_config or {}))
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
        stack.enter_context(patch.object(AlprPipeline, "__init__", initialize))
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

    @contextmanager
    def _stop_fixture(self, enabled=True):
        request = self.root / "stop.request"
        cfg = AlprConfig(source=str(self.source), out=str(self.out), start_sec=1.2, frame_step=2,
                         profile="checkpoint_test", evidence_dir=str(self.root / "evidence"),
                         stop_file=str(request) if enabled else "", registry_enabled=True,
                         hud_mode="dashboard", summary_sec=1)
        registry = Registry()
        self.addCleanup(registry.close)
        registry.set_meta("disclaimer", "SIMULATED: cooperative stop fixture")
        checker = PlateChecker(registry, rules=["ILLEGAL_FORMAT", "DUPLICATE"])
        raw_capture = cv2.VideoCapture(str(self.source))
        self.addCleanup(raw_capture.release)
        capture = Mock(wraps=raw_capture)
        writer = Mock(codec="test")
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(pipeline.cv2, "VideoCapture", return_value=capture))
            stack.enter_context(patch.object(pipeline, "VideoOut", return_value=writer))
            stack.enter_context(patch.object(PlateChecker, "from_config", return_value=checker))
            stack.enter_context(patch.object(AlprPipeline, "__init__", _initialize))
            process = stack.enter_context(patch.object(AlprPipeline, "process_frame", autospec=True,
                                                       side_effect=_process_frame))
            lanes = stack.enter_context(patch.object(AlprPipeline, "lane_report", return_value=None))
            stack.enter_context(patch.object(AlprPipeline, "_stats", return_value={}))
            stack.enter_context(patch.object(AlprPipeline, "_lane_view", return_value=None))
            card = stack.enter_context(patch.object(pipeline, "render_summary",
                                                    return_value=np.zeros((48, 64, 3), dtype=np.uint8)))
            stack.enter_context(patch.object(pipeline, "PARTIAL_SAVE_INTERVAL_SEC", float("inf")))
            stack.enter_context(redirect_stdout(output))
            yield SimpleNamespace(cfg=cfg, request=request, capture=capture, raw_capture=raw_capture,
                                  writer=writer, process=process, lanes=lanes, card=card, output=output)

    def _assert_stopped(self, fixture, frames):
        partial = json.loads(self.partial.read_text(encoding="utf-8"))
        self.assertIs(partial["complete"], False)
        self.assertEqual(partial["frames_processed"], frames)
        for key, value in self._metadata(round(1.2 + frames / 5, 2)).items():
            self.assertEqual(partial[key], value)
        self.assertEqual(partial["registry"], {"rules": ["DUPLICATE", "ILLEGAL_FORMAT"],
                                             "disclaimer": "SIMULATED: cooperative stop fixture"})
        self.assertNotIn("simulated_events", partial)
        self.assertFalse(self.final.exists())
        fixture.card.assert_not_called()
        fixture.capture.release.assert_called_once_with()
        fixture.writer.close.assert_called_once_with()
        self.assertNotIn("done:", fixture.output.getvalue())
        self.assertTrue(fixture.request.exists())  # Recognition never deletes the request.
        return partial

    def test_stop_option_cli_and_config(self):
        import run_alpr
        from alpr.config import load_config
        config = self.root / "config.yaml"
        config.write_text('profiles:\n  fixture:\n    stop_file: configured.request\n', encoding="utf-8")
        self.assertEqual(AlprConfig().stop_file, "")
        self.assertEqual(load_config("fixture", str(config)).stop_file, "configured.request")
        with patch.object(run_alpr, "run", return_value=0) as run:
            self.assertEqual(run_alpr.main(["--profile", "fixture", "--config", str(config),
                                            "--stop-file", str(self.root / "absolute.request")]), 0)
        self.assertEqual(run.call_args.args[0].stop_file, str(self.root / "absolute.request"))

    def test_disabled_stop_ignores_existing_request_and_completes(self):
        with self._stop_fixture(enabled=False) as f:
            f.request.touch()
            self.assertEqual(pipeline.run(f.cfg), 0)
            self.assertEqual(f.process.call_count, 9)
            self.assertTrue(self.final.exists())
            self.assertIn("done:", f.output.getvalue())
            f.card.assert_called_once()
            f.capture.release.assert_called_once_with()
            f.writer.close.assert_called_once_with()
            self.assertTrue(f.request.exists())

    def test_stop_present_after_initialization_saves_zero_progress(self):
        with self._stop_fixture() as f:
            f.request.touch()
            self.assertEqual(pipeline.run(f.cfg), 0)
            f.process.assert_not_called()
            f.capture.read.assert_not_called()
            self.assertEqual(self._assert_stopped(f, 0)["plates"], [])

    def test_missing_stop_file_completes_without_creating_request(self):
        with self._stop_fixture() as f:
            self.assertEqual(pipeline.run(f.cfg), 0)
            self.assertTrue(self.final.exists())
            self.assertEqual(f.process.call_count, 9)
            self.assertFalse(f.request.exists())
            f.capture.release.assert_called_once_with()
            f.writer.close.assert_called_once_with()

    def test_stop_during_frame_finishes_frame_and_preserves_evidence(self):
        with self._stop_fixture() as f:
            def process(pipe, frame, idx):
                if idx == 2:
                    f.request.touch()  # Arrives before this frame's work has completed.
                return _process_frame(pipe, frame, idx)
            f.process.side_effect = process
            self.assertEqual(pipeline.run(f.cfg), 0)
            self.assertEqual(f.process.call_count, 2)
            self.assertEqual(f.capture.read.call_count, 2)
            row, = self._assert_stopped(f, 2)["plates"]
            self.assertEqual((row["first_sec"], row["last_sec"]), (1.4, 1.6))
            evidence = row["evidence"]
            self.assertEqual((evidence["frame"], evidence["time_sec"]), (2, 1.6))
            self.assertEqual(cv2.imread(evidence["crop"]).shape, (12, 40, 3))
            self.assertEqual(cv2.imread(evidence["scene"]).shape, (48, 64, 3))
            self.assertAlmostEqual(float(cv2.imread(evidence["crop"]).mean()), 20, delta=1)

    def test_stop_during_view_gate_skip(self):
        with self._stop_fixture() as f:
            f.cfg.view_gate = {"template": "unused"}
            def gate(frame):
                f.request.touch()
                return False
            with patch.object(pipeline.ViewGate, "from_config", return_value=gate):
                self.assertEqual(pipeline.run(f.cfg), 0)
            f.process.assert_not_called()
            self.assertEqual(f.capture.read.call_count, 1)
            self.assertEqual(self._assert_stopped(f, 1)["plates"], [])

    def test_stop_during_finalization_prevents_report_and_end_card(self):
        with self._stop_fixture() as f:
            def lanes():
                f.request.touch()
                return None
            f.lanes.side_effect = lanes
            self.assertEqual(pipeline.run(f.cfg), 0)
            self._assert_stopped(f, 9)

    def test_stop_during_writer_close_prevents_final_publication(self):
        with self._stop_fixture() as f:
            f.cfg.summary_sec = 0
            f.writer.close.side_effect = f.request.touch
            self.assertEqual(pipeline.run(f.cfg), 0)
            self._assert_stopped(f, 9)

    def test_stop_save_and_cleanup_failures_preserve_original_errors_and_snapshot(self):
        with self._stop_fixture() as f:
            def process(pipe, frame, idx):
                if idx == 2:
                    f.request.touch()
                return _process_frame(pipe, frame, idx)
            f.process.side_effect = process
            original_replace = pipeline.os.replace
            def replace(source, target):
                if str(target) == str(self.partial):
                    snapshot = json.loads(Path(source).read_text(encoding="utf-8"))
                    if snapshot["frames_processed"] == 2:
                        raise OSError("forced checkpoint replace failed")
                return original_replace(source, target)
            def release():
                f.raw_capture.release()
                raise RuntimeError("input cleanup failed")
            f.capture.release.side_effect = release
            f.writer.close.side_effect = RuntimeError("output cleanup failed")
            with patch.object(pipeline, "PARTIAL_SAVE_INTERVAL_SEC", 0), \
                    patch.object(pipeline.os, "replace", side_effect=replace):
                with self.assertRaises(ExceptionGroup) as raised:
                    pipeline.run(f.cfg)
            errors = raised.exception.exceptions
            self.assertEqual([str(e) for e in errors],
                             ["forced checkpoint replace failed", "output cleanup failed", "input cleanup failed"])
            partial = self._assert_stopped(f, 1)
            evidence = partial["plates"][0]["evidence"]
            self.assertEqual((evidence["frame"], evidence["time_sec"]), (1, 1.4))
            self.assertIsNotNone(cv2.imread(evidence["crop"]))
            self.assertIsNotNone(cv2.imread(evidence["scene"]))

    def test_stop_flush_failure_has_nonzero_process_exit(self):
        request = self.root / "stop.request"
        request.touch()
        script = '''
import sys
from unittest.mock import patch
from alpr.tests.test_checkpoints import _run_video
from alpr import pipeline
original = pipeline._write_json_atomic
calls = 0
def save(path, report):
    global calls
    calls += 1
    if calls == 2:
        raise OSError("forced stop flush failed")
    return original(path, report)
with patch.object(pipeline, "_write_json_atomic", side_effect=save):
    sys.exit(_run_video(sys.argv[1], sys.argv[2], registry_config={"stop_file": sys.argv[3]}))
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.source), str(self.out), str(request)],
                                capture_output=True, text=True, timeout=30,
                                cwd=str(Path(__file__).resolve().parents[2]))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forced stop flush failed", result.stderr)
        self.assertEqual(json.loads(self.partial.read_text(encoding="utf-8"))["frames_processed"], 0)
        self.assertFalse(self.final.exists())

    def test_stop_cleanup_failure_propagates_after_successful_flush(self):
        with self._stop_fixture() as f:
            f.request.touch()
            f.writer.close.side_effect = OSError("video close failed")
            with self.assertRaises(ExceptionGroup) as raised:
                pipeline.run(f.cfg)
            self.assertEqual(str(raised.exception.exceptions[0]), "video close failed")
            self._assert_stopped(f, 0)

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

    def test_registry_context_in_initial_periodic_and_final_snapshots(self):
        registry_path = self.root / "registry.db"
        source_registry = Registry(str(registry_path))
        self.addCleanup(source_registry.close)
        original_write = pipeline._write_json_atomic
        original_checker = PlateChecker.from_config
        cases = [(True, ["unmatched", "DUPLICATE", "UNMATCHED"], "SIMULATED: test registry"),
                 (True, [], None), (False, ["DUPLICATE"], "SIMULATED: disabled")]
        for enabled, rules, disclaimer in cases:
            with self.subTest(enabled=enabled, rules=rules, disclaimer=disclaimer):
                source_registry.db.execute("DELETE FROM meta")
                if disclaimer is not None:
                    source_registry.set_meta("disclaimer", disclaimer)
                source_registry.db.commit()
                snapshots = []

                def capture(path, report):
                    original_write(path, report)
                    snapshots.append(json.loads(self.partial.read_text(encoding="utf-8")))

                def make_checker(cfg):
                    checker = original_checker(cfg)
                    self.addCleanup(checker.registry.close)
                    return checker

                config = {"registry_enabled": enabled, "registry_path": str(registry_path),
                          "registry_rules": rules, "read_plates": False, "count_line_y": 0}
                # Exercise real pipeline initialization; detector loading and frame work are model-free.
                with patch.object(pipeline, "_write_json_atomic", side_effect=capture), \
                        patch.object(PlateChecker, "from_config", side_effect=make_checker) as factory, \
                        patch.object(pipeline, "load_detector", return_value=(None, {})):
                    self.assertEqual(_run_video(self.source, self.out, registry_config=config,
                                                initialize=AlprPipeline.__init__), 0)
                self.assertEqual(factory.call_count, int(enabled))  # The preloaded checker is reused.
                self.assertEqual([s["frames_processed"] for s in snapshots], [0, *range(1, 10), 9])
                final = json.loads(self.final.read_text(encoding="utf-8"))
                expected = {"rules": sorted({r.upper() for r in rules}), "disclaimer": disclaimer or ""}
                for snapshot in snapshots:
                    frames = snapshot["frames_processed"]
                    self.assertEqual({k: snapshot[k] for k in self._metadata(1.2)},
                                     self._metadata(round(1.2 + frames / 5, 2)))
                    self.assertIs(snapshot["complete"], False)
                    self.assertNotIn("simulated_events", snapshot)
                    if enabled:
                        self.assertEqual(snapshot["registry"], expected)
                        self.assertEqual(snapshot["registry"],
                                         {k: final["registry"][k] for k in ("rules", "disclaimer")})
                    else:
                        self.assertNotIn("registry", snapshot)
                if enabled:
                    self.assertEqual(set(final["registry"]),
                                     {"path", "rules", "disclaimer", "alerts", "simulated_alerts"})
                    self.assertEqual(final["registry"]["path"], str(registry_path))
                    self.assertEqual((final["registry"]["alerts"], final["registry"]["simulated_alerts"]), (0, 0))
                    self.assertEqual(final["simulated_events"], [])
                else:
                    self.assertNotIn("registry", final)
                    self.assertNotIn("simulated_events", final)

    def test_initial_registry_context_survives_model_load_failure(self):
        registry_path = self.root / "registry.db"
        registry = Registry(str(registry_path))
        self.addCleanup(registry.close)
        registry.set_meta("disclaimer", "SIMULATED: available before models")
        checker = PlateChecker(registry, rules=["duplicate", "ILLEGAL_FORMAT"])
        cfg = AlprConfig(source=str(self.source), out=str(self.out), headless=True,
                         start_sec=1.2, frame_step=2, profile="checkpoint_test", registry_enabled=True)
        capture = cv2.VideoCapture(str(self.source))
        self.addCleanup(capture.release)
        with patch.object(pipeline.cv2, "VideoCapture", return_value=capture), \
                patch.object(PlateChecker, "from_config", return_value=checker), \
                patch.object(pipeline, "load_detector", side_effect=RuntimeError("model load stopped")):
            with self.assertRaisesRegex(RuntimeError, "model load stopped"):
                pipeline.run(cfg)
        self.assertEqual(json.loads(self.partial.read_text(encoding="utf-8")),
                         {**self._metadata(1.2), "complete": False, "frames_processed": 0, "plates": [],
                          "registry": {"rules": ["DUPLICATE", "ILLEGAL_FORMAT"],
                                       "disclaimer": "SIMULATED: available before models"}})
        self.assertFalse(self.final.exists())

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
