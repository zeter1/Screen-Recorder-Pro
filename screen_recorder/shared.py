import os
import gc
import atexit
import io
import re
import sys
import json
import hashlib
import shutil
import subprocess
import tempfile
import threading
import queue
import time
import ctypes
import traceback
import wave
import platform
import statistics
from ctypes import wintypes

try:
    import psutil
    PSUTIL_AVAILABLE = True
except Exception:
    psutil = None
    PSUTIL_AVAILABLE = False
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except Exception:
    np = None
    NUMPY_AVAILABLE = False

try:
    from PIL import Image, ImageDraw
    PIL_AVAILABLE = True
    try:
        from PIL import ImageGrab
    except Exception:
        ImageGrab = None
    try:
        from PIL import ImageTk
    except Exception:
        ImageTk = None
except Exception:
    Image = None
    ImageDraw = None
    ImageGrab = None
    ImageTk = None
    PIL_AVAILABLE = False

try:
    import keyboard
    HOTKEY_AVAILABLE = True
except Exception:
    HOTKEY_AVAILABLE = False

try:
    import pystray
    TRAY_AVAILABLE = True
except Exception:
    TRAY_AVAILABLE = False

try:
    import dxcam
    DXCAM_AVAILABLE = True
except Exception:
    dxcam = None
    DXCAM_AVAILABLE = False

# DXcam was the source of intermittent hard freezes on this project: the
# library keeps an internal singleton camera and can block inside create(),
# start(), get_latest_frame(), stop() or release(). Those calls were enough to
# make Tkinter show "Python is not responding" when recording was started
# from the floating panel. For the stable build we do not use DXcam at all; the
# recommended path is FFmpeg Desktop Duplication (ddagrab) with gdigrab fallback.
DXCAM_CAPTURE_ENABLED = False

APP_NAME = "ScreenRecorderProWin11"
APP_BUILD = "2026-08-24-recording-cursor-size-v24"
DIAGNOSTIC_SCHEMA = "screen_recorder_diagnostics_v20"
PROBLEM_LOGS_FOLDER_NAME = "Логи проблем"
NO_AUDIO = "Не записывать"
MIC_AUDIO_DEFAULT = "Микрофон (по умолчанию Windows)"
SYSTEM_AUDIO_DEFAULT = "Звук компьютера (по умолчанию Windows)"
SYSTEM_AUDIO_COMMUNICATION = "Звук компьютера (устройство связи Windows)"
# Старое название оставлено только для совместимости со старыми settings.json.
SYSTEM_AUDIO_WASAPI = "Звук компьютера (WASAPI loopback — авто)"
WASAPI_RENDER_PREFIX = "WASAPI loopback: "
WEBCAM_AUTO = "Авто (первая найденная)"
VIDEO_FORMATS = ["mp4", "mkv", "avi", "mov"]
RECORDING_CURSOR_SIZE_PERCENT_OPTIONS = (50, 75, 100, 125, 150, 175, 200, 250, 300)

# Собственные инструменты скриншота. Значения хранятся в settings.json, поэтому
# список и нормализаторы находятся в shared.py и одинаково используются UI,
# сохранением настроек и регрессионными тестами.
SCREENSHOT_ANNOTATION_COLORS = (
    ("red", "#ff3b30"),
    ("orange", "#ff9500"),
    ("yellow", "#ffd60a"),
    ("green", "#34c759"),
    ("cyan", "#00c7be"),
    ("blue", "#0a84ff"),
    ("purple", "#bf5af2"),
    ("white", "#ffffff"),
    ("black", "#111111"),
)
SCREENSHOT_DRAW_SIZES = (2, 5, 8, 12, 18)
SCREENSHOT_ARROW_SIZES = (2, 4, 6, 8, 12)


def normalize_screenshot_annotation_color(value, default="#ff3b30"):
    allowed = {color.lower(): color for _color_id, color in SCREENSHOT_ANNOTATION_COLORS}
    normalized_default = str(default or "#ff3b30").strip().lower()
    fallback = allowed.get(normalized_default, SCREENSHOT_ANNOTATION_COLORS[0][1])
    return allowed.get(str(value or "").strip().lower(), fallback)


def normalize_screenshot_annotation_size(value, tool="draw"):
    tool = str(tool or "draw").strip().lower()
    choices = SCREENSHOT_ARROW_SIZES if tool == "arrow" else SCREENSHOT_DRAW_SIZES
    default = 4 if tool == "arrow" else 5
    try:
        size = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return size if size in choices else default

CAPTURE_METHODS = [
    "Desktop Duplication / ddagrab",
    "Авто — ddagrab, потом GDI",
    "Старый GDI / gdigrab",
    "DXcam / Desktop Duplication — отключён из-за зависаний",
]
ENCODER_METHODS = [
    "Авто — NVIDIA NVENC",
    "NVIDIA NVENC",
    "CPU x264",
    "NVIDIA NVENC H.265 (HEVC)",
    "CPU x265 (HEVC)",
]
DEFAULT_CAPTURE_METHOD = CAPTURE_METHODS[0]
DEFAULT_ENCODER_METHOD = ENCODER_METHODS[0]

def get_app_folder():
    """Корневая папка модульного проекта или папка собранного EXE."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    try:
        # shared.py лежит в <проект>/screen_recorder/shared.py.
        return Path(__file__).resolve().parent.parent
    except Exception:
        return Path.cwd()


APP_DIR = get_app_folder()



def get_program_entry_path():
    """Возвращает переносимую точку запуска модульной версии."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    candidates = [
        APP_DIR / "Screen Recorder Pro.py",
        APP_DIR / "main.py",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except Exception:
            continue
    return (APP_DIR / "main.py").resolve()


def write_modular_source_snapshot(target_path, max_bytes=5_000_000):
    """Сохраняет все исходники проекта в один читаемый нейросетью .py-файл."""
    target_path = Path(target_path)
    root = APP_DIR
    chunks = [
        "# SCREEN RECORDER PRO — MODULAR SOURCE SNAPSHOT\n",
        f"# Project root: {root}\n\n",
    ]
    total = sum(len(chunk.encode("utf-8")) for chunk in chunks)
    candidates = []
    for path in root.rglob("*.py"):
        try:
            relative = path.relative_to(root)
        except Exception:
            continue
        parts_lower = {part.lower() for part in relative.parts}
        if "__pycache__" in parts_lower or "логи проблем" in parts_lower:
            continue
        if relative.parts and relative.parts[0].startswith("_original"):
            continue
        candidates.append((str(relative).lower(), path, relative))

    for _key, path, relative in sorted(candidates):
        try:
            body = path.read_text(encoding="utf-8")
        except Exception as exc:
            body = f"# Не удалось прочитать файл: {exc}\n"
        block = (
            "\n# " + "=" * 76 + "\n"
            f"# FILE: {relative.as_posix()}\n"
            "# " + "=" * 76 + "\n"
            + body.rstrip() + "\n"
        )
        block_size = len(block.encode("utf-8"))
        if total + block_size > int(max_bytes):
            chunks.append(
                f"\n# Снимок остановлен на лимите {int(max_bytes)} байт. "
                f"Следующий файл: {relative.as_posix()}\n"
            )
            break
        chunks.append(block)
        total += block_size

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("".join(chunks), encoding="utf-8")
    return target_path


def write_modular_source_manifest(target_path):
    """Пишет компактный список исходников и SHA-256 вместо копии всего кода."""
    target_path = Path(target_path)
    root = APP_DIR
    files = []
    for path in root.rglob("*.py"):
        try:
            relative = path.relative_to(root)
        except Exception:
            continue
        parts_lower = {part.lower() for part in relative.parts}
        if "__pycache__" in parts_lower or "логи проблем" in parts_lower:
            continue
        if relative.parts and relative.parts[0].startswith("_original"):
            continue
        try:
            raw = path.read_bytes()
            stat = path.stat()
            files.append({
                "path": relative.as_posix(),
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "mtime_ns": int(stat.st_mtime_ns),
            })
        except Exception as exc:
            files.append({"path": relative.as_posix(), "error": repr(exc)})
    files.sort(key=lambda item: str(item.get("path", "")).lower())
    manifest = {
        "schema": "screen_recorder_source_manifest_v1",
        "app_build": APP_BUILD,
        "project_root": str(root),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "file_count": len(files),
        "files": files,
        "note_for_ai": (
            "Это отпечаток исходников конкретной сессии. Полный snapshot создаётся "
            "только при реальной ошибке, чтобы обычные логи не разрастались."
        ),
    }
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest

def get_writable_data_root():
    """Возвращает папку для настроек/логов, куда точно можно писать.

    Сначала пробуем папку рядом с программой, чтобы сохранить привычное поведение.
    Если программа лежит в защищённой папке вроде Program Files, используем
    %LOCALAPPDATA%/ScreenRecorderProWin11. Это убирает тихие ошибки сохранения
    настроек/логов/временных файлов.
    """
    candidates = [APP_DIR]
    try:
        local = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
        if local:
            candidates.append(Path(local) / APP_NAME)
    except Exception:
        pass
    candidates.append(Path.home() / f".{APP_NAME}")

    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            try:
                probe.unlink()
            except Exception:
                pass
            return folder
        except Exception:
            continue
    return Path(tempfile.gettempdir()) / APP_NAME


def get_extended_logs_root():
    """Папка логов проблем, удобная для передачи нейросети.

    Главный путь теперь не техническая старая папка логов, а понятная
    пользователю папка APP_DIR/Логи проблем. Внутри неё для каждой записи
    создаётся отдельная папка с датой и временем записи.
    """
    candidates = []
    try:
        candidates.append(APP_DIR / PROBLEM_LOGS_FOLDER_NAME)
    except Exception:
        pass
    try:
        candidates.append(APP_DIR.parent / PROBLEM_LOGS_FOLDER_NAME)
    except Exception:
        pass
    try:
        candidates.append(get_writable_data_root() / PROBLEM_LOGS_FOLDER_NAME)
    except Exception:
        pass
    candidates.append(Path(tempfile.gettempdir()) / PROBLEM_LOGS_FOLDER_NAME)

    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            try:
                probe.unlink()
            except Exception:
                pass
            return folder
        except Exception:
            continue
    return Path(tempfile.gettempdir()) / PROBLEM_LOGS_FOLDER_NAME


DATA_DIR = get_writable_data_root()
SETTINGS_DIR = DATA_DIR / "settings"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"
SETTINGS_BACKUP_PATH = SETTINGS_DIR / "settings.backup.json"
LOGS_DIR = get_extended_logs_root()
TEMP_RECORDINGS_DIR = DATA_DIR / "recording_temp"


def atomic_write_text(path, text, encoding="utf-8"):
    """Надёжно записывает небольшой текстовый файл через замену в той же папке."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with open(temp_path, "w", encoding=encoding, newline="") as file:
            file.write(str(text))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass


class SingleInstanceGuard:
    """Защита от запуска второй копии программы.

    На Windows используется именованный mutex, поэтому второй запуск
    останавливается ещё до создания окна и до запуска FFmpeg/аудиометров.
    На других ОС используется простой lock-файл в DATA_DIR.
    """

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name=APP_NAME):
        self.name = str(name)
        self.mutex_handle = None
        self.lock_fd = None
        self.lock_path = DATA_DIR / f"{APP_NAME}.lock"

    def acquire(self):
        if os.name == "nt":
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
                kernel32.CreateMutexW.restype = wintypes.HANDLE
                handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}_SingleInstanceMutex")
                last_error = ctypes.get_last_error()
                if not handle:
                    # Если mutex создать не удалось, не ломаем запуск программы.
                    return True
                self.mutex_handle = handle
                if last_error == self.ERROR_ALREADY_EXISTS:
                    self.release()
                    return False
                return True
            except Exception:
                return True

        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.lock_fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                return False
            os.write(self.lock_fd, str(os.getpid()).encode("ascii", errors="ignore"))
            return True
        except Exception:
            return True

    def release(self):
        if os.name == "nt":
            try:
                if self.mutex_handle:
                    ctypes.windll.kernel32.CloseHandle(self.mutex_handle)
            except Exception:
                pass
            self.mutex_handle = None
            return

        try:
            if self.lock_fd is not None:
                os.close(self.lock_fd)
        except Exception:
            pass
        self.lock_fd = None
        try:
            if self.lock_path.exists():
                self.lock_path.unlink()
        except Exception:
            pass

    @staticmethod
    def notify_already_running():
        text = "Программа для записи экрана уже запущена. Вторую копию запускать нельзя."
        if os.name == "nt":
            try:
                ctypes.windll.user32.MessageBoxW(None, text, "Screen Recorder Pro", 0x40)
                return
            except Exception:
                pass
        print(text, file=sys.stderr)


def normalize_video_bitrate_mbps(value, default=12):
    """Возвращает битрейт видео числом в Мбит/с, строго от 1 до 100.

    Пользователь может писать: 12, 12M, 12 мбит, 12.5.
    Для записи берём целое значение, чтобы FFmpeg получал понятный вид: 12M.
    """
    try:
        raw = str(value).strip().replace(",", ".")
        match = re.search(r"(\d+(?:\.\d+)?)", raw)
        if not match:
            number = default
        else:
            number = float(match.group(1))
        number = int(round(number))
    except Exception:
        number = default

    if number < 1:
        number = 1
    if number > 100:
        number = 100
    return number


def video_bitrate_to_ffmpeg(value):
    return f"{normalize_video_bitrate_mbps(value)}M"



def video_bitrate_to_bufsize(value):
    mbps = normalize_video_bitrate_mbps(value)
    return f"{max(2, mbps * 2)}M"


def minimum_quality_bitrate_mbps(fps_int, width=1920, height=1080):
    """Минимум для записи экрана без заметной деградации при движении.

    Значения намеренно консервативные: это не "маленький файл", а режим, где
    сохранённое видео должно быть похоже на то, что видно глазами во время записи.
    """
    try:
        fps = int(fps_int)
    except Exception:
        fps = 60
    try:
        pixels = max(1, int(width) * int(height))
    except Exception:
        pixels = 1920 * 1080

    # 16 Mbps для 1080p60, дальше масштабируем по FPS и площади кадра.
    base = 16.0 * (pixels / float(1920 * 1080)) * (max(24, fps) / 60.0)
    return max(8, min(60, int(round(base))))


def normalize_floating_panel_size(value, default=34):
    """Возвращает размер маленькой плавающей кнопки в пикселях.

    Старый размер был 54 px и на панели задач выглядел слишком крупно.
    Делаем компактный размер по умолчанию, но оставляем пользователю выбор
    в настройках, чтобы можно было увеличить кнопку под свой экран.
    """
    try:
        number = int(round(float(str(value).strip().replace(",", "."))))
    except Exception:
        number = int(default)
    if number < 24:
        number = 24
    if number > 72:
        number = 72
    return number


def normalize_recording_cursor_size_percent(value, default=100):
    """Нормализует размер курсора в записи к одному из значений UI."""
    try:
        number = int(round(float(str(value).strip().rstrip("%").replace(",", "."))))
    except Exception:
        number = int(default)
    return min(
        RECORDING_CURSOR_SIZE_PERCENT_OPTIONS,
        key=lambda option: (abs(option - number), abs(option - int(default))),
    )


def detect_primary_refresh_hz(default=60):
    """Частота обновления основного монитора в Гц. default при неудаче."""
    if os.name != "nt":
        return default
    try:
        import ctypes
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            hz = ctypes.windll.gdi32.GetDeviceCaps(hdc, 116)  # VREFRESH
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        return hz if hz and hz > 1 else default
    except Exception:
        return default


def detect_monitor_count(default=1):
    """Число мониторов (для выбора, какой писать через ddagrab output_idx)."""
    if os.name != "nt":
        return default
    try:
        import ctypes
        n = ctypes.windll.user32.GetSystemMetrics(80)  # SM_CMONITORS
        return n if n and n > 0 else default
    except Exception:
        return default


def smooth_fps_for_refresh(requested, refresh_hz, allowed, tolerance=0.25):
    """Подбирает FPS, кратный частоте монитора, ближайший к запрошенному.

    Когда выбранный FPS — делитель частоты обновления (60→60/120/144 и т.п.),
    дублирование кадров при захвате равномерное и видео не рвётся. Если рядом
    (в пределах ±tolerance) подходящего делителя нет, выбор пользователя не
    трогаем — лучше слегка неровно, чем резко поменять FPS файла.
    """
    try:
        refresh_hz = int(refresh_hz)
        requested = int(requested)
    except Exception:
        return requested
    if refresh_hz < 24 or requested <= 0:
        return requested
    divisors = [f for f in allowed if f <= refresh_hz and refresh_hz % f == 0]
    if refresh_hz in allowed and refresh_hz not in divisors:
        divisors.append(refresh_hz)
    if not divisors:
        return requested
    # При равной близости предпочитаем больший FPS (плавнее).
    best = min(divisors, key=lambda f: (abs(f - requested), -f))
    if requested * (1 - tolerance) <= best <= requested * (1 + tolerance):
        return best
    return requested
