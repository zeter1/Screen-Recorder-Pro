from ..shared import *


class ProblemLogsMixin:
    def _read_problem_log_setting(self, setting_key, var_name, default):
        """Читает Tk-переменную только из GUI-потока, иначе обычный dict."""
        gui_thread_ident = getattr(self, "gui_thread_ident", None)
        is_gui_thread = gui_thread_ident is None or threading.get_ident() == gui_thread_ident
        if is_gui_thread:
            try:
                var = getattr(self, var_name, None)
                if var is not None:
                    value = var.get()
                    settings = getattr(self, "settings", None)
                    if isinstance(settings, dict):
                        settings[setting_key] = value
                    return value
            except Exception:
                pass
        try:
            settings = getattr(self, "settings", None)
            if isinstance(settings, dict) and setting_key in settings:
                return settings.get(setting_key, default)
        except Exception:
            pass
        try:
            if SETTINGS_PATH.exists():
                raw_settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if setting_key in raw_settings:
                    return raw_settings.get(setting_key, default)
        except Exception:
            pass
        return default

    def should_write_problem_logs(self):
        """Главный переключатель логов проблем.

        До создания Tk-переменных пытаемся прочитать settings.json напрямую.
        Это важно для самого раннего старта, когда setup_diagnostic_logging()
        вызывается раньше load_settings().
        """
        return bool(self._read_problem_log_setting(
            "problem_logs_enabled",
            "problem_logs_enabled_var",
            True,
        ))

    @staticmethod
    def _coerce_number(value, default, minimum, maximum, allow_float=False):
        try:
            raw = str(value).strip().replace(",", ".")
            match = re.search(r"-?\d+(?:\.\d+)?", raw)
            number = float(match.group(0)) if match else float(default)
        except Exception:
            number = float(default)
        if number < minimum:
            number = minimum
        if number > maximum:
            number = maximum
        return float(number) if allow_float else int(round(number))

    def get_problem_logs_retention_days(self):
        return self._coerce_number(
            self._read_problem_log_setting(
                "problem_logs_retention_days",
                "problem_logs_retention_days_var",
                "120",
            ),
            default=120,
            minimum=0,
            maximum=3650,
        )

    def get_problem_logs_error_retention_days(self):
        return self._coerce_number(
            self._read_problem_log_setting(
                "problem_logs_error_retention_days",
                "problem_logs_error_retention_days_var",
                "120",
            ),
            default=120,
            minimum=0,
            maximum=3650,
        )

    def get_problem_log_file_limit_bytes(self):
        mb = self._coerce_number(
            self._read_problem_log_setting(
                "problem_logs_max_file_mb",
                "problem_logs_max_file_mb_var",
                "2",
            ),
            default=2.0,
            minimum=0.2,
            maximum=20.0,
            allow_float=True,
        )
        return int(float(mb) * 1024 * 1024)

    def keep_successful_problem_logs(self):
        return bool(self._read_problem_log_setting(
            "problem_logs_keep_successful",
            "problem_logs_keep_successful_var",
            True,
        ))

    def get_current_recording_log_path(self):
        """Путь для подробного лога текущей записи.

        Если логи проблем выключены, возвращаем os.devnull. Так существующая
        логика записи FFmpeg не ломается, но реальные .txt-файлы не создаются.
        """
        if not self.should_write_problem_logs():
            return os.devnull
        return self.current_log_path or (LOGS_DIR / "recording_log.txt")

    def on_problem_logs_setting_changed(self, *_args):
        try:
            self._session_log_max_file_bytes = self.get_problem_log_file_limit_bytes()
            self.update_problem_logs_status_text()
        except Exception:
            pass
        self.schedule_save_settings()

    def update_problem_logs_status_text(self):
        try:
            if not hasattr(self, "problem_logs_status_var"):
                return
            count = 0
            total = 0
            if LOGS_DIR.exists():
                for item in LOGS_DIR.iterdir():
                    try:
                        count += 1
                        if item.is_file():
                            total += item.stat().st_size
                        elif item.is_dir():
                            for child in item.rglob("*"):
                                try:
                                    if child.is_file():
                                        total += child.stat().st_size
                                except Exception:
                                    pass
                    except Exception:
                        pass
            total_mb = total / (1024 * 1024)
            enabled = "включены" if self.should_write_problem_logs() else "выключены"
            self.problem_logs_status_var.set(
                f"Статус: логи {enabled}. Объектов в папке: {count}. Примерный размер: {total_mb:.2f} МБ."
            )
        except Exception:
            pass

    def open_problem_logs_folder(self):
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            self.open_folder_with_explorer_fallback(LOGS_DIR)
            self.update_problem_logs_status_text()
        except Exception as exc:
            messagebox.showerror("Логи проблем", f"Не удалось открыть папку логов:\n{exc}")

    def cleanup_problem_logs_on_start(self):
        try:
            if not self.should_write_problem_logs():
                return
            if not bool(self._read_problem_log_setting(
                "problem_logs_cleanup_on_start",
                "problem_logs_cleanup_on_start_var",
                True,
            )):
                return
            self.cleanup_problem_logs(reason="auto_start")
        except Exception as exc:
            try:
                self.diagnostic_log("cleanup_problem_logs_on_start_failed", {"error": repr(exc)}, level="WARN")
            except Exception:
                pass

    def cleanup_problem_logs_manual(self):
        try:
            stats = self.cleanup_problem_logs(reason="manual")
            self.update_problem_logs_status_text()
            messagebox.showinfo(
                "Логи проблем",
                "Очистка старых логов завершена.\n\n"
                f"Удалено объектов: {stats.get('deleted', 0)}\n"
                f"Освобождено: {stats.get('freed_mb', 0.0):.2f} МБ\n"
                f"Оставлено объектов: {stats.get('kept', 0)}"
            )
        except Exception as exc:
            messagebox.showerror("Логи проблем", f"Не удалось очистить старые логи:\n{exc}")

    def delete_all_problem_logs_prompt(self):
        try:
            if not LOGS_DIR.exists():
                messagebox.showinfo("Логи проблем", "Папка логов пока не создана.")
                return
            if not messagebox.askyesno(
                "Удалить все логи?",
                "Удалить все файлы и папки внутри «Логи проблем»?\n\n"
                "Текущая активная запись, если она есть, не удаляется."
            ):
                return
            stats = self.cleanup_problem_logs(reason="manual_delete_all", delete_all=True)
            self.update_problem_logs_status_text()
            messagebox.showinfo(
                "Логи проблем",
                f"Готово. Удалено объектов: {stats.get('deleted', 0)}. Освобождено: {stats.get('freed_mb', 0.0):.2f} МБ."
            )
        except Exception as exc:
            messagebox.showerror("Логи проблем", f"Не удалось удалить логи:\n{exc}")

    def _path_size_bytes(self, path):
        try:
            path = Path(path)
            if path.is_file():
                return path.stat().st_size
            total = 0
            if path.is_dir():
                for child in path.rglob("*"):
                    try:
                        if child.is_file():
                            total += child.stat().st_size
                    except Exception:
                        pass
            return total
        except Exception:
            return 0

    def _problem_log_item_age_days(self, path):
        """Возраст лог-объекта в днях: сначала дата из имени, потом mtime."""
        try:
            name = Path(path).name
            match = re.search(r"(\d{4}-\d{2}-\d{2})[_ -](\d{2})-(\d{2})-(\d{2})", name)
            if match:
                dt = datetime.strptime(
                    f"{match.group(1)} {match.group(2)}:{match.group(3)}:{match.group(4)}",
                    "%Y-%m-%d %H:%M:%S",
                )
                return max(0.0, (datetime.now() - dt).total_seconds() / 86400.0)
        except Exception:
            pass
        try:
            return max(0.0, (time.time() - Path(path).stat().st_mtime) / 86400.0)
        except Exception:
            return 0.0

    def _problem_log_folder_has_errors(self, folder):
        """Определяет, относится ли папка логов к ошибочной записи."""
        try:
            folder = Path(folder)
            error_file = folder / "04_ошибки_и_трейсы.txt"
            if error_file.exists() and error_file.stat().st_size > 220:
                text = error_file.read_text(encoding="utf-8", errors="ignore")[:6000].lower()
                if any(k in text for k in ("traceback", "exception", "error", "ошибка", "не удалось", "failed", "timeout")):
                    return True
            summary = folder / "00_прочитать_нейросети_сначала.txt"
            if summary.exists():
                text = summary.read_text(encoding="utf-8", errors="ignore")[:6000].lower()
                if any(k in text for k in ("ошибка сохранения", "error_text:", "не удалось", "exception", "failed", "timeout")):
                    # Успешные записи тоже содержат строку error_text: пустую,
                    # поэтому проверяем не только наличие поля, а реальные слова ошибки.
                    if "error_text: " in text and "error_text: \n" in text:
                        return False
                    return True
            events = folder / "01_события_записи.jsonl"
            if events.exists():
                text = events.read_text(encoding="utf-8", errors="ignore")[:12000].lower()
                if '"level":"error"' in text or '"level":"warn"' in text:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _is_owned_problem_log_item(path):
        """Разрешает автоочистке только имена, создаваемые этой программой."""
        path = Path(path)
        name = path.name
        try:
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                return False
        except Exception:
            return False
        if path.is_dir():
            return bool(re.fullmatch(
                r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?: \(\d+\))?",
                name,
            ))
        if name in {"diagnostic_latest.txt", "recording_log.txt"}:
            return True
        return bool(re.fullmatch(
            r"diagnostic_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{3}_pid\d+\.txt",
            name,
        ))

    def cleanup_problem_logs(self, reason="manual", delete_all=False):
        """Удаляет старые логи по настройкам хранения.

        Обычные успешные логи и логи с ошибками могут жить разное число дней.
        0 дней в настройке означает «не удалять по возрасту».
        """
        stats = {
            "reason": reason,
            "delete_all": bool(delete_all),
            "deleted": 0,
            "kept": 0,
            "skipped_unrecognized": 0,
            "skipped_unrecognized_names": [],
            "errors": 0,
            "error_details": [],
            "freed_bytes": 0,
            "freed_mb": 0.0,
        }
        try:
            if not LOGS_DIR.exists():
                return stats
            current = None
            try:
                if self.current_session_log_dir:
                    current = Path(self.current_session_log_dir).resolve(strict=False)
            except Exception:
                current = None

            normal_days = self.get_problem_logs_retention_days()
            error_days = self.get_problem_logs_error_retention_days()
            stats["normal_retention_days"] = normal_days
            stats["error_retention_days"] = error_days

            for item in list(LOGS_DIR.iterdir()):
                try:
                    path = Path(item)
                    try:
                        if current and path.resolve(strict=False) == current:
                            stats["kept"] += 1
                            continue
                    except Exception:
                        pass

                    if not delete_all:
                        if not self._is_owned_problem_log_item(path):
                            stats["kept"] += 1
                            stats["skipped_unrecognized"] += 1
                            if len(stats["skipped_unrecognized_names"]) < 20:
                                stats["skipped_unrecognized_names"].append(path.name)
                            continue
                        if path.name == "diagnostic_latest.txt":
                            stats["kept"] += 1
                            continue
                        is_error_log = path.is_dir() and self._problem_log_folder_has_errors(path)
                        days_limit = error_days if is_error_log else normal_days
                        if days_limit <= 0:
                            stats["kept"] += 1
                            continue
                        age_days = self._problem_log_item_age_days(path)
                        if age_days <= days_limit:
                            stats["kept"] += 1
                            continue

                    size = self._path_size_bytes(path)
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                    if path.exists():
                        raise OSError(f"Объект лога остался после удаления: {path}")
                    stats["deleted"] += 1
                    stats["freed_bytes"] += size
                except Exception as item_exc:
                    stats["errors"] += 1
                    if len(stats["error_details"]) < 20:
                        stats["error_details"].append({
                            "path": str(item),
                            "error": repr(item_exc),
                        })
            stats["freed_mb"] = stats["freed_bytes"] / (1024 * 1024)
            self.diagnostic_log("cleanup_problem_logs_finish", stats, level="INFO" if not stats["errors"] else "WARN")
        except Exception as exc:
            stats["errors"] += 1
            try:
                self.diagnostic_log("cleanup_problem_logs_failed", {"error": repr(exc), "stats": stats}, level="WARN")
            except Exception:
                pass
        return stats

    def maybe_delete_successful_session_logs(self):
        """Удаляет папку логов успешной записи, если пользователь так настроил."""
        try:
            if self.keep_successful_problem_logs():
                return False
            folder = self.current_session_log_dir
            if not folder:
                return False
            folder = Path(folder)
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
            self.current_session_log_dir = None
            self.session_summary_path = None
            self.session_events_path = None
            self.session_ffmpeg_path = None
            self.session_errors_path = None
            self.session_settings_path = None
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
            self.current_log_path = None
            self.last_debug_log_path = None
            self.update_problem_logs_status_text()
            return True
        except Exception:
            return False

    def setup_diagnostic_logging(self):
        """Создаёт компактный общий лог запуска в папке «Логи проблем»."""
        try:
            if getattr(self, "_diagnostic_logging_initialized", False):
                return
            self._diagnostic_logging_initialized = True
            # На самом раннем старте self.settings ещё не загружен, поэтому
            # читаем settings.json напрямую. Если пользователь выключил логи,
            # общий diagnostic_*.txt тоже не создаём.
            try:
                if SETTINGS_PATH.exists():
                    raw_settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                    if raw_settings.get("problem_logs_enabled") is False:
                        self.diagnostic_log_paths = []
                        return
            except Exception:
                pass
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            latest = LOGS_DIR / "diagnostic_latest.txt"
            archived = LOGS_DIR / f"diagnostic_{self.diagnostic_session_id}.txt"
            # Полный лог храним только один раз. Раньше latest и archived были
            # идентичными копиями и удваивали вес каждой сессии.
            self.diagnostic_log_paths = [archived]
            # В v9 текстовый header строился выражением с соседними строковыми
            # литералами и `* 90`; из-за приоритета операций весь блок мог
            # повториться десятки раз. Теперь файл от первой строки — чистый JSONL.
            first_entry = {
                "time": datetime.now().isoformat(timespec="milliseconds"),
                "level": "INFO",
                "uptime_sec": 0.0,
                "thread": threading.current_thread().name,
                "event": "diagnostic_session_start",
                "data": {
                    "session_id": self.diagnostic_session_id,
                    "app_name": APP_NAME,
                    "app_build": APP_BUILD,
                    "diagnostic_schema": DIAGNOSTIC_SCHEMA,
                    "started_from_windows_startup": getattr(self, "started_from_windows_startup", False),
                    "arguments": [str(arg) for arg in sys.argv[1:]],
                    "log_folder": str(LOGS_DIR),
                    "problem_logs_folder_name": PROBLEM_LOGS_FOLDER_NAME,
                    "ai_hint": (
                        "Сначала ERROR/WARN, затем hotkey_*, screenshot_*, capture_region_* "
                        "и последние события перед сбоем."
                    ),
                },
            }
            with open(archived, "w", encoding="utf-8", errors="ignore") as log:
                log.write(json.dumps(first_entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            latest.write_text(
                "SCREEN RECORDER PRO — УКАЗАТЕЛЬ ПОСЛЕДНЕГО ДИАГНОСТИЧЕСКОГО ЛОГА\n"
                f"current_log={archived.name}\n"
                f"session_id={self.diagnostic_session_id}\n"
                f"app_build={APP_BUILD}\n"
                "Полный лог хранится только в current_log, чтобы не удваивать размер папки.\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def safe_log_folder_name(value):
        """Безопасное имя папки/файла для Windows с сохранением даты и смысла."""
        try:
            text = str(value or "").strip()
            text = re.sub(r'[<>:"/\\|?*]+', "-", text)
            text = re.sub(r"\s+", " ", text).strip(" .")
            return text[:150] or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        except Exception:
            return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    def create_recording_problem_log_folder(self):
        """Создаёт папку логов конкретной записи и базовые файлы для нейросети."""
        if not self.should_write_problem_logs():
            self.current_session_log_dir = None
            self.session_summary_path = None
            self.session_events_path = None
            self.session_ffmpeg_path = None
            self.current_log_path = None
            self.session_errors_path = None
            self.session_settings_path = None
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
            return None
        base_name = self.safe_log_folder_name(self.recording_session_id or datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
        root = LOGS_DIR
        root.mkdir(parents=True, exist_ok=True)
        folder = root / base_name
        counter = 2
        while folder.exists():
            folder = root / f"{base_name} ({counter})"
            counter += 1
        folder.mkdir(parents=True, exist_ok=True)

        self.current_session_log_dir = folder
        self.session_summary_path = folder / "00_прочитать_нейросети_сначала.txt"
        self.session_events_path = folder / "01_события_записи.jsonl"
        self.session_ffmpeg_path = folder / "02_ffmpeg_команды_и_вывод.txt"
        self.current_log_path = folder / "03_подробный_лог_записи.txt"
        self.session_errors_path = folder / "04_ошибки_и_трейсы.txt"
        self.session_settings_path = folder / "05_настройки_и_окружение.json"
        self.session_ai_smoothness_path = folder / "06_отчет_плавности_для_ChatGPT_5.6_SOL.json"
        self.session_ffmpeg_progress_path = folder / "07_ffmpeg_progress_по_времени.jsonl"
        self.session_performance_path = folder / "08_нагрузка_системы_по_времени.jsonl"
        self.session_frame_content_path = folder / "09_анализ_одинаковых_кадров.json"
        self.session_auto_stutter_path = folder / "10_автоматические_кандидаты_рывков.json"
        self.session_source_manifest_path = folder / "11_версия_исходников.json"
        self.session_source_snapshot_path = folder / "11_исходный_код_только_при_ошибке.py"
        self.session_ai_prompt_path = folder / "12_готовый_промпт_для_ChatGPT_5.6_SOL.txt"
        self.session_clock_alignment_path = folder / "13_сводка_времени_и_дрейфа.json"
        self.session_timing_detail_path = folder / "14_детальный_тайминг_видео.json"
        self._session_events_truncated = False
        self._session_ffmpeg_truncated = False
        self._session_errors_truncated = False
        self._source_snapshot_written_for_error = False

        try:
            snapshot = {
                "purpose": "Снимок настроек и окружения на момент старта записи. Передавать нейросети вместе с остальными логами.",
                "recording_session_id": self.recording_session_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "runtime": self.collect_basic_runtime_info(),
                "settings": self.collect_settings_snapshot(),
                "audio_devices": {
                    "dshow": getattr(self, "dshow_audio_devices", []),
                    "wasapi_capture": getattr(self, "wasapi_capture_devices", []),
                    "wasapi_render": getattr(self, "wasapi_render_devices", []),
                    "mic_choices": getattr(self, "mic_audio_devices", []),
                    "system_choices": getattr(self, "system_audio_devices", []),
                },
                "webcam_devices": getattr(self, "webcam_devices", []),
                "monitor_count": detect_monitor_count(),
                "primary_refresh_hz": detect_primary_refresh_hz(),
                "smoothness_environment": self.collect_smoothness_environment_snapshot(),
            }
            self.session_settings_path.write_text(
                json.dumps(self._safe_log_value(snapshot, max_text=4000), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

        try:
            self.session_events_path.write_text("", encoding="utf-8")
            self.session_ffmpeg_path.write_text(
                "FFmpeg/FFprobe команды и важный вывод. Длинный вывод автоматически обрезается, чтобы логи не разрастались.\n\n",
                encoding="utf-8",
            )
            self.session_errors_path.write_text(
                "Ошибки, исключения и предупреждения, которые важны для исправления программы.\n\n",
                encoding="utf-8",
            )
            self.session_ffmpeg_progress_path.write_text("", encoding="utf-8")
            self.session_performance_path.write_text("", encoding="utf-8")
            self.session_auto_stutter_path.write_text(
                json.dumps({
                    "status": "pending",
                    "purpose": (
                        "После сохранения здесь появятся автоматически найденные кандидаты "
                        "на замирания изображения внутри движущихся участков."
                    ),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.session_frame_content_path.write_text(
                json.dumps({
                    "status": "pending",
                    "purpose": "После сохранения здесь появится анализ визуально одинаковых кадров.",
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.session_ai_smoothness_path.write_text(
                json.dumps({
                    "schema": DIAGNOSTIC_SCHEMA,
                    "status": "recording_in_progress",
                    "ai_target": "ChatGPT 5.6 Thinking / SOL",
                    "app_build": APP_BUILD,
                    "timing_strategy": "ddagrab fixed monotonic PTS by frame number",
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.session_clock_alignment_path.write_text(
                json.dumps({
                    "status": "pending",
                    "purpose": (
                        "После сохранения здесь появится сравнение медиатаймлайна FFmpeg "
                        "с реальным временем по монотонным часам без смешивания со стартовой задержкой."
                    ),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.session_timing_detail_path.write_text(
                json.dumps({
                    "status": "pending",
                    "purpose": "После сохранения здесь появится полный timing summary без раздувания 01/diagnostic JSONL.",
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # В обычной успешной сессии сохраняем только SHA-256/размеры исходников.
            # Полный ограниченный snapshot создаётся лениво только при реальной ошибке.
            write_modular_source_manifest(self.session_source_manifest_path)
            self.session_ai_prompt_path.write_text(
                """Проанализируй папку «Логи проблем» Screen Recorder Pro как инженер по Python/Windows/FFmpeg.

Главная цель: найти реальные баги, сбои, зависания, гонки запуска, проблемы горячих клавиш,
записи/сохранения и места, где программу можно сделать надёжнее. Не ограничивай анализ только плавностью видео.

Порядок анализа:
1. Сначала прочитай 00_прочитать_нейросети_сначала.txt.
2. Затем 04_ошибки_и_трейсы.txt и 01_события_записи.jsonl.
3. Проверь 05_настройки_и_окружение.json и последовательность событий перед проблемой.
4. Для проблем FFmpeg используй 02 и 03. Для рывков/плавности дополнительно используй 06–10, 13 и 14.
5. Файл 11_версия_исходников.json — SHA-256 и размеры файлов конкретной версии.
   11_исходный_код_только_при_ошибке.py появляется только если в сессии была реальная ошибка.

Для каждой найденной проблемы дай:
- симптом;
- конкретные строки/события лога, которые это подтверждают;
- наиболее вероятную первопричину;
- уверенность 0–100%;
- какие данные опровергают альтернативные причины;
- минимальное безопасное исправление;
- какие файлы/функции программы надо проверить или изменить;
- как проверить исправление после правки.

Особые правила:
- ERROR/WARN важнее повторяющихся INFO.
- repeated_events_suppressed_since_previous_entry означает, что одинаковые штатные события были агрегированы, а не потеряны.
- expected_device_probe=true — штатное перечисление устройств FFmpeg; его stderr специально не сохраняется целиком.
- Для скриншотов сопоставляй hotkey_registration_*, hotkey_callback_received, screenshot_requested,
  native_screenshot_hotkey_ready, native_screenshot_hotkey_failed, native_screenshot_hotkey_stop_result,
  screenshot_snapshot_started, screenshot_snapshot_ready или screenshot_snapshot_failed,
  capture_region_selector_opened, screenshot_annotation_tool_selected,
  screenshot_annotation_color_selected, screenshot_annotation_size_selected,
  screenshot_annotation_toolbar_moved, screenshot_annotation_added,
  screenshot_annotation_undone, screenshot_annotations_cleared,
  capture_region_selection_started, capture_region_selection_finished
  и screenshot_copied_to_clipboard. Исправный снимок временных меню имеет capture_stage=before_selector_focus,
  background_mode=frozen_snapshot_before_selector_focus и итоговый source=frozen_snapshot_before_selector_focus.
  Для собственной панели пометок ожидается annotation_backend=screenshot_canvas_v3;
  сравни annotation_count, annotation_colors и annotation_sizes при завершении выбора и после
  копирования в буфер. toolbar_position_source=settings подтверждает восстановление позиции.
  Для Print Screen на Windows ожидаемый screenshot_backend=windows_register_hotkey, native_registered=true
  и native_thread_alive=true. keyboard_fallback означает неуспешный RegisterHotKey; читай windows_error.
  Если регистрация есть, но callback отсутствует после реального
  нажатия — проблема до очереди GUI, в глобальном keyboard-hook/Windows. Если callback есть,
  точная причина выбора находится в status события capture_region_selection_finished.
- Не называй предположение доказанным фактом.
- Не предлагай большой рефакторинг, если проблему можно исправить локально.
- Если нужного участка исходников нет в ограниченном файле 11, укажи точный файл/функцию, которую надо запросить из архива программы.
""",
                encoding="utf-8",
            )
        except Exception:
            pass

        self.write_ai_problem_summary(outcome="Запись запущена, итоговый результат ещё неизвестен.")
        self.problem_log_event("recording_problem_log_folder_created", {
            "folder": folder,
            "summary_file": self.session_summary_path,
            "events_file": self.session_events_path,
            "ffmpeg_file": self.session_ffmpeg_path,
            "recording_log_file": self.current_log_path,
            "errors_file": self.session_errors_path,
            "settings_file": self.session_settings_path,
            "ai_smoothness_report": self.session_ai_smoothness_path,
            "ffmpeg_progress_file": self.session_ffmpeg_progress_path,
            "performance_file": self.session_performance_path,
            "frame_content_file": self.session_frame_content_path,
            "automatic_stutter_candidates_file": self.session_auto_stutter_path,
            "source_manifest_file": self.session_source_manifest_path,
            "source_snapshot_file": self.session_source_snapshot_path,
            "ai_prompt_file": self.session_ai_prompt_path,
            "clock_alignment_file": self.session_clock_alignment_path,
            "timing_detail_file": self.session_timing_detail_path,
        })
        return folder

    def append_limited_text_file(self, path, text, max_bytes=None, truncated_attr=None):
        """Дописывает текст с ограничением размера и сохранением свежего хвоста.

        Для потоковых логов (01/03/07/08) после достижения лимита больше не
        прекращаем запись навсегда: оставляем короткое начало + свежий хвост и
        продолжаем писать. Так длинная запись не теряет последние секунды перед
        ошибкой — обычно именно они важнее всего для диагностики.
        """
        try:
            if not self.should_write_problem_logs():
                return False
            if not path:
                return False
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            limit = int(max_bytes or self.get_problem_log_file_limit_bytes())
            # Разным типам логов нужен разный объём. Ограничиваем самые шумные
            # технические потоки жёстче, сохраняя больший запас для ошибок.
            name = path.name.lower()
            smart_caps = {
                "01_": 450_000,
                "02_": 550_000,
                "03_": 650_000,
                "04_": 700_000,
                "07_": 500_000,
                "08_": 500_000,
            }
            for prefix, cap in smart_caps.items():
                if name.startswith(prefix):
                    limit = min(limit, cap)
                    break
            with getattr(self, "_problem_log_file_lock", threading.RLock()):
                incoming = str(text)
                current_size = path.stat().st_size if path.exists() else 0
                incoming_size = len(incoming.encode("utf-8", errors="ignore"))
                stream_log = name.startswith(("01_", "03_", "07_", "08_"))
                if current_size + incoming_size > limit:
                    if stream_log and path.exists():
                        # Компактация идёт по целым UTF-8 строкам. Для JSONL это
                        # принципиально: нельзя обрезать строку посередине и тем
                        # самым делать файл нечитаемым для анализатора/ChatGPT.
                        existing_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
                        head_budget = max(4096, int(limit * 0.10))
                        tail_budget = max(16_384, int(limit * 0.58))

                        head_lines = []
                        head_size = 0
                        for line in existing_lines:
                            size = len(line.encode("utf-8", errors="ignore"))
                            if head_lines and head_size + size > head_budget:
                                break
                            head_lines.append(line)
                            head_size += size

                        tail_lines_rev = []
                        tail_size = 0
                        for line in reversed(existing_lines):
                            size = len(line.encode("utf-8", errors="ignore"))
                            if tail_lines_rev and tail_size + size > tail_budget:
                                break
                            tail_lines_rev.append(line)
                            tail_size += size
                        tail_lines = list(reversed(tail_lines_rev))

                        if name.startswith(("01_", "07_", "08_")):
                            marker = json.dumps({
                                "event": "stream_log_compacted",
                                "level": "INFO",
                                "reason": "file_size_limit",
                                "limit_bytes": limit,
                                "note_for_ai": (
                                    "Средняя штатная часть потока удалена; сохранены начало и свежий хвост. "
                                    "Это штатная экономия размера, а не потеря из-за сбоя."
                                ),
                            }, ensure_ascii=False, separators=(",", ":")) + "\n"
                        else:
                            marker = (
                                "\n--- STREAM LOG COMPACTED ---\n"
                                f"Предыдущая середина удалена после достижения лимита {limit} байт; "
                                "сохранены начало и последние события, запись лога продолжается.\n"
                            )

                        with open(path, "w", encoding="utf-8", errors="ignore") as log:
                            log.writelines(head_lines)
                            if head_lines and not head_lines[-1].endswith("\n"):
                                log.write("\n")
                            log.write(marker)
                            log.writelines(tail_lines)
                            if tail_lines and not tail_lines[-1].endswith("\n"):
                                log.write("\n")
                        if truncated_attr:
                            setattr(self, truncated_attr, True)
                    else:
                        if truncated_attr and not getattr(self, truncated_attr, False):
                            setattr(self, truncated_attr, True)
                            with open(path, "a", encoding="utf-8", errors="ignore") as log:
                                log.write("\n--- ЛОГ ДОСТИГ ЛИМИТА ---\n")
                                log.write(
                                    f"Файл ограничен {limit} байт. Новые обычные записи не добавляются; "
                                    "ошибки сохраняются в отдельном 04-файле.\n"
                                )
                        return False
                with open(path, "a", encoding="utf-8", errors="ignore") as log:
                    log.write(incoming)
            return True
        except Exception:
            return False

    def ensure_error_source_snapshot(self):
        """Создаёт ограниченный snapshot кода один раз и только при реальной ошибке."""
        try:
            if getattr(self, "_source_snapshot_written_for_error", False):
                return getattr(self, "session_source_snapshot_path", None)
            path = getattr(self, "session_source_snapshot_path", None)
            if not path or not self.should_write_problem_logs():
                return None
            self._source_snapshot_written_for_error = True
            write_modular_source_snapshot(path, max_bytes=300_000)
            self.problem_log_event("source_snapshot_created_for_error", {
                "path": path,
                "max_bytes": 300_000,
            }, level="INFO")
            return path
        except Exception:
            return None

    def problem_log_event(self, event, data=None, level="INFO"):
        """Компактный JSONL-журнал событий записи, удобный для анализа нейросетью."""
        try:
            if not self.should_write_problem_logs():
                return
            if not getattr(self, "session_events_path", None):
                return
            now = datetime.now().isoformat(timespec="milliseconds")
            try:
                uptime = round(time.perf_counter() - float(getattr(self, "diagnostic_started_perf", time.perf_counter())), 3)
            except Exception:
                uptime = 0.0
            entry = {
                "time": now,
                "uptime_sec": uptime,
                "level": str(level).upper(),
                "event": str(event),
                "thread": threading.current_thread().name,
                "recording_session_id": getattr(self, "recording_session_id", None),
            }
            if data is not None:
                entry["data"] = self._safe_log_value(data, max_text=2500)
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            self.append_limited_text_file(self.session_events_path, line, truncated_attr="_session_events_truncated")
        except Exception:
            pass

    def append_problem_error(self, context, details=None):
        """Отдельный файл только с ошибками/трейсами, чтобы нейросеть быстро нашла проблему."""
        try:
            if not self.should_write_problem_logs():
                return
            if not getattr(self, "session_errors_path", None):
                return
            text = (
                "\n" + "=" * 70 + "\n"
                f"time={datetime.now().isoformat(timespec='seconds')}\n"
                f"context={context}\n"
                f"recording_session_id={getattr(self, 'recording_session_id', None)}\n"
                f"details:\n{self._safe_log_value(details or '', max_text=8000)}\n"
            )
            self.append_limited_text_file(self.session_errors_path, text, truncated_attr="_session_errors_truncated")
        except Exception:
            pass

    def is_ffmpeg_related_command(self, command):
        try:
            if isinstance(command, (list, tuple)) and command:
                name = Path(str(command[0])).name.lower()
                return name in ("ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe") or "ffmpeg" in name or "ffprobe" in name
            low = str(command).lower()
            return "ffmpeg" in low or "ffprobe" in low
        except Exception:
            return False

    def is_expected_ffmpeg_device_probe_command(self, command):
        """True для штатного перечисления устройств FFmpeg через dummy input.

        FFmpeg при `-list_devices true -i dummy` почти всегда пишет строки вида
        `Error opening input file dummy`. Это не сбой записи, а нормальный способ
        завершить команду перечисления устройств. Помечаем такие команды отдельно,
        чтобы логи проблем не пугали нейросеть ложной ошибкой.
        """
        try:
            if not isinstance(command, (list, tuple)):
                parts = str(command).split()
            else:
                parts = [str(item) for item in command]
            low_parts = [part.lower() for part in parts]
            text = " ".join(low_parts)
            return (
                "-list_devices" in low_parts
                and "true" in low_parts
                and "dummy" in low_parts
                and ("-f dshow" in text or "-f wasapi" in text or "dshow" in low_parts or "wasapi" in low_parts)
            )
        except Exception:
            return False

    def expected_ffmpeg_probe_note(self, command=None):
        return (
            "Это штатная проверка/перечисление аудио- или видео-устройств FFmpeg. "
            "Строки про 'dummy' и 'Error opening input file dummy' ожидаемы для `-list_devices true`; "
            "они не означают проблему записи и не должны считаться ошибкой сохранения видео."
        )

    def normalize_expected_ffmpeg_probe_stderr(self, stderr):
        """Добавляет понятное пояснение к stderr от FFmpeg device probe.

        Сами строки устройств оставляем, потому что они полезны для диагностики.
        Но в начало добавляем классификацию, чтобы следующий раз нейросеть сразу
        понимала: это ожидаемый вывод проверки устройств, а не падение записи.
        """
        if not stderr:
            return stderr
        try:
            text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, (bytes, bytearray)) else str(stderr)
        except Exception:
            text = str(stderr)
        marker = "[ОЖИДАЕМЫЙ ВЫВОД ПРОВЕРКИ УСТРОЙСТВ]"
        if marker in text:
            return text
        return marker + "\n" + self.expected_ffmpeg_probe_note() + "\n\n" + text

    def append_ffmpeg_problem_log(self, title, command=None, stdout=None, stderr=None, extra=None):
        """Отдельный компактный лог FFmpeg/FFprobe без повторов больших команд/проб."""
        try:
            if not self.should_write_problem_logs():
                return
            if not getattr(self, "session_ffmpeg_path", None):
                return

            expected_device_probe = self.is_expected_ffmpeg_device_probe_command(command) if command is not None else False
            extra = dict(extra) if isinstance(extra, dict) else ({} if extra is None else {"details": extra})

            # `-list_devices true` возвращает большой stderr со списком устройств и
            # ожидаемой строкой про dummy. Полный список уже попадает в событие
            # audio_devices_refreshed, поэтому второй раз хранить его в 02-файле нет смысла.
            if expected_device_probe:
                extra.setdefault("expected_device_probe", True)
                extra.setdefault("note_for_ai", self.expected_ffmpeg_probe_note(command))
                extra.setdefault("raw_probe_output_omitted", True)
                stdout = None
                stderr = None

            command_text = self.command_to_log_text(command) if command is not None else None
            now_perf = time.perf_counter()
            duplicate_command = False
            if command_text:
                previous = getattr(self, "_ffmpeg_problem_last_command", None)
                previous_perf = getattr(self, "_ffmpeg_problem_last_command_perf", 0.0) or 0.0
                duplicate_command = bool(previous == command_text and (now_perf - previous_perf) <= 2.0)
                if not duplicate_command:
                    self._ffmpeg_problem_last_command = command_text
                    self._ffmpeg_problem_last_command_perf = now_perf
                else:
                    extra.setdefault("command_deduplicated", True)
                    extra.setdefault("command_reference", "same_as_previous_ffmpeg_entry")

            chunks = ["\n" + "=" * 80 + "\n", f"{datetime.now().isoformat(timespec='seconds')} | {title}\n"]
            if expected_device_probe:
                chunks.append("classification: EXPECTED_DEVICE_PROBE_NOT_RECORDING_ERROR\n")
                chunks.append(self.expected_ffmpeg_probe_note(command) + "\n")
            if command_text:
                chunks.append("command:\n")
                if duplicate_command:
                    chunks.append("<та же команда, что в предыдущей FFmpeg-записи; повтор удалён>\n")
                else:
                    chunks.append(command_text + "\n")
            if extra:
                chunks.append("extra:\n")
                chunks.append(json.dumps(self._safe_log_value(extra, max_text=4000), ensure_ascii=False, indent=2) + "\n")
            if stdout:
                chunks.append("stdout_truncated:\n")
                chunks.append(str(self._safe_log_value(stdout, max_text=4000)) + "\n")
            if stderr:
                chunks.append("stderr_truncated:\n")
                chunks.append(str(self._safe_log_value(stderr, max_text=4000)) + "\n")
            self.append_limited_text_file(self.session_ffmpeg_path, "".join(chunks), truncated_attr="_session_ffmpeg_truncated")
        except Exception:
            pass

    def write_ai_problem_summary(self, outcome=None, error_text=None):
        """Пишет короткую AI-карту сессии без копирования больших отчётов."""
        try:
            path = getattr(self, "session_summary_path", None)
            if not path:
                return

            output = getattr(self, "output_path", None)
            output_exists = bool(output and Path(output).exists())
            try:
                output_size = Path(output).stat().st_size if output_exists else None
            except Exception:
                output_size = None

            timing = getattr(self, "last_video_timing_summary", None) or {}
            clock_alignment = self.summarize_capture_clock_alignment() or {}
            progress = self.summarize_ffmpeg_progress() or {}
            performance = self.summarize_performance_samples() or {}
            frame_content = getattr(self, "last_frame_content_analysis", None) or {}
            ai_report = getattr(self, "last_ai_smoothness_report", None) or {}
            verdict = ai_report.get("automatic_smoothness_verdict") or {}

            # Считаем реальные записи из 04, а не повторяем весь файл в сводке.
            error_sections = 0
            try:
                error_path = getattr(self, "session_errors_path", None)
                if error_path and Path(error_path).exists():
                    error_text_file = Path(error_path).read_text(encoding="utf-8", errors="ignore")
                    error_sections = error_text_file.count("context=")
            except Exception:
                error_sections = 0

            # Последние ключевые события дают нейросети быстрый timeline без копии 01.
            recent_events = []
            try:
                events_path = getattr(self, "session_events_path", None)
                if events_path and Path(events_path).exists():
                    lines = Path(events_path).read_text(encoding="utf-8", errors="ignore").splitlines()[-80:]
                    important_tokens = (
                        "record", "ffmpeg", "save", "stop", "error", "exception", "hotkey",
                        "screenshot", "audio", "timing", "final", "process", "startup",
                    )
                    for line in lines:
                        try:
                            item = json.loads(line)
                        except Exception:
                            continue
                        event = str(item.get("event") or "")
                        level = str(item.get("level") or "INFO").upper()
                        if level in {"WARN", "ERROR", "CRITICAL"} or any(token in event.lower() for token in important_tokens):
                            recent_events.append({
                                "time": item.get("time"),
                                "level": level,
                                "event": event,
                            })
                    recent_events = recent_events[-16:]
            except Exception:
                recent_events = []

            def stat_max(key):
                try:
                    return self._summary_stat_max(performance, key)
                except Exception:
                    return None

            candidate_count = len(list(frame_content.get("suspected_freeze_candidates") or []))
            low_cadence_count = int(
                ((frame_content.get("moving_content_cadence_analysis") or {}).get("low_cadence_window_count")) or 0
            )
            snapshot_exists = bool(
                getattr(self, "session_source_snapshot_path", None)
                and Path(self.session_source_snapshot_path).exists()
            )

            summary = {
                "session": {
                    "recording_session_id": getattr(self, "recording_session_id", None),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "outcome": outcome or "pending",
                    "error_text": error_text or "",
                    "app_build": APP_BUILD,
                    "diagnostic_schema": DIAGNOSTIC_SCHEMA,
                    "capture_backend": getattr(self, "recording_capture_backend", None),
                    "requested_fps": getattr(self, "recording_requested_fps", None),
                    "target_fps": getattr(self, "recording_effective_fps", None),
                    "monitor_refresh_hz": getattr(self, "recording_refresh_hz", None),
                },
                "result": {
                    "output_path": str(output) if output else None,
                    "output_exists": output_exists,
                    "output_size_bytes": output_size,
                    "recorded_media_seconds": getattr(self, "recorded_seconds", None),
                    "recorded_wall_seconds": getattr(self, "recorded_wall_seconds", None),
                },
                "health": {
                    "error_or_warning_sections_in_04": error_sections,
                    "automatic_status": verdict.get("status") or ai_report.get("outcome"),
                    "timing_status": (timing.get("timing_health") or {}).get("status"),
                    "clock_alignment_status": clock_alignment.get("status"),
                    "effective_fps": timing.get("reported_avg_frame_rate"),
                    "ffmpeg_dup": progress.get("total_dup_frames_across_segments"),
                    "ffmpeg_drop": progress.get("total_drop_frames_across_segments"),
                    "progress_stalls": progress.get("possible_progress_stalls_count"),
                    "steady_speed_median": progress.get("steady_speed_median_after_3s"),
                    "cpu_max_percent": stat_max("system_cpu_percent"),
                    "ram_max_percent": stat_max("memory_percent"),
                    "gpu_max_percent": stat_max("nvidia_gpu_util_percent"),
                    "nvenc_max_percent": stat_max("nvidia_encoder_util_percent"),
                    "visual_freeze_candidate_count": candidate_count,
                    "continuous_motion_low_cadence_window_count": low_cadence_count,
                    "post_visual_analysis_status": frame_content.get("status") or "pending",
                },
                "important_recent_events": recent_events,
                "source_version": {
                    "manifest": str(getattr(self, "session_source_manifest_path", None) or ""),
                    "full_snapshot_created_because_of_error": snapshot_exists,
                    "error_snapshot": str(getattr(self, "session_source_snapshot_path", None) or "") if snapshot_exists else None,
                },
            }

            text = (
                "AI-ДИАГНОСТИКА SCREEN RECORDER PRO — КОРОТКАЯ КАРТА СЕССИИ\n\n"
                "Эта сводка специально маленькая. Не ищи здесь все подробности: она показывает, куда смотреть.\n"
                "Порядок: 04 ошибки → 01 timeline → 05 окружение. Для видео: 06/13 → 07/08 → 09/10.\n"
                "11_версия_исходников.json содержит SHA-256 файлов; полный код в 11_*_только_при_ошибке.py появляется только при ERROR.\n\n"
                + json.dumps(summary, ensure_ascii=False, indent=2)
                + "\n\nПРАВИЛА ДЛЯ CHATGPT:\n"
                "- Отделяй подтверждённый факт от гипотезы и указывай уверенность.\n"
                "- Не считай статичный экран рывком без движения/технического совпадения.\n"
                "- low cadence считается предупреждением только для окон с непрерывным движением >=50% переходов.\n"
                "- Если нужен код, сверяй APP_BUILD/SHA-256 с архивом программы; не предполагай, что manifest содержит сам код.\n"
                "- Предлагай минимальную правку и конкретный способ проверки.\n"
            )
            Path(path).write_text(text, encoding="utf-8", errors="ignore")
        except Exception:
            pass

    def _safe_log_value(self, value, depth=0, max_text=8000):
        if depth > 5:
            return "<max-depth>"
        try:
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, (str, int, float, bool)) or value is None:
                text = str(value) if isinstance(value, str) else value
                if isinstance(text, str) and len(text) > max_text:
                    return text[: max_text // 2] + "\n...<truncated>...\n" + text[-max_text // 2 :]
                return text
            if isinstance(value, bytes):
                text = value.decode("utf-8", errors="replace")
                return self._safe_log_value(text, depth=depth + 1, max_text=max_text)
            if isinstance(value, dict):
                result = {}
                for index, (key, item) in enumerate(value.items()):
                    if index >= 120:
                        result["<truncated>"] = f"{len(value) - index} more keys"
                        break
                    result[str(key)] = self._safe_log_value(item, depth=depth + 1, max_text=max_text)
                return result
            if isinstance(value, (list, tuple, set)):
                items = list(value)
                result = [self._safe_log_value(item, depth=depth + 1, max_text=max_text) for item in items[:120]]
                if len(items) > 120:
                    result.append(f"<truncated: {len(items) - 120} more items>")
                return result
            return self._safe_log_value(repr(value), depth=depth + 1, max_text=max_text)
        except Exception:
            try:
                return repr(value)
            except Exception:
                return "<unrepresentable>"

    def command_to_log_text(self, command):
        try:
            if isinstance(command, (list, tuple)):
                return subprocess.list2cmdline([str(item) for item in command])
            return str(command)
        except Exception:
            return repr(command)

    def stream_to_log_text(self, stream):
        if stream is None:
            return "None"
        if stream is subprocess.PIPE:
            return "PIPE"
        if stream is subprocess.DEVNULL:
            return "DEVNULL"
        if stream is subprocess.STDOUT:
            return "STDOUT"
        try:
            name = getattr(stream, "name", None)
            if name:
                return f"file:{name}"
        except Exception:
            pass
        return type(stream).__name__

    def diagnostic_log(self, event, data=None, level="INFO"):
        """Компактный структурированный лог запуска, оптимизированный для AI."""
        try:
            if not self.should_write_problem_logs() or not getattr(self, "diagnostic_log_paths", None):
                return
            now_perf = time.perf_counter()
            try:
                uptime = now_perf - float(getattr(self, "diagnostic_started_perf", now_perf))
            except Exception:
                uptime = 0.0
            safe_data = self._safe_log_value(data, max_text=3000) if data is not None else None

            # Самые шумные штатные события не теряем полностью, а агрегируем.
            noisy_interval = None
            dedupe_key = None
            if str(level).upper() not in {"WARN", "ERROR", "CRITICAL"}:
                if event == "audio_devices_refreshed":
                    noisy_interval = 30.0
                    dedupe_key = json.dumps(safe_data, ensure_ascii=False, sort_keys=True, default=str)
                elif event in {"subprocess_run_start", "subprocess_run_finish"} and isinstance(safe_data, dict) and safe_data.get("expected_device_probe"):
                    noisy_interval = 30.0
                    dedupe_key = json.dumps({
                        "event": event,
                        "command": safe_data.get("command"),
                        "returncode": safe_data.get("returncode"),
                        "expected": safe_data.get("expected_returncode"),
                    }, ensure_ascii=False, sort_keys=True, default=str)
            if noisy_interval and dedupe_key is not None:
                state_map = getattr(self, "_diagnostic_noise_state", None)
                if not isinstance(state_map, dict):
                    state_map = {}
                    self._diagnostic_noise_state = state_map
                key = (event, dedupe_key)
                state = state_map.get(key)
                if state and now_perf - state.get("last_written", 0.0) < noisy_interval:
                    state["suppressed"] = int(state.get("suppressed", 0)) + 1
                    return
                suppressed = int(state.get("suppressed", 0)) if state else 0
                state_map[key] = {"last_written": now_perf, "suppressed": 0}
                if suppressed and isinstance(safe_data, dict):
                    safe_data = dict(safe_data)
                    safe_data["repeated_events_suppressed_since_previous_entry"] = suppressed

            entry = {
                "time": datetime.now().isoformat(timespec="milliseconds"),
                "level": str(level).upper(),
                "uptime_sec": round(uptime, 3),
                "thread": threading.current_thread().name,
                "event": str(event),
            }
            if safe_data is not None:
                entry["data"] = safe_data
            entry_text = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            with self.diagnostic_log_lock:
                for path in list(self.diagnostic_log_paths):
                    try:
                        path = Path(path)
                        limit = min(int(self.get_problem_log_file_limit_bytes()), 750_000)
                        if path.exists() and path.stat().st_size + len(entry_text.encode("utf-8")) >= limit:
                            # Общий diagnostic тоже должен сохранять конец долгой
                            # сессии. Переписываем его валидными JSONL-строками:
                            # стартовый контекст + свежий хвост + marker compaction.
                            try:
                                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
                                valid_lines = [line for line in lines if line.lstrip().startswith("{")]
                                head = valid_lines[:16]
                                tail_budget = max(48_000, int(limit * 0.58))
                                tail = []
                                used = 0
                                for line in reversed(valid_lines[16:]):
                                    size = len((line + "\n").encode("utf-8", errors="ignore"))
                                    if tail and used + size > tail_budget:
                                        break
                                    tail.append(line)
                                    used += size
                                tail.reverse()
                                marker = json.dumps({
                                    "time": datetime.now().isoformat(timespec="milliseconds"),
                                    "level": "INFO",
                                    "event": "diagnostic_log_compacted",
                                    "data": {
                                        "limit_bytes": limit,
                                        "preserved_head_lines": len(head),
                                        "preserved_tail_lines": len(tail),
                                        "note_for_ai": "Середина штатного diagnostic удалена, конец продолжает записываться.",
                                    },
                                }, ensure_ascii=False, separators=(",", ":"))
                                with open(path, "w", encoding="utf-8", errors="ignore") as log:
                                    for line in head:
                                        log.write(line + "\n")
                                    log.write(marker + "\n")
                                    for line in tail:
                                        log.write(line + "\n")
                            except Exception:
                                pass
                        with open(path, "a", encoding="utf-8", errors="ignore") as log:
                            log.write(entry_text)
                    except Exception:
                        pass
            try:
                self.problem_log_event(event, safe_data, level=level)
            except Exception:
                pass
        except Exception:
            pass

    def collect_basic_runtime_info(self):
        return {
            "app_name": APP_NAME,
            "app_build": APP_BUILD,
            "diagnostic_schema": DIAGNOSTIC_SCHEMA,
            "app_dir": APP_DIR,
            "data_dir": DATA_DIR,
            "settings_path": SETTINGS_PATH,
            "logs_dir": LOGS_DIR,
            "problem_logs_folder_name": PROBLEM_LOGS_FOLDER_NAME,
            "temp_recordings_dir": TEMP_RECORDINGS_DIR,
            "cwd": Path.cwd(),
            "python_executable": sys.executable,
            "python_version": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "os_name": os.name,
            "pid": os.getpid(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "ffmpeg_path": getattr(self, "ffmpeg_path", shutil.which("ffmpeg") or "ffmpeg"),
            "ffmpeg_in_path": shutil.which("ffmpeg"),
            "dxcam_available": DXCAM_AVAILABLE,
            "dxcam_capture_enabled": DXCAM_CAPTURE_ENABLED,
            "numpy_available": NUMPY_AVAILABLE,
            "psutil_available": PSUTIL_AVAILABLE,
            "pil_available": PIL_AVAILABLE,
            "screenshot_available": bool(PIL_AVAILABLE and ImageGrab is not None),
            "hotkey_available": HOTKEY_AVAILABLE,
            "tray_available": TRAY_AVAILABLE,
        }

    def collect_settings_snapshot(self):
        def safe_get(name, default=None):
            try:
                value = getattr(self, name)
                return value.get() if hasattr(value, "get") else value
            except Exception:
                return default

        raw_settings = getattr(self, "settings", {})
        if not isinstance(raw_settings, dict):
            raw_settings = {"<non_dict_settings>": raw_settings}

        return {
            "settings_file_exists": SETTINGS_PATH.exists(),
            "raw_settings_keys": sorted(list(raw_settings.keys())),
            "output_folder": safe_get("output_folder"),
            "format": safe_get("format_var"),
            "fps": safe_get("fps_var"),
            "auto_adjust_fps": safe_get("auto_adjust_fps_var"),
            "video_bitrate_mbps": safe_get("video_bitrate_var"),
            "capture_method": safe_get("capture_method_var"),
            "encoder": safe_get("encoder_var"),
            "mic_device": safe_get("mic_device_var"),
            "system_device": safe_get("system_device_var"),
            "mic_volume": safe_get("mic_volume_var"),
            "system_volume": safe_get("system_volume_var"),
            "audio_bitrate": safe_get("audio_bitrate_var"),
            "monitor_index": safe_get("monitor_index_var"),
            "auto_stop_minutes": safe_get("auto_stop_minutes_var"),
            "countdown_enabled": safe_get("countdown_enabled_var"),
            "show_keys_overlay": safe_get("show_keys_overlay_var"),
            "cursor_visible": safe_get("cursor_visible_var"),
            "cursor_highlight": safe_get("cursor_highlight_var"),
            "cursor_highlight_size": safe_get("cursor_highlight_size_var"),
            "open_folder_after_stop": safe_get("open_folder_after_stop_var"),
            "hotkey": safe_get("hotkey_var"),
            "screenshot_hotkey": safe_get("screenshot_hotkey_var"),
            "problem_logs_enabled": safe_get("problem_logs_enabled_var"),
            "problem_logs_retention_days": safe_get("problem_logs_retention_days_var"),
            "problem_logs_error_retention_days": safe_get("problem_logs_error_retention_days_var"),
            "problem_logs_max_file_mb": safe_get("problem_logs_max_file_mb_var"),
            "problem_logs_cleanup_on_start": safe_get("problem_logs_cleanup_on_start_var"),
            "problem_logs_keep_successful": safe_get("problem_logs_keep_successful_var"),
        }

    def log_startup_snapshot(self, stage):
        self.diagnostic_log(stage, {
            "runtime": self.collect_basic_runtime_info(),
            "settings": self.collect_settings_snapshot(),
            "audio_devices": {
                "dshow": getattr(self, "dshow_audio_devices", []),
                "wasapi_capture": getattr(self, "wasapi_capture_devices", []),
                "wasapi_render": getattr(self, "wasapi_render_devices", []),
                "mic_choices": getattr(self, "mic_audio_devices", []),
                "system_choices": getattr(self, "system_audio_devices", []),
            },
            "webcam_devices": getattr(self, "webcam_devices", []),
            "monitor_count": detect_monitor_count(),
            "primary_refresh_hz": detect_primary_refresh_hz(),
        })

    def install_exception_hooks(self):
        try:
            self._original_sys_excepthook = sys.excepthook

            def sys_hook(exc_type, exc, tb):
                self.log_uncaught_exception("sys.excepthook", exc_type, exc, tb)
                try:
                    if self._original_sys_excepthook and self._original_sys_excepthook is not sys_hook:
                        self._original_sys_excepthook(exc_type, exc, tb)
                except Exception:
                    pass

            sys.excepthook = sys_hook
        except Exception:
            pass

        try:
            self._original_threading_excepthook = getattr(threading, "excepthook", None)

            def thread_hook(args):
                self.log_uncaught_exception(
                    f"threading.excepthook thread={getattr(args.thread, 'name', None)}",
                    args.exc_type,
                    args.exc_value,
                    args.exc_traceback,
                )
                try:
                    if self._original_threading_excepthook and self._original_threading_excepthook is not thread_hook:
                        self._original_threading_excepthook(args)
                except Exception:
                    pass

            if hasattr(threading, "excepthook"):
                threading.excepthook = thread_hook
        except Exception:
            pass

        try:
            def tk_exception_hook(exc_type, exc, tb):
                self.log_uncaught_exception("tkinter_callback", exc_type, exc, tb)

            self.root.report_callback_exception = tk_exception_hook
        except Exception:
            pass

    def log_uncaught_exception(self, context, exc_type, exc, tb):
        try:
            details = "".join(traceback.format_exception(exc_type, exc, tb))
        except Exception:
            details = f"{exc_type}: {exc}"
        self.ensure_error_source_snapshot()
        self.diagnostic_log("uncaught_exception", {"context": context, "traceback": details}, level="ERROR")
        try:
            if self.log_handle:
                self.log_handle.write(f"\n--- UNCAUGHT ERROR: {context} ---\n{details}\n")
                self.log_handle.flush()
        except Exception:
            pass

    def embed_recording_log_in_diagnostics(self, log_path, max_chars=50000):
        """Оставляет в общем diagnostic только ссылку на лог записи.

        Раньше сюда встраивалось до десятков тысяч символов из 03-файла, хотя
        исходник уже лежит рядом. Это раздувало diagnostic и дублировало данные.
        """
        try:
            if not log_path:
                return
            path = Path(log_path)
            if not path.exists():
                return
            self.diagnostic_log("recording_log_reference", {
                "path": path,
                "size_bytes": path.stat().st_size,
                "content_embedded": False,
                "reason": "03-файл не дублируется в общем diagnostic; читать его напрямую при необходимости",
            })
        except Exception as exc:
            self.diagnostic_log("recording_log_reference_failed", {"path": str(log_path), "error": repr(exc)}, level="WARN")

    def log_message(self, message):
        """Безопасно пишет диагностическое сообщение в текущий и общий лог."""
        text = str(message).rstrip()
        wrote_current = False
        try:
            if self.log_handle:
                self.log_handle.write(text + "\n")
                self.log_handle.flush()
                wrote_current = True
        except Exception:
            pass
        try:
            self.diagnostic_log("message", {"text": text})
            low = text.lower()
            if any(marker in low for marker in ("error", "ошибка", "failed", "timeout", "warning", "warn", "не удалось")):
                self.append_problem_error("log_message", text)
        except Exception:
            pass
        if not wrote_current and self.should_write_problem_logs():
            try:
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                fallback = LOGS_DIR / "screen_recorder_runtime.log"
                with open(fallback, "a", encoding="utf-8", errors="ignore") as log:
                    log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [no-recording-log] {text}\n")
            except Exception:
                pass

    def log_exception(self, context, exc=None):
        """Пишет исключение в лог вместо полного молчания через except: pass."""
        try:
            self.ensure_error_source_snapshot()
            if exc is None:
                details = traceback.format_exc()
            else:
                if getattr(exc, "__traceback__", None):
                    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                else:
                    details = f"{type(exc).__name__}: {exc}"
            self.log_message(f"--- ERROR: {context} ---\n{details}")
            self.append_problem_error(context, details)
            self.diagnostic_log("exception", {"context": context, "traceback": details}, level="ERROR")
        except Exception:
            pass
