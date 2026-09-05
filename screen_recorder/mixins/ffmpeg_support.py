from ..shared import *


class FfmpegSupportMixin:
    @staticmethod
    def bitrate_to_bufsize(video_bitrate):
        return video_bitrate_to_bufsize(video_bitrate)

    def make_output_path_at_save_time(self):
        folder = Path(self.output_folder.get().strip() or os.getcwd())
        ext = self.format_var.get().lower().strip() or "mkv"
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        base = f"Запись экрана {stamp}"
        # No destination I/O before the capture process has been stopped.
        return folder / f"{base}.{ext}"

    def prepare_recording_output(self):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".recording_save_", dir=self.output_path.parent))
        self.pending_output_path = stage / f"{self.output_path.stem}.pending{self.output_path.suffix}"
        return self.pending_output_path

    def publish_recording_output(self):
        source = self.pending_output_path
        desired = self.output_path
        target = desired
        counter = 2
        while True:
            try:
                if os.name == "nt":
                    # Same-volume Windows rename refuses even a late collision.
                    os.rename(source, target)
                else:
                    os.link(source, target)
                break
            except FileExistsError:
                target = desired.with_name(f"{desired.stem} ({counter}){desired.suffix}")
                counter += 1
        self.output_path = target
        self.pending_output_path = None
        try:
            if os.name != "nt":
                source.unlink()
            source.parent.rmdir()
        except OSError as exc:
            self.log_exception("publish_recording_output.cleanup", exc)
        return target

    def quarantine_incomplete_output(self):
        """Retain only this save's staged file; never touch the public destination."""
        path = getattr(self, "pending_output_path", None)
        if not path or not path.exists():
            return None
        self.incomplete_output_path = path
        self.diagnostic_log("incomplete_output_quarantined", {"source": path, "target": path}, level="WARN")
        return path

    def check_ffmpeg(self):
        # Раньше при каждом старте записи выполнялся ffmpeg -version. Даже если
        # это 0.2–0.5 секунды, пользователь видел задержку. Теперь проверка
        # кэшируется и заранее прогревается после запуска программы.
        if self._ffmpeg_ok_cache is True:
            return True
        if self._ffmpeg_ok_cache is False:
            messagebox.showerror(
                "FFmpeg не найден",
                "Нужно установить FFmpeg и добавить ffmpeg.exe в PATH.\n\n"
                "Без FFmpeg качественная запись экрана, микрофона и системного звука работать не будет."
            )
            return False
        if self.is_gui_thread():
            # Не блокируем окно проверкой ffmpeg -version. Если preflight ещё не
            # успел заполнить кэш, пробуем стартовать; реальная ошибка запуска
            # будет поймана при Popen/проверке сегмента. Важно: здесь НЕ запускаем
            # DXcam-буфер, чтобы не создавать новую DXcam-гонку во время старта.
            try:
                if self._preflight_thread is None or not self._preflight_thread.is_alive():
                    self._preflight_thread = threading.Thread(target=self.preflight_worker, daemon=True)
                    self._preflight_thread.start()
            except Exception:
                pass
            return True
        try:
            self.run_managed_process([self.ffmpeg_path, "-version"], capture_output=True, text=True, timeout=5, creationflags=self.creation_flags())
            self._ffmpeg_ok_cache = True
            return True
        except Exception:
            self._ffmpeg_ok_cache = False
            messagebox.showerror(
                "FFmpeg не найден",
                "Нужно установить FFmpeg и добавить ffmpeg.exe в PATH.\n\n"
                "Без FFmpeg качественная запись экрана, микрофона и системного звука работать не будет."
            )
            return False

    def ffmpeg_supports_encoder(self, encoder_name):
        """Проверяем, доступен ли кодировщик FFmpeg, например h264_nvenc."""
        key = str(encoder_name).lower()
        if key in self._encoder_support_cache:
            return self._encoder_support_cache[key]
        if self.is_gui_thread():
            try:
                if self._preflight_thread is None or not self._preflight_thread.is_alive():
                    self._preflight_thread = threading.Thread(target=self.preflight_worker, daemon=True)
                    self._preflight_thread.start()
            except Exception:
                pass
            return False
        try:
            result = self.run_managed_process(
                [self.ffmpeg_path, "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=8,
                creationflags=self.creation_flags(),
            )
            encoders_text = (result.stdout or "") + "\n" + (result.stderr or "")
            ok = key in encoders_text.lower()
        except Exception:
            ok = False
        self._encoder_support_cache[key] = ok
        return ok

    def ffmpeg_supports_filter(self, filter_name):
        """Проверяем фильтр FFmpeg. Для плавного захвата нужен ddagrab."""
        key = str(filter_name).lower()
        if key in self._filter_support_cache:
            return self._filter_support_cache[key]
        if self.is_gui_thread():
            try:
                if self._preflight_thread is None or not self._preflight_thread.is_alive():
                    self._preflight_thread = threading.Thread(target=self.preflight_worker, daemon=True)
                    self._preflight_thread.start()
            except Exception:
                pass
            return False
        try:
            result = self.run_managed_process(
                [self.ffmpeg_path, "-hide_banner", "-filters"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=8,
                creationflags=self.creation_flags(),
            )
            filters_text = (result.stdout or "") + "\n" + (result.stderr or "")
            ok = key in filters_text.lower()
        except Exception:
            ok = False
        self._filter_support_cache[key] = ok
        return ok

    def ffmpeg_supports_input_format(self, format_name):
        """Проверяем input device FFmpeg, например wasapi для системного звука."""
        key = str(format_name).lower()
        if key in self._input_format_support_cache:
            return self._input_format_support_cache[key]
        if self.is_gui_thread():
            try:
                if self._preflight_thread is None or not self._preflight_thread.is_alive():
                    self._preflight_thread = threading.Thread(target=self.preflight_worker, daemon=True)
                    self._preflight_thread.start()
            except Exception:
                pass
            return False
        try:
            result = self.run_managed_process(
                [self.ffmpeg_path, "-hide_banner", "-devices"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=8,
                creationflags=self.creation_flags(),
            )
            devices_text = ((result.stdout or "") + "\n" + (result.stderr or "")).lower()
            ok = bool(re.search(r"(^|\n)\s*d\s+" + re.escape(key) + r"\b", devices_text)) or (key in devices_text)
        except Exception:
            ok = False
        self._input_format_support_cache[key] = ok
        return ok

    def supports_wasapi_loopback(self):
        return bool(os.name == "nt" and self.ffmpeg_supports_input_format("wasapi"))

    def validate_encoder_choice_before_recording(self):
        """Не молчим, если пользователь явно выбрал NVENC, а FFmpeg его не видит."""
        try:
            choice = self.encoder_var.get()
        except Exception:
            return
        if choice == "NVIDIA NVENC" and not self.ffmpeg_supports_encoder("h264_nvenc"):
            self.encoder_var.set("CPU x264")
            messagebox.showwarning(
                "NVENC недоступен",
                "Ты выбрал NVIDIA NVENC, но текущий FFmpeg не видит кодировщик h264_nvenc.\n\n"
                "Запись будет запущена через CPU x264, чтобы программа не упала молча. "
                "Для NVENC установи сборку FFmpeg с поддержкой NVIDIA и актуальный драйвер видеокарты."
            )
            self.log_message("NVENC was explicitly selected but h264_nvenc is unavailable; switched to CPU x264.")
        if choice == "NVIDIA NVENC H.265 (HEVC)" and not self.ffmpeg_supports_encoder("hevc_nvenc"):
            self.encoder_var.set("CPU x265 (HEVC)")
            messagebox.showwarning(
                "NVENC HEVC недоступен",
                "Ты выбрал NVIDIA NVENC H.265, но текущий FFmpeg не видит hevc_nvenc.\n\n"
                "Запись пойдёт через CPU x265 (HEVC). Это медленнее, но даёт тот же кодек."
            )
            self.log_message("hevc_nvenc explicitly selected but unavailable; switched to CPU x265.")

    def should_use_hevc(self):
        """True, если выбран кодек H.265/HEVC."""
        choice = str(self.encoder_var.get())
        return "HEVC" in choice or "H.265" in choice or "x265" in choice

    def should_use_nvenc(self):
        """Возвращает True только если NVENC реально доступен."""
        choice = self.encoder_var.get()
        if choice in ("CPU x264", "CPU x265 (HEVC)"):
            return False
        if self.should_use_hevc():
            ok = self.ffmpeg_supports_encoder("hevc_nvenc")
            if not ok:
                self.log_message("hevc_nvenc unavailable; using libx265.")
            return ok
        if choice in ("Авто — NVIDIA NVENC", "NVIDIA NVENC"):
            ok = self.ffmpeg_supports_encoder("h264_nvenc")
            if not ok:
                self.log_message("h264_nvenc unavailable; using libx264.")
            return ok
        return False

    def choose_capture_backend(self):
        """Выбираем стабильный способ захвата без DXcam.

        На этой машине зависание происходит именно в момент старта записи,
        когда выбран DXcam и библиотека пытается работать со своим singleton-
        объектом камеры. Поэтому даже если в старом settings.json сохранён DXcam,
        фактически используем FFmpeg Desktop Duplication (ddagrab), а если он
        недоступен — gdigrab. Это убирает зависание Tkinter на "Запускаю запись...".
        """
        choice = self.capture_method_var.get()

        if choice == "Старый GDI / gdigrab":
            return "gdigrab"

        # Один ddagrab-вход захватывает только один output. Если пользователь
        # выделил прямоугольник сразу на двух мониторах, берём виртуальный
        # рабочий стол через gdigrab, иначе часть области была бы потеряна.
        if self.capture_region_spans_multiple_monitors():
            try:
                self.status_var.set(
                    "Область пересекает несколько мониторов. Использую GDI для точного захвата."
                )
            except Exception:
                pass
            return "gdigrab"

        # Старые и новые DXcam/Auto-значения считаем запросом на стабильный
        # Desktop Duplication через FFmpeg, а не на Python-DXcam.
        if "DXcam" in str(choice):
            try:
                self.status_var.set("DXcam отключён из-за зависаний. Использую Desktop Duplication / ddagrab.")
            except Exception:
                pass
            return "ddagrab" if self._filter_support_cache.get("ddagrab") is not False else "gdigrab"

        if choice in ("Desktop Duplication / ddagrab", "Авто — ddagrab, потом GDI"):
            # Если preflight ещё не успел проверить ddagrab, пробуем его сразу:
            # запуск FFmpeg ниже сам проверит ошибку и откатится на gdigrab.
            return "ddagrab" if self._filter_support_cache.get("ddagrab") is not False else "gdigrab"

        return "ddagrab" if self._filter_support_cache.get("ddagrab") is not False else "gdigrab"

    def get_recording_temp_root(self):
        """Возвращает безопасную папку для временных сегментов записи.

        По умолчанию временные сегменты лежат рядом с будущим видео: это даёт
        быстрое сохранение без копирования гигабайтов. Но если папка сохранения
        похожа на облачную/синхронизируемую папку, пишем временные файлы в
        локальную служебную папку. Так меньше риск рывков, блокировок и ошибок
        из-за OneDrive/Google Drive/Dropbox/Яндекс.Диска во время записи.
        """
        try:
            folder = Path(self.output_folder.get().strip() or os.getcwd()).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            folder_text = str(folder).lower()
            cloud_markers = (
                "onedrive",
                "google drive",
                "google дис",
                "dropbox",
                "yandexdisk",
                "яндексдиск",
                "icloud",
            )
            if any(marker in folder_text for marker in cloud_markers):
                temp_root = DATA_DIR / "recording_temp_local"
                temp_root.mkdir(parents=True, exist_ok=True)
                try:
                    self.log_message(
                        f"Output folder looks cloud-synced; using local recording temp folder: {temp_root}"
                    )
                except Exception:
                    pass
                return temp_root
            temp_root = folder / ".recording_temp"
            temp_root.mkdir(parents=True, exist_ok=True)
            return temp_root
        except Exception:
            TEMP_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            return TEMP_RECORDINGS_DIR

    def get_segment_extension(self):
        """Возвращает контейнер временного сегмента записи.

        Этот метод нужен при старте каждого сегмента. Без него start_new_segment()
        падал с ошибкой:
        'ScreenRecorderProWin11' object has no attribute 'get_segment_extension'.

        Логика:
        - итоговый MKV пишем во временный MKV;
        - итоговые MP4/MOV пишем во временный фрагментированный MP4;
        - AVI и неизвестные форматы пишем во временный MKV, а при сохранении
          программа уже конвертирует/ремультиплексирует в нужный итоговый формат.

        Так старт записи не зависит от выбранного пользователем контейнера и
        не ломается из-за отсутствующего метода.
        """
        try:
            ext = str(self.format_var.get() or "mkv").lower().strip().lstrip(".")
        except Exception:
            ext = "mkv"

        if ext == "mkv":
            return "mkv"
        if ext in ("mp4", "mov"):
            # Временный MP4 пишется как fragmented MP4 через
            # append_segment_container_options(): это безопаснее обычного MP4,
            # если запись остановлена неидеально.
            return "mp4"
        # Для AVI надёжнее писать временный MKV, а финальный AVI получать на
        # этапе finalize/merge. Так сегменты меньше рискуют повредиться.
        return "mkv"
