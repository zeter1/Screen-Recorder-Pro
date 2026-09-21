"""Progress ownership and fail-closed publication; no desktop/audio devices."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from screen_recorder.app import ScreenRecorderProWin11
from verify_capture_recovery import harness
import verify_recording_publication


class DeferredThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        pass

    def is_alive(self):
        return False


class RecordingBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recording_boundaries_")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.app = harness(self.folder, "ffmpeg")

    def reader(self, frame, seconds, segment):
        process = SimpleNamespace(
            stdout=io.BytesIO(f"frame={frame}\nout_time_us={seconds * 1000000}\nprogress=end\n".encode()),
            pid=segment, poll=lambda: 0,
        )
        self.app.start_ffmpeg_progress_reader(process, self.folder / f"segment_{segment:04d}.mp4", "ddagrab", 0.0)
        return self.app.recording_progress_threads[-1]

    def test_late_reader_cannot_corrupt_new_segment_or_session(self):
        for new_session in (False, True):
            with self.subTest(new_session=new_session), patch(
                "screen_recorder.mixins.smoothness_diagnostics.threading.Thread", DeferredThread
            ):
                app = harness(self.folder, "ffmpeg")
                self.app = app
                writes = []
                app.session_ffmpeg_progress_path = self.folder / "old.jsonl"
                app._append_specialized_jsonl = lambda path, item: writes.append((path, item))
                old = self.reader(18000, 300, 1)
                app.segment_index = 2
                app.recording_process_generation += 1
                if new_session:
                    app.recording_session_id = "new-session"
                    app.recording_process_generation = 1
                    app.session_ffmpeg_progress_path = self.folder / "new.jsonl"
                new = self.reader(60, 1, 2)
                new.target()
                before = (app.current_segment_media_seconds, app.current_segment_last_video_frame_value,
                          app.segment_capture_started_perf, dict(app.recording_progress_latest))
                old.target()
                after = (app.current_segment_media_seconds, app.current_segment_last_video_frame_value,
                         app.segment_capture_started_perf, dict(app.recording_progress_latest))
                self.assertEqual(after, before)
                self.assertFalse(any(path and path.name == "new.jsonl" and item.get("ffmpeg_pid") == 1
                                     for path, item in writes))

    def test_committed_segment_ignores_delayed_final_progress(self):
        with patch("screen_recorder.mixins.smoothness_diagnostics.threading.Thread", DeferredThread):
            app = self.app
            old = self.reader(18000, 300, 1)
            app.segments = [self.folder / "segment_0001.mp4"]
            app.current_segment_media_seconds = 1.0
            app.commit_current_segment_duration(segment_perf_end=1.0)
            old.target()
            self.assertEqual(app.recorded_seconds, 1.0)
            self.assertEqual(app.current_segment_media_seconds, 0.0)
            self.assertIsNone(app.segment_capture_started_perf)

    def test_current_reader_finishes_and_releases_stdout(self):
        with patch("screen_recorder.mixins.smoothness_diagnostics.threading.Thread", DeferredThread):
            self.reader(60, 2, 1).target()
            self.assertEqual(self.app.current_segment_media_seconds, 2.0)
            self.assertEqual(self.app.current_segment_last_video_frame_value, 60)
            self.assertEqual(self.app.recording_progress_latest["progress"], "end")

    def test_missing_or_malformed_timing_never_publishes(self):
        for summary in (None, {}, {"timing_health": "broken"}):
            with self.subTest(summary=summary):
                fixture = verify_recording_publication.RecordingPublication()
                fixture.setUp()
                try:
                    app = fixture.app
                    app.validate_final_timing_summary = ScreenRecorderProWin11.validate_final_timing_summary.__get__(app)
                    app.log_video_timing_summary = lambda *args, **kwargs: summary
                    destination = app.output_path
                    destination.write_bytes(b"previous recording")
                    app._stop_recording_worker(False)
                    self.assertFalse(app.finished[0], app.finished)
                    self.assertEqual(destination.read_bytes(), b"previous recording")
                    self.assertTrue(app.segments[0].exists())
                    self.assertEqual(app.incomplete_output_path.read_bytes(), b"new video")
                finally:
                    fixture.doCleanups()

    def test_probe_failures_cannot_produce_healthy_timing(self):
        app = self.app
        source = self.folder / "source.mp4"
        source.write_bytes(b"synthetic probe input")
        app.scan_recording_log_for_timing_warnings = lambda: {}
        app.summarize_capture_clock_alignment = lambda: {}
        metadata = json.dumps({"streams": [{"codec_type": "video", "r_frame_rate": "30/1",
            "avg_frame_rate": "30/1", "duration": "1", "nb_frames": "30"}], "format": {"duration": "1"}})
        for failed_stage in ("streams", "packets", "empty_packets", "bad_json", "no_video"):
            with self.subTest(stage=failed_stage):
                def run(command, **kwargs):
                    is_stream = "json" in command
                    failed = (is_stream and failed_stage == "streams") or (not is_stream and failed_stage == "packets")
                    return SimpleNamespace(returncode=1 if failed else 0, stderr="probe failed" if failed else "",
                        stdout=(("{" if failed_stage == "bad_json" else "{}" if failed_stage == "no_video" else metadata)
                                if is_stream else ("" if failed_stage == "empty_packets" else "0,0,0.033333\n")))
                app.run_managed_process = run
                self.assertIsNone(app.log_video_timing_summary(source))


if __name__ == "__main__":
    unittest.main()
