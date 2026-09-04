from ..shared import *


class RecordingSessionMixin:
    def start_recording(self):
        if self.is_recording or self.is_finalizing or getattr(self, "is_starting", False):
            self.diagnostic_log("start_recording_ignored", {
                "is_recording": self.is_recording,
                "is_finalizing": self.is_finalizing,
                "is_starting": getattr(self, "is_starting", False),
            }, level="WARN")
            return

        # Если после предыдущей записи ещё идёт тяжёлый визуальный анализ логов,
        # новая запись важнее: просим фоновый FFmpeg-анализ немедленно завершиться.
        self.cancel_post_save_diagnostics(reason="new_recording")
        self.close_recording_problem_log_session(
            recording_session_id=getattr(self, "_session_log_owner_id", None),
            reason="new_recording_requested",
        )

        self.is_starting = True
        self.cancel_start_requested = False
        self.diagnostic_log("start_recording_requested", {
            "settings": self.collect_settings_snapshot(),
            "capture_region_pending": self._pending_region,
            "ffmpeg_path": self.ffmpeg_path,
        })
        try:
            # Сразу меняем состояние кнопок, чтобы повторный клик по плавающей
            # панели не отправил второй старт, пока идёт подготовка.
            try:
                self.start_button.configure(state="disabled")
                self.pause_button.configure(state="disabled", text="⏸ Пауза")
                self.stop_button.configure(state="disabled")
                self.status_var.set("Запускаю запись...")
                if self.annotation_overlay:
                    self.annotation_overlay.update_record_controls()
                self.root.update_idletasks()
            except Exception:
                pass

            # Фикс задержки старта: момент клика запоминаем сразу.
            # Раньше фактическая DXcam-запись начиналась только после подготовки
            # папок, логов, FFmpeg и проверки процесса — из-за этого начало видео
            # появлялось чуть позже клика.
            self.recording_start_requested_perf = time.perf_counter()
            self.recording_stop_requested_perf = None

            # Разовая область: если запись запущена кнопкой «Область» — пишем её,
            # иначе (обычная «Запись», хоткей, кнопка окна) — весь экран.
            self.capture_region = self._pending_region
            self._pending_region = None

            if not self.check_ffmpeg():
                try:
                    self.start_button.configure(state="normal")
                    self.status_var.set("FFmpeg не найден или не запускается.")
                except Exception:
                    pass
                return

            self.wait_for_preflight_caches(timeout=1.2)

            if not self.check_disk_space_or_warn():
                try:
                    self.start_button.configure(state="normal")
                    self.status_var.set("Запись отменена: мало места на диске.")
                except Exception:
                    pass
                return

            self.validate_encoder_choice_before_recording()
            try:
                selected_system = self.normalize_saved_audio_choice(self.system_device_var.get(), "system")
                if (
                    selected_system not in (NO_AUDIO, SYSTEM_AUDIO_DEFAULT, SYSTEM_AUDIO_COMMUNICATION, SYSTEM_AUDIO_WASAPI)
                    and not self.is_wasapi_render_choice(selected_system)
                    and not self.is_valid_dshow_system_audio_source(selected_system)
                ):
                    self.system_device_var.set(SYSTEM_AUDIO_DEFAULT)
                    selected_system = SYSTEM_AUDIO_DEFAULT
                    self.status_var.set("В звуке компьютера был выбран вход записи. Переключил на Windows default.")
                if selected_system in (SYSTEM_AUDIO_DEFAULT, SYSTEM_AUDIO_COMMUNICATION) and not self.supports_wasapi_loopback():
                    fallback_system = self.resolve_default_system_dshow_device()
                    if os.name == "nt":
                        self.status_var.set("FFmpeg без WASAPI: системный звук будет записан напрямую через Windows CoreAudio loopback.")
                    elif not fallback_system or fallback_system == NO_AUDIO:
                        messagebox.showwarning(
                            "Системный звук недоступен",
                            "Выбран звук компьютера по умолчанию, но текущий FFmpeg не поддерживает WASAPI loopback, "
                            "и в dshow не найден Stereo Mix / Стерео микшер / virtual-audio-capturer.\n\n"
                            "Установи свежий FFmpeg с поддержкой wasapi или включи Stereo Mix в Windows."
                        )
                        try:
                            self.start_button.configure(state="normal")
                        except Exception:
                            pass
                        return
            except Exception as exc:
                # Не скрываем traceback: ошибка проверки аудио возникает до
                # создания папки конкретной записи, поэтому пишем её в общий
                # диагностический лог приложения.
                self.log_exception("start_recording.audio_validation", exc)
                messagebox.showwarning("Ошибка проверки звука", str(exc))
                try:
                    self.start_button.configure(state="normal")
                except Exception:
                    pass
                return
            self.save_settings()
            self.recording_session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            try:
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                TEMP_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            self.create_recording_problem_log_folder()
            self.diagnostic_log("recording_session_created", {
                "recording_session_id": self.recording_session_id,
                "settings": self.collect_settings_snapshot(),
                "problem_log_folder": self.current_session_log_dir,
                "summary_log": self.session_summary_path,
                "recording_log": self.current_log_path,
            })
            temp_root = self.get_recording_temp_root()
            self.temp_dir = temp_root / self.recording_session_id
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            self.segments = []
            self.python_loopback_audio_segments = {}
            self.python_loopback_sync_metadata = {}
            self.segment_index = 0
            self.recorded_seconds = 0.0
            self.recorded_wall_seconds = 0.0
            self.recording_requested_fps = None
            self.recording_effective_fps = None
            self.recording_refresh_hz = None
            self.recording_capture_backend = None
            self.recording_ffmpeg_command = None
            self.recording_ffmpeg_pid = None
            with self.recording_progress_lock:
                self.recording_progress_samples = []
                self.recording_progress_latest = {}
            self.recording_progress_threads = []
            self.recording_first_frame_perf = None
            self.recording_last_frame_perf = None
            self.segment_capture_started_perf = None
            self.segment_first_progress_out_time = None
            self.current_segment_media_seconds = 0.0
            self.current_segment_last_progress_perf = None
            self.current_segment_last_video_frame_value = None
            self.current_segment_last_video_frame_advance_perf = None
            self.current_segment_last_video_frame_out_time_seconds = None
            self.current_segment_video_stall_detected = False
            self.recording_capture_recovery_attempts = 0
            self.recording_stderr_threads = [
                entry
                for entry in list(getattr(self, "recording_stderr_threads", []) or [])
                if entry.get("thread") and entry["thread"].is_alive()
            ]
            self.recording_capture_signal_queue = queue.Queue(maxsize=32)
            self.recording_process_generation = 0
            self.current_capture_access_lost = None
            self.current_capture_access_lost_wait_logged = False
            self.capture_recovery_segments = {}
            self.recording_segment_start_perfs = {}
            self.recording_ffmpeg_args = []
            self.automatic_segment_restart_thread = None
            self.automatic_segment_restart_generation += 1
            self.automatic_segment_restart_result_queue = queue.Queue(maxsize=8)
            previous_restart_poll = getattr(self, "automatic_segment_restart_poll_job", None)
            self.automatic_segment_restart_poll_job = None
            if previous_restart_poll:
                try:
                    self.root.after_cancel(previous_restart_poll)
                except Exception:
                    pass
            with self.recording_performance_lock:
                self.recording_performance_samples = []
            self.recording_performance_stop_event = threading.Event()
            self._performance_cpu_measurement_count = 0
            self._performance_high_cpu_consecutive = 0
            self._performance_high_cpu_snapshot_count = 0
            self._performance_high_cpu_last_snapshot_perf = 0.0
            self._performance_process_cpu_handles = {}
            self._performance_process_cpu_last_snapshot_perf = None
            self.last_video_timing_summary = None
            self.last_frame_content_analysis = None
            self.last_ai_smoothness_report = None
            self.recording_settings_snapshot = self.collect_settings_snapshot()
            try:
                self.recording_output_folder_snapshot = str(self.output_folder.get().strip() or os.getcwd())
            except Exception:
                self.recording_output_folder_snapshot = str(os.getcwd())
            self._last_gpu_sample_perf = 0.0
            self.write_ai_problem_summary(outcome="Запись подготовлена; ожидается первый кадр FFmpeg.")
            self.output_path = None
            self.incomplete_output_path = None
            self.recording_failure_reason = None
            self.is_recording = True
            self.is_paused = False
            self.is_finalizing = False
            self.recording_audio_bitrate = self.normalize_audio_bitrate_value(self.audio_bitrate_var.get())

            # Во время записи отключаем аудио-индикаторы: они сами запускали FFmpeg
            # и могли создавать рывки, конкурируя с основным процессом записи.
            self.stop_audio_meters(join_timeout=0.02)

            # Обратный отсчёт 3-2-1: маскирует неизбежный cold-start ddagrab/аудио,
            # к моменту реального старта FFmpeg пользователь уже готов.
            self.run_start_countdown()

            if self.cancel_start_requested:
                self.is_recording = False
                self.is_paused = False
                self.is_finalizing = False
                self.cleanup_temp_dir()
                self.diagnostic_log(
                    "recording_start_cancelled_for_exit",
                    {"recording_session_id": self.recording_session_id},
                    level="INFO",
                )
                try:
                    self.root.after_idle(self.exit_app)
                except Exception:
                    pass
                return

            self.refresh_recording_cursor_cache()
            self.start_cursor_highlight_overlay()
            try:
                self.start_new_segment()
            except Exception as exc:
                self.stop_cursor_highlight_overlay()
                self.log_exception("start_recording", exc)
                try:
                    self.stop_recording_performance_sampler()
                except Exception:
                    pass
                self.is_recording = False
                self.is_finalizing = False
                self.set_settings_window_enabled(True)
                self.start_audio_meters()
                try:
                    self.start_button.configure(state="normal")
                    self.pause_button.configure(state="disabled", text="⏸ Пауза")
                    self.stop_button.configure(state="disabled")
                    if self.annotation_overlay:
                        self.annotation_overlay.update_record_controls()
                except Exception:
                    pass
                messagebox.showerror("Ошибка запуска записи", str(exc))
                return

            if self.cancel_start_requested:
                self.stop_recording()
                return

            self.schedule_recording_watchdog()

            if self.draw_enabled_var.get():
                self.show_annotation_overlay(open_toolbar=True)

            self.start_button.configure(state="disabled")
            self.pause_button.configure(state="normal", text="⏸ Пауза")
            self.stop_button.configure(state="normal")
            self.set_settings_window_enabled(False)
            self.set_rec_state("recording")
            self.status_var.set("Идёт запись. Плавающая панель остаётся на экране; наведи на индикатор ●, чтобы открыть кнопки и карандаши.")
            self.schedule_auto_stop()
            self.start_keys_overlay()
            # Cursor overlay уже запущен до FFmpeg, чтобы он был в первом кадре.
        finally:
            self.is_starting = False
            try:
                if self.annotation_overlay:
                    self.annotation_overlay.update_record_controls()
            except Exception:
                pass

    def start_new_segment(self):
        self.segment_index += 1
        try:
            segment_ext = self.get_segment_extension()
        except AttributeError:
            # Защитный fallback: если метод случайно удалят при следующей правке,
            # запись всё равно стартует во временный MKV вместо падения с messagebox.
            segment_ext = "mkv"
            try:
                self.append_problem_error(
                    "get_segment_extension_missing_fallback",
                    "Метод get_segment_extension отсутствовал, использован временный контейнер MKV."
                )
            except Exception:
                pass
        segment_path = self.temp_dir / f"segment_{self.segment_index:04d}.{segment_ext}"

        log_path = self.get_current_recording_log_path()
        self.log_handle = open(log_path, "a", encoding="utf-8", errors="ignore")
        self.problem_log_event("new_segment_log_opened", {"segment_index": self.segment_index, "segment_path": segment_path, "log_path": log_path})
        self.log_handle.write("\n\n--- NEW SEGMENT ---\n")
        self.log_handle.write(f"time={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        self.log_handle.write(f"program_folder={APP_DIR}\n")
        self.log_handle.write(f"settings_path={SETTINGS_PATH}\n")
        self.log_handle.write(f"logs_folder={LOGS_DIR}\n")
        self.log_handle.write(f"session_logs_folder={self.current_session_log_dir}\n")
        self.log_handle.write(f"temp_segments_folder={self.temp_dir}\n")
        self.log_handle.write(f"segment_container={segment_path.suffix.lower()}\n")
        self.log_handle.write(f"DXCAM_AVAILABLE={DXCAM_AVAILABLE}\n")
        self.log_handle.write(f"selected_capture_method={self.capture_method_var.get()}\n")
        self.log_handle.write(f"cursor_visible={self.recording_cursor_visible}\n")
        self.log_handle.write(f"cursor_size_percent={self.recording_cursor_size_percent}\n")
        self.log_handle.write(f"cursor_render_mode={self.get_recording_cursor_render_mode()}\n")
        self.log_handle.write(f"cursor_highlight={self.recording_cursor_highlight}\n")
        self.log_handle.write(f"cursor_highlight_size={self.recording_cursor_highlight_size}\n")
        self.log_handle.flush()
        self.diagnostic_log("recording_segment_start", {
            "recording_session_id": self.recording_session_id,
            "segment_index": self.segment_index,
            "segment_path": segment_path,
            "recording_log_path": log_path,
            "segment_container": segment_path.suffix.lower(),
            "settings": self.collect_settings_snapshot(),
        })

        python_loopback_started = False
        try:
            capture_backend = self.choose_capture_backend()
            timing_mode = "gpu_direct_ddagrab_wallclock_cfr" if capture_backend == "ddagrab" else "single_filter_cfr_fallback"
            self.log_handle.write(
                f"capture_backend={capture_backend}, encoder={'nvenc' if self.should_use_nvenc() else 'x264'}, "
                f"timing_mode={timing_mode}\n"
            )
            self.log_handle.flush()

            if self.should_capture_system_audio_with_python_loopback():
                self.start_python_loopback_for_segment(segment_path)
                python_loopback_started = True

            if capture_backend == "dxcam":
                try:
                    self.start_dxcam_segment(segment_path)
                    return
                except Exception as dx_exc:
                    self.log_exception("start_dxcam_segment", dx_exc)
                    self.disable_dxcam_for_session(dx_exc)
                    fallback_backend = self.get_safe_ffmpeg_fallback_backend()
                    self.log_handle.write(f"\n--- FALLBACK ---\nDXcam failed at start: {dx_exc}\nTrying {fallback_backend} without blocking GUI...\n")
                    self.log_handle.flush()
                    try:
                        if segment_path.exists():
                            segment_path.unlink()
                    except Exception:
                        pass
                    try:
                        self.launch_checked_ffmpeg_segment(segment_path, fallback_backend)
                    except Exception as fb_exc:
                        # Если ddagrab был выбран из кэша, но всё равно упал,
                        # последняя безопасная попытка — gdigrab. Без повторных
                        # ffmpeg -filters проверок в GUI-потоке.
                        if fallback_backend != "gdigrab":
                            self.log_exception("fallback_capture_backend", fb_exc)
                            try:
                                if segment_path.exists():
                                    segment_path.unlink()
                            except Exception:
                                pass
                            self.launch_checked_ffmpeg_segment(segment_path, "gdigrab")
                        else:
                            raise
                    return

            # Если выбран не DXcam, отключаем прогретую DXcam-камеру, чтобы она не
            # забирала ресурсы во время записи другим способом.
            self.stop_instant_dxcam_buffer(release_camera=True)

            try:
                self.launch_checked_ffmpeg_segment(segment_path, capture_backend)
            except RuntimeError as exc:
                if capture_backend == "ddagrab":
                    self.log_handle.write(f"\n--- FALLBACK ---\nddagrab failed at start: {exc}\nTrying gdigrab...\n")
                    self.log_handle.flush()
                    try:
                        if segment_path.exists():
                            segment_path.unlink()
                    except Exception:
                        pass
                    self.launch_checked_ffmpeg_segment(segment_path, "gdigrab")
                else:
                    raise
        except Exception:
            if python_loopback_started:
                try:
                    self.stop_python_loopback_for_current_segment()
                except Exception:
                    pass
            # Закрываем лог-хендл, иначе на error-пути старта он утекает (новый
            # сегмент откроет ещё один open() поверх).
            if self.log_handle:
                try:
                    self.log_handle.flush()
                    self.log_handle.close()
                except Exception:
                    pass
                self.log_handle = None
            raise

    def launch_checked_ffmpeg_segment(self, segment_path, capture_backend):
        command = self.build_ffmpeg_command(segment_path, capture_backend=capture_backend)
        try:
            click_perf = self.recording_start_requested_perf or time.perf_counter()
            self.log_handle.write(f"start_delay_before_ffmpeg={time.perf_counter() - click_perf:.3f}\n")
        except Exception:
            pass
        self.log_handle.write(self.command_to_log_text(command) + "\n")
        self.log_handle.flush()
        self.append_ffmpeg_problem_log("recording segment command", command=command, extra={"segment_path": segment_path, "capture_backend": capture_backend})
        self.diagnostic_log("ffmpeg_recording_segment_command", {
            "capture_backend": capture_backend,
            "segment_path": segment_path,
            "command": self.command_to_log_text(command),
        })

        self.recording_process_generation += 1
        process_generation = self.recording_process_generation
        self.current_capture_access_lost = None
        self.current_capture_access_lost_wait_logged = False
        process_launch_perf = time.perf_counter()
        process = self.start_managed_process(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=self.recording_creation_flags(),
        )
        self.recording_capture_backend = capture_backend
        self.recording_ffmpeg_command = self.command_to_log_text(command)
        self.recording_ffmpeg_args = list(command)
        self.recording_ffmpeg_pid = getattr(process, "pid", None)
        self.segment_capture_started_perf = None
        self.segment_first_progress_out_time = None
        self.current_segment_media_seconds = 0.0
        self.current_segment_last_progress_perf = None
        self.current_segment_last_video_frame_value = None
        self.current_segment_last_video_frame_advance_perf = None
        self.current_segment_last_video_frame_out_time_seconds = None
        self.current_segment_video_stall_detected = False
        self.start_ffmpeg_stderr_reader(
            process,
            log_path=self.get_current_recording_log_path(),
            segment_path=segment_path,
            capture_backend=capture_backend,
            process_generation=process_generation,
        )
        self.start_ffmpeg_progress_reader(
            process,
            segment_path=segment_path,
            capture_backend=capture_backend,
            process_launch_perf=process_launch_perf,
        )

        # Если команда неправильная, FFmpeg падает сразу. Ждём короткое окно,
        # но НЕ морозим GUI: качаем Tk-цикл и выходим раньше, если процесс уже
        # упал. Кнопки на старте уже disabled, повторного клика не будет.
        # ponytail: окно ~0.35с; если понадобится полностью неблокирующий старт —
        # вынести start_new_segment в daemon-поток по образцу _stop_recording_worker.
        deadline = time.perf_counter() + 0.35
        while time.perf_counter() < deadline and process.poll() is None:
            try:
                self.root.update()
            except Exception:
                pass
            time.sleep(0.02)
        if process.poll() is not None:
            code = process.returncode
            try:
                if process.stdin:
                    process.stdin.close()
            except Exception:
                pass
            self.wait_for_ffmpeg_stderr_reader(process, timeout=1.0)
            self.unregister_child_process(process)
            raise RuntimeError(f"FFmpeg сразу завершился с кодом {code}. Лог: {self.current_log_path}")

        self.current_segment_engine = "ffmpeg"
        with self.process_lock:
            self.process = process

        self.start_recording_performance_sampler()
        self.segments.append(segment_path)
        # Считаем только время жизни активного FFmpeg-сегмента. Раньше первый
        # сегмент начинал таймер от клика пользователя, поэтому в длительность
        # ошибочно попадали preflight, обратный отсчёт, открытие устройств и
        # запуск процесса. Это создавало ложный вывод о потерянных кадрах.
        self.segment_started_at = process_launch_perf
        timer_start_source = "ffmpeg_process_launch"
        try:
            self.log_handle.write(
                f"segment_timer_start_source={timer_start_source}, "
                f"timer_started_before_ready={time.perf_counter() - self.segment_started_at:.3f}s\n"
            )
            self.log_handle.flush()
        except Exception:
            pass
        self.diagnostic_log("ffmpeg_recording_segment_ready", {
            "capture_backend": capture_backend,
            "segment_path": segment_path,
            "pid": getattr(process, "pid", None),
            "timer_start_source": timer_start_source,
            "startup_check_elapsed_sec": round(time.perf_counter() - process_launch_perf, 3),
        })

    def get_recording_fps_int(self):
        try:
            fps_int = int(str(self.fps_var.get()).strip() or "60")
        except Exception:
            fps_int = 60
        if fps_int not in self.ALLOWED_RECORDING_FPS:
            # Берём ближайшее разрешённое значение вместо жёсткого отката на 60,
            # чтобы нестандартный FPS из старого settings.json не терялся молча.
            fps_int = min(self.ALLOWED_RECORDING_FPS, key=lambda allowed: abs(allowed - fps_int))
        return fps_int

    def get_video_settings_for_ffmpeg(self):
        requested_fps = self.get_recording_fps_int()
        try:
            auto_adjust_fps = bool(self.auto_adjust_fps_var.get())
        except Exception:
            auto_adjust_fps = False
        refresh_hz = detect_primary_refresh_hz()
        # FPS, кратный refresh, даёт равномерное дублирование кадров. Но это
        # должно быть явным выбором пользователя: при выключенной галочке 60
        # остаётся 60, даже если монитор работает на 72/144 Гц.
        if auto_adjust_fps:
            fps_int = smooth_fps_for_refresh(requested_fps, refresh_hz, self.ALLOWED_RECORDING_FPS)
        else:
            fps_int = requested_fps
        # Сохраняем обычные числа, а не Tkinter-переменные: итоговая проверка
        # выполняется в рабочем потоке после остановки записи.
        self.recording_requested_fps = int(requested_fps)
        self.recording_effective_fps = int(fps_int)
        self.recording_refresh_hz = int(refresh_hz)
        try:
            if self.log_handle:
                self.log_handle.write(
                    f"monitor_refresh_hz={refresh_hz}, requested_fps={requested_fps}, "
                    f"auto_adjust_fps={auto_adjust_fps}, smooth_fps={fps_int}\n"
                )
                self.log_handle.flush()
        except Exception:
            pass
        if auto_adjust_fps and fps_int != requested_fps:
            try:
                self.status_var.set(
                    f"FPS подстроен под монитор {refresh_hz} Гц: {requested_fps}→{fps_int} для плавного видео."
                )
            except Exception:
                pass
        requested_video_mbps = normalize_video_bitrate_mbps(self.video_bitrate_var.get(), default=16)
        min_quality_mbps = minimum_quality_bitrate_mbps(fps_int)
        video_mbps = max(requested_video_mbps, min_quality_mbps)
        try:
            self.video_bitrate_var.set(str(video_mbps))
        except Exception:
            pass
        try:
            if self.log_handle and video_mbps != requested_video_mbps:
                self.log_handle.write(
                    f"video_bitrate_auto_raised={requested_video_mbps}->{video_mbps}M "
                    f"for_quality_at_{fps_int}fps\n"
                )
                self.log_handle.flush()
        except Exception:
            pass
        return fps_int, f"{video_mbps}M", video_bitrate_to_bufsize(video_mbps)
