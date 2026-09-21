"""Save transaction regressions using synthetic files, never screen/devices."""
import tempfile
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from screen_recorder.app import ScreenRecorderProWin11
from screen_recorder.shared import NO_AUDIO
from screen_recorder.components.annotation_overlay import AnnotationOverlay


class RecordingPublication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recorder_publication_")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        app = object.__new__(ScreenRecorderProWin11)
        self.app = app
        app.output_path = self.folder / "recording.mp4"
        app.incomplete_output_path = None
        app.session_timing_detail_path = self.folder / "timing.json"
        app.segments = [self.folder / "segment_0001.mp4"]
        app.segments[0].write_bytes(b"original segment")
        app.current_log_path = self.folder / "recording.log"
        app.recording_session_id = "test"
        app.recording_failure_reason = None
        app.recording_start_requested_perf = None
        app.recording_stop_requested_perf = None
        app.recorded_wall_seconds = app.recorded_seconds = 1.0
        app.segment_started_at = None
        app.stopped = False
        app.finished = None
        app.root = SimpleNamespace(after=lambda delay, callback: callback())
        for name in ("diagnostic_log", "log_exception", "stop_cursor_highlight_overlay",
                     "stop_recording_performance_sampler", "write_pending_post_diagnostics_report",
                     "write_ai_problem_summary", "embed_recording_log_in_diagnostics"):
            setattr(app, name, lambda *a, **kw: None)
        app.stop_current_segment = lambda: setattr(app, "stopped", True)
        app.log_video_timing_summary = lambda path, **kw: {"path": str(path)}
        app.validate_final_timing_summary = lambda summary: True
        app.build_post_save_diagnostics_context = lambda *a, **kw: {}
        app.copy_debug_log_to_output = lambda: None
        app._finish_stop_recording = lambda ok, error, log: setattr(app, "finished", (ok, error))
        app.merge_segments = lambda: Path(getattr(app, "pending_output_path", None) or app.output_path).write_bytes(b"new video")
        app.validate_media_file = lambda path, **kw: True

    def test_candidate_hidden_until_validation(self):
        app = self.app
        destination = app.output_path
        def validate(path, **kw):
            self.assertTrue(app.stopped)
            self.assertNotEqual(Path(path), destination)
            self.assertFalse(destination.exists())
            self.assertEqual(Path(path).read_bytes(), b"new video")
        app.validate_media_file = validate
        app._stop_recording_worker(False)
        self.assertTrue(app.finished[0], app.finished)
        self.assertEqual(destination.read_bytes(), b"new video")
        self.assertEqual(app.last_video_timing_summary["path"], str(destination))
        self.assertEqual(json.loads(app.session_timing_detail_path.read_text(encoding="utf-8"))["path"], str(destination))

    def test_collision_preserves_existing_video(self):
        app = self.app
        destination = app.output_path
        def validate(path, **kw):
            destination.write_bytes(b"other recording")
        app.validate_media_file = validate
        app._stop_recording_worker(False)
        self.assertEqual(destination.read_bytes(), b"other recording")
        self.assertTrue(app.finished[0], app.finished)
        self.assertNotEqual(app.output_path, destination)
        self.assertEqual(app.output_path.read_bytes(), b"new video")

    def test_invalid_candidate_never_moves_existing_destination(self):
        app = self.app
        destination = app.output_path
        destination.write_bytes(b"other recording")
        app.validate_final_timing_summary = lambda summary: (_ for _ in ()).throw(RuntimeError("bad timing"))
        app._stop_recording_worker(False)
        self.assertFalse(app.finished[0])
        self.assertEqual(destination.read_bytes(), b"other recording")
        self.assertEqual(app.incomplete_output_path.read_bytes(), b"new video")
        self.assertTrue(app.segments[0].exists())

    def test_report_failure_keeps_successful_video(self):
        app = self.app
        app.write_pending_post_diagnostics_report = lambda context: (_ for _ in ()).throw(OSError("report disk error"))
        app._stop_recording_worker(False)
        self.assertTrue(app.finished[0], app.finished)
        self.assertEqual(app.output_path.read_bytes(), b"new video")
        self.assertIsNone(app.incomplete_output_path)

    def test_output_path_selection_does_not_create_directory(self):
        app = self.app
        folder = self.folder / "missing"
        app.output_folder = SimpleNamespace(get=lambda: str(folder))
        app.format_var = SimpleNamespace(get=lambda: "mp4")
        with patch.object(Path, "mkdir", side_effect=PermissionError("unavailable")):
            output = app.make_output_path_at_save_time()
        self.assertEqual(output.parent, folder)
        self.assertFalse(folder.exists())

    def test_destination_failure_still_stops_capture(self):
        app = self.app
        with patch.object(Path, "mkdir", side_effect=PermissionError("unavailable")):
            app._stop_recording_worker(False)
        self.assertTrue(app.stopped)
        self.assertFalse(app.finished[0])
        self.assertTrue(app.segments[0].exists())

    def test_stop_during_launch_waits_for_registered_segment(self):
        for exit_requested in (False, True):
            with self.subTest(exit_requested=exit_requested):
                app = MagicMock()
                app.is_recording = app.is_finalizing = app.is_starting = False
                app.is_paused = app.is_pause_transitioning = False
                app.current_session_log_dir = None
                app.recording_session_id = None
                app.recording_stderr_threads = []
                app.recording_capture_recovery_attempts = 0
                app.process = None
                app.normalize_saved_audio_choice.return_value = NO_AUDIO
                app.get_recording_temp_root.return_value = self.folder
                app.build_ffmpeg_command.return_value = ["ffmpeg"]
                app.make_output_path_at_save_time.return_value = self.folder / "saved.mp4"
                process = SimpleNamespace(pid=123, poll=lambda: None)
                app.start_managed_process.return_value = process
                app.stop_recording = ScreenRecorderProWin11.stop_recording.__get__(app)
                segment = self.folder / "segment_0001.mp4"
                app.start_new_segment.side_effect = lambda: ScreenRecorderProWin11.launch_checked_ffmpeg_segment(app, segment, "ddagrab")
                observed = []
                def stop_during_update():
                    if exit_requested:
                        app.cancel_start_requested = True
                    else:
                        app.stop_recording()
                    self.assertFalse(app.is_finalizing)
                    self.assertIsNone(app.process)
                app.root.update.side_effect = stop_during_update
                app._stop_recording_worker.side_effect = lambda paused: observed.append((app.process, list(app.segments), app.is_starting))
                def immediate_thread(target, args=(), **kw):
                    return SimpleNamespace(start=lambda: target(*args))
                with patch("screen_recorder.mixins.recording_control.threading.Thread", side_effect=immediate_thread), \
                     patch("screen_recorder.mixins.recording_session.LOGS_DIR", self.folder), \
                     patch("screen_recorder.mixins.recording_session.TEMP_RECORDINGS_DIR", self.folder):
                    ScreenRecorderProWin11.start_recording(app)
                self.assertEqual(observed, [(process, [segment], False)])
                self.assertFalse(app._stop_after_start_requested)


class RecordingTransitions(unittest.TestCase):
    def test_exit_during_failed_start_is_completed_once(self):
        for stage in ("countdown", "preflight", "launch"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(prefix="recorder_exit_") as folder:
                folder = Path(folder)
                app = MagicMock()
                app.is_recording = app.is_finalizing = app.is_starting = False
                app.is_paused = app.is_pause_transitioning = False
                app._exiting = app._exit_after_finalize = False
                app.running = True
                app.current_session_log_dir = app.recording_session_id = None
                app.recording_stderr_threads = []
                app.recording_capture_recovery_attempts = 0
                app.process = app.segment_started_at = None
                app.tray_icon = app.finalize_thread = app.post_diagnostics_thread = None
                app.screenshot_prepare_thread = app.screenshot_thread = None
                app.normalize_saved_audio_choice.return_value = NO_AUDIO
                app.get_recording_temp_root.return_value = folder
                app.exit_app = ScreenRecorderProWin11.exit_app.__get__(app)
                idle = []
                app.root.after_idle.side_effect = idle.append

                def request_exit():
                    app.exit_app()
                    self.assertTrue(app.cancel_start_requested)
                    self.assertTrue(app.is_starting)
                    app.root.destroy.assert_not_called()

                if stage == "countdown":
                    app.run_start_countdown.side_effect = request_exit
                elif stage == "preflight":
                    def failed_preflight():
                        request_exit()
                        return False
                    app.check_ffmpeg.side_effect = failed_preflight
                else:
                    def failed_launch():
                        request_exit()
                        raise RuntimeError("controlled FFmpeg startup failure")
                    app.start_new_segment.side_effect = failed_launch

                with patch("screen_recorder.mixins.recording_session.LOGS_DIR", folder), \
                     patch("screen_recorder.mixins.recording_session.TEMP_RECORDINGS_DIR", folder), \
                     patch("screen_recorder.mixins.recording_session.messagebox.showerror") as showerror:
                    ScreenRecorderProWin11.start_recording(app)
                    self.assertFalse(app.is_starting)
                    self.assertEqual(len(idle), 1, "deferred exit must be scheduled exactly once")
                    idle.pop()()
                self.assertFalse(app.running)
                self.assertTrue(app._exiting)
                app.root.destroy.assert_called_once()
                app.start_audio_meters.assert_not_called()
                showerror.assert_not_called()

    def test_failed_start_without_exit_stays_available(self):
        with tempfile.TemporaryDirectory(prefix="recorder_failed_start_") as folder:
            folder = Path(folder)
            app = MagicMock()
            app.is_recording = app.is_finalizing = app.is_starting = False
            app.is_paused = app.is_pause_transitioning = False
            app._exiting = app._exit_after_finalize = False
            app.running = True
            app.current_session_log_dir = app.recording_session_id = None
            app.recording_stderr_threads = []
            app.recording_capture_recovery_attempts = 0
            app.normalize_saved_audio_choice.return_value = NO_AUDIO
            app.get_recording_temp_root.return_value = folder
            app.start_new_segment.side_effect = RuntimeError("controlled FFmpeg startup failure")
            with patch("screen_recorder.mixins.recording_session.LOGS_DIR", folder), \
                 patch("screen_recorder.mixins.recording_session.TEMP_RECORDINGS_DIR", folder), \
                 patch("screen_recorder.mixins.recording_session.messagebox.showerror") as showerror:
                ScreenRecorderProWin11.start_recording(app)
            self.assertTrue(app.running)
            self.assertFalse(app.is_recording or app.is_starting)
            app.root.after_idle.assert_not_called()
            app.start_audio_meters.assert_called_once()
            showerror.assert_called_once()

    def test_countdown_pause_does_not_skip_stop(self):
        with tempfile.TemporaryDirectory(prefix="recorder_countdown_") as folder:
            folder = Path(folder)
            app = MagicMock()
            app.is_recording = app.is_finalizing = app.is_starting = False
            app.is_paused = app.is_pause_transitioning = False
            app.current_session_log_dir = app.recording_session_id = None
            app.recording_stderr_threads = []
            app.recording_capture_recovery_attempts = 0
            app.process = app.segment_started_at = None
            app.finish_pending_auto_stop.return_value = False
            app.normalize_saved_audio_choice.return_value = NO_AUDIO
            app.get_recording_temp_root.return_value = folder
            app.make_output_path_at_save_time.return_value = folder / "saved.mp4"
            app.root.after.side_effect = lambda delay, callback: callback()
            for name in ("toggle_pause", "pause_recording", "_pause_recording_worker",
                         "_finish_pause_recording", "stop_recording"):
                setattr(app, name, getattr(ScreenRecorderProWin11, name).__get__(app))

            def countdown():
                self.assertTrue(app.is_starting and app.is_recording)
                self.assertIsNone(app.process)
                app.toggle_pause()

            app.run_start_countdown.side_effect = countdown
            process = SimpleNamespace(pid=123, poll=lambda: None)
            app.start_new_segment.side_effect = lambda: setattr(app, "process", process)
            observed = []
            app._stop_recording_worker.side_effect = lambda paused: observed.append((paused, app.process))

            def immediate_thread(target, args=(), **kwargs):
                return SimpleNamespace(start=lambda: target(*args))

            with patch("screen_recorder.mixins.recording_control.threading.Thread", side_effect=immediate_thread), \
                 patch("screen_recorder.mixins.recording_session.LOGS_DIR", folder), \
                 patch("screen_recorder.mixins.recording_session.TEMP_RECORDINGS_DIR", folder):
                ScreenRecorderProWin11.start_recording(app)
                self.assertFalse(app.is_starting)
                self.assertFalse(app.is_paused, "countdown must not enter paused state")
                app.stop_current_segment.assert_not_called()
                app.stop_recording()
            self.assertEqual(observed, [(False, process)])

    def test_pause_and_resume_reject_starting_or_finalizing(self):
        for state in ("is_starting", "is_finalizing"):
            for method, paused in (("pause_recording", False), ("resume_recording", True)):
                with self.subTest(state=state, method=method):
                    app = MagicMock()
                    app.is_recording = True
                    app.is_paused = paused
                    app.is_starting = app.is_finalizing = app.is_pause_transitioning = False
                    setattr(app, state, True)
                    with patch("screen_recorder.mixins.recording_control.threading.Thread") as thread:
                        getattr(ScreenRecorderProWin11, method)(app)
                    thread.assert_not_called()
                    app.start_new_segment.assert_not_called()
                    self.assertEqual(app.is_paused, paused)
                    self.assertFalse(app.is_pause_transitioning)

    def test_toolbar_disables_pause_until_start_completes(self):
        overlay = MagicMock()
        overlay.app.is_recording = True
        overlay.app.is_finalizing = overlay.app.is_pause_transitioning = False
        overlay.app.last_output_path = None
        for starting, paused in ((True, False), (False, False), (False, True)):
            with self.subTest(starting=starting, paused=paused):
                overlay.app.is_starting = starting
                overlay.app.is_paused = paused
                AnnotationOverlay.update_record_controls(overlay)
                self.assertEqual(overlay.toolbar_pause_button.configure.call_args.kwargs["state"],
                                 "disabled" if starting else "normal")
                overlay.root.after.reset_mock()
                AnnotationOverlay.toggle_recording_pause_from_toolbar(overlay)
                self.assertEqual(overlay.root.after.called, not starting)

    def test_normal_pause_and_resume_still_work(self):
        app = MagicMock()
        app.is_recording = True
        app.is_starting = app.is_finalizing = app.is_pause_transitioning = app.is_paused = False
        app.segment_started_at = None
        app.finish_pending_auto_stop.return_value = False
        app.root.after.side_effect = lambda delay, callback: callback()
        app._finish_pause_recording = ScreenRecorderProWin11._finish_pause_recording.__get__(app)
        app._pause_recording_worker = ScreenRecorderProWin11._pause_recording_worker.__get__(app)
        with patch("screen_recorder.mixins.recording_control.threading.Thread",
                   side_effect=lambda target, **kwargs: SimpleNamespace(start=target)):
            ScreenRecorderProWin11.pause_recording(app)
        app.stop_current_segment.assert_called_once()
        self.assertTrue(app.is_paused)
        ScreenRecorderProWin11.resume_recording(app)
        app.start_new_segment.assert_called_once()
        self.assertFalse(app.is_paused)
        self.assertFalse(app.is_pause_transitioning)


class RecoveryLimit(unittest.TestCase):
    def setUp(self):
        app = object.__new__(ScreenRecorderProWin11)
        self.app = app
        app.running = app.is_recording = True
        app.is_paused = app.is_finalizing = app.is_pause_transitioning = False
        app.process_lock = threading.Lock()
        app.recording_progress_lock = threading.Lock()
        app.process = SimpleNamespace(pid=100, poll=lambda: None)
        app.current_capture_access_lost = None
        app._consume_current_capture_signal = lambda process: None
        app.recording_capture_backend = "ddagrab"
        app.recording_session_id = "test"
        app.recording_process_generation = 1
        app.recording_capture_recovery_attempts = 0
        app.recording_effective_fps = 30
        app.current_python_loopback_recorder = None
        app.status_var = SimpleNamespace(set=lambda value: None)
        app.schedule_recording_watchdog = lambda: None
        app.append_problem_error = lambda *a, **kw: None
        app.diagnostic_log = lambda *a, **kw: None
        self.restarts = []
        self.stops = []
        app.request_automatic_segment_restart = lambda kind, details: self.restarts.append(details["recovery_attempt"])
        app.stop_recording = lambda: self.stops.append(True)

    def tick(self, now):
        with patch("screen_recorder.mixins.processes.time.perf_counter", return_value=now):
            self.app._recording_watchdog_tick()

    def test_repeated_single_frame_stalls_exhaust_limit(self):
        app = self.app
        for cycle in range(4):
            app.segment_index = cycle + 1
            app.segments = [f"segment_{cycle + 1}"]
            app.segment_started_at = 100.0 + cycle * 10
            app.current_segment_last_video_frame_value = 1
            app.current_segment_last_video_frame_advance_perf = app.segment_started_at + 0.1
            app.current_segment_last_video_frame_out_time_seconds = 0.01
            self.tick(app.segment_started_at + 3.1)
            self.tick(app.segment_started_at + 6.2)
        self.assertEqual(self.restarts, [1, 2, 3])
        self.assertEqual(self.stops, [True])

    def test_sparse_video_with_advancing_audio_does_not_reset(self):
        app = self.app
        app.segment_index = 1
        app.segments = ["segment_1"]
        app.segment_started_at = 100.0
        app.recording_capture_recovery_attempts = 2
        app.current_segment_last_video_frame_value = 2
        app.current_segment_last_video_frame_advance_perf = 103.1
        app.current_segment_last_video_frame_out_time_seconds = 3.0
        self.tick(103.1)
        self.assertEqual(app.recording_capture_recovery_attempts, 2)
        app.current_segment_last_video_frame_value = 95
        app.current_segment_last_video_frame_advance_perf = 103.2
        self.tick(103.2)
        self.assertEqual(app.recording_capture_recovery_attempts, 0)


if __name__ == "__main__":
    unittest.main()
