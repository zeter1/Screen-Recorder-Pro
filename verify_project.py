from __future__ import annotations

import ast
import compileall
import inspect
import queue
import sys
import threading
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
    }
    missing = sorted(required - methods)
    if missing:
        print("ОШИБКА: отсутствуют методы:", ", ".join(missing))
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
