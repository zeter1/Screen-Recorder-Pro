from __future__ import annotations

import ast
import compileall
import inspect
import json
import queue
import sys
import tempfile
import threading
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = ROOT / "screen_recorder"


def find_invalid_staticmethods() -> list[str]:
    """Ищет @staticmethod, которые ошибочно обращаются к self/cls."""
    problems: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:
            problems.append(f"{path.relative_to(ROOT)}: не удалось разобрать AST: {exc}")
            continue

        for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
            for function_node in (
                node
                for node in class_node.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                decorators = {
                    decorator.id
                    for decorator in function_node.decorator_list
                    if isinstance(decorator, ast.Name)
                }
                if "staticmethod" not in decorators:
                    continue
                referenced_names = {
                    node.id for node in ast.walk(function_node) if isinstance(node, ast.Name)
                }
                invalid = sorted(referenced_names.intersection({"self", "cls"}))
                if invalid:
                    problems.append(
                        f"{path.relative_to(ROOT)}:{function_node.lineno} "
                        f"{class_node.name}.{function_node.name}: "
                        f"staticmethod обращается к {', '.join(invalid)}"
                    )
    return problems


def main() -> int:
    print("1. Компиляция всех Python-файлов...")
    if not compileall.compile_dir(str(ROOT), quiet=1):
        print("ОШИБКА: compileall обнаружил ошибку.")
        return 1

    print("2. Проверка статических методов...")
    staticmethod_problems = find_invalid_staticmethods()
    if staticmethod_problems:
        print("ОШИБКА: найдены некорректные staticmethod:")
        for problem in staticmethod_problems:
            print(" -", problem)
        return 1

    print("3. Импорт главного класса...")
    from screen_recorder.app import ScreenRecorderProWin11
    from screen_recorder.shared import (
        APP_BUILD,
        APP_DIR,
        NO_AUDIO,
        Image,
        RECORDING_CURSOR_SIZE_PERCENT_OPTIONS,
        normalize_recording_cursor_size_percent,
        normalize_screenshot_annotation_color,
        normalize_screenshot_annotation_size,
    )

    methods = {
        name for name, value in inspect.getmembers(ScreenRecorderProWin11)
        if callable(value) and not name.startswith("__")
    }
    required = {
        "start_recording", "stop_recording", "build_ffmpeg_command",
        "build_smooth_video_filter", "write_ai_smoothness_report",
        "open_settings_window", "ensure_tray_icon", "exit_app",
        "normalize_capture_region_drag", "select_capture_region",
        "get_screenshot_snapshot_crop_box", "apply_screenshot_annotations",
        "parse_native_print_screen_hotkey", "_start_native_screenshot_hotkey",
        "_stop_native_screenshot_hotkey", "_is_native_screenshot_hotkey_healthy",
        "_enqueue_screenshot_hotkey_from_backend",
        "_is_owned_problem_log_item",
        "build_python_loopback_sync_plan", "build_python_loopback_audio_filter",
        "summarize_python_loopback_audio_sync", "classify_visual_motion_window",
        "classify_recording_video_progress_health",
        "classify_ffmpeg_capture_stderr",
        "classify_input_desktop_name",
        "should_restart_after_capture_access_lost",
        "capture_signal_matches",
        "start_ffmpeg_stderr_reader",
        "wait_for_ffmpeg_stderr_reader",
        "_poll_automatic_segment_restart_result",
        "should_rollover_coreaudio_segment",
        "calculate_coreaudio_segment_rollover_seconds",
        "is_segment_video_capture_truncated",
        "calculate_python_loopback_mix_timeout",
        "is_effective_fps_validation_applicable",
        "should_draw_native_recording_cursor",
        "get_recording_cursor_render_mode",
        "build_cursor_overlay_geometry",
        "get_tk_toplevel_hwnd",
        "position_cursor_overlay_window",
        "close_recording_problem_log_session",
        "summarize_resource_pressure",
        "summarize_visual_diagnostic_coverage",
    }
    missing = sorted(required - methods)
    if missing:
        print("ОШИБКА: отсутствуют методы:", ", ".join(missing))
        return 1

    print("3б. Регрессионная проверка размера курсора в записи...")
    if tuple(RECORDING_CURSOR_SIZE_PERCENT_OPTIONS) != (50, 75, 100, 125, 150, 175, 200, 250, 300):
        print("ОШИБКА: изменился поддерживаемый набор размеров курсора.")
        return 1
    for raw_value, expected in (
        (None, 100),
        ("bad", 100),
        ("150%", 150),
        (49, 50),
        (88, 100),
        (999, 300),
    ):
        actual = normalize_recording_cursor_size_percent(raw_value)
        if actual != expected:
            print("ОШИБКА: размер курсора нормализован неверно:", raw_value, actual, expected)
            return 1

    cursor_app = object.__new__(ScreenRecorderProWin11)
    cursor_app.capture_region = None
    cursor_app.recording_cursor_visible = True
    cursor_app.recording_cursor_size_percent = 100
    cursor_app.recording_custom_cursor_overlay_ready = False
    if not cursor_app.should_draw_native_recording_cursor():
        print("ОШИБКА: режим 100% перестал использовать настоящий системный курсор.")
        return 1
    native_source = cursor_app.build_ddagrab_source_expression(144, True, 0)
    if "draw_mouse=1" not in native_source or "dup_frames=1" not in native_source:
        print("ОШИБКА: системный курсор или dup_frames потерян в ddagrab:", native_source)
        return 1

    cursor_app.MP4_VIDEO_TRACK_TIMESCALE = 39600
    stable_filter = cursor_app.build_smooth_video_filter(
        72,
        use_nvenc=True,
        capture_backend="ddagrab",
    )
    if (
        "RTCTIME-RTCSTART" not in stable_filter
        or stable_filter.count("fps=72:round=near") != 1
        or "tpad=" in stable_filter
        or "realtime=" in stable_filter
        or "setpts=N*" in stable_filter
        or "hwdownload" in stable_filter
    ):
        print("ОШИБКА: настройка курсора изменила стабильный ddagrab/NVENC filter:", stable_filter)
        return 1
    encoder_command = []
    cursor_app.should_use_hevc = lambda: False
    cursor_app.append_encoder_options(
        encoder_command,
        72,
        "16M",
        "32M",
        use_nvenc=True,
        capture_backend="ddagrab",
    )
    if (
        "-fps_mode" not in encoder_command
        or encoder_command[encoder_command.index("-fps_mode") + 1] != "passthrough"
        or "hwdownload" in encoder_command
    ):
        print("ОШИБКА: потерян fps_mode passthrough или GPU-direct NVENC:", encoder_command)
        return 1
    cursor_app.recording_refresh_hz = 144
    if cursor_app.get_ddagrab_poll_fps(72) != 144:
        print("ОШИБКА: 144 Гц / 72 FPS больше не даёт ddagrab poll 144.")
        return 1
    gdigrab_filter = cursor_app.build_smooth_video_filter(
        72,
        use_nvenc=False,
        capture_backend="gdigrab",
    )
    if "tpad=" in gdigrab_filter:
        print("ОШИБКА: secure-desktop stop-frame затронул gdigrab:", gdigrab_filter)
        return 1

    cursor_app.recording_cursor_size_percent = 200
    cursor_app.recording_custom_cursor_overlay_ready = True
    if cursor_app.should_draw_native_recording_cursor():
        print("ОШИБКА: custom cursor создаёт двойной native-курсор.")
        return 1
    custom_source = cursor_app.build_ddagrab_source_expression(144, False, 0)
    if "draw_mouse=0" not in custom_source:
        print("ОШИБКА: native cursor не отключён при готовом custom overlay:", custom_source)
        return 1
    cursor_app.recording_custom_cursor_overlay_ready = False
    if not cursor_app.should_draw_native_recording_cursor():
        print("ОШИБКА: при сбое custom overlay не включается системный fallback.")
        return 1
    cursor_app.recording_cursor_visible = False
    if cursor_app.should_draw_native_recording_cursor():
        print("ОШИБКА: скрытый курсор снова включил native draw_mouse.")
        return 1
    if cursor_app.build_cursor_overlay_geometry(80, 90, -25, 10) != "80x90-25+10":
        print("ОШИБКА: геометрия курсора не поддерживает отрицательные координаты монитора.")
        return 1

    command_source = inspect.getsource(ScreenRecorderProWin11.build_ffmpeg_command)
    if command_source.count("should_draw_native_recording_cursor") < 2:
        print("ОШИБКА: ddagrab и gdigrab используют разные правила cursor render mode.")
        return 1
    overlay_source = inspect.getsource(ScreenRecorderProWin11.start_cursor_highlight_overlay)
    if (
        "create_polygon" not in overlay_source
        or "recording_custom_cursor_overlay_ready" not in overlay_source
        or "if not self.make_window_clickthrough(win)" not in overlay_source
        or "position_cursor_overlay_window" not in overlay_source
    ):
        print("ОШИБКА: custom cursor не подключён к безопасному owned click-through overlay.")
        return 1
    position_source = inspect.getsource(ScreenRecorderProWin11.position_cursor_overlay_window)
    hwnd_source = inspect.getsource(ScreenRecorderProWin11.get_tk_toplevel_hwnd)
    if "SetWindowPos" not in position_source or "GetAncestor" not in hwnd_source:
        print("ОШИБКА: cursor overlay не поддерживает Tk wrapper HWND и абсолютные координаты.")
        return 1

    from screen_recorder.mixins import instant_buffer as instant_buffer_module

    class FakeCursorWindow:
        def __init__(self):
            self.geometries = []
            self.destroyed = False

        def overrideredirect(self, _value):
            return None

        def attributes(self, *_args):
            return None

        def configure(self, **_kwargs):
            return None

        def wm_attributes(self, *_args):
            return None

        def geometry(self, value):
            self.geometries.append(value)

        def destroy(self):
            self.destroyed = True

    class FakeCursorCanvas:
        def __init__(self):
            self.polygons = []
            self.ovals = []

        def pack(self, **_kwargs):
            return None

        def create_polygon(self, *args, **kwargs):
            self.polygons.append((args, kwargs))

        def create_oval(self, *args, **kwargs):
            self.ovals.append((args, kwargs))

    class FakeCursorRoot:
        def __init__(self):
            self.cancelled = []

        def after(self, _delay_ms, _callback):
            return "cursor-job"

        def after_cancel(self, job):
            self.cancelled.append(job)

    fake_window = FakeCursorWindow()
    fake_canvas = FakeCursorCanvas()
    original_toplevel = instant_buffer_module.tk.Toplevel
    original_canvas = instant_buffer_module.tk.Canvas
    instant_buffer_module.tk.Toplevel = lambda _root: fake_window
    instant_buffer_module.tk.Canvas = lambda *_args, **_kwargs: fake_canvas
    try:
        cursor_app.root = FakeCursorRoot()
        cursor_app.is_recording = True
        cursor_app.is_finalizing = False
        cursor_app.recording_cursor_visible = True
        cursor_app.recording_cursor_size_percent = 200
        cursor_app.recording_cursor_highlight = False
        cursor_app.recording_cursor_highlight_size = 70
        cursor_app.recording_custom_cursor_overlay_ready = False
        cursor_app._cursor_highlight_window = None
        cursor_app._cursor_highlight_canvas = None
        cursor_app._cursor_highlight_job = None
        cursor_app.get_cursor_position = lambda: (-10, 20)
        cursor_app.make_window_clickthrough = lambda _window: True
        cursor_app.position_cursor_overlay_window = lambda window, width, height, x, y: (
            window.geometry(cursor_app.build_cursor_overlay_geometry(width, height, x, y)) is None
        )
        cursor_app.log_exception = lambda *_args, **_kwargs: None
        custom_ready = cursor_app.start_cursor_highlight_overlay()
        if (
            not custom_ready
            or not cursor_app.recording_custom_cursor_overlay_ready
            or len(fake_canvas.polygons) != 2
            or not fake_window.geometries
        ):
            print("ОШИБКА: custom cursor overlay не создаётся как единый owned Tk-объект.")
            return 1
        cursor_app.stop_cursor_highlight_overlay()
        if (
            cursor_app.recording_custom_cursor_overlay_ready
            or not fake_window.destroyed
            or "cursor-job" not in cursor_app.root.cancelled
        ):
            print("ОШИБКА: custom cursor overlay не очищает окно/job при остановке.")
            return 1
    finally:
        instant_buffer_module.tk.Toplevel = original_toplevel
        instant_buffer_module.tk.Canvas = original_canvas

    recording_start_source = inspect.getsource(ScreenRecorderProWin11.start_recording)
    if recording_start_source.find("start_cursor_highlight_overlay") > recording_start_source.find("start_new_segment"):
        print("ОШИБКА: custom cursor запускается после первого кадра FFmpeg.")
        return 1
    settings_source = inspect.getsource(ScreenRecorderProWin11.save_settings)
    if '"cursor_size_percent"' not in settings_source:
        print("ОШИБКА: размер курсора не сохраняется в settings.json.")
        return 1

    print("4. Регрессионная проверка выбора системного звука...")
    invalid_only = ScreenRecorderProWin11.find_default_system_audio_device(
        ["Microphone", "Analogue 1 + 2 (Focusrite)"]
    )
    if invalid_only != NO_AUDIO:
        print("ОШИБКА: микрофон ошибочно признан источником системного звука:", invalid_only)
        return 1

    valid = ScreenRecorderProWin11.find_default_system_audio_device(
        ["Microphone", "Stereo Mix (Realtek Audio)"]
    )
    if valid != "Stereo Mix (Realtek Audio)":
        print("ОШИБКА: Stereo Mix не был найден:", valid)
        return 1

    print("4б. Регрессионная проверка восстановления CoreAudio loopback...")
    from screen_recorder.components.audio_loopback import (
        WasapiLoopbackWaveRecorder,
        _CoreAudioHRESULTError,
    )

    if not WasapiLoopbackWaveRecorder.is_retryable_hresult(0x88890004):
        print("ОШИБКА: AUDCLNT_E_DEVICE_INVALIDATED не распознан как восстанавливаемая ошибка.")
        return 1
    if WasapiLoopbackWaveRecorder.is_retryable_hresult(0x80004005):
        print("ОШИБКА: произвольный HRESULT ошибочно признан восстанавливаемым.")
        return 1
    if not ScreenRecorderProWin11.is_python_loopback_duration_incomplete(30.0, 12.0):
        print("ОШИБКА: сильно укороченный CoreAudio WAV не распознан.")
        return 1
    if ScreenRecorderProWin11.is_python_loopback_duration_incomplete(30.0, 29.5):
        print("ОШИБКА: допустимая разница длительности CoreAudio ошибочно признана обрывом.")
        return 1
    stalled_health = ScreenRecorderProWin11.classify_recording_video_progress_health(
        "ddagrab", 100.0, 0.0, 90.0, 194047, stall_seconds=6.0
    )
    if stalled_health.get("status") != "frame_stalled":
        print("ERROR: frozen ddagrab frame counter was not detected:", stalled_health)
        return 1
    healthy_progress = ScreenRecorderProWin11.classify_recording_video_progress_health(
        "ddagrab", 100.0, 0.0, 99.0, 194048, stall_seconds=6.0
    )
    if healthy_progress.get("status") != "healthy":
        print("ERROR: advancing ddagrab frame counter was classified as stalled:", healthy_progress)
        return 1
    gdigrab_progress = ScreenRecorderProWin11.classify_recording_video_progress_health(
        "gdigrab", 100.0, 0.0, 10.0, 1
    )
    if gdigrab_progress.get("status") != "not_applicable":
        print("ERROR: ddagrab-only watchdog affected another backend:", gdigrab_progress)
        return 1
    if ScreenRecorderProWin11.classify_ffmpeg_capture_stderr(
        "AcquireNextFrame failed: 887a0026",
        "ddagrab",
    ) != "dxgi_access_lost":
        print("ERROR: DXGI access-lost marker was not recognized.")
        return 1
    if ScreenRecorderProWin11.classify_ffmpeg_capture_stderr(
        "AcquireNextFrame failed: 887a0026",
        "gdigrab",
    ) is not None:
        print("ERROR: ddagrab-only stderr marker affected gdigrab.")
        return 1
    if ScreenRecorderProWin11.classify_input_desktop_name("Default") != "default":
        print("ERROR: normal Windows desktop was not recognized.")
        return 1
    if ScreenRecorderProWin11.classify_input_desktop_name("Winlogon") != "non_default":
        print("ERROR: secure Windows desktop was not separated from Default.")
        return 1
    if ScreenRecorderProWin11.should_restart_after_capture_access_lost("unavailable", 30.0):
        print("ERROR: recovery would restart while the secure desktop is still active.")
        return 1
    if not ScreenRecorderProWin11.should_restart_after_capture_access_lost("default", 0.0):
        print("ERROR: recovery did not resume on the Default desktop.")
        return 1
    current_signal = {
        "recording_session_id": "verify-session",
        "segment_index": 2,
        "process_generation": 4,
        "ffmpeg_pid": 4242,
    }
    if not ScreenRecorderProWin11.capture_signal_matches(
        current_signal,
        "verify-session",
        2,
        4,
        4242,
    ):
        print("ERROR: current capture signal was rejected.")
        return 1
    if ScreenRecorderProWin11.capture_signal_matches(
        current_signal,
        "verify-session",
        2,
        5,
        4242,
    ):
        print("ERROR: stale capture generation was accepted.")
        return 1

    stderr_app = object.__new__(ScreenRecorderProWin11)
    stderr_app.recording_session_id = "verify-session"
    stderr_app.segment_index = 2
    stderr_app.recording_capture_signal_queue = queue.Queue(maxsize=4)
    stderr_app.recording_stderr_threads = []
    stderr_app.log_message = lambda _message: None
    stderr_reader_errors = []
    stderr_app.log_exception = lambda context, exc: stderr_reader_errors.append((context, repr(exc)))
    with tempfile.TemporaryDirectory(prefix="screen_recorder_stderr_verify_") as temp_name:
        temp_root = Path(temp_name)
        stderr_source = temp_root / "stderr_source.bin"
        stderr_log = temp_root / "stderr.log"
        raw_stderr = (
            b"x" * 4090
            + b"AcquireNextFrame failed: 887a0026\r\n"
        )
        stderr_source.write_bytes(raw_stderr)

        class FakeProcess:
            pid = 4242

        fake_process = FakeProcess()
        fake_process.stderr = stderr_source.open("rb")
        stderr_thread = stderr_app.start_ffmpeg_stderr_reader(
            fake_process,
            log_path=stderr_log,
            segment_path=temp_root / "segment_0002.mp4",
            capture_backend="ddagrab",
            process_generation=4,
        )
        stderr_thread.join(timeout=2.0)
        fake_process.stderr.close()
        if stderr_thread.is_alive() or stderr_reader_errors:
            print("ERROR: stderr reader did not finish cleanly:", stderr_reader_errors)
            return 1
        queued_signal = stderr_app.recording_capture_signal_queue.get_nowait()
        if not ScreenRecorderProWin11.capture_signal_matches(
            queued_signal,
            "verify-session",
            2,
            4,
            4242,
        ):
            print("ERROR: stderr reader queued a stale or incomplete signal:", queued_signal)
            return 1
        if stderr_log.read_bytes() != raw_stderr:
            print("ERROR: stderr reader did not preserve the complete FFmpeg log.")
            return 1
    if not ScreenRecorderProWin11.should_rollover_coreaudio_segment(0.0, 14400.0, True):
        print("ERROR: four-hour CoreAudio segment rollover was not requested.")
        return 1
    if ScreenRecorderProWin11.should_rollover_coreaudio_segment(0.0, 14399.0, True):
        print("ERROR: CoreAudio segment rollover was requested too early.")
        return 1
    rollover_48k = ScreenRecorderProWin11.calculate_coreaudio_segment_rollover_seconds(48000)
    rollover_96k = ScreenRecorderProWin11.calculate_coreaudio_segment_rollover_seconds(96000)
    if rollover_48k != 14400.0:
        print("ERROR: 48 kHz CoreAudio rollover changed unexpectedly:", rollover_48k)
        return 1
    if not (60.0 < rollover_96k < rollover_48k):
        print("ERROR: high sample rate did not shorten safe WAV rollover:", rollover_96k)
        return 1
    if not ScreenRecorderProWin11.is_segment_video_capture_truncated(2695.0, 26489.0):
        print("ERROR: audio continuing after video capture was not detected.")
        return 1
    if ScreenRecorderProWin11.is_segment_video_capture_truncated(30.0, 30.5):
        print("ERROR: normal A/V duration tolerance was classified as truncation.")
        return 1
    if ScreenRecorderProWin11.calculate_python_loopback_mix_timeout(14400.0, 14400.0) <= 180:
        print("ERROR: long CoreAudio mix still uses the short fixed timeout.")
        return 1
    if ScreenRecorderProWin11.is_effective_fps_validation_applicable(0.521, 36, 72):
        print("ERROR: sub-second clip still receives fatal effective-FPS validation.")
        return 1
    if not ScreenRecorderProWin11.is_effective_fps_validation_applicable(10.0, 720, 72):
        print("ERROR: normal recording skipped effective-FPS validation.")
        return 1
    progress_reader_source = inspect.getsource(ScreenRecorderProWin11.start_ffmpeg_progress_reader)
    watchdog_source = inspect.getsource(ScreenRecorderProWin11._recording_watchdog_tick)
    restart_worker_source = inspect.getsource(ScreenRecorderProWin11._automatic_segment_restart_worker)
    restart_poll_source = inspect.getsource(ScreenRecorderProWin11._poll_automatic_segment_restart_result)
    restart_finish_source = inspect.getsource(ScreenRecorderProWin11._finish_automatic_segment_restart)
    launch_segment_source = inspect.getsource(ScreenRecorderProWin11.launch_checked_ffmpeg_segment)
    if "current_segment_last_video_frame_advance_perf" not in progress_reader_source:
        print("ERROR: progress reader no longer records advancing video-frame time.")
        return 1
    if "request_automatic_segment_restart" not in watchdog_source:
        print("ERROR: video watchdog is disconnected from segment recovery.")
        return 1
    if "stop_current_segment" not in restart_worker_source or "start_new_segment" not in restart_finish_source:
        print("ERROR: automatic recovery does not use the safe segment lifecycle.")
        return 1
    if "root.after" in restart_worker_source or "put_nowait" not in restart_worker_source:
        print("ERROR: recovery worker still touches Tkinter or no longer returns through a queue.")
        return 1
    if "_finish_automatic_segment_restart" not in restart_poll_source:
        print("ERROR: main-thread recovery poll is disconnected from restart completion.")
        return 1
    if "stderr=subprocess.PIPE" not in launch_segment_source or "start_ffmpeg_stderr_reader" not in launch_segment_source:
        print("ERROR: recording stderr is not drained by the dedicated reader.")
        return 1

    early_plan = ScreenRecorderProWin11.build_python_loopback_sync_plan(100.0, 101.25)
    if early_plan.get("correction_action") != "trim_early_loopback_audio" or abs(
        float(early_plan.get("trim_loopback_start_seconds") or 0.0) - 1.25
    ) > 0.000001:
        print("ОШИБКА: ранний CoreAudio WAV не получил точный trim-план:", early_plan)
        return 1
    early_filter = ScreenRecorderProWin11.build_python_loopback_audio_filter(early_plan)
    if "atrim=start=1.250000" not in early_filter or "asetpts=PTS-STARTPTS" not in early_filter:
        print("ОШИБКА: trim-план не попал в FFmpeg audio-filter:", early_filter)
        return 1

    late_plan = ScreenRecorderProWin11.build_python_loopback_sync_plan(102.0, 100.0)
    late_filter = ScreenRecorderProWin11.build_python_loopback_audio_filter(late_plan)
    if late_plan.get("correction_action") != "delay_late_loopback_audio" or "adelay=2000|2000" not in late_filter:
        print("ОШИБКА: поздний CoreAudio WAV не получил точный delay-план:", late_plan, late_filter)
        return 1

    aligned_plan = ScreenRecorderProWin11.build_python_loopback_sync_plan(100.0, 100.01)
    if aligned_plan.get("correction_action") != "none_already_aligned":
        print("ОШИБКА: микросдвиг CoreAudio ошибочно требует коррекции:", aligned_plan)
        return 1

    bursty_motion = ScreenRecorderProWin11.classify_visual_motion_window(
        [0, 1, 2, 32, 33, 34, 65, 66],
        72,
        72,
    )
    if bursty_motion.get("motion_pattern_classification") != "bursty_or_interrupted_motion":
        print("ОШИБКА: прокрутка порциями не отделена от непрерывного движения:", bursty_motion)
        return 1
    continuous_motion = ScreenRecorderProWin11.classify_visual_motion_window(range(60), 72, 72)
    if continuous_motion.get("motion_pattern_classification") != "continuous_motion":
        print("ОШИБКА: непрерывное движение ошибочно классифицировано:", continuous_motion)
        return 1

    diagnostics_app = ScreenRecorderProWin11.__new__(ScreenRecorderProWin11)

    sustained_pressure = diagnostics_app.summarize_resource_pressure(
        [99.7, 99.5, 99.4, 99.0, 99.5, 99.1], threshold=95.0
    )
    if sustained_pressure.get("status") != "sustained_saturation":
        print("ОШИБКА: устойчивая CPU-перегрузка классифицирована как единичный пик:", sustained_pressure)
        return 1
    brief_pressure = diagnostics_app.summarize_resource_pressure([20.0, 99.0, 25.0], threshold=95.0)
    if brief_pressure.get("status") != "brief_or_intermittent_peak":
        print("ОШИБКА: единичный CPU-пик ошибочно признан устойчивой перегрузкой:", brief_pressure)
        return 1

    static_visual_coverage = diagnostics_app.summarize_visual_diagnostic_coverage({
        "status": "ok",
        "analyzed_frame_count": 2114,
        "moving_content_cadence_analysis": {"moving_window_count": 0},
    })
    if (
        static_visual_coverage.get("status") != "insufficient_continuous_motion"
        or static_visual_coverage.get("can_assess_visual_smoothness")
    ):
        print("ОШИБКА: статичный тест ошибочно подтверждает визуальную плавность:", static_visual_coverage)
        return 1
    moving_visual_coverage = diagnostics_app.summarize_visual_diagnostic_coverage({
        "status": "ok",
        "analyzed_frame_count": 2784,
        "moving_content_cadence_analysis": {"moving_window_count": 6},
    })
    if not moving_visual_coverage.get("can_assess_visual_smoothness"):
        print("ОШИБКА: достаточное движение не признано пригодным для анализа:", moving_visual_coverage)
        return 1

    diagnostics_app.recording_performance_lock = threading.RLock()
    diagnostics_app.recording_performance_samples = [{
        "system_cpu_measurement_warmup": True,
        "system_cpu_percent": 0.0,
        "perf_counter": 0.0,
        "disk_io_total": {"read_bytes": 0, "write_bytes": 0},
        "ffmpeg_process": {"write_bytes_total": 0},
    }]
    for index in range(1, 7):
        sample = {
            "system_cpu_measurement_warmup": False,
            "system_cpu_percent": 99.0,
            "system_cpu_per_core_percent": [100.0, 98.0],
            "perf_counter": float(index),
            "disk_io_total": {"read_bytes": index * 2000, "write_bytes": index * 1000},
            "ffmpeg_process": {"write_bytes_total": index * 500},
            "memory": {"percent": 80.0},
            "swap": {"percent": 0.0, "used_bytes": 0, "sin_bytes_total": 0, "sout_bytes_total": 0},
            "output_disk": {"free_bytes": 10_000_000},
        }
        if index == 3:
            sample["high_cpu_process_attribution"] = {
                "status": "ok",
                "top_processes": [{"pid": 123, "name": "load.exe"}],
            }
        diagnostics_app.recording_performance_samples.append(sample)
    performance_summary = diagnostics_app.summarize_performance_samples()
    if (
        (performance_summary.get("system_cpu_percent") or {}).get("min") != 99.0
        or (performance_summary.get("system_cpu_pressure") or {}).get("status") != "sustained_saturation"
        or performance_summary.get("cpu_measurement_warmup_samples_excluded") != 1
        or len(performance_summary.get("high_cpu_process_attribution_snapshots") or []) != 1
        or not performance_summary.get("system_disk_read_bytes_per_second")
    ):
        print("ОШИБКА: расширенная performance-сводка потеряла evidence:", performance_summary)
        return 1

    with tempfile.TemporaryDirectory(prefix="screen_recorder_session_log_test_") as temp_dir:
        session_app = ScreenRecorderProWin11.__new__(ScreenRecorderProWin11)
        session_app.should_write_problem_logs = lambda: True
        session_app.get_problem_log_file_limit_bytes = lambda: 1_000_000
        session_app._problem_log_file_lock = threading.RLock()
        session_app._session_events_truncated = False
        session_app.diagnostic_started_perf = 0.0
        session_app.recording_session_id = "session-a"
        session_app._session_log_owner_id = "session-a"
        session_app._session_log_accepts_live_events = True
        session_app.session_events_path = Path(temp_dir) / "events.jsonl"
        session_app.session_errors_path = Path(temp_dir) / "errors.txt"
        session_app.session_ffmpeg_path = Path(temp_dir) / "ffmpeg.txt"
        session_app.session_events_path.write_text("", encoding="utf-8")
        session_app.session_errors_path.write_text("errors header\n", encoding="utf-8")
        session_app.session_ffmpeg_path.write_text("ffmpeg header\n", encoding="utf-8")
        session_app.problem_log_event("recording_event", {"ok": True})
        if not session_app.close_recording_problem_log_session("session-a", reason="test_complete"):
            print("ОШИБКА: session-log не закрылся для своего owner id.")
            return 1
        session_app.problem_log_event("unrelated_after_close", {})
        session_app.append_problem_error("unrelated_after_close", "must stay global")
        session_app.append_ffmpeg_problem_log("unrelated_after_close", command=["ffmpeg", "-version"])
        rows = [json.loads(line) for line in session_app.session_events_path.read_text(encoding="utf-8").splitlines()]
        if len(rows) != 2 or rows[-1].get("event") != "recording_problem_log_session_closed":
            print("ОШИБКА: закрытый session-log продолжает принимать обычные события:", rows)
            return 1
        if session_app.session_errors_path.read_text(encoding="utf-8") != "errors header\n":
            print("ОШИБКА: закрытый 04-файл получил несвязанный traceback.")
            return 1
        if session_app.session_ffmpeg_path.read_text(encoding="utf-8") != "ffmpeg header\n":
            print("ОШИБКА: закрытый 02-файл получил несвязанную FFmpeg-команду.")
            return 1
        session_app._session_log_owner_id = "session-b"
        session_app._session_log_accepts_live_events = True
        if session_app.close_recording_problem_log_session("session-a", reason="stale_worker"):
            print("ОШИБКА: старый post-save worker закрыл новую session-log.")
            return 1
        if not session_app._session_log_accepts_live_events:
            print("ОШИБКА: owner guard не сохранил новую session-log активной.")
            return 1

    healthy_bursty_verdict = diagnostics_app.build_automatic_smoothness_verdict(
        {"timing_health": {"status": "ok"}},
        {
            "moving_content_cadence_analysis": {
                "low_cadence_window_count": 1,
                "strong_continuous_low_cadence_window_count": 0,
                "bursty_or_stepwise_low_cadence_window_count": 1,
            },
            "suspected_freeze_candidates": [],
            "exact_duplicate_like_percent": 70.0,
        },
        {
            "total_dup_frames_across_segments": 0,
            "total_drop_frames_across_segments": 0,
            "possible_progress_stalls_count": 0,
            "steady_speed_median_after_3s": 1.0,
        },
        {
            "memory_percent": {"max": 88.0},
            "swap_percent": {"max": 12.0},
            "swap_in_bytes_during_recording": 0,
            "swap_out_bytes_during_recording": 0,
        },
        clock_alignment={"status": "clocks_aligned", "aggregate": {}},
        candidate_evidence=[],
    )
    if healthy_bursty_verdict.get("status") != "healthy_with_observations" or healthy_bursty_verdict.get("warnings"):
        print("ОШИБКА: bursty-прокрутка/RAM без swap ошибочно понизили здоровье записи:", healthy_bursty_verdict)
        return 1

    correlated_verdict = diagnostics_app.build_automatic_smoothness_verdict(
        {"timing_health": {"status": "ok"}},
        {
            "moving_content_cadence_analysis": {
                "low_cadence_window_count": 1,
                "strong_continuous_low_cadence_window_count": 1,
                "bursty_or_stepwise_low_cadence_window_count": 0,
            },
            "suspected_freeze_candidates": [],
        },
        {
            "total_dup_frames_across_segments": 0,
            "total_drop_frames_across_segments": 0,
            "possible_progress_stalls_count": 0,
            "steady_speed_median_after_3s": 1.0,
        },
        {},
        clock_alignment={"status": "clocks_aligned", "aggregate": {}},
        candidate_evidence=[{
            "candidate": {"candidate_kind": "continuous_low_cadence"},
            "technical_correlation_signals": ["ffmpeg_speed_below_0_90"],
        }],
    )
    if correlated_verdict.get("status") != "healthy_with_warnings":
        print("ОШИБКА: технически подтверждённый low-cadence кандидат не дал предупреждение:", correlated_verdict)
        return 1

    diagnostics_app.python_loopback_sync_metadata = {
        "segment.mp4": {
            "segment_path": "segment.mp4",
            "status": "mixed",
            "alignment_applied": True,
            "sync_plan": {"correction_action": "trim_early_loopback_audio"},
            "endpoint_invalidations": 0,
            "reconnect_attempts": 0,
            "reconnect_successes": 0,
        }
    }
    audio_sync_summary = diagnostics_app.summarize_python_loopback_audio_sync()
    if audio_sync_summary.get("status") != "aligned_with_applied_correction" or audio_sync_summary.get("recovery_path_exercised"):
        print("ОШИБКА: сводка A/V-якоря CoreAudio недостоверна:", audio_sync_summary)
        return 1
    with tempfile.TemporaryDirectory(prefix="screen_recorder_audio_sync_log_test_") as temp_dir:
        diagnostics_app.session_audio_sync_path = Path(temp_dir) / "15_audio_sync.json"
        diagnostics_app.log_exception = lambda *args, **kwargs: None
        diagnostics_app.write_python_loopback_audio_sync_report()
        saved_audio_sync = json.loads(diagnostics_app.session_audio_sync_path.read_text(encoding="utf-8"))
        if saved_audio_sync.get("status") != "aligned_with_applied_correction":
            print("ОШИБКА: отдельный файл 15 не сохранил сводку A/V-якоря:", saved_audio_sync)
            return 1

    with tempfile.TemporaryDirectory(prefix="screen_recorder_loopback_test_") as temp_dir:
        output_wav = Path(temp_dir) / "loopback.wav"
        recorder = WasapiLoopbackWaveRecorder(output_wav)
        reconnect_wav = recorder._part_path(output_wav, 1)
        for wav_path, frame_count in ((output_wav, 100), (reconnect_wav, 200)):
            with wave.open(str(wav_path), "wb") as test_wav:
                test_wav.setnchannels(2)
                test_wav.setsampwidth(2)
                test_wav.setframerate(48000)
                test_wav.writeframes(b"\x00" * frame_count * 4)
        valid_wav = WasapiLoopbackWaveRecorder.inspect_wav_file(output_wav)
        if not valid_wav.get("valid"):
            print("ERROR: a normal generated WAV failed header validation:", valid_wav)
            return 1
        invalid_wav = Path(temp_dir) / "invalid_header.wav"
        with wave.open(str(invalid_wav), "wb") as test_wav:
            test_wav.setnchannels(2)
            test_wav.setsampwidth(2)
            test_wav.setframerate(48000)
            test_wav.writeframes(b"\x00" * 40)
        with invalid_wav.open("ab") as invalid_file:
            invalid_file.write(b"trailing-pcm-not-declared")
        invalid_details = WasapiLoopbackWaveRecorder.inspect_wav_file(invalid_wav)
        if invalid_details.get("valid") or invalid_details.get("reason") != "wav_header_size_mismatch":
            print("ERROR: inconsistent WAV header/body size was not rejected:", invalid_details)
            return 1
        recorder._merge_wav_parts([output_wav, reconnect_wav])
        with wave.open(str(output_wav), "rb") as merged_wav:
            if merged_wav.getnframes() != 300:
                print("ОШИБКА: reconnect WAV-фрагменты объединены с потерей кадров.")
                return 1
        if reconnect_wav.exists():
            print("ОШИБКА: временный reconnect WAV не очищен после успешного объединения.")
            return 1

        class SyntheticReconnectRecorder(WasapiLoopbackWaveRecorder):
            RECONNECT_INTERVAL_SECONDS = 0.0

            def _run_capture_session(self, output_path, capture_start_perf, session_index=0):
                frame_count = 100 if session_index == 0 else 200
                with wave.open(str(output_path), "wb") as test_wav:
                    test_wav.setnchannels(2)
                    test_wav.setsampwidth(2)
                    test_wav.setframerate(48000)
                    test_wav.writeframes(b"\x00" * frame_count * 4)
                if session_index == 0:
                    raise _CoreAudioHRESULTError("synthetic GetNextPacketSize", 0x88890004)

        recovered_wav = Path(temp_dir) / "synthetic_recovered.wav"
        recovered = SyntheticReconnectRecorder(recovered_wav)
        recovered.capture_start_perf = 1.0
        recovered._run()
        if recovered.error is not None or recovered.endpoint_invalidations != 1 or recovered.reconnect_attempts != 1:
            print(
                "ОШИБКА: синтетическое отключение CoreAudio не восстановлено:",
                recovered.error,
                recovered.endpoint_invalidations,
                recovered.reconnect_attempts,
            )
            return 1
        with wave.open(str(recovered_wav), "rb") as recovered_file:
            if recovered_file.getnframes() != 300:
                print("ОШИБКА: после восстановления CoreAudio потеряны WAV-кадры.")
                return 1

        recorder.error = RuntimeError("synthetic loopback failure")
        try:
            recorder.stop(timeout=0.01)
        except RuntimeError as exc:
            if "synthetic loopback failure" not in str(exc):
                print("ОШИБКА: stop() потерял исходную ошибку CoreAudio:", exc)
                return 1
        else:
            print("ОШИБКА: stop() замаскировал ошибку CoreAudio как успех.")
            return 1

    print("5. Регрессионная проверка выделения скриншота и безопасной очистки логов...")
    region, details = ScreenRecorderProWin11.normalize_capture_region_drag(100, 200, 180, 260)
    if region != [100, 200, 80, 60] or details.get("status") != "selected":
        print("ОШИБКА: обычное выделение скриншота нормализовано неверно:", region, details)
        return 1

    reverse_region, reverse_details = ScreenRecorderProWin11.normalize_capture_region_drag(180, 260, 100, 200)
    if reverse_region != region or reverse_details.get("status") != "selected":
        print("ОШИБКА: обратное направление выделения работает неверно:", reverse_region, reverse_details)
        return 1

    tiny_region, tiny_details = ScreenRecorderProWin11.normalize_capture_region_drag(10, 10, 20, 25)
    if tiny_region is not None or tiny_details.get("status") != "too_small":
        print("ОШИБКА: слишком маленькая область не получила точную причину:", tiny_region, tiny_details)
        return 1

    crop_box = ScreenRecorderProWin11.get_screenshot_snapshot_crop_box(
        [100, 200, 80, 60], [0, 0, 1920, 1080], [1920, 1080]
    )
    if crop_box != (100, 200, 180, 260):
        print("ОШИБКА: область сохранённого снимка вычислена неверно:", crop_box)
        return 1

    negative_origin_box = ScreenRecorderProWin11.get_screenshot_snapshot_crop_box(
        [-1800, 100, 500, 300], [-1920, 0, 3840, 1080], [3840, 1080]
    )
    if negative_origin_box != (120, 100, 620, 400):
        print("ОШИБКА: отрицательные координаты виртуального экрана обработаны неверно:", negative_origin_box)
        return 1

    scaled_box = ScreenRecorderProWin11.get_screenshot_snapshot_crop_box(
        [20, 10, 40, 20], [0, 0, 200, 100], [100, 50]
    )
    if scaled_box != (10, 5, 30, 15):
        print("ОШИБКА: DPI-масштаб снимка обработан неверно:", scaled_box)
        return 1

    if normalize_screenshot_annotation_color("#34C759") != "#34c759":
        print("ОШИБКА: сохранённый цвет скриншота не нормализуется.")
        return 1
    if normalize_screenshot_annotation_color("not-a-color") != "#ff3b30":
        print("ОШИБКА: повреждённый цвет скриншота не заменён безопасным значением.")
        return 1
    if normalize_screenshot_annotation_size("12", "draw") != 12:
        print("ОШИБКА: размер кисти не восстанавливается из settings.json.")
        return 1
    if normalize_screenshot_annotation_size(8, "arrow") != 8:
        print("ОШИБКА: размер стрелки не восстанавливается из settings.json.")
        return 1
    if normalize_screenshot_annotation_size(999, "arrow") != 4:
        print("ОШИБКА: повреждённый размер стрелки не заменён безопасным значением.")
        return 1

    if Image is None:
        print("ОШИБКА: Pillow недоступен, инструменты скриншота не смогут собрать изображение.")
        return 1
    annotation_image = Image.new("RGB", (120, 100), "white")
    try:
        applied_annotations = ScreenRecorderProWin11.apply_screenshot_annotations(
            annotation_image,
            [
                {
                    "tool": "draw",
                    "points": [[10, 10], [25, 25], [40, 40]],
                    "color": "#34c759",
                    "width": 12,
                },
                {
                    "tool": "arrow",
                    "start": [60, 70],
                    "end": [105, 70],
                    "color": "#0a84ff",
                    "width": 8,
                },
                {"tool": "draw", "points": [None, ["bad", 2]]},
            ],
            [0, 0, 120, 100],
        )
        if applied_annotations != 2:
            print("ОШИБКА: нанесено неверное число аннотаций:", applied_annotations)
            return 1
        if annotation_image.getpixel((25, 25)) != (52, 199, 89):
            print("ОШИБКА: выбранный зелёный цвет карандаша не попал в изображение.")
            return 1
        if annotation_image.getpixel((25, 29)) != (52, 199, 89):
            print("ОШИБКА: выбранная толщина кисти не применена к изображению.")
            return 1
        if annotation_image.getpixel((80, 70)) != (10, 132, 255):
            print("ОШИБКА: выбранный синий цвет стрелки не попал в изображение.")
            return 1
        if annotation_image.getpixel((80, 73)) != (10, 132, 255):
            print("ОШИБКА: выбранный размер стрелки не применён к изображению.")
            return 1
    finally:
        annotation_image.close()

    native_print_screen = ScreenRecorderProWin11.parse_native_print_screen_hotkey("print screen")
    if native_print_screen != {
        "modifiers": 0,
        "virtual_key": 0x2C,
        "normalized_hotkey": "print screen",
    }:
        print("ОШИБКА: Print Screen неверно подготовлен для RegisterHotKey:", native_print_screen)
        return 1
    native_combination = ScreenRecorderProWin11.parse_native_print_screen_hotkey("ctrl+shift+prtsc")
    if not native_combination or native_combination.get("modifiers") != 0x0006:
        print("ОШИБКА: модификаторы Print Screen обработаны неверно:", native_combination)
        return 1
    if ScreenRecorderProWin11.parse_native_print_screen_hotkey("f10") is not None:
        print("ОШИБКА: обычная клавиша ошибочно направлена в нативный Print Screen backend.")
        return 1

    native_worker_source = inspect.getsource(ScreenRecorderProWin11._native_screenshot_hotkey_worker)
    native_start_source = inspect.getsource(ScreenRecorderProWin11._start_native_screenshot_hotkey)
    recovery_source = inspect.getsource(ScreenRecorderProWin11.schedule_startup_hotkey_recovery)
    if "RegisterHotKey" not in native_worker_source or "GetMessageW" not in native_worker_source:
        print("ОШИБКА: нативная регистрация Print Screen не имеет очереди Windows-сообщений.")
        return 1
    if "ready_event.wait" not in native_start_source or "_stop_native_screenshot_hotkey" not in native_start_source:
        print("ОШИБКА: результат нативной регистрации не подтверждается или старый поток не останавливается.")
        return 1

    from screen_recorder.components.annotation_overlay import AnnotationOverlay

    panel_position = AnnotationOverlay.clamp_panel_position(
        1894, 1042, 26, (0, 0, 1920, 1040), padding=8,
    )
    if panel_position != (1886, 1006):
        print("ОШИБКА: плавающая панель не возвращается из-под панели задач в рабочую область.")
        return 1

    duplicate_app = object.__new__(ScreenRecorderProWin11)
    duplicate_app._exiting = False
    duplicate_app.screenshot_hotkey_callback_lock = threading.Lock()
    duplicate_app.screenshot_hotkey_last_accepted_callback_perf = None
    duplicate_app.hotkey_callback_counts = {"record": 0, "screenshot": 0}
    duplicate_app.hotkey_registration_generation = 1
    duplicate_app.screenshot_hotkey_backend = "windows_register_hotkey"
    duplicate_app.native_screenshot_hotkey_thread_id = 123
    duplicate_app.hotkey_action_queue = queue.Queue()
    duplicate_events = []
    duplicate_app.diagnostic_log = lambda event, data=None, level="INFO": duplicate_events.append(event)
    if not duplicate_app._enqueue_screenshot_hotkey_from_backend("windows_register_hotkey"):
        print("ОШИБКА: первый callback Print Screen ошибочно отклонён.")
        return 1
    if duplicate_app._enqueue_screenshot_hotkey_from_backend("keyboard_backup"):
        print("ОШИБКА: резервный callback создаёт второй скриншот для одного нажатия.")
        return 1
    if duplicate_app.hotkey_action_queue.qsize() != 1 or "hotkey_callback_duplicate_ignored" not in duplicate_events:
        print("ОШИБКА: дедупликация двух backend Print Screen не подтверждена диагностикой.")
        return 1

    class TrackedTkVar:
        def __init__(self):
            self.get_calls = 0

        def get(self):
            self.get_calls += 1
            return "unexpected_tk_value"

    background_log_app = object.__new__(ScreenRecorderProWin11)
    background_log_app.gui_thread_ident = -1
    background_log_app.settings = {
        "problem_logs_enabled": True,
        "problem_logs_retention_days": "120",
        "problem_logs_error_retention_days": "240",
        "problem_logs_max_file_mb": "15",
        "problem_logs_keep_successful": False,
    }
    tracked_log_vars = []
    for var_name in (
        "problem_logs_enabled_var",
        "problem_logs_retention_days_var",
        "problem_logs_error_retention_days_var",
        "problem_logs_max_file_mb_var",
        "problem_logs_keep_successful_var",
    ):
        tracked = TrackedTkVar()
        tracked_log_vars.append(tracked)
        setattr(background_log_app, var_name, tracked)
    background_values = (
        background_log_app.should_write_problem_logs(),
        background_log_app.get_problem_logs_retention_days(),
        background_log_app.get_problem_logs_error_retention_days(),
        background_log_app.get_problem_log_file_limit_bytes(),
        background_log_app.keep_successful_problem_logs(),
    )
    if background_values != (True, 120, 240, 15 * 1024 * 1024, False):
        print("ОШИБКА: фоновые логи не используют кэшированные обычные настройки.")
        return 1
    if any(var.get_calls for var in tracked_log_vars):
        print("ОШИБКА: фоновая диагностика обращается к Tkinter-переменным и может заблокировать GUI.")
        return 1
    if 'source="windows_startup_recovery"' not in recovery_source:
        print("ОШИБКА: восстановление Print Screen после автозапуска отсутствует.")
        return 1
    if "hotkey_startup_recovery_skipped_healthy" not in recovery_source:
        print("ОШИБКА: автозапуск снова пересоздаёт уже исправный нативный Print Screen backend.")
        return 1
    if "psutil.process_iter" in recovery_source or "GetShellWindow" not in recovery_source:
        print("ОШИБКА: проверка Explorer при автозапуске снова может заблокировать GUI-поток.")
        return 1

    from screen_recorder.mixins import screenshots_hotkeys as hotkey_module

    class FakeRoot:
        def __init__(self):
            self.callbacks = []

        def after(self, delay_ms, callback):
            job = f"job-{len(self.callbacks) + 1}"
            self.callbacks.append((delay_ms, callback))
            return job

        def after_cancel(self, _job):
            return None

    class FakeVar:
        def get(self):
            return "print screen"

    recovery_app = object.__new__(ScreenRecorderProWin11)
    recovery_app.root = FakeRoot()
    recovery_app.hotkey_recovery_jobs = []
    recovery_app.started_from_windows_startup = True
    recovery_app.running = True
    recovery_app._exiting = False
    recovery_app.screenshot_hotkey_backend = "windows_register_hotkey"
    recovery_app.native_screenshot_hotkey_thread_id = 123
    recovery_app.hotkey_registration_generation = 1
    recovery_app.screenshot_hotkey_var = FakeVar()
    recovery_events = []
    registration_calls = []
    recovery_app._is_native_screenshot_hotkey_healthy = lambda: True
    recovery_app.diagnostic_log = lambda event, data=None, level="INFO": recovery_events.append(event)
    recovery_app.register_hotkey = lambda *args, **kwargs: registration_calls.append((args, kwargs))
    previous_hotkey_available = hotkey_module.HOTKEY_AVAILABLE
    hotkey_module.HOTKEY_AVAILABLE = True
    try:
        recovery_app.schedule_startup_hotkey_recovery()
        for _delay_ms, callback in recovery_app.root.callbacks[:3]:
            callback()
    finally:
        hotkey_module.HOTKEY_AVAILABLE = previous_hotkey_available
    if registration_calls or recovery_events.count("hotkey_startup_recovery_skipped_healthy") != 3:
        print("ОШИБКА: здоровый нативный Print Screen backend повторно регистрируется при автозапуске.")
        return 1

    selector_source = inspect.getsource(ScreenRecorderProWin11.select_capture_region)
    if "tk.Label(" in selector_source or "canvas.create_text(" not in selector_source:
        print("ОШИБКА: подсказка снова может перекрывать мышь в окне выделения.")
        return 1
    if "background_image" not in selector_source or "frozen_snapshot_before_selector_focus" not in selector_source:
        print("ОШИБКА: окно выделения не показывает кадр, сохранённый до потери фокуса.")
        return 1
    for required_tool_marker in (
        '("select", "▣ Область"',
        '("draw", "✎ Рисовать"',
        '("arrow", "➜ Стрелка"',
        'f"capture_tool_{action}"',
        "capture_color_palette",
        "capture_size_palette",
        "SCREENSHOT_ANNOTATION_COLORS",
        'f"capture_color_{color_id}"',
        'f"capture_size_{size_value}"',
        "screenshot_toolbar_x",
        "screenshot_toolbar_y",
        "screenshot_canvas_v3",
    ):
        if required_tool_marker not in selector_source:
            print("ОШИБКА: отсутствует собственный инструмент скриншота:", required_tool_marker)
            return 1
    if "AnnotationOverlay(" in selector_source:
        print("ОШИБКА: скриншот снова создаёт рисовалку видеозаписи вместо собственной панели.")
        return 1
    for required_event in (
        "capture_region_selector_opened",
        "capture_region_selection_started",
        "capture_region_selection_finished",
        "screenshot_annotation_tool_selected",
        "screenshot_annotation_color_selected",
        "screenshot_annotation_size_selected",
        "screenshot_annotation_toolbar_moved",
        "screenshot_annotation_added",
        "screenshot_annotation_undone",
        "screenshot_annotations_cleared",
    ):
        if required_event not in selector_source:
            print("ОШИБКА: отсутствует диагностическое событие:", required_event)
            return 1

    settings_source = inspect.getsource(ScreenRecorderProWin11.save_settings)
    for persisted_key in (
        "screenshot_draw_color",
        "screenshot_draw_size",
        "screenshot_arrow_color",
        "screenshot_arrow_size",
        "screenshot_toolbar_x",
        "screenshot_toolbar_y",
    ):
        if persisted_key not in settings_source:
            print("ОШИБКА: параметр скриншота не сохраняется в settings.json:", persisted_key)
            return 1

    snapshot_worker_source = inspect.getsource(ScreenRecorderProWin11._capture_screenshot_snapshot_worker)
    clipboard_worker_source = inspect.getsource(ScreenRecorderProWin11._take_screenshot_worker)
    if "ImageGrab.grab" not in snapshot_worker_source:
        print("ОШИБКА: экран не сохраняется до открытия окна выделения.")
        return 1
    if "ImageGrab.grab" in clipboard_worker_source or "snapshot.crop" not in clipboard_worker_source:
        print("ОШИБКА: после выбора область снова читается с изменившегося рабочего стола.")
        return 1
    annotation_position = clipboard_worker_source.find("apply_screenshot_annotations")
    crop_position = clipboard_worker_source.find("snapshot.crop")
    if annotation_position < 0 or crop_position < 0 or annotation_position > crop_position:
        print("ОШИБКА: пометки скриншота наносятся не на замороженный кадр до обрезки.")
        return 1
    screenshot_source = "\n".join((
        inspect.getsource(ScreenRecorderProWin11._start_screenshot_snapshot_worker),
        snapshot_worker_source,
        inspect.getsource(ScreenRecorderProWin11._take_screenshot_worker),
    ))
    for required_event in (
        "screenshot_snapshot_started",
        "screenshot_snapshot_ready",
        "screenshot_snapshot_failed",
        "screenshot_copied_to_clipboard",
    ):
        if required_event not in screenshot_source:
            print("ОШИБКА: отсутствует диагностическое событие:", required_event)
            return 1

    owned_diagnostic = Path("diagnostic_2026-08-11_12-00-00-123_pid42.txt")
    if not ScreenRecorderProWin11._is_owned_problem_log_item(owned_diagnostic):
        print("ОШИБКА: диагностический лог программы не распознан как собственный.")
        return 1
    if ScreenRecorderProWin11._is_owned_problem_log_item(Path("manual_notes.md")):
        print("ОШИБКА: пользовательский файл ошибочно разрешён для автоочистки.")
        return 1

    print(f"APP_BUILD: {APP_BUILD}")
    print(f"APP_DIR: {APP_DIR}")
    print(f"Методов главного класса: {len(methods)}")
    print("Проверка структуры и аудиологики завершена успешно.")
    print("Реальную запись ddagrab/NVENC нужно проверить на Windows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
