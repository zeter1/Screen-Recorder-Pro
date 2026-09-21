"""Startup rollback and save-thread regressions; no desktop or audio capture."""
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from screen_recorder.app import ScreenRecorderProWin11


class FakeProcess:
    def __init__(self):
        self.pid = 123
        self.returncode = None
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def poll(self):
        return self.returncode


class StartSaveFailures(unittest.TestCase):
    def launch_app(self, failed_stage, termination_works=True):
        app = MagicMock()
        process = FakeProcess()
        app.process = None
        app.segments = [Path('previous.mkv')]
        app.recording_progress_threads = []
        app.recording_process_generation = 0
        app.recording_progress_lock = threading.RLock()
        app.process_lock = threading.Lock()
        app.log_handle = io.StringIO()
        app.build_ffmpeg_command.return_value = ['ffmpeg', 'segment.mkv']
        app.command_to_log_text.side_effect = lambda command: ' '.join(command)
        app.start_managed_process.return_value = process
        app.recording_start_requested_perf = None
        app._active_ffmpeg_progress_token = object()
        getattr(app, failed_stage).side_effect = RuntimeError('cannot start new thread')
        def terminate(*args, **kwargs):
            if termination_works:
                process.returncode = -1
        app.terminate_process_tree.side_effect = terminate
        return app, process

    def test_startup_failure_stops_process_before_fallback(self):
        for stage in ('start_ffmpeg_stderr_reader', 'start_ffmpeg_progress_reader',
                      'start_recording_performance_sampler'):
            with self.subTest(stage=stage):
                app, process = self.launch_app(stage)
                with self.assertRaises(RuntimeError):
                    ScreenRecorderProWin11.launch_checked_ffmpeg_segment(app, Path('segment.mkv'), 'ddagrab')
                self.assertIsNotNone(process.poll(), 'failed startup must not leave a writer alive')
                self.assertIsNone(app.process)
                self.assertEqual(app.segments, [Path('previous.mkv')])
                self.assertIsNone(app._active_ffmpeg_progress_token)
                self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))

    def test_failed_termination_blocks_runtimeerror_fallback_and_keeps_ownership(self):
        app, process = self.launch_app('start_ffmpeg_progress_reader', termination_works=False)
        with self.assertRaises(OSError):
            ScreenRecorderProWin11.launch_checked_ffmpeg_segment(app, Path('segment.mkv'), 'ddagrab')
        self.assertIsNone(process.poll())
        self.assertIs(app.process, process)
        app.register_child_process.assert_called_with(process)
        self.assertEqual(app.segments, [Path('previous.mkv')])
        with self.assertRaises(OSError):
            ScreenRecorderProWin11.launch_checked_ffmpeg_segment(app, Path('segment.mkv'), 'gdigrab')
        self.assertEqual(app.start_managed_process.call_count, 1)

    def test_stop_snapshots_encoder_before_starting_worker(self):
        app = MagicMock()
        app.is_starting = app.is_finalizing = app.is_pause_transitioning = False
        app.is_recording = True
        app.is_paused = False
        app.should_use_hevc.return_value = True
        observed = []
        def make_thread(**kwargs):
            return SimpleNamespace(start=lambda: observed.append(app.recording_save_hevc))
        with patch('screen_recorder.mixins.recording_control.threading.Thread', side_effect=make_thread):
            ScreenRecorderProWin11.stop_recording(app)
        app.should_use_hevc.assert_called_once_with()
        self.assertEqual(observed, [True])

    def test_save_commands_never_read_tk_in_worker(self):
        for count in (1, 2):
            for hevc in (False, True):
                with self.subTest(count=count, hevc=hevc), tempfile.TemporaryDirectory() as folder:
                    app = object.__new__(ScreenRecorderProWin11)
                    app.temp_dir = Path(folder)
                    app.output_path = app.temp_dir / 'output.mp4'
                    app.segments = [app.temp_dir / f'segment_{i}.mkv' for i in range(count)]
                    app.ffmpeg_path = 'ffmpeg'
                    app.recording_save_hevc = hevc
                    main_thread = threading.get_ident()
                    def tk_read():
                        if threading.get_ident() != main_thread:
                            raise RuntimeError('Tk accessed outside main thread')
                        return 'CPU x265 (HEVC)' if hevc else 'CPU x264'
                    app.encoder_var = SimpleNamespace(get=tk_read)
                    app.validate_media_file = lambda *a, **k: None
                    app.prepare_segments_with_capture_recovery = lambda paths: paths
                    app.prepare_segments_with_python_loopback_audio = lambda paths: paths
                    app.prepare_segments_with_aligned_audio = lambda paths: paths
                    app.probe_av_stream_timing = lambda path: {'audio_stream_index': None}
                    commands, errors = [], []
                    app.run_merge_command = commands.append
                    def worker():
                        try:
                            app.merge_segments()
                        except Exception as exc:
                            errors.append(exc)
                    thread = threading.Thread(target=worker)
                    thread.start()
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(len(commands), 1)
                    self.assertEqual('hvc1' in commands[0], hevc)


if __name__ == '__main__':
    unittest.main()
