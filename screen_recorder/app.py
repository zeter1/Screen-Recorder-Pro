from .shared import *
from .mixins.ui import UiMixin
from .mixins.file_tools import FileToolsMixin
from .mixins.settings import SettingsMixin
from .mixins.webcam_devices import WebcamDevicesMixin
from .mixins.audio_devices import AudioDevicesMixin
from .mixins.processes import ProcessMixin
from .mixins.problem_logs import ProblemLogsMixin
from .mixins.ffmpeg_support import FfmpegSupportMixin
from .mixins.instant_buffer import InstantBufferMixin
from .mixins.recording_session import RecordingSessionMixin
from .mixins.segment_audio import SegmentAudioMixin
from .mixins.capture_commands import CaptureCommandsMixin
from .mixins.dxcam_capture import DxcamCaptureMixin
from .mixins.recording_control import RecordingControlMixin
from .mixins.timing import TimingMixin
from .mixins.smoothness_diagnostics import SmoothnessDiagnosticsMixin
from .mixins.finalize import FinalizeMixin
from .mixins.overlay_controls import OverlayControlsMixin
from .mixins.timer_state import TimerStateMixin
from .mixins.screenshots_hotkeys import ScreenshotsHotkeysMixin
from .mixins.tray_startup import TrayStartupMixin
from .mixins.lifecycle import LifecycleMixin


class ScreenRecorderProWin11(
    UiMixin,
    FileToolsMixin,
    SettingsMixin,
    WebcamDevicesMixin,
    AudioDevicesMixin,
    ProcessMixin,
    ProblemLogsMixin,
    FfmpegSupportMixin,
    InstantBufferMixin,
    RecordingSessionMixin,
    SegmentAudioMixin,
    CaptureCommandsMixin,
    DxcamCaptureMixin,
    RecordingControlMixin,
    TimingMixin,
    SmoothnessDiagnosticsMixin,
    FinalizeMixin,
    OverlayControlsMixin,
    TimerStateMixin,
    ScreenshotsHotkeysMixin,
    TrayStartupMixin,
    LifecycleMixin,
):
    """Главный класс приложения. Функциональные блоки вынесены в mixin-модули."""

    ALLOWED_RECORDING_FPS = (24, 25, 30, 48, 50, 60, 72, 75, 90, 100, 120, 144, 165, 240)
    MP4_VIDEO_TRACK_TIMESCALE = 39600

    def __init__(self, root):
        self.root = root
        self.started_from_windows_startup = any(
            str(arg).strip().lower() in {"--tray", "--windows-startup"}
            for arg in sys.argv[1:]
        )
        # Tkinter безопасно трогать только из основного GUI-потока.
        # Поток DXcam читает только кэшированные координаты служебных окон,
        # иначе остановка из плавающей панели могла зависать на winfo/update_idletasks.
        self.gui_thread_ident = threading.get_ident()
        self.root.title("Screen Recorder Pro — Windows 11")
        self.root.geometry("980x560")
        self.root.minsize(860, 480)
        self.root.configure(bg="#1e1e1e")

        self.diagnostic_started_perf = time.perf_counter()
        self.diagnostic_session_id = f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S-%f')[:-3]}_pid{os.getpid()}"
        self.diagnostic_log_lock = threading.RLock()
        self.diagnostic_log_paths = []
        self._diagnostic_truncated_paths = set()
        self.current_session_log_dir = None
        self.session_summary_path = None
        self.session_events_path = None
        self.session_errors_path = None
        self.session_ffmpeg_path = None
        self.session_settings_path = None
        # Специализированные логи плавности. Они создаются отдельно от общего
        # текста, чтобы ChatGPT мог читать структурированные данные без поиска
        # нужных чисел среди тысяч строк FFmpeg.
        self.session_ai_smoothness_path = None
        self.session_ffmpeg_progress_path = None
        self.session_performance_path = None
        self.session_frame_content_path = None
        self.session_auto_stutter_path = None
        self.session_source_manifest_path = None
        self.session_source_snapshot_path = None
        self.session_ai_prompt_path = None
        self.session_clock_alignment_path = None
        self.session_timing_detail_path = None
        self.session_audio_sync_path = None
        self._session_events_truncated = False
        self._session_ffmpeg_truncated = False
        self._session_errors_truncated = False
        self._problem_log_file_lock = threading.RLock()
        self._source_snapshot_written_for_error = False
        self.post_diagnostics_thread = None
        self.post_diagnostics_cancel_event = threading.Event()
        self.post_diagnostics_running = False
        self.audio_device_refresh_job = None
        self._session_log_max_file_bytes = 1_800_000
        self._session_log_max_text_chars = 6000
        self._process_seq = 0
        self._process_meta = {}
        self.log_handle = None
        self.current_log_path = None
        self.setup_diagnostic_logging()
        self.install_exception_hooks()
        self.diagnostic_log("app_constructor_start", self.collect_basic_runtime_info())
        self.diagnostic_log("app_launch_mode", {
            "started_from_windows_startup": self.started_from_windows_startup,
            "arguments": [str(arg) for arg in sys.argv[1:]],
        })

        self.settings = self.load_settings()
        self.diagnostic_log("settings_loaded", {
            "settings_path": SETTINGS_PATH,
            "exists": SETTINGS_PATH.exists(),
            "settings": self.settings,
        })
        self.ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
        self._encoder_support_cache = {}
        self._filter_support_cache = {}
        self._input_format_support_cache = {}
        self._ddagrab_warm_done = False

        self.is_recording = False
        self.is_paused = False
        self.is_finalizing = False
        # Отдельный флаг фазы запуска. Нужен, чтобы повторный клик по
        # плавающей панели не запускал второй старт поверх первого, пока
        # FFmpeg/DXcam ещё готовятся.
        self.is_starting = False
        self.running = True
        self.initializing = True
        self.finalize_thread = None
        self.pause_transition_thread = None
        # True while a pause/resume transition is running. Prevents double clicks
        # and keeps heavy FFmpeg stop work out of the Tkinter GUI thread.
        self.is_pause_transitioning = False
        self.recording_audio_bitrate = "192k"
        # Фактический FPS, с которым стартовал текущий FFmpeg-сегмент.
        # Значение сохраняется отдельно от Tkinter-переменных, чтобы фоновая
        # проверка итогового файла не читала GUI-объекты из другого потока.
        self.recording_requested_fps = None
        self.recording_effective_fps = None
        self.recording_refresh_hz = None
        self.recording_ddagrab_poll_fps = None

        # Диагностика плавности текущей записи. Всё ниже — обычные Python-данные,
        # которые безопасно читать из фоновых потоков без обращения к Tkinter.
        self.recording_capture_backend = None
        self.recording_ffmpeg_command = None
        self.recording_ffmpeg_pid = None
        self.recording_progress_lock = threading.RLock()
        self.recording_progress_samples = []
        self.recording_progress_latest = {}
        self.recording_progress_threads = []
        self.recording_first_frame_perf = None
        self.recording_last_frame_perf = None
        self.segment_capture_started_perf = None
        self.segment_first_progress_out_time = None
        self.current_segment_media_seconds = 0.0
        self.current_segment_last_progress_perf = None
        self.recording_performance_lock = threading.RLock()
        self.recording_performance_samples = []
        self.recording_performance_thread = None
        self.recording_performance_stop_event = threading.Event()
        self.last_video_timing_summary = None
        self.last_frame_content_analysis = None
        self.last_ai_smoothness_report = None
        self.recording_settings_snapshot = None
        self.recording_output_folder_snapshot = None
        self._psutil_system_cpu_warmed = False
        self._psutil_processes = {}
        self._last_gpu_sample_perf = 0.0
        self._cached_nvidia_smi_path = None

        self.process = None
        self.process_lock = threading.Lock()
        self.recording_session_id = None

        self.current_segment_engine = "ffmpeg"
        self.dxcam_stop_event = None
        self.dxcam_thread = None
        self.dxcam_camera = None
        self.dxcam_stats = {}

        self.temp_dir = None
        self.segments = []
        self.segment_index = 0
        self.segment_started_at = None
        self.recorded_seconds = 0.0
        self.recorded_wall_seconds = 0.0
        self.output_path = None
        self.last_output_path = None
        self.last_debug_log_path = None

        self.meter_queue = queue.Queue()
        self.audio_meter_processes = {}
        self.audio_meter_threads = {}
        self.audio_meters_running = False
        self.audio_meter_generation = 0
        self.meter_restart_job = None

        # Все дочерние процессы FFmpeg/ffprobe, которые запускает программа.
        # Нужны для жёсткой зачистки при выходе, чтобы после закрытия программы
        # не оставались ffmpeg.exe и не блокировали папки/файлы в File Locksmith.
        self.child_processes = set()
        self.child_processes_lock = threading.Lock()
        self._exiting = False
        self._exit_after_finalize = False
        self._exit_retry_job = None
        self.cancel_start_requested = False
        self.recording_watchdog_job = None
        self.recording_failure_reason = None
        self.incomplete_output_path = None

        self.save_job = None
        self.hotkey_job = None
        self.hotkey_poll_job = None
        self.hotkey_action_queue = queue.Queue()
        self.hotkey_handle = None
        self.screenshot_hotkey_handle = None
        self.screenshot_hotkey_backup_handle = None
        self.screenshot_hotkey_backend = None
        self.native_screenshot_hotkey_thread = None
        self.native_screenshot_hotkey_thread_id = None
        self.native_screenshot_hotkey_ready_event = None
        self.native_screenshot_hotkey_stop_event = None
        self.native_screenshot_hotkey_registered = False
        self.native_screenshot_hotkey_last_result = None
        self.hotkey_recovery_jobs = []
        self.hotkey_registration_generation = 0
        self.hotkey_callback_counts = {"record": 0, "screenshot": 0}
        self.hotkey_last_callback_perf = None
        self.hotkey_last_registration_perf = None
        self.screenshot_hotkey_callback_lock = threading.Lock()
        self.screenshot_hotkey_last_accepted_callback_perf = None
        # Временный глобальный перехват нужен для настройки горячей клавиши
        # простым нажатием. Поток библиотеки keyboard не обращается к Tkinter:
        # он только кладёт готовый результат в hotkey_action_queue.
        self.screenshot_hotkey_capture_hook = None
        self.screenshot_hotkey_capture_active = False
        self.screenshot_hotkey_capture_pressed = []
        self.screenshot_hotkey_capture_result_sent = False
        self.screenshot_hotkey_capture_lock = threading.Lock()
        # Защита от двойного срабатывания горячей клавиши, когда клавиши ещё зажаты.
        self.last_record_toggle_hotkey_time = 0.0
        # Момент нажатия «Начать запись» нужен для измерения задержки запуска.
        # В длительность активного захвата FFmpeg это время больше не входит:
        # подготовка, обратный отсчёт и открытие устройств не являются кадрами видео.
        # Для DXcam поле также используется горячим буфером начала записи.
        self.recording_start_requested_perf = None
        self.recording_stop_requested_perf = None

        # Горячий буфер DXcam. Он постоянно держит последние кадры экрана,
        # пока запись не идёт. Поэтому при нажатии Start в видео попадают кадры
        # с момента нажатия, даже если FFmpeg/аудио открываются ещё 0.5–1 секунду.
        self.instant_buffer_lock = threading.Lock()
        # DXcam использует singleton-камеру внутри библиотеки. Все обращения к
        # одному объекту камеры держим под отдельным lock, чтобы фоновый буфер и
        # поток записи не трогали camera.get_latest_frame/grab/stop одновременно.
        # Без этого на повторном старте dxcam мог вернуть старый instance и
        # зависнуть, что выглядело как «Python не отвечает».
        self.dxcam_camera_io_lock = threading.RLock()
        # Circuit breaker для DXcam. Если библиотека один раз зависла/не отдала
        # камеру при старте, до перезапуска программы уходим на FFmpeg-захват.
        # Это лучше, чем снова и снова рисковать зависанием окна при клике
        # «Запись» на плавающей панели.
        self.dxcam_disabled_for_session = False
        self.dxcam_disabled_reason = ""
        self.instant_buffer_frames = []          # [(perf_time, frame_bgr, cursor_pos), ...]
        self.instant_buffer_camera = None
        self.instant_buffer_thread = None
        self.instant_buffer_stop_event = None
        self.instant_buffer_ready = False
        self.instant_buffer_last_error = None
        # Буфер мгновенного старта хранит последние секунды экрана, чтобы
        # компенсировать подготовку FFmpeg/аудио после клика «Запись».
        # Память ограничена отдельно: на 4K кадрах буфер становится разреженным,
        # но всё равно сохраняет сам момент старта вместо полной потери начала.
        self.instant_buffer_max_seconds = 3.0
        self.instant_buffer_max_frames = 120
        self.instant_buffer_max_bytes = 420 * 1024 * 1024
        self._ffmpeg_ok_cache = None
        self._preflight_thread = None

        self.tray_icon = None
        self.tray_thread = None
        self.tray_ready_event = threading.Event()
        self.tray_error = None
        self.tray_unavailable_warned = False
        self.annotation_overlay = None
        self.webcam_preview = None
        # Оставлено для совместимости со старой логикой. В этой версии
        # плавающая панель специально видна на экране и попадает в итоговое видео.
        self.annotation_toolbar_clean_frame = None

        self.output_folder = tk.StringVar(value=self.settings.get("output_folder", os.getcwd()))
        self.format_var = tk.StringVar(value=self.settings.get("format", "mkv"))
        self.fps_var = tk.StringVar(value=self.settings.get("fps", "60"))
        self.auto_adjust_fps_var = tk.BooleanVar(value=bool(self.settings.get("auto_adjust_fps", False)))
        self.video_bitrate_var = tk.StringVar(value=str(normalize_video_bitrate_mbps(self.settings.get("video_bitrate", "16"))))
        saved_capture_method = self.settings.get("capture_method", DEFAULT_CAPTURE_METHOD)
        # Older builds saved DXcam as the recommended method. On this system it
        # intermittently freezes the GUI at "Запускаю запись...", so old DXcam
        # choices are migrated to the stable FFmpeg Desktop Duplication path.
        if "DXcam" in str(saved_capture_method):
            saved_capture_method = DEFAULT_CAPTURE_METHOD
        self.capture_method_var = tk.StringVar(value=saved_capture_method)
        if self.capture_method_var.get() not in CAPTURE_METHODS:
            self.capture_method_var.set(DEFAULT_CAPTURE_METHOD)
        self.encoder_var = tk.StringVar(value=self.settings.get("encoder", DEFAULT_ENCODER_METHOD))
        if self.encoder_var.get() not in ENCODER_METHODS:
            self.encoder_var.set(DEFAULT_ENCODER_METHOD)
        saved_webcam_device = str(self.settings.get("webcam_device", WEBCAM_AUTO) or WEBCAM_AUTO).strip()
        self.webcam_device_var = tk.StringVar(value=saved_webcam_device or WEBCAM_AUTO)
        self.audio_bitrate_var = tk.StringVar(value=self.settings.get("audio_bitrate", "192k"))
        saved_mic_device = self.normalize_saved_audio_choice(self.settings.get("mic_device", MIC_AUDIO_DEFAULT), "mic")
        saved_system_device = self.normalize_saved_audio_choice(self.settings.get("system_device", SYSTEM_AUDIO_DEFAULT), "system")
        self.mic_device_var = tk.StringVar(value=saved_mic_device)
        self.system_device_var = tk.StringVar(value=saved_system_device)
        self.mic_volume_var = tk.IntVar(value=int(self.settings.get("mic_volume", 100)))
        self.system_volume_var = tk.IntVar(value=int(self.settings.get("system_volume", 100)))
        # Минимальный режим программы всегда использует плавающую панель как
        # единственный основной интерфейс. Поэтому панель включена принудительно,
        # даже если старая версия сохранила draw_enabled=false.
        self.draw_enabled_var = tk.BooleanVar(value=True)
        self.floating_panel_size_var = tk.IntVar(value=normalize_floating_panel_size(self.settings.get("floating_panel_size", 34)))
        self.cursor_visible_var = tk.BooleanVar(value=bool(self.settings.get("cursor_visible", True)))
        self.cursor_highlight_var = tk.BooleanVar(value=bool(self.settings.get("cursor_highlight", False)))
        try:
            cursor_size_value = int(self.settings.get("cursor_highlight_size", 70))
        except Exception:
            cursor_size_value = 70
        cursor_size_value = max(20, min(200, cursor_size_value))
        self.cursor_highlight_size_var = tk.IntVar(value=cursor_size_value)
        # Кэш параметров курсора для потока записи. Tk-переменные нельзя читать
        # из фонового DXcam-потока, особенно во время остановки из GUI-панели.
        self.recording_cursor_visible = bool(self.cursor_visible_var.get())
        self.recording_cursor_highlight = bool(self.cursor_highlight_var.get())
        self.recording_cursor_highlight_size = int(cursor_size_value)
        self._cursor_bitmap_cache = None
        self.startup_tray_var = tk.BooleanVar(value=bool(self.settings.get("startup_tray", False)))
        self.hotkey_var = tk.StringVar(value=self.settings.get("hotkey", "ctrl+shift+r"))
        self.screenshot_hotkey_var = tk.StringVar(value=str(self.settings.get("screenshot_hotkey", "f10") or "f10"))
        self.screenshot_hotkey_display_var = tk.StringVar(value=self.screenshot_hotkey_var.get())
        self.screenshot_hotkey_combo = None
        self.screenshot_status_var = tk.StringVar(value="Нажми горячую клавишу, выдели область — снимок попадёт в буфер обмена.")
        self.screenshot_prepare_thread = None
        self.screenshot_thread = None
        self._screenshot_in_progress = False
        self._screenshot_frozen_image = None
        self._screenshot_frozen_screen_rect = None
        self._screenshot_snapshot_captured_perf = None
        self.last_screenshot_hotkey_time = 0.0
        self._screenshot_restore_settings_window = False
        # Новые опции (волна 1): монитор для записи, авто-стоп по таймеру, отсчёт.
        self.monitor_index_var = tk.StringVar(value=str(self.settings.get("monitor_index", "1")))
        self.auto_stop_minutes_var = tk.StringVar(value=str(self.settings.get("auto_stop_minutes", "0")))
        self.countdown_enabled_var = tk.BooleanVar(value=bool(self.settings.get("countdown_enabled", True)))
        self._auto_stop_after_id = None
        self.show_keys_overlay_var = tk.BooleanVar(value=bool(self.settings.get("show_keys_overlay", False)))
        self.open_folder_after_stop_var = tk.BooleanVar(value=bool(self.settings.get("open_folder_after_stop", False)))

        # Настройки вкладки «Логи проблем». По умолчанию подробные логи включены,
        # потому что они нужны для быстрого исправления багов нейросетью. При
        # выключении программа не создаёт папку сессии и пишет FFmpeg-лог в os.devnull.
        self.problem_logs_enabled_var = tk.BooleanVar(value=bool(self.settings.get("problem_logs_enabled", True)))
        self.problem_logs_retention_days_var = tk.StringVar(value=str(self.settings.get("problem_logs_retention_days", "120")))
        self.problem_logs_error_retention_days_var = tk.StringVar(value=str(self.settings.get("problem_logs_error_retention_days", "120")))
        self.problem_logs_max_file_mb_var = tk.StringVar(value=str(self.settings.get("problem_logs_max_file_mb", "2")))
        self.problem_logs_cleanup_on_start_var = tk.BooleanVar(value=bool(self.settings.get("problem_logs_cleanup_on_start", True)))
        self.problem_logs_keep_successful_var = tk.BooleanVar(value=bool(self.settings.get("problem_logs_keep_successful", True)))
        self.problem_logs_status_var = tk.StringVar(value="")

        self._keys_overlay = None
        self._keys_overlay_label = None
        self._keys_hook = None
        self._keys_recent = []
        self._cursor_highlight_window = None
        self._cursor_highlight_canvas = None
        self._cursor_highlight_job = None
        # Область — разовая: применяется к ОДНОЙ записи, запущенной кнопкой
        # «Область». Обычная «Запись» всегда пишет весь экран.
        self.capture_region = None       # активная область текущей записи (None = весь экран)
        self._pending_region = None      # область, выбранная с панели для старта «по области»

        self.status_var = tk.StringVar(value="Готово")
        self.timer_var = tk.StringVar(value="00:00:00")
        self.rec_indicator_var = tk.StringVar(value="● READY")
        self.mic_level_var = tk.DoubleVar(value=0)
        self.system_level_var = tk.DoubleVar(value=0)
        self.mic_level_text = tk.StringVar(value="0%")
        self.system_level_text = tk.StringVar(value="0%")

        self.audio_devices = [NO_AUDIO]
        self.dshow_audio_devices = []
        self.wasapi_capture_devices = []
        self.wasapi_render_devices = []
        self.mic_audio_devices = [NO_AUDIO, MIC_AUDIO_DEFAULT]
        self.system_audio_devices = [NO_AUDIO, SYSTEM_AUDIO_DEFAULT, SYSTEM_AUDIO_COMMUNICATION]
        self.webcam_devices = [WEBCAM_AUTO]
        self.settings_window = None
        self.mic_combo = None
        self.system_combo = None
        self.webcam_combo = None
        self.hotkey_combo = None
        self.open_output_folder_button = None
        self.open_log_button = None
        self.make_gif_button = None
        self.trim_button = None
        self.python_loopback_audio_segments = {}
        self.python_loopback_sync_metadata = {}
        self.current_python_loopback_recorder = None
        self.current_python_loopback_segment = None
        self.current_python_loopback_path = None

        self.setup_styles()
        self.create_ui()
        self.bind_setting_traces()
        self.refresh_audio_devices(silent=True)
        self.refresh_webcam_devices(silent=True)
        self.initializing = False
        self.log_startup_snapshot("ui_initialized")
        self.update_problem_logs_status_text()
        # Настройка в settings.json является главным источником истины. Если
        # Windows/старая версия потеряла запись Run, следующий ручной запуск
        # программы сам её восстановит. При выключенной настройке удаляется
        # оставшаяся устаревшая запись.
        self.root.after(
            80,
            lambda: self.sync_startup_tray_setting(
                show_errors=False,
                source="startup_reconcile",
            ),
        )
        self.root.after(1800, self.cleanup_problem_logs_on_start)

        # Если предыдущая версия была закрыта неудачно, она могла оставить
        # фоновые ffmpeg.exe от аудио-индикаторов или временной записи.
        # Чистим только процессы с характерными командами этой программы.
        self.cleanup_stale_ffmpeg_processes_from_previous_runs()

        self.register_hotkey(source="startup_initial", recovery_attempt=0)
        self.schedule_startup_hotkey_recovery()
        self.process_hotkey_actions()
        # Аудио-индикаторы нужны только когда открыто окно настроек. При обычной
        # работе в трее два лишних ffmpeg-процесса не запускаем.
        self.stop_audio_meters(join_timeout=0.02)
        self.schedule_audio_device_refresh()
        self.root.after(600, self.start_background_preparation)

        # Предпросмотр экрана полностью убран. Он делал второй захват экрана
        # параллельно FFmpeg и мог давать рывки в итоговой записи.

        self.update_timer()
        self.update_audio_levels_from_queue()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Запуск теперь всегда минимальный: главное окно скрыто, программа
        # находится в трее, а на экране остаётся только плавающая панель записи.
        self.root.after(250, self.start_minimal_panel_mode)

        # Восстановление после сбоя: если прошлый сеанс упал, в .recording_temp
        # остались сегменты — предложим собрать их в готовое видео.
        self.root.after(1500, self.recover_orphan_segments)

        if shutil.which("ffmpeg") is None:
            messagebox.showwarning(
                "Нужен FFmpeg",
                "Программа работает через FFmpeg. Установи FFmpeg и добавь ffmpeg.exe в PATH.\n\n"
                "После установки перезапусти программу."
            )
