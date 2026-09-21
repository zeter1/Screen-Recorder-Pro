"""Isolated regressions for export, missing audio and foreign-process safety.

Uses synthetic files and simulated processes; never captures or kills real tasks.
"""
import json
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from screen_recorder.app import ScreenRecorderProWin11
import verify_recording_publication


class ImportantFixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recorder_important_")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.app = object.__new__(ScreenRecorderProWin11)
        self.app.log_exception = lambda *a, **k: None
        self.app.diagnostic_log = lambda *a, **k: None

    def test_missing_registered_loopback_blocks_save(self):
        segment = self.folder / "segment.mp4"
        segment.write_bytes(b"original video")
        wav = self.folder / "required.wav"
        self.app.python_loopback_audio_segments = {str(segment): str(wav)}
        for content in (None, b"", b"0" * 44):
            with self.subTest(content=content):
                if content is not None:
                    wav.write_bytes(content)
                with self.assertRaises(RuntimeError):
                    self.app.prepare_segments_with_python_loopback_audio([segment])
                self.assertEqual(segment.read_bytes(), b"original video")

    def test_segment_without_loopback_is_allowed(self):
        self.app.python_loopback_audio_segments = {}
        segment = self.folder / "silent.mp4"
        self.assertEqual(self.app.prepare_segments_with_python_loopback_audio([segment]), [segment])

    def test_missing_loopback_cannot_publish_success(self):
        for content in (None, b"0" * 44):
            with self.subTest(content=content):
                fixture = verify_recording_publication.RecordingPublication()
                fixture.setUp()
                try:
                    app = fixture.app
                    wav = fixture.folder / "required.wav"
                    if content is not None:
                        wav.write_bytes(content)
                    app.python_loopback_audio_segments = {str(app.segments[0]): str(wav)}
                    old_merge = app.merge_segments
                    def merge():
                        app.prepare_segments_with_python_loopback_audio(app.segments)
                        old_merge()
                    app.merge_segments = merge
                    app._stop_recording_worker(False)
                    self.assertFalse(app.finished[0])
                    self.assertFalse(app.output_path.exists())
                    self.assertEqual(app.segments[0].read_bytes(), b"original segment")
                finally:
                    fixture.doCleanups()

    def test_foreign_audio_filters_are_not_process_ownership(self):
        commands = [
            'ffmpeg -i music.wav -af astats=metadata=1:reset=0.25 -f null -',
            'ffmpeg -i music.wav -af ametadata=print:key=lavfi.astats.Overall.RMS_level -f null -',
        ]
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(stdout=json.dumps([
                {"ProcessId": 90001 + i, "CommandLine": cmd} for i, cmd in enumerate(commands)
            ]))
        self.app.subprocess_cwd = lambda: str(self.folder)
        with patch("screen_recorder.mixins.processes.subprocess.run", side_effect=run):
            self.app.cleanup_stale_ffmpeg_processes_from_previous_runs()
        self.assertFalse(any(c[0] == "taskkill" for c in calls), calls)

    def export(self, returncode=0, invalid=False, existing=True, timeout=False):
        app = self.app
        destination = self.folder / "result.gif"
        if existing:
            destination.write_bytes(b"previous valid result")
        scheduled = queue.Queue()
        self.ui_threads = []
        def after(delay, callback):
            self.ui_threads.append(threading.get_ident())
            scheduled.put(callback)
        app.root = SimpleNamespace(after=after)
        app.status_var = SimpleNamespace(set=lambda text: None)
        shown = []
        errors = []
        app.reveal_in_file_manager = shown.append
        def run(command, **kwargs):
            Path(command[-1]).write_bytes(b"new candidate")
            if timeout:
                raise subprocess.TimeoutExpired(command, 1800)
            return SimpleNamespace(returncode=returncode, stderr="controlled failure")
        app.run_managed_process = run
        def start(command, **kwargs):
            def communicate(timeout=None):
                result = run(command)
                return "", result.stderr
            return SimpleNamespace(communicate=communicate, returncode=returncode)
        app.start_managed_process = start
        self.unregistered = []
        self.terminated = []
        app.unregister_child_process = self.unregistered.append
        app.terminate_process_tree = lambda *a, **k: self.terminated.append(a)
        def validate(path, **kwargs):
            self.assertEqual(Path(path).read_bytes(), b"new candidate")
            if invalid:
                raise RuntimeError("invalid media")
            return True
        app.validate_media_file = validate
        with patch("screen_recorder.mixins.file_tools.messagebox.showerror", side_effect=lambda *a: errors.append(a)):
            app._run_export_in_thread(["ffmpeg", "-y", str(destination)], destination, "busy", str(destination))
            deadline = time.monotonic() + 3
            while not (shown or errors) and time.monotonic() < deadline:
                try:
                    scheduled.get(timeout=0.01)()
                except queue.Empty:
                    pass
            self.assertTrue(shown or errors, "export never delivered result")
        return destination, shown, errors

    def test_export_failure_preserves_previous_result(self):
        destination, shown, errors = self.export(returncode=1)
        self.assertFalse(shown)
        self.assertTrue(errors)
        self.assertEqual(destination.read_bytes(), b"previous valid result")

    def test_export_invalid_candidate_is_not_published(self):
        destination, shown, errors = self.export(invalid=True, existing=False)
        self.assertFalse(shown)
        self.assertTrue(errors)
        self.assertFalse(destination.exists())

    def test_export_collision_preserves_previous_result(self):
        destination, shown, errors = self.export()
        self.assertFalse(errors)
        self.assertEqual(destination.read_bytes(), b"previous valid result")
        self.assertEqual(Path(shown[0]).read_bytes(), b"new candidate")
        self.assertNotEqual(Path(shown[0]), destination)

    def test_export_timeout_preserves_previous_result(self):
        destination, shown, errors = self.export(timeout=True)
        self.assertFalse(shown)
        self.assertTrue(errors)
        self.assertEqual(destination.read_bytes(), b"previous valid result")
        self.assertTrue(self.terminated)
        self.assertTrue(self.unregistered)

    def test_export_schedules_ui_only_on_main_thread(self):
        self.export(existing=False)
        self.assertTrue(self.ui_threads)
        self.assertEqual(set(self.ui_threads), {threading.get_ident()})

    def test_close_before_worker_prevents_process_launch(self):
        app = self.app
        workers = []
        app.status_var = SimpleNamespace(set=lambda text: None)
        app.root = SimpleNamespace(after=lambda *args: None)
        launches = []
        app.start_managed_process = lambda *a, **k: launches.append(a)
        def thread(*args, target, **kwargs):
            return SimpleNamespace(start=lambda: workers.append(target))
        with patch("screen_recorder.mixins.file_tools.threading.Thread", side_effect=thread):
            app._run_export_in_thread(["ffmpeg", str(self.folder / "out.gif")], self.folder / "out.gif", "busy", "done")
        app._exiting = True
        workers[0]()
        self.assertFalse(launches)

    def test_close_during_registration_terminates_late_process(self):
        app = self.app
        workers = []
        terminated = []
        unregistered = []
        communicated = []
        app.status_var = SimpleNamespace(set=lambda text: None)
        app.root = SimpleNamespace(after=lambda *args: None)
        process = SimpleNamespace(communicate=lambda **k: communicated.append(True))
        def start(*args, **kwargs):
            app._exiting = True
            return process
        app.start_managed_process = start
        app.terminate_process_tree = lambda p, **k: terminated.append(p)
        app.unregister_child_process = unregistered.append
        def thread(*args, target, **kwargs):
            return SimpleNamespace(start=lambda: workers.append(target))
        with patch("screen_recorder.mixins.file_tools.threading.Thread", side_effect=thread):
            app._run_export_in_thread(["ffmpeg", str(self.folder / "out.gif")], self.folder / "out.gif", "busy", "done")
        workers[0]()
        self.assertEqual(terminated, [process])
        self.assertEqual(unregistered, [process])
        self.assertFalse(communicated)


if __name__ == "__main__":
    unittest.main()
