"""Regression checks; optional local FFmpeg integration, no screen/device capture.

python verify_capture_recovery.py
python verify_capture_recovery.py --ffmpeg C:/ffmpeg/bin/ffmpeg.exe [--nvenc]
"""
from __future__ import annotations

import argparse
import hashlib
import math
import queue
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from screen_recorder.app import ScreenRecorderProWin11


def harness(root, ffmpeg):
    app = object.__new__(ScreenRecorderProWin11)
    app.ffmpeg_path = str(ffmpeg)
    app.get_ffprobe_path = lambda: str(Path(ffmpeg).with_name("ffprobe.exe" if Path(ffmpeg).suffix else "ffprobe"))
    app.events = []
    app.diagnostic_log = lambda event, data=None, **kw: app.events.append((event, data))
    app.problem_log_event = app.diagnostic_log
    app.append_ffmpeg_problem_log = lambda *args, **kwargs: None
    app.append_problem_error = lambda *args, **kwargs: None
    app.log_message = lambda *args, **kwargs: None
    app.log_exception = lambda where, exc: app.events.append((where, str(exc)))
    app._process_seq = 0
    app._process_meta = {}
    app.child_processes = set()
    app.child_processes_lock = threading.Lock()
    app.process_lock = threading.Lock()
    app.recording_progress_lock = threading.RLock()
    app.recording_progress_threads = []
    app.recording_progress_samples = []
    app.recording_progress_latest = {}
    app.session_ffmpeg_progress_path = None
    app._append_specialized_jsonl = lambda *args, **kwargs: None
    app.recording_stderr_threads = []
    app.recording_capture_signal_queue = queue.Queue(maxsize=32)
    app.capture_recovery_segments = {}
    app.recording_segment_start_perfs = {}
    app.python_loopback_audio_segments = {}
    app.python_loopback_sync_metadata = {}
    app.write_python_loopback_audio_sync_report = lambda: None
    app.recording_audio_bitrate = "192k"
    app.recording_failure_reason = None
    app.recording_session_id = "synthetic-recovery-test"
    app.segment_index = 1
    app.recording_process_generation = 1
    app.recording_effective_fps = 30
    app.recording_requested_fps = 30
    app.recording_refresh_hz = 60
    app.recording_capture_backend = "ddagrab"
    app.recording_start_requested_perf = time.perf_counter()
    app.recording_first_frame_perf = None
    app.recording_last_frame_perf = None
    app.segment_capture_started_perf = None
    app.segment_started_at = None
    app.current_segment_media_seconds = 0.0
    app.current_segment_last_video_frame_value = None
    app.current_segment_last_video_frame_advance_perf = None
    app.current_segment_last_video_frame_out_time_seconds = None
    app.current_segment_video_stall_detected = False
    app.current_segment_engine = "ffmpeg"
    app.recorded_seconds = 0.0
    app.recorded_wall_seconds = 0.0
    app.log_handle = None
    app.current_log_path = root / "recording.log"
    app.get_current_recording_log_path = lambda: app.current_log_path
    app.stop_python_loopback_for_current_segment = lambda: None
    app.should_use_hevc = lambda: False
    app.temp_dir = root
    return app


def checked(app, args, **kwargs):
    result = app.run_managed_process(
        [app.ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True, timeout=45, creationflags=app.creation_flags(), **kwargs,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def static_checks():
    plan = ScreenRecorderProWin11.build_capture_recovery_plan(1.0, 2.4, 30)
    assert plan["padding_frames"] == 42
    assert ScreenRecorderProWin11.build_capture_recovery_plan(2, 1, 30)["padding_frames"] == 0
    for values in ((0, 2, 30), (1, float("nan"), 30), (1, 2, 0)):
        try:
            ScreenRecorderProWin11.build_capture_recovery_plan(*values)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f"invalid recovery plan accepted: {values}")
    app = harness(Path(tempfile.gettempdir()), "ffmpeg")
    stable = app.build_smooth_video_filter(72, True, capture_backend="ddagrab")
    assert "tpad=" not in stable and "realtime=" not in stable
    assert stable.count("fps=72:round=near") == 1 and "hwdownload" not in stable
    for use_nvenc in (False, True):
        for hevc in (False, True):
            app.should_use_hevc = lambda: hevc
            recording = []
            app.append_encoder_options(recording, 30, "4M", "8M", use_nvenc)
            command = app.build_capture_recovery_command("original.mp4", "candidate.mp4", plan, recording)
            assert command.count("-vf") == 1
            assert command[command.index("-vf") + 1] == "tpad=stop_mode=clone:stop=42"
            assert "-shortest" not in command and "-y" not in command
            assert command[command.index("-c:v") + 1] == recording[recording.index("-c:v") + 1]
    source = Path("segment_0001.mp4")
    app.capture_recovery_segments = {str(source): {"capture_start_perf": None}}
    try:
        app.prepare_segments_with_capture_recovery([source])
    except RuntimeError:
        pass
    else:
        raise AssertionError("missing anchor must not produce a candidate")
    app.capture_recovery_segments = {}
    assert app.prepare_segments_with_capture_recovery([source]) == [source]
    print("PASS static: finite plan, codecs, invalid anchors, unaffected normal path")


def stop_check(root, ffmpeg, with_audio):
    app = harness(root, ffmpeg)
    output = root / f"stop_{with_audio}.mp4"
    video_source = "testsrc2=s=160x96:r=30" + (":d=0.8" if with_audio else "")
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "warning", "-stats_period", "0.1",
               "-progress", "pipe:1", "-re", "-f", "lavfi", "-i", video_source]
    if with_audio:
        command += ["-re", "-f", "lavfi", "-i", "sine=frequency=600:sample_rate=48000"]
    command += ["-vf", app.build_smooth_video_filter(30, True, capture_backend="ddagrab"),
                "-c:v", "libx264", "-preset", "ultrafast", "-bf", "0", "-g", "120",
                "-fps_mode", "passthrough", "-c:a", "aac"]
    app.append_segment_container_options(command, output)
    command.append(str(output))
    app.recording_ffmpeg_args = command
    app.segments = [output]
    launched = time.perf_counter()
    process = app.start_managed_process(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, creationflags=app.recording_creation_flags())
    app.process = process
    app.segment_started_at = launched
    app.start_ffmpeg_progress_reader(process, output, "ddagrab", launched)
    app.start_ffmpeg_stderr_reader(process, root / f"stderr_{with_audio}.log", output, "ddagrab", 1)
    try:
        deadline = time.perf_counter() + 6
        while (app.current_segment_last_video_frame_value or 0) < 10:
            assert process.poll() is None and time.perf_counter() < deadline, "first-frame timeout"
            time.sleep(0.03)
        if with_audio:
            # Allow finite video input to end; microphone-like input remains live.
            time.sleep(0.9)
        stopped = time.perf_counter()
        app.stop_current_segment()
        elapsed = time.perf_counter() - stopped
        assert process.returncode == 0 and elapsed < 4.0, (process.returncode, elapsed)
        app.validate_media_file(output)
        decoded = checked(app, ["-i", str(output), "-map", "0:v:0", "-f", "null", "-"])
        assert not app.child_processes
        for thread in app.recording_progress_threads:
            thread.join(timeout=1)
            assert not thread.is_alive()
        print(f"PASS stop: audio={with_audio}, seconds={elapsed:.3f}, decoded MP4, no owned child")
    finally:
        if process.poll() is None:
            app.terminate_process_tree(process, timeout=2, name="synthetic_test")


def failure_checks():
    """Fault injection at the staged-output boundary; no FFmpeg needed."""
    for failure in ("nonzero", "timeout", "duration", "wrong_frame"):
        with tempfile.TemporaryDirectory(prefix="codex_recovery_failure_") as folder:
            root = Path(folder)
            source = root / "original.mp4"
            source.write_bytes(b"original fixture must survive")
            app = harness(root, "ffmpeg")
            app.recorded_seconds = 1.0
            app.recorded_wall_seconds = 2.4
            app.capture_recovery_segments = {str(source): {
                "capture_start_perf": 100.0, "stop_requested_perf": 102.4,
                "fps": 30, "ffmpeg_args": ["-c:v", "libx264", "-max_muxing_queue_size", "4096"],
                "committed_media_seconds": 1.0, "committed_wall_seconds": 2.4,
            }}
            def run(command, **kwargs):
                candidate = Path(command[-1])
                assert candidate != source and not candidate.exists()
                candidate.write_bytes(b"partial fixture")
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                return subprocess.CompletedProcess(command, 1 if failure == "nonzero" else 0, None, "injected failure")
            app.run_managed_process = run
            app.validate_media_file = lambda *args, **kwargs: True
            app.probe_av_stream_timing = lambda path: {
                "video_duration": 1.0 if Path(path) == source else (7.0 if failure == "duration" else 2.4),
            }
            app.read_capture_recovery_tail = lambda path, *args: (b"\x00" if Path(path) == source else b"\xff") * 2304
            try:
                app.prepare_segments_with_capture_recovery([source])
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            else:
                raise AssertionError(f"invalid candidate accepted: {failure}")
            assert source.read_bytes() == b"original fixture must survive"
            assert list(root.glob("capture_recovery_*.mp4")), "failed candidate was removed"
            assert app.recorded_seconds == 1.0 and app.recorded_wall_seconds == 2.4
            assert "output" not in app.capture_recovery_segments[str(source)]
    print("PASS failure gates: nonzero, timeout, wrong duration/frame preserve source/candidate and reject publication")


def media_check(root, ffmpeg, encoder, suffix, with_audio=True):
    app = harness(root, ffmpeg)
    hevc = encoder in {"hevc_nvenc", "libx265"}
    app.should_use_hevc = lambda: hevc
    variant = encoder if with_audio else encoder + "_silent"
    source, second = root / f"source_{variant}{suffix}", root / f"second_{variant}{suffix}"
    wav = root / f"loopback_{encoder}{suffix}.wav"
    encoding = []
    app.append_encoder_options(encoding, 30, "4M", "8M", "nvenc" in encoder, capture_backend="gdigrab")
    assert encoding[encoding.index("-c:v") + 1] == encoder
    common = [*encoding, "-c:a", "aac", "-ar", "48000", "-ac", "2"] if with_audio else [*encoding, "-an"]
    app.append_segment_container_options(common, source)
    source_audio = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2.2"] if with_audio else []
    second_audio = ["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1"] if with_audio else []
    checked(app, ["-f", "lavfi", "-i", "testsrc2=s=160x96:r=30:d=1",
                  *source_audio,
                  *common, str(source)])
    checked(app, ["-f", "lavfi", "-i", "color=c=blue:s=160x96:r=30:d=1",
                  *second_audio,
                  *common, str(second)])
    if with_audio:
        checked(app, ["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=2.2",
                      "-c:a", "pcm_s16le", "-ac", "2", str(wav)])
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    app.capture_recovery_segments = {str(source): {
        "capture_start_perf": 100.0, "stop_requested_perf": 102.2,
        "resume_segment_path": str(second), "fps": 30, "ffmpeg_args": encoding,
        "committed_media_seconds": 1.0, "committed_wall_seconds": 2.2,
    }}
    app.recording_segment_start_perfs = {str(second): 102.4}
    if with_audio:
        app.python_loopback_audio_segments = {str(source): wav}
        app.python_loopback_sync_metadata = {str(source): {"sync_plan": app.build_python_loopback_sync_plan(100, 100)}}
    app.recorded_seconds = 2.0
    app.recorded_wall_seconds = 3.2
    app.segments = [source, second]
    app.output_path = root / f"joined_{variant}{suffix}"
    app.merge_segments()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert abs(app.recorded_seconds - 3.4) < 0.06
    timing = app.probe_av_stream_timing(app.output_path)
    assert abs(timing["video_duration"] - 3.4) < 0.12, timing
    if with_audio:
        assert abs(timing["video_end"] - timing["audio_end"]) < 0.12, timing
    else:
        assert timing["audio_stream_index"] is None
    raw = checked(app, ["-i", str(app.output_path), "-map", "0:v:0", "-vf", "scale=64:36,format=gray",
                       "-fps_mode", "passthrough", "-f", "rawvideo", "-"])
    frames = [raw[i:i + 2304] for i in range(0, len(raw), 2304)]
    assert 100 <= len(frames) <= 105, len(frames)
    assert len({hashlib.sha256(f).digest() for f in frames[:25]}) > 15, "source movement lost"
    def error(a, b):
        return sum(abs(x - y) for x, y in zip(a, b)) / len(a)
    assert max(error(frames[28], frame) for frame in frames[35:68]) < 6, "gap is not last frame"
    assert error(frames[28], frames[-1]) > 10, "resumed scene missing"
    for at in ((1.4, 2.8) if with_audio else ()):
        pcm = checked(app, ["-ss", str(at), "-i", str(app.output_path), "-t", "0.25",
                           "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"])
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        rms = math.sqrt(sum(x * x for x in samples) / max(1, len(samples)))
        assert rms > 300, (at, rms)
    print(f"PASS media: {encoder}/{suffix}, audio={with_audio}, movement -> frozen interval -> new scene; originals unchanged")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--nvenc", action="store_true")
    args = parser.parse_args()
    static_checks()
    failure_checks()
    if args.ffmpeg:
        root = Path(tempfile.mkdtemp(prefix="codex_recovery_тест "))
        print(f"TEST_ARTIFACTS: {root}")
        try:
            stop_check(root, args.ffmpeg, False)
            stop_check(root, args.ffmpeg, True)
            media_check(root, args.ffmpeg, "libx264", ".mp4")
            media_check(root, args.ffmpeg, "libx264", ".mkv")
            media_check(root, args.ffmpeg, "libx264", ".mp4", with_audio=False)
            if args.nvenc:
                media_check(root, args.ffmpeg, "h264_nvenc", ".mp4")
                media_check(root, args.ffmpeg, "hevc_nvenc", ".mp4")
        except Exception:
            print(f"FAILED_TEST_ARTIFACTS_PRESERVED: {root}")
            raise
        else:
            assert root.resolve().parent == Path(tempfile.gettempdir()).resolve()
            assert root.name.startswith("codex_recovery_")
            shutil.rmtree(root)
    else:
        print("NOT_VERIFIED: FFmpeg integration (pass --ffmpeg to run)")


if __name__ == "__main__":
    main()
