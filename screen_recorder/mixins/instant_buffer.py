from ..shared import *


class InstantBufferMixin:
    def cleanup_dxcam_references(self):
        """Даёт Python шанс удалить старые DXcam-объекты перед новым стартом.

        В dxcam есть внутренний singleton на device/output. Если старый объект ещё
        жив хотя бы по одной ссылке, новый dxcam.create() возвращает тот же
        instance. Само по себе это не ошибка, но после stop/release старый объект
        иногда остаётся в промежуточном состоянии и может подвесить grab/start.
        """
        try:
            gc.collect()
        except Exception:
            pass

    def release_dxcam_camera_safely(self, camera, label="dxcam_camera"):
        """Останавливает и освобождает DXcam-камеру без зависания GUI.

        DXcam иногда зависает внутри camera.stop()/release() на старом
        singleton-объекте. Если вызвать это из Tkinter-потока, всё окно Windows
        помечает как «Не отвечает». Поэтому GUI-поток только отдаёт освобождение
        отдельному daemon-потоку; реальные DXcam-вызовы выполняются не в UI.
        """
        if camera is None:
            return
        try:
            gui_ident = getattr(self, "gui_thread_ident", None)
            if gui_ident is not None and threading.get_ident() == gui_ident:
                threading.Thread(
                    target=self._release_dxcam_camera_worker,
                    args=(camera, label),
                    name=f"release_{label}",
                    daemon=True,
                ).start()
                return
        except Exception:
            pass
        self._release_dxcam_camera_worker(camera, label)

    def _release_dxcam_camera_worker(self, camera, label="dxcam_camera"):
        if camera is None:
            return
        try:
            with self.dxcam_camera_io_lock:
                try:
                    camera.stop()
                except Exception as exc:
                    self.log_message(f"{label}.stop ignored: {exc}")
                try:
                    camera.release()
                except Exception as exc:
                    self.log_message(f"{label}.release ignored: {exc}")
        except Exception as exc:
            self.log_exception(f"{label}.release_dxcam_camera_safely", exc)
        finally:
            try:
                del camera
            except Exception:
                pass
            self.cleanup_dxcam_references()

    def create_dxcam_camera_safely(self):
        self.cleanup_dxcam_references()
        with self.dxcam_camera_io_lock:
            return dxcam.create(output_color="BGR")

    def safe_start_dxcam_camera(self, camera_obj, fps_int):
        """Старт DXcam без падения, если singleton уже запущен буфером."""
        with self.dxcam_camera_io_lock:
            try:
                camera_obj.start(target_fps=fps_int, video_mode=True)
                return True
            except Exception as exc:
                message = str(exc).lower()
                if "already running" in message or "capture is already running" in message:
                    self.log_message("DXcam camera is already running; reusing active capture instead of starting it twice.")
                    return True
                raise

    def get_dxcam_frame_safely(self, camera, allow_grab=False):
        """Берёт один кадр DXcam под общим lock. Возвращает None при ошибке."""
        if camera is None:
            return None
        with self.dxcam_camera_io_lock:
            frame = None
            try:
                frame = camera.get_latest_frame()
            except Exception as exc:
                self.log_message(f"DXcam get_latest_frame ignored: {exc}")
                frame = None
            if frame is None and allow_grab:
                try:
                    frame = camera.grab()
                except Exception as exc:
                    self.log_message(f"DXcam grab ignored: {exc}")
                    frame = None
            return frame

    def get_dxcam_recording_frame_fast(self, camera):
        """Быстро берёт кадр в потоке записи без глобального lifecycle-lock.

        Рывки в сохранённом видео появлялись, когда поток записи на каждом
        кадре ждал общий dxcam_camera_io_lock. Этот же lock мог держать фоновый
        release/stop старой DXcam-камеры после предыдущего старта. В итоге
        get_latest_frame() задерживался рывками, а rawvideo-поток получал кадры
        неравномерно. После передачи камеры записи она принадлежит только
        dxcam_capture_loop, поэтому читать latest frame можно напрямую.
        """
        if camera is None:
            return None
        try:
            return camera.get_latest_frame()
        except Exception as exc:
            try:
                self.log_message(f"DXcam recording get_latest_frame ignored: {exc}")
            except Exception:
                pass
            return None

    def wait_for_dxcam_frame(self, camera, timeout=0.8):
        """Ждёт первый кадр у уже запущенной DXcam-камеры без camera.grab().

        grab() на некоторых системах подвисает на старом singleton-объекте DXcam.
        Для видеорежима достаточно дождаться get_latest_frame(), поэтому старт
        больше не блокирует GUI на неопределённое время.
        """
        deadline = time.perf_counter() + max(0.05, float(timeout or 0.8))
        frame = None
        while time.perf_counter() < deadline:
            frame = self.get_dxcam_frame_safely(camera, allow_grab=False)
            if frame is not None:
                return frame
            time.sleep(0.015)
        return None

    def ensure_frame_is_copy(self, frame):
        if frame is None:
            return None
        try:
            return frame.copy()
        except Exception:
            return frame

    def append_timing_guard_frame(self, frames, click_perf, handoff_perf, fallback_frame=None):
        """Не даёт первым секундам схлопнуться, если буфер был пустой/разреженный.

        FFmpeg получает rawvideo с постоянным FPS. Если после клика есть только
        один кадр, а подготовка заняла 1–2 секунды, без guard-кадра видео сразу
        перескакивает к живому кадру и выглядит так, будто начало не записалось.
        Добавляем последний известный кадр с timestamp handoff — цикл записи
        восстановит длительность дублированием по timestamp.
        """
        try:
            fps_int = self.get_recording_fps_int()
        except Exception:
            fps_int = 60
        frame_interval = 1.0 / max(1, fps_int)
        handoff_perf = float(handoff_perf or time.perf_counter())
        click_perf = float(click_perf or handoff_perf)
        if not frames:
            if fallback_frame is None:
                return []
            frames = [(click_perf, fallback_frame, self.get_cursor_position())]
        try:
            first_ts = float(frames[0][0])
            last_ts = float(frames[-1][0])
        except Exception:
            first_ts = click_perf
            last_ts = click_perf
        expected_span = max(0.0, handoff_perf - click_perf)
        actual_span = max(0.0, last_ts - first_ts)
        if expected_span > max(0.25, frame_interval * 3) and actual_span + frame_interval * 2 < expected_span:
            last_frame = frames[-1][1] if frames else fallback_frame
            last_cursor = frames[-1][2] if frames else self.get_cursor_position()
            if last_frame is not None:
                frames = list(frames)
                frames.append((handoff_perf, last_frame, last_cursor))
        return frames

    def start_background_preparation(self):
        """Прогрев FFmpeg в фоне, без DXcam-буфера.

        DXcam-буфер специально не запускаем: именно он создавал второй поток,
        который держал singleton-камеру и затем мог подвесить старт записи.
        """
        if not self.running:
            return
        if self._preflight_thread is None or not self._preflight_thread.is_alive():
            self.diagnostic_log("preflight_thread_start_requested")
            self._preflight_thread = threading.Thread(target=self.preflight_worker, daemon=True)
            self._preflight_thread.start()

    def preflight_worker(self):
        """Заранее проверяем FFmpeg/NVENC/ddagrab, чтобы не делать это при старте."""
        self.diagnostic_log("preflight_worker_start")
        try:
            self.run_managed_process([self.ffmpeg_path, "-version"], capture_output=True, text=True, timeout=5, creationflags=self.creation_flags())
            self._ffmpeg_ok_cache = True
        except Exception:
            self._ffmpeg_ok_cache = False
            self.diagnostic_log("preflight_worker_ffmpeg_failed", {"ffmpeg_path": self.ffmpeg_path}, level="ERROR")
            return
        try:
            self.ffmpeg_supports_encoder("h264_nvenc")
        except Exception:
            pass
        ddagrab_ok = False
        try:
            ddagrab_ok = bool(self.ffmpeg_supports_filter("ddagrab"))
        except Exception:
            pass
        if ddagrab_ok:
            self.warm_ddagrab_capture()
        try:
            self.ffmpeg_supports_input_format("wasapi")
        except Exception:
            pass
        self.diagnostic_log("preflight_worker_finish", {
            "ffmpeg_ok": self._ffmpeg_ok_cache,
            "encoder_support_cache": self._encoder_support_cache,
            "filter_support_cache": self._filter_support_cache,
            "input_format_support_cache": self._input_format_support_cache,
        })

    def wait_for_preflight_caches(self, timeout=1.2):
        """Коротко ждёт фоновые проверки FFmpeg, не замораживая окно.

        Если пользователь нажал «Запись» сразу после запуска программы, кэш
        поддержки NVENC/WASAPI/ddagrab может ещё быть пустым. Раньше в такой
        момент программа могла ошибочно перейти на CPU-кодирование или Python
        CoreAudio loopback. Здесь даём preflight небольшой шанс закончиться и
        прокачиваем Tk-цикл, чтобы окно не выглядело зависшим.
        """
        try:
            if self._preflight_thread is None or not self._preflight_thread.is_alive():
                return
            deadline = time.perf_counter() + max(0.0, float(timeout or 0.0))
            while time.perf_counter() < deadline and self._preflight_thread.is_alive():
                try:
                    self.root.update()
                except Exception:
                    break
                time.sleep(0.02)
        except Exception as exc:
            self.log_exception("wait_for_preflight_caches", exc)

    def warm_ddagrab_capture(self):
        """Коротко прогревает Desktop Duplication в фоне, чтобы первый реальный старт был ровнее."""
        if os.name != "nt" or self._ddagrab_warm_done:
            return False
        self._ddagrab_warm_done = True
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "ddagrab=framerate=60:draw_mouse=0:output_idx=0:dup_frames=1",
            "-vf",
            "hwdownload,format=bgra",
            "-frames:v",
            "2",
            "-f",
            "null",
            "-",
        ]
        try:
            self.diagnostic_log("ddagrab_warmup_start", {"command": self.command_to_log_text(command)})
            result = self.run_managed_process(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=6,
                creationflags=self.creation_flags(),
                expected_returncodes=(0,),
            )
            ok = result.returncode == 0
            self.diagnostic_log(
                "ddagrab_warmup_finish",
                {"returncode": result.returncode, "stderr": (result.stderr or "").strip()},
                level="INFO" if ok else "WARN",
            )
            return ok
        except Exception as exc:
            self.diagnostic_log("ddagrab_warmup_failed", {
                "error": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            }, level="WARN")
            return False

    def disable_dxcam_for_session(self, reason=""):
        """Отключает DXcam до перезапуска программы после зависания/гонки.

        У dxcam есть внутренний singleton. Если он однажды застрял между
        фоновым буфером и стартом записи, самый безопасный путь — больше не
        трогать его в этой сессии и использовать FFmpeg-захват.
        """
        self.dxcam_disabled_for_session = True
        self.dxcam_disabled_reason = str(reason or "DXcam временно отключён из-за сбоя старта.")
        try:
            self.log_message(f"DXcam disabled for this session: {self.dxcam_disabled_reason}")
        except Exception:
            pass
        try:
            self.stop_instant_dxcam_buffer(release_camera=True, join_timeout=0.03)
        except Exception:
            pass

    def get_safe_ffmpeg_fallback_backend(self):
        """Возвращает безопасный FFmpeg-захват без долгой проверки в GUI."""
        try:
            if self._filter_support_cache.get("ddagrab") is True:
                return "ddagrab"
        except Exception:
            pass
        # gdigrab старее и медленнее, но почти всегда доступен в Windows-FFmpeg
        # и не требует DXcam singleton. Это аварийный стабильный путь.
        return "gdigrab"

    def should_keep_instant_dxcam_buffer(self):
        # Stable build: never start the DXcam warm buffer. Even an idle DXcam
        # singleton can keep a stale capture object and freeze the next Start.
        return False

    def start_instant_dxcam_buffer(self):
        """DXcam warm buffer is disabled in the stable build."""
        try:
            if self.instant_buffer_stop_event:
                self.instant_buffer_stop_event.set()
        except Exception:
            pass
        return

    def stop_instant_dxcam_buffer(self, release_camera=True, join_timeout=0.25):
        """Останавливает горячий DXcam-буфер без блокировки GUI.

        Важный момент: если поток буфера ещё жив, нельзя отцеплять и release()
        его камеру из другого потока. Иначе получалась гонка: один поток висит в
        get_latest_frame(), второй пытается stop/release того же singleton — после
        этого повторный старт с плавающей панели мог подвесить Tkinter.
        """
        try:
            if self.instant_buffer_stop_event:
                self.instant_buffer_stop_event.set()
        except Exception:
            pass

        thread = self.instant_buffer_thread
        gui_thread = False
        try:
            gui_ident = getattr(self, "gui_thread_ident", None)
            gui_thread = gui_ident is not None and threading.get_ident() == gui_ident
        except Exception:
            gui_thread = False

        # Из GUI-потока не ждём DXcam вообще: максимум даём микрошанс на быстрый
        # выход. Всё остальное буферный поток сделает сам в finally.
        effective_timeout = float(join_timeout or 0.0)
        if gui_thread:
            effective_timeout = min(effective_timeout, 0.03)

        if thread is not None and thread.is_alive() and effective_timeout > 0:
            try:
                thread.join(timeout=effective_timeout)
            except Exception:
                pass

        thread_alive = bool(thread is not None and thread.is_alive())
        if thread_alive:
            try:
                self.log_message("Instant DXcam buffer stop requested; thread is still finishing in background.")
            except Exception:
                pass
            return

        self.instant_buffer_thread = None
        self.instant_buffer_stop_event = None

        if release_camera:
            with self.instant_buffer_lock:
                camera = self.instant_buffer_camera
                self.instant_buffer_camera = None
                self.instant_buffer_ready = False
                self.instant_buffer_frames = []
            self.release_dxcam_camera_safely(camera, label="instant_buffer_camera")
        else:
            with self.instant_buffer_lock:
                self.instant_buffer_ready = False
                self.instant_buffer_frames = []

    def instant_dxcam_buffer_loop(self, stop_event):
        camera = None
        try:
            fps_int = self.get_recording_fps_int()
            camera = self.create_dxcam_camera_safely()
            self.safe_start_dxcam_camera(camera, fps_int)
            with self.instant_buffer_lock:
                self.instant_buffer_camera = camera
                self.instant_buffer_ready = True
                self.instant_buffer_last_error = None
                self.instant_buffer_frames = []

            frame_interval = 1.0 / max(1, fps_int)
            while not stop_event.is_set() and self.should_keep_instant_dxcam_buffer():
                loop_started = time.perf_counter()
                frame = self.get_dxcam_frame_safely(camera, allow_grab=False)

                if frame is not None:
                    # Копия нужна обязательно: DXcam может переиспользовать буфер кадра.
                    if not frame.flags["C_CONTIGUOUS"]:
                        frame = frame.copy()
                    else:
                        frame = frame.copy()
                    cursor_pos = self.get_cursor_position()
                    now = time.perf_counter()
                    with self.instant_buffer_lock:
                        self.instant_buffer_frames.append((now, frame, cursor_pos))
                        self.prune_instant_buffer_frames_locked(now, latest_frame=frame)

                spent = time.perf_counter() - loop_started
                time.sleep(max(0.001, min(0.02, frame_interval - spent)))
        except Exception as exc:
            with self.instant_buffer_lock:
                self.instant_buffer_last_error = str(exc)
                self.instant_buffer_ready = False
        finally:
            # Если камера была передана записи, self.instant_buffer_camera уже не она —
            # тогда не останавливаем её здесь, запись сама освободит камеру.
            should_release = False
            with self.instant_buffer_lock:
                if self.instant_buffer_camera is camera:
                    self.instant_buffer_camera = None
                    self.instant_buffer_ready = False
                    should_release = True
            if should_release and camera is not None:
                self.release_dxcam_camera_safely(camera, label="instant_buffer_finally_camera")

    def get_instant_buffer_snapshot(self):
        with self.instant_buffer_lock:
            camera = self.instant_buffer_camera
            frames = list(self.instant_buffer_frames)
            ready = self.instant_buffer_ready
            error = self.instant_buffer_last_error
        return camera, frames, ready, error

    def take_instant_buffer_for_recording(self):
        """Безопасно передаёт прогретую DXcam-камеру записи.

        Если буферный поток не завершился быстро, камеру НЕ используем для
        записи. Это важнее мгновенного DXcam-старта: зависший/полуживой поток
        DXcam мог держать внутренний lock/singleton, и повторный старт с
        плавающей панели превращался в «Python не отвечает». В такой ситуации
        start_dxcam_segment поднимет fallback на ddagrab/gdigrab.
        """
        stop_event = self.instant_buffer_stop_event
        try:
            if stop_event:
                stop_event.set()
        except Exception as exc:
            self.log_exception("take_instant_buffer_for_recording.set_stop", exc)

        # Сначала отсоединяем камеру от self.instant_buffer_camera, чтобы finally
        # буферного потока не освободил её, если поток успел корректно выйти.
        with self.instant_buffer_lock:
            camera = self.instant_buffer_camera
            frames = list(self.instant_buffer_frames)
            self.instant_buffer_camera = None
            self.instant_buffer_ready = False
            self.instant_buffer_frames = []

        thread = self.instant_buffer_thread
        thread_alive = False
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=0.25)
            except Exception as exc:
                self.log_exception("take_instant_buffer_for_recording.join", exc)
            thread_alive = bool(thread.is_alive())

        if thread_alive:
            self.log_message("Instant DXcam buffer did not stop fast enough; refusing to reuse its camera and falling back safely.")

            def release_later(cam=camera, th=thread):
                try:
                    th.join(timeout=5.0)
                except Exception:
                    pass
                try:
                    if th.is_alive():
                        self.log_message("Instant DXcam buffer thread is still stuck; camera release skipped to avoid deadlock.")
                        return
                except Exception:
                    pass
                self.release_dxcam_camera_safely(cam, label="instant_buffer_handoff_aborted_camera")

            if camera is not None:
                try:
                    threading.Thread(target=release_later, name="release_aborted_dxcam_handoff", daemon=True).start()
                except Exception:
                    pass
            return None, frames

        self.instant_buffer_thread = None
        self.instant_buffer_stop_event = None
        return camera, frames

    def prune_instant_buffer_frames_locked(self, now, latest_frame=None):
        """Ограничивает горячий DXcam-буфер по времени, кадрам и памяти.

        Функция вызывается уже под self.instant_buffer_lock. В обычном режиме
        буфер хранит короткую историю. В момент запуска записи важно не удалить
        кадры, снятые сразу после клика, пока FFmpeg ещё открывается, поэтому
        ориентируемся на recording_start_requested_perf и оставляем окно от
        клика до текущего момента, но при нехватке памяти прореживаем кадры
        равномерно, а не просто выбрасываем самое старое начало.
        """
        frames = self.instant_buffer_frames
        if not frames:
            return
        try:
            fps_int = self.get_recording_fps_int()
        except Exception:
            fps_int = 60
        click_perf = None
        try:
            if self.recording_start_requested_perf and (now - self.recording_start_requested_perf) <= 6.0:
                click_perf = float(self.recording_start_requested_perf)
        except Exception:
            click_perf = None

        cutoff = now - float(getattr(self, "instant_buffer_max_seconds", 3.0))
        if click_perf is not None:
            cutoff = min(cutoff, click_perf - (1.0 / max(1, fps_int)))
        while frames and frames[0][0] < cutoff:
            frames.pop(0)

        max_frames = int(getattr(self, "instant_buffer_max_frames", 120) or 120)
        try:
            frame_bytes = int(getattr(latest_frame, "nbytes", 0) or (frames[-1][1].nbytes if frames else 0))
            max_bytes = int(getattr(self, "instant_buffer_max_bytes", 420 * 1024 * 1024) or 0)
            if frame_bytes > 0 and max_bytes > 0:
                max_frames = min(max_frames, max(6, max_bytes // frame_bytes))
        except Exception:
            pass
        max_frames = max(6, int(max_frames))

        if len(frames) <= max_frames:
            return
        if click_perf is None:
            del frames[:-max_frames]
            return

        # При старте записи прореживаем равномерно, чтобы не потерять именно
        # первые кадры после клика. Поток записи потом восстановит длительность
        # дублированием ближайших кадров по их timestamp.
        n = len(frames)
        if max_frames <= 1:
            self.instant_buffer_frames = [frames[-1]]
            return
        keep = sorted({round(i * (n - 1) / (max_frames - 1)) for i in range(max_frames)})
        self.instant_buffer_frames = [frames[i] for i in keep]

    def select_frames_from_click(self, buffered_frames, click_perf, fallback_frame=None):
        """Берём из буфера кадры начиная максимально близко к нажатию Start."""
        if not buffered_frames:
            if fallback_frame is None:
                return []
            return [(click_perf, fallback_frame, self.get_cursor_position())]
        fps_int = self.get_recording_fps_int()
        tolerance = 0.5 / max(1, fps_int)
        selected = [item for item in buffered_frames if item[0] >= click_perf - tolerance]
        if selected:
            return selected
        before = [item for item in buffered_frames if item[0] < click_perf]
        if before:
            return [before[-1]]
        return [buffered_frames[-1]]

    def check_disk_space_or_warn(self, min_minutes=2):
        """Предупреждает, если на диске мало места под выбранный битрейт.

        Оцениваем расход как (видео+аудио битрейт) и требуем запас хотя бы на
        min_minutes минут. Если мало — спрашиваем, продолжать ли.
        """
        try:
            mbps = normalize_video_bitrate_mbps(self.video_bitrate_var.get(), default=16)
            audio_k = 192
            try:
                audio_k = int(re.sub(r"\D", "", self.audio_bitrate_var.get()) or "192")
            except Exception:
                pass
            bytes_per_min = (mbps * 1_000_000 + audio_k * 1000) / 8 * 60
            need = bytes_per_min * min_minutes
        except Exception:
            return True

        try:
            output_folder = Path(self.output_folder.get().strip() or os.getcwd()).expanduser()
            output_folder.mkdir(parents=True, exist_ok=True)
            temp_folder = self.get_recording_temp_root()
            targets = [
                ("временные сегменты", Path(temp_folder)),
                ("готовое видео", output_folder),
            ]
            checked_volumes = set()
            low_space = []
            for label, folder in targets:
                folder.mkdir(parents=True, exist_ok=True)
                try:
                    volume_key = Path(folder.resolve(strict=False)).anchor.lower() or str(folder)
                except Exception:
                    volume_key = str(folder)
                if volume_key in checked_volumes:
                    continue
                checked_volumes.add(volume_key)
                free = shutil.disk_usage(folder).free
                if free < need:
                    low_space.append((label, folder, free))
        except Exception as exc:
            self.diagnostic_log(
                "disk_space_check_failed",
                {"error": repr(exc)},
                level="WARN",
            )
            return True  # не смогли проверить — не мешаем записи

        if not low_space:
            return True
        need_mb = int(need / (1024 * 1024))
        details = "\n".join(
            f"• {label}: {folder} — свободно ~{int(free / (1024 * 1024))} МБ"
            for label, folder, free in low_space
        )
        return messagebox.askyesno(
            "Мало места на диске",
            f"Недостаточно места для записи:\n{details}\n\n"
            f"При битрейте ~{mbps} Мбит/с для {min_minutes} мин нужно минимум ~{need_mb} МБ.\n\n"
            "Всё равно начать запись?",
        )

    def run_start_countdown(self, seconds=3):
        """Большой отсчёт 3-2-1 по центру экрана перед стартом записи."""
        try:
            if not self.countdown_enabled_var.get():
                return
        except Exception:
            return
        try:
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            try:
                top.attributes("-alpha", 0.85)
            except Exception:
                pass
            lbl = tk.Label(top, text=str(seconds), font=("Segoe UI", 110, "bold"), fg="white", bg="black")
            lbl.pack(padx=60, pady=30)
            top.update_idletasks()
            sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
            w, h = top.winfo_width(), top.winfo_height()
            top.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")
            for n in range(seconds, 0, -1):
                lbl.config(text=str(n))
                end = time.perf_counter() + 1.0
                while time.perf_counter() < end:
                    try:
                        top.update()
                    except Exception:
                        break
                    time.sleep(0.02)
            top.destroy()
        except Exception as exc:
            self.log_exception("run_start_countdown", exc)

    def schedule_auto_stop(self):
        self.cancel_auto_stop()
        try:
            raw = str(self.auto_stop_minutes_var.get()).strip().replace(",", ".")
            minutes = float(raw or "0")
        except Exception:
            minutes = 0
        if minutes > 0:
            try:
                self._auto_stop_after_id = self.root.after(int(minutes * 60_000), self._auto_stop_trigger)
            except Exception:
                self._auto_stop_after_id = None

    def cancel_auto_stop(self):
        after_id = getattr(self, "_auto_stop_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._auto_stop_after_id = None

    def _auto_stop_trigger(self):
        self._auto_stop_after_id = None
        if self.is_recording and not self.is_finalizing:
            try:
                self.status_var.set("Авто-остановка по таймеру.")
            except Exception:
                pass
            self.stop_recording()

    def _region_label(self):
        r = getattr(self, "capture_region", None)
        if r and len(r) == 4:
            return f"Область {int(r[2])}×{int(r[3])} @ ({int(r[0])},{int(r[1])})"
        return "Весь экран"

    @staticmethod
    def normalize_capture_region_drag(start_x, start_y, end_x, end_y, minimum_size=16):
        """Нормализует выделение в координатах виртуального рабочего стола."""
        start_x = int(start_x)
        start_y = int(start_y)
        end_x = int(end_x)
        end_y = int(end_y)
        minimum_size = max(1, int(minimum_size))
        x = min(start_x, end_x)
        y = min(start_y, end_y)
        width = abs(end_x - start_x)
        height = abs(end_y - start_y)
        selected = width >= minimum_size and height >= minimum_size
        details = {
            "status": "selected" if selected else "too_small",
            "start": [start_x, start_y],
            "end": [end_x, end_y],
            "width": width,
            "height": height,
            "minimum_size": minimum_size,
        }
        region = [x, y, width, height] if selected else None
        return region, details

    def select_capture_region(
        self,
        on_done=None,
        allow_while_recording=False,
        hint_text=None,
        on_result=None,
        purpose="recording",
        background_image=None,
        screen_rect=None,
        enable_annotations=False,
    ):
        """Полноэкранный выбор прямоугольной области мышью.

        Старый callback on_done(region) сохраняется для совместимости.
        Новый on_result(region, details) дополнительно получает точную причину
        результата, координаты и длительность выбора для диагностики.
        Координаты берутся в пикселях виртуального рабочего стола, поэтому выбор
        работает и на мультимониторных конфигурациях, включая мониторы с
        отрицательными координатами Windows. Для скриншота выбор можно разрешить
        во время записи через allow_while_recording=True.
        """
        purpose = str(purpose or "recording")
        annotation_enabled = bool(enable_annotations and background_image is not None and purpose == "screenshot")
        selector_started = time.perf_counter()
        done_called = {"v": False}

        def finish(region=None, status="cancelled", details=None):
            if done_called["v"]:
                return
            done_called["v"] = True
            result = {
                "purpose": purpose,
                "status": str(status or "cancelled"),
                "region": list(region) if region else None,
                "elapsed_sec": round(time.perf_counter() - selector_started, 3),
            }
            if isinstance(details, dict):
                result.update(details)
                result["purpose"] = purpose
                result["status"] = str(status or details.get("status") or "cancelled")
                result["region"] = list(region) if region else None
            level = "WARN" if result["status"] in {"selector_error", "release_without_press"} else "INFO"
            log_result = dict(result)
            annotation_commands = log_result.pop("annotations", None)
            if annotation_commands is not None:
                log_result["annotation_count"] = len(annotation_commands)
                log_result["annotation_tools"] = sorted({
                    str(item.get("tool"))
                    for item in annotation_commands
                    if isinstance(item, dict) and item.get("tool")
                })
            self.diagnostic_log("capture_region_selection_finished", log_result, level=level)
            try:
                if on_result:
                    on_result(region, result)
                elif on_done:
                    on_done(region)
            except Exception as exc:
                self.log_exception("capture_region_result_callback", exc)

        if getattr(self, "is_recording", False) and not allow_while_recording:
            messagebox.showinfo("Область", "Нельзя менять область во время записи.")
            finish(status="blocked_while_recording")
            return

        sel = None
        try:
            sel = tk.Toplevel(self.root)
            if screen_rect is None:
                vx, vy, vw, vh = self.get_virtual_screen_rect()
            else:
                vx, vy, vw, vh = [int(value) for value in screen_rect]
            sel.overrideredirect(True)
            sel.geometry(f"{vw}x{vh}{vx:+d}{vy:+d}")
            if background_image is None:
                try:
                    sel.attributes("-alpha", 0.3)
                except Exception:
                    pass
            sel.attributes("-topmost", True)
            canvas = tk.Canvas(sel, cursor="cross", bg="gray20", highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            background_mode = "live_transparent_overlay"
            if background_image is not None:
                if ImageTk is None or Image is None:
                    raise RuntimeError("Pillow ImageTk недоступен для показа сохранённого кадра.")
                display_image = background_image
                resized_display_image = None
                if tuple(background_image.size) != (int(vw), int(vh)):
                    resampling = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
                    resized_display_image = background_image.resize((int(vw), int(vh)), resampling)
                    display_image = resized_display_image
                try:
                    background_photo = ImageTk.PhotoImage(display_image, master=sel)
                finally:
                    if resized_display_image is not None:
                        resized_display_image.close()
                sel._capture_background_photo = background_photo
                canvas.create_image(0, 0, image=background_photo, anchor="nw")
                canvas.create_rectangle(
                    0,
                    0,
                    int(vw),
                    int(vh),
                    fill="black",
                    outline="",
                    stipple="gray50",
                )
                background_mode = "frozen_snapshot_before_selector_focus"
            # Подсказка и панель инструментов являются элементами Canvas, а не
            # отдельными окнами. Служебный интерфейс не попадает в итоговый кадр:
            # скриншот строится из background_image и списка аннотаций.
            hint_item = canvas.create_text(
                int(vw / 2),
                24,
                text=hint_text or "Выдели область для записи мышью.  Esc — отмена.",
                fill="white",
                font=("Segoe UI", 13, "bold"),
                anchor="n",
                justify="center",
                tags=("capture_hint",),
            )
            hint_background = None
            hint_bounds = canvas.bbox(hint_item)
            if hint_bounds:
                left, top, right, bottom = hint_bounds
                hint_background = canvas.create_rectangle(
                    left - 12,
                    top - 6,
                    right + 12,
                    bottom + 6,
                    fill="black",
                    outline="",
                    tags=("capture_hint",),
                )
                canvas.tag_lower(hint_background, hint_item)
            saved_settings = (
                self.settings
                if isinstance(getattr(self, "settings", None), dict)
                else {}
            )
            initial_draw_color = normalize_screenshot_annotation_color(
                saved_settings.get("screenshot_draw_color"),
            )
            initial_arrow_color = normalize_screenshot_annotation_color(
                saved_settings.get("screenshot_arrow_color"),
            )
            initial_draw_size = normalize_screenshot_annotation_size(
                saved_settings.get("screenshot_draw_size"),
                "draw",
            )
            initial_arrow_size = normalize_screenshot_annotation_size(
                saved_settings.get("screenshot_arrow_size"),
                "arrow",
            )
            state = {
                "sx": 0,
                "sy": 0,
                "cx": 0,
                "cy": 0,
                "rect": None,
                "pressed": False,
                "interaction": None,
                "mode": "select",
                "toolbar_press": False,
                "active_item": None,
                "active_global_points": [],
                "active_local_points": [],
                "active_color": initial_draw_color,
                "active_size": initial_draw_size,
                "annotation_records": [],
                "hover_action": None,
                "hover_color_id": None,
                "hover_size": None,
                "panel_dragging": False,
                "panel_drag_start": None,
                "panel_drag_origin": None,
                "panel_drag_moved": False,
                "tool_colors": {
                    "draw": initial_draw_color,
                    "arrow": initial_arrow_color,
                },
                "tool_sizes": {
                    "draw": initial_draw_size,
                    "arrow": initial_arrow_size,
                },
            }
            toolbar_buttons = []
            color_buttons = []
            size_buttons = []
            color_specs = SCREENSHOT_ANNOTATION_COLORS
            panel_x = 8
            panel_y = 64
            panel_width = 0
            panel_height = 42
            color_panel_height = 40
            size_panel_height = 40
            option_panel_gap = 4
            panel_position_source = "near_cursor"
            panel_position_clamped = False

            def annotation_details():
                commands = [dict(record["command"]) for record in state["annotation_records"]]
                return {
                    "annotations": commands,
                    "annotation_count": len(commands),
                    "annotation_tools": sorted({
                        str(command.get("tool")) for command in commands if command.get("tool")
                    }),
                    "annotation_colors": sorted({
                        str(command.get("color")) for command in commands if command.get("color")
                    }),
                    "annotation_sizes": sorted({
                        int(command.get("width")) for command in commands if command.get("width")
                    }),
                }

            def update_hint(text):
                canvas.itemconfigure(hint_item, text=text)
                bounds = canvas.bbox(hint_item)
                if hint_background is not None and bounds:
                    left, top, right, bottom = bounds
                    canvas.coords(
                        hint_background,
                        left - 12,
                        top - 6,
                        right + 12,
                        bottom + 6,
                    )

            if annotation_enabled:
                try:
                    pointer_root_x, pointer_root_y = sel.winfo_pointerxy()
                except Exception:
                    pointer_root_x, pointer_root_y = vx + int(vw / 2), vy + int(vh / 2)
                button_specs = (
                    ("move", "✥", 36),
                    ("select", "▣ Область", 82),
                    ("draw", "✎ Рисовать", 92),
                    ("arrow", "➜ Стрелка", 82),
                    ("undo", "↶ Назад", 72),
                    ("clear", "× Очистить", 84),
                )
                panel_width = sum(spec[2] for spec in button_specs) + (len(button_specs) - 1) * 4 + 12
                try:
                    if "screenshot_toolbar_x" not in saved_settings or "screenshot_toolbar_y" not in saved_settings:
                        raise KeyError("saved screenshot toolbar position is incomplete")
                    panel_x = int(saved_settings.get("screenshot_toolbar_x")) - int(vx)
                    panel_y = int(saved_settings.get("screenshot_toolbar_y")) - int(vy)
                    panel_position_source = "settings"
                except (TypeError, ValueError, OverflowError, KeyError):
                    panel_x = int(pointer_root_x - vx + 22)
                    panel_y = int(pointer_root_y - vy + 22)
                    panel_position_source = "near_cursor"
                requested_panel_position = [int(panel_x), int(panel_y)]
                panel_x = max(8, min(max(8, int(vw) - panel_width - 8), panel_x))
                options_height = color_panel_height + option_panel_gap + size_panel_height
                if panel_y + panel_height + options_height + 14 > int(vh):
                    panel_y = int(pointer_root_y - vy - panel_height - options_height - 22)
                panel_y = max(
                    8,
                    min(
                        max(8, int(vh) - panel_height - options_height - 14),
                        panel_y,
                    ),
                )
                panel_position_clamped = requested_panel_position != [int(panel_x), int(panel_y)]
                canvas.create_rectangle(
                    panel_x,
                    panel_y,
                    panel_x + panel_width,
                    panel_y + panel_height,
                    fill="#171717",
                    outline="#555555",
                    width=1,
                    tags=("capture_toolbar",),
                )
                button_x = panel_x + 6
                for action, label, button_width in button_specs:
                    bounds = (
                        button_x,
                        panel_y + 6,
                        button_x + button_width,
                        panel_y + panel_height - 6,
                    )
                    rect_item = canvas.create_rectangle(
                        *bounds,
                        fill="#303030",
                        outline="#555555",
                        width=1,
                        tags=("capture_toolbar", f"capture_tool_{action}"),
                    )
                    text_item = canvas.create_text(
                        int((bounds[0] + bounds[2]) / 2),
                        int((bounds[1] + bounds[3]) / 2),
                        text=label,
                        fill="white",
                        font=("Segoe UI", 9, "bold"),
                        tags=("capture_toolbar", f"capture_tool_{action}"),
                    )
                    toolbar_buttons.append({
                        "action": action,
                        "bounds": bounds,
                        "rect": rect_item,
                        "text": text_item,
                    })
                    button_x += button_width + 4

            def raise_selector_controls():
                if annotation_enabled:
                    canvas.tag_raise("capture_color_palette")
                    canvas.tag_raise("capture_size_palette")
                    canvas.tag_raise("capture_toolbar")
                canvas.tag_raise("capture_hint")

            def refresh_color_palette(hover_color_id=None):
                selected_color = state["tool_colors"].get(state["mode"])
                for color_button in color_buttons:
                    active = color_button["color"] == selected_color
                    hovered = color_button["id"] == hover_color_id
                    outline = "#ffffff" if active else ("#9ec5ff" if hovered else "#6b7280")
                    width = 3 if active else (2 if hovered else 1)
                    canvas.itemconfigure(color_button["swatch"], outline=outline, width=width)
                raise_selector_controls()

            def refresh_size_palette(hover_size=None):
                selected_size = state["tool_sizes"].get(state["mode"])
                for size_button in size_buttons:
                    active = size_button["size"] == selected_size
                    hovered = size_button["size"] == hover_size
                    fill = "#2563eb" if active else ("#454545" if hovered else "#303030")
                    outline = "#9ec5ff" if active else "#666666"
                    canvas.itemconfigure(
                        size_button["button"],
                        fill=fill,
                        outline=outline,
                        width=2 if active else 1,
                    )
                raise_selector_controls()

            def render_tool_option_palettes():
                canvas.delete("capture_color_palette")
                canvas.delete("capture_size_palette")
                color_buttons.clear()
                size_buttons.clear()
                if state["mode"] not in {"draw", "arrow"}:
                    state["hover_color_id"] = None
                    state["hover_size"] = None
                    raise_selector_controls()
                    return
                active_button = next(
                    (button for button in toolbar_buttons if button["action"] == state["mode"]),
                    None,
                )
                if active_button is None:
                    return
                swatch_size = 22
                swatch_gap = 4
                palette_width = 12 + len(color_specs) * swatch_size + (len(color_specs) - 1) * swatch_gap
                button_left, _button_top, button_right, button_bottom = active_button["bounds"]
                palette_x = int((button_left + button_right - palette_width) / 2)
                palette_x = max(8, min(max(8, int(vw) - palette_width - 8), palette_x))
                palette_y = int(button_bottom + 12)
                canvas.create_rectangle(
                    palette_x,
                    palette_y,
                    palette_x + palette_width,
                    palette_y + color_panel_height,
                    fill="#171717",
                    outline="#555555",
                    width=1,
                    tags=("capture_color_palette",),
                )
                swatch_x = palette_x + 6
                swatch_y = palette_y + int((color_panel_height - swatch_size) / 2)
                for color_id, color_value in color_specs:
                    bounds = (
                        swatch_x,
                        swatch_y,
                        swatch_x + swatch_size,
                        swatch_y + swatch_size,
                    )
                    swatch = canvas.create_oval(
                        *bounds,
                        fill=color_value,
                        outline="#6b7280",
                        width=1,
                        tags=("capture_color_palette", f"capture_color_{color_id}"),
                    )
                    color_buttons.append({
                        "id": color_id,
                        "color": color_value,
                        "bounds": bounds,
                        "swatch": swatch,
                    })
                    swatch_x += swatch_size + swatch_gap
                size_choices = (
                    SCREENSHOT_ARROW_SIZES
                    if state["mode"] == "arrow"
                    else SCREENSHOT_DRAW_SIZES
                )
                size_panel_y = palette_y + color_panel_height + option_panel_gap
                canvas.create_rectangle(
                    palette_x,
                    size_panel_y,
                    palette_x + palette_width,
                    size_panel_y + size_panel_height,
                    fill="#171717",
                    outline="#555555",
                    width=1,
                    tags=("capture_size_palette",),
                )
                canvas.create_text(
                    palette_x + 8,
                    size_panel_y + int(size_panel_height / 2),
                    text="Размер:",
                    fill="white",
                    font=("Segoe UI", 9, "bold"),
                    anchor="w",
                    tags=("capture_size_palette",),
                )
                size_button_width = 30
                size_button_gap = 4
                size_button_x = palette_x + 64
                size_button_y = size_panel_y + 6
                for size_value in size_choices:
                    bounds = (
                        size_button_x,
                        size_button_y,
                        size_button_x + size_button_width,
                        size_button_y + size_panel_height - 12,
                    )
                    size_button = canvas.create_rectangle(
                        *bounds,
                        fill="#303030",
                        outline="#666666",
                        width=1,
                        tags=("capture_size_palette", f"capture_size_{size_value}"),
                    )
                    canvas.create_text(
                        int((bounds[0] + bounds[2]) / 2),
                        int((bounds[1] + bounds[3]) / 2),
                        text=str(size_value),
                        fill="white",
                        font=("Segoe UI", 9, "bold"),
                        tags=("capture_size_palette", f"capture_size_{size_value}"),
                    )
                    size_buttons.append({
                        "size": int(size_value),
                        "bounds": bounds,
                        "button": size_button,
                    })
                    size_button_x += size_button_width + size_button_gap
                refresh_color_palette(state.get("hover_color_id"))
                refresh_size_palette(state.get("hover_size"))

            def refresh_toolbar(hover_action=None):
                for button in toolbar_buttons:
                    action = button["action"]
                    active = action == state["mode"]
                    hovered = action == hover_action
                    fill = "#2563eb" if active else ("#454545" if hovered else "#303030")
                    outline = "#78a9ff" if active else "#555555"
                    canvas.itemconfigure(button["rect"], fill=fill, outline=outline)
                raise_selector_controls()

            def set_mode(mode, log_event=True):
                state["mode"] = mode
                state["pressed"] = False
                state["interaction"] = None
                state["active_item"] = None
                state["active_global_points"] = []
                state["active_local_points"] = []
                if mode == "draw":
                    # `cross` штатно поддерживается Tk на Windows. Имена вроде
                    # `pencil` зависят от платформы и могли дать TclError уже
                    # после нажатия кнопки рисования.
                    canvas.configure(cursor="cross")
                    update_hint("Рисуй мышью. Затем нажми «Область» и выдели итоговый снимок.")
                elif mode == "arrow":
                    canvas.configure(cursor="cross")
                    update_hint("Протяни мышью к острию стрелки. Затем нажми «Область».")
                else:
                    canvas.configure(cursor="cross")
                    update_hint("Выдели область или сначала добавь пометки.  Esc — отмена.")
                render_tool_option_palettes()
                refresh_toolbar(state.get("hover_action"))
                if log_event:
                    self.diagnostic_log("screenshot_annotation_tool_selected", {
                        "purpose": purpose,
                        "tool": mode,
                        "selected_color": state["tool_colors"].get(mode),
                        "selected_size": state["tool_sizes"].get(mode),
                        "color_palette_visible": mode in {"draw", "arrow"},
                        "size_palette_visible": mode in {"draw", "arrow"},
                        "annotation_count": len(state["annotation_records"]),
                    })

            def hit_toolbar(x, y):
                if not annotation_enabled:
                    return None
                for button in toolbar_buttons:
                    left, top, right, bottom = button["bounds"]
                    if left <= x <= right and top <= y <= bottom:
                        return button["action"]
                # Свободные промежутки и фон основной панели тоже служат зоной
                # перетаскивания, а кнопка с символом ✥ делает это заметным.
                if panel_x <= x <= panel_x + panel_width and panel_y <= y <= panel_y + panel_height:
                    return "move"
                return None

            def hit_color_palette(x, y):
                if state["mode"] not in {"draw", "arrow"}:
                    return None
                for color_button in color_buttons:
                    left, top, right, bottom = color_button["bounds"]
                    if left <= x <= right and top <= y <= bottom:
                        return color_button
                return None

            def hit_size_palette(x, y):
                if state["mode"] not in {"draw", "arrow"}:
                    return None
                for size_button in size_buttons:
                    left, top, right, bottom = size_button["bounds"]
                    if left <= x <= right and top <= y <= bottom:
                        return size_button
                return None

            def persist_screenshot_tool_setting(key, value):
                if not isinstance(getattr(self, "settings", None), dict):
                    self.settings = {}
                self.settings[str(key)] = value
                schedule_save = getattr(self, "schedule_save_settings", None)
                if callable(schedule_save):
                    try:
                        schedule_save()
                    except Exception as exc:
                        self.diagnostic_log(
                            "screenshot_tool_setting_save_schedule_failed",
                            {"key": str(key), "error": repr(exc)},
                            level="WARN",
                        )

            def select_annotation_color(color_button):
                tool = state["mode"]
                if tool not in {"draw", "arrow"}:
                    return
                color_value = color_button["color"]
                state["tool_colors"][tool] = color_value
                persist_screenshot_tool_setting(f"screenshot_{tool}_color", color_value)
                refresh_color_palette(state.get("hover_color_id"))
                self.diagnostic_log("screenshot_annotation_color_selected", {
                    "purpose": purpose,
                    "tool": tool,
                    "color_id": color_button["id"],
                    "color": color_value,
                    "annotation_count": len(state["annotation_records"]),
                })

            def select_annotation_size(size_button):
                tool = state["mode"]
                if tool not in {"draw", "arrow"}:
                    return
                size_value = normalize_screenshot_annotation_size(size_button["size"], tool)
                state["tool_sizes"][tool] = size_value
                persist_screenshot_tool_setting(f"screenshot_{tool}_size", size_value)
                refresh_size_palette(state.get("hover_size"))
                self.diagnostic_log("screenshot_annotation_size_selected", {
                    "purpose": purpose,
                    "tool": tool,
                    "size": size_value,
                    "annotation_count": len(state["annotation_records"]),
                })

            def remove_annotation_record(record):
                for item in record.get("items", []):
                    try:
                        canvas.delete(item)
                    except Exception:
                        pass

            def clamp_toolbar_position(x, y):
                options_height = color_panel_height + option_panel_gap + size_panel_height
                max_x = max(8, int(vw) - panel_width - 8)
                max_y = max(8, int(vh) - panel_height - options_height - 14)
                return (
                    max(8, min(max_x, int(round(x)))),
                    max(8, min(max_y, int(round(y)))),
                )

            def move_toolbar_to(x, y):
                nonlocal panel_x, panel_y
                new_x, new_y = clamp_toolbar_position(x, y)
                dx = new_x - panel_x
                dy = new_y - panel_y
                if not dx and not dy:
                    return False
                canvas.move("capture_toolbar", dx, dy)
                for button in toolbar_buttons:
                    left, top, right, bottom = button["bounds"]
                    button["bounds"] = (
                        left + dx,
                        top + dy,
                        right + dx,
                        bottom + dy,
                    )
                panel_x = new_x
                panel_y = new_y
                render_tool_option_palettes()
                refresh_toolbar(state.get("hover_action"))
                return True

            def persist_toolbar_position():
                global_x = int(vx) + int(panel_x)
                global_y = int(vy) + int(panel_y)
                persist_screenshot_tool_setting("screenshot_toolbar_x", global_x)
                persist_screenshot_tool_setting("screenshot_toolbar_y", global_y)
                self.diagnostic_log("screenshot_annotation_toolbar_moved", {
                    "purpose": purpose,
                    "global_position": [global_x, global_y],
                    "local_position": [int(panel_x), int(panel_y)],
                    "virtual_screen": [int(vx), int(vy), int(vw), int(vh)],
                    "visible_screen_clamp_enforced": True,
                })

            def handle_toolbar_action(action):
                if action in {"select", "draw", "arrow"}:
                    set_mode(action)
                    return
                if action == "undo":
                    if state["annotation_records"]:
                        record = state["annotation_records"].pop()
                        remove_annotation_record(record)
                        self.diagnostic_log("screenshot_annotation_undone", {
                            "purpose": purpose,
                            "tool": record["command"].get("tool"),
                            "annotation_count": len(state["annotation_records"]),
                        })
                    refresh_toolbar(state.get("hover_action"))
                    return
                if action == "clear":
                    removed = len(state["annotation_records"])
                    for record in state["annotation_records"]:
                        remove_annotation_record(record)
                    state["annotation_records"].clear()
                    self.diagnostic_log("screenshot_annotations_cleared", {
                        "purpose": purpose,
                        "removed": removed,
                    })
                    refresh_toolbar(state.get("hover_action"))

            def close(status="escape"):
                try:
                    sel.destroy()
                except Exception:
                    pass
                finish(status=status, details=annotation_details())

            def on_press(event):
                color_button = hit_color_palette(event.x, event.y)
                if color_button:
                    state["toolbar_press"] = True
                    state["pressed"] = False
                    select_annotation_color(color_button)
                    return "break"
                size_button = hit_size_palette(event.x, event.y)
                if size_button:
                    state["toolbar_press"] = True
                    state["pressed"] = False
                    select_annotation_size(size_button)
                    return "break"
                toolbar_action = hit_toolbar(event.x, event.y)
                if toolbar_action:
                    if toolbar_action == "move":
                        state["toolbar_press"] = False
                        state["pressed"] = False
                        state["panel_dragging"] = True
                        state["panel_drag_start"] = [int(event.x), int(event.y)]
                        state["panel_drag_origin"] = [int(panel_x), int(panel_y)]
                        state["panel_drag_moved"] = False
                        update_hint("Перемести панель и отпусти мышь — позиция сохранится.")
                        return "break"
                    state["toolbar_press"] = True
                    state["pressed"] = False
                    handle_toolbar_action(toolbar_action)
                    return "break"
                state["toolbar_press"] = False
                state["sx"], state["sy"] = event.x_root, event.y_root
                state["cx"], state["cy"] = event.x, event.y
                state["pressed"] = True
                state["interaction"] = state["mode"]
                if state["mode"] == "select":
                    if state["rect"]:
                        canvas.delete(state["rect"])
                    state["rect"] = canvas.create_rectangle(
                        event.x,
                        event.y,
                        event.x,
                        event.y,
                        outline="#ff3b3b",
                        width=2,
                        tags=("capture_selection_rect",),
                    )
                    self.diagnostic_log("capture_region_selection_started", {
                        "purpose": purpose,
                        "start": [int(event.x_root), int(event.y_root)],
                        "minimum_size": 16,
                        "annotation_count": len(state["annotation_records"]),
                    })
                elif state["mode"] == "draw":
                    state["active_color"] = state["tool_colors"]["draw"]
                    state["active_size"] = state["tool_sizes"]["draw"]
                    state["active_global_points"] = [[int(event.x_root), int(event.y_root)]]
                    state["active_local_points"] = [[int(event.x), int(event.y)]]
                    state["active_item"] = canvas.create_line(
                        event.x,
                        event.y,
                        event.x + 1,
                        event.y + 1,
                        fill=state["active_color"],
                        width=state["active_size"],
                        capstyle=tk.ROUND,
                        joinstyle=tk.ROUND,
                        tags=("capture_annotation",),
                    )
                elif state["mode"] == "arrow":
                    state["active_color"] = state["tool_colors"]["arrow"]
                    state["active_size"] = state["tool_sizes"]["arrow"]
                    arrow_size = int(state["active_size"])
                    state["active_item"] = canvas.create_line(
                        event.x,
                        event.y,
                        event.x,
                        event.y,
                        fill=state["active_color"],
                        width=arrow_size,
                        arrow=tk.LAST,
                        arrowshape=(
                            max(12, arrow_size * 4),
                            max(16, arrow_size * 5),
                            max(6, arrow_size * 2),
                        ),
                        tags=("capture_annotation",),
                    )
                raise_selector_controls()
                return "break"

            def on_drag(event):
                if state["panel_dragging"]:
                    drag_start = state["panel_drag_start"] or [int(event.x), int(event.y)]
                    drag_origin = state["panel_drag_origin"] or [int(panel_x), int(panel_y)]
                    moved = move_toolbar_to(
                        drag_origin[0] + int(event.x) - drag_start[0],
                        drag_origin[1] + int(event.y) - drag_start[1],
                    )
                    state["panel_drag_moved"] = bool(state["panel_drag_moved"] or moved)
                    return "break"
                if not state["pressed"]:
                    return "break"
                if state["interaction"] == "select" and state["rect"]:
                    canvas.coords(state["rect"], state["cx"], state["cy"], event.x, event.y)
                elif state["interaction"] == "draw" and state["active_item"]:
                    points = state["active_local_points"]
                    if points and abs(event.x - points[-1][0]) + abs(event.y - points[-1][1]) < 2:
                        return "break"
                    points.append([int(event.x), int(event.y)])
                    state["active_global_points"].append([int(event.x_root), int(event.y_root)])
                    canvas.coords(state["active_item"], *[value for point in points for value in point])
                elif state["interaction"] == "arrow" and state["active_item"]:
                    canvas.coords(state["active_item"], state["cx"], state["cy"], event.x, event.y)
                raise_selector_controls()
                return "break"

            def on_release(event):
                if state["panel_dragging"]:
                    state["panel_dragging"] = False
                    state["panel_drag_start"] = None
                    state["panel_drag_origin"] = None
                    persist_toolbar_position()
                    set_mode(state["mode"], log_event=False)
                    return "break"
                if state["toolbar_press"]:
                    state["toolbar_press"] = False
                    return "break"
                if not state["pressed"]:
                    if annotation_enabled:
                        self.diagnostic_log("capture_region_release_ignored", {
                            "purpose": purpose,
                            "reason": "release_without_canvas_press",
                        })
                        return "break"
                    close(status="release_without_press")
                    return "break"
                state["pressed"] = False
                interaction = state["interaction"]
                state["interaction"] = None
                if interaction == "draw":
                    points = state["active_global_points"]
                    item = state["active_item"]
                    if points:
                        command = {
                            "tool": "draw",
                            "points": [list(point) for point in points],
                            "color": state["active_color"],
                            "width": state["active_size"],
                        }
                        state["annotation_records"].append({"command": command, "items": [item]})
                        self.diagnostic_log("screenshot_annotation_added", {
                            "purpose": purpose,
                            "tool": "draw",
                            "color": state["active_color"],
                            "size": state["active_size"],
                            "point_count": len(points),
                            "annotation_count": len(state["annotation_records"]),
                        })
                    state["active_item"] = None
                    state["active_global_points"] = []
                    state["active_local_points"] = []
                    refresh_toolbar(state.get("hover_action"))
                    return "break"
                if interaction == "arrow":
                    start = [int(state["sx"]), int(state["sy"])]
                    end = [int(event.x_root), int(event.y_root)]
                    item = state["active_item"]
                    if abs(end[0] - start[0]) + abs(end[1] - start[1]) >= 8:
                        command = {
                            "tool": "arrow",
                            "start": start,
                            "end": end,
                            "color": state["active_color"],
                            "width": state["active_size"],
                        }
                        state["annotation_records"].append({"command": command, "items": [item]})
                        self.diagnostic_log("screenshot_annotation_added", {
                            "purpose": purpose,
                            "tool": "arrow",
                            "color": state["active_color"],
                            "size": state["active_size"],
                            "start": start,
                            "end": end,
                            "annotation_count": len(state["annotation_records"]),
                        })
                    elif item:
                        canvas.delete(item)
                    state["active_item"] = None
                    refresh_toolbar(state.get("hover_action"))
                    return "break"
                region, details = self.normalize_capture_region_drag(
                    state["sx"],
                    state["sy"],
                    event.x_root,
                    event.y_root,
                    minimum_size=16,
                )
                details.update(annotation_details())
                try:
                    sel.destroy()
                except Exception:
                    pass
                finish(region, status=details["status"], details=details)
                return "break"

            def on_motion(event):
                if not annotation_enabled or state["pressed"] or state["panel_dragging"]:
                    return
                hover_action = hit_toolbar(event.x, event.y)
                if hover_action != state["hover_action"]:
                    state["hover_action"] = hover_action
                    refresh_toolbar(hover_action)
                color_button = hit_color_palette(event.x, event.y)
                hover_color_id = color_button["id"] if color_button else None
                if hover_color_id != state["hover_color_id"]:
                    state["hover_color_id"] = hover_color_id
                    refresh_color_palette(hover_color_id)
                size_button = hit_size_palette(event.x, event.y)
                hover_size = size_button["size"] if size_button else None
                if hover_size != state["hover_size"]:
                    state["hover_size"] = hover_size
                    refresh_size_palette(hover_size)

            canvas.bind("<ButtonPress-1>", on_press)
            canvas.bind("<B1-Motion>", on_drag)
            canvas.bind("<ButtonRelease-1>", on_release)
            canvas.bind("<Motion>", on_motion)
            if annotation_enabled:
                set_mode("select", log_event=False)
            else:
                raise_selector_controls()
            sel.bind("<Escape>", lambda _event: close(status="escape"))
            sel.update_idletasks()
            self.diagnostic_log("capture_region_selector_opened", {
                "purpose": purpose,
                "virtual_screen": [int(vx), int(vy), int(vw), int(vh)],
                "minimum_size": 16,
                "hint_widget": "canvas_item",
                "mouse_event_surface": "fullscreen_canvas",
                "annotation_tools_enabled": annotation_enabled,
                "annotation_backend": "screenshot_canvas_v3" if annotation_enabled else None,
                "initial_tool": "select" if annotation_enabled else None,
                "available_annotation_tools": (
                    ["select", "draw", "arrow", "undo", "clear"]
                    if annotation_enabled else []
                ),
                "available_annotation_colors": (
                    [color_value for _color_id, color_value in color_specs]
                    if annotation_enabled else []
                ),
                "available_annotation_sizes": {
                    "draw": list(SCREENSHOT_DRAW_SIZES),
                    "arrow": list(SCREENSHOT_ARROW_SIZES),
                } if annotation_enabled else {},
                "initial_annotation_settings": {
                    "draw": {
                        "color": initial_draw_color,
                        "size": initial_draw_size,
                    },
                    "arrow": {
                        "color": initial_arrow_color,
                        "size": initial_arrow_size,
                    },
                } if annotation_enabled else {},
                "color_palette_behavior": (
                    "persistent_below_active_draw_or_arrow_tool"
                    if annotation_enabled else None
                ),
                "size_palette_behavior": (
                    "persistent_below_color_palette"
                    if annotation_enabled else None
                ),
                "toolbar_position": (
                    [int(vx) + int(panel_x), int(vy) + int(panel_y)]
                    if annotation_enabled else None
                ),
                "toolbar_position_source": panel_position_source if annotation_enabled else None,
                "toolbar_position_clamped": panel_position_clamped if annotation_enabled else False,
                "toolbar_drag_surface": "handle_and_panel_background" if annotation_enabled else None,
                "background_mode": background_mode,
                "background_image_size": (
                    [int(background_image.size[0]), int(background_image.size[1])]
                    if background_image is not None else None
                ),
                "focus_effect": (
                    "transient_panels_preserved_in_frozen_snapshot"
                    if background_image is not None else "live_desktop_may_change_on_focus"
                ),
            })
            try:
                overlay = getattr(self, "annotation_overlay", None)
                if overlay is not None:
                    overlay.make_window_not_recorded(sel)
            except Exception:
                pass
            try:
                sel.grab_set()
                sel.focus_force()
            except Exception:
                pass
        except Exception as exc:
            if sel is not None:
                try:
                    sel.destroy()
                except Exception:
                    pass
            self.log_exception("select_capture_region", exc)
            finish(status="selector_error", details={"error": repr(exc)})

    def start_keys_overlay(self):
        """Экранный оверлей последних нажатых клавиш (для обучающих видео).

        Это обычное topmost-окно, поэтому ddagrab/gdigrab захватывают его прямо
        в видео. Глобальный хук keyboard работает в своём потоке — обновляем GUI
        строго через root.after.
        """
        try:
            if not self.show_keys_overlay_var.get() or not HOTKEY_AVAILABLE:
                return
            import keyboard as _kb
            ov = tk.Toplevel(self.root)
            ov.overrideredirect(True)
            ov.attributes("-topmost", True)
            try:
                ov.attributes("-alpha", 0.8)
            except Exception:
                pass
            ov.configure(bg="black")
            lbl = tk.Label(ov, text="", font=("Consolas", 22, "bold"), fg="white", bg="black", padx=18, pady=8)
            lbl.pack()
            ov.withdraw()
            self._keys_overlay = ov
            self._keys_overlay_label = lbl
            self._keys_recent = []

            def on_key(event):
                try:
                    name = getattr(event, "name", "") or ""
                    if name:
                        self.root.after(0, lambda n=name: self._push_key(n))
                except Exception:
                    pass

            self._keys_hook = _kb.on_press(on_key)
        except Exception as exc:
            self.log_exception("start_keys_overlay", exc)

    def _push_key(self, name):
        try:
            if not self._keys_overlay:
                return
            pretty = name.upper() if len(name) == 1 else name
            self._keys_recent.append(pretty)
            self._keys_recent = self._keys_recent[-6:]
            self._keys_overlay_label.config(text="   ".join(self._keys_recent))
            ov = self._keys_overlay
            ov.update_idletasks()
            sw, sh = ov.winfo_screenwidth(), ov.winfo_screenheight()
            w, h = ov.winfo_width(), ov.winfo_height()
            ov.geometry(f"+{(sw - w) // 2}+{sh - h - 80}")
            ov.deiconify()
            ov.lift()
            self._keys_clear_at = time.perf_counter() + 1.5
            self.root.after(1600, self._maybe_clear_keys)
        except Exception:
            pass

    def _maybe_clear_keys(self):
        try:
            if self._keys_overlay and time.perf_counter() >= getattr(self, "_keys_clear_at", 0):
                self._keys_recent = []
                self._keys_overlay_label.config(text="")
                self._keys_overlay.withdraw()
        except Exception:
            pass

    @staticmethod
    def get_tk_toplevel_hwnd(window):
        """Возвращает Win32 wrapper HWND, а не внутреннее client-окно Tk."""
        if os.name != "nt" or window is None:
            return 0
        try:
            window.update_idletasks()
            client_hwnd = wintypes.HWND(int(window.winfo_id()))
            get_ancestor = ctypes.windll.user32.GetAncestor
            get_ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
            get_ancestor.restype = wintypes.HWND
            wrapper_hwnd = get_ancestor(client_hwnd, 2)  # GA_ROOT
            return int(wrapper_hwnd or client_hwnd.value or 0)
        except Exception:
            return 0

    def make_window_clickthrough(self, window):
        """Делает оверлей некликабельным для мыши на Windows."""
        if os.name != "nt" or window is None:
            return False
        try:
            hwnd = self.get_tk_toplevel_hwnd(window)
            if not hwnd:
                return False
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_TOOLWINDOW = 0x00000080
            required_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_long.argtypes = [wintypes.HWND, ctypes.c_int]
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            set_long.restype = ctypes.c_ssize_t
            hwnd_value = wintypes.HWND(hwnd)
            style = get_long(hwnd_value, GWL_EXSTYLE)
            set_long(hwnd_value, GWL_EXSTYLE, style | required_style)
            applied_style = get_long(hwnd_value, GWL_EXSTYLE)
            return (applied_style & required_style) == required_style
        except Exception:
            return False

    @staticmethod
    def build_cursor_overlay_geometry(width, height, x, y):
        """Строка geometry для fallback вне Win32-позиционирования."""
        width = max(1, int(width))
        height = max(1, int(height))
        x = int(x)
        y = int(y)
        return f"{width}x{height}{x:+d}{y:+d}"

    def position_cursor_overlay_window(self, window, width, height, x, y):
        """Ставит overlay в абсолютные координаты виртуального экрана."""
        if window is None:
            return False
        width = max(1, int(width))
        height = max(1, int(height))
        x = int(x)
        y = int(y)
        if os.name != "nt":
            try:
                window.geometry(self.build_cursor_overlay_geometry(width, height, x, y))
                return True
            except Exception:
                return False
        try:
            hwnd = self.get_tk_toplevel_hwnd(window)
            if not hwnd:
                return False
            HWND_TOPMOST = -1
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            set_window_pos = ctypes.windll.user32.SetWindowPos
            set_window_pos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            set_window_pos.restype = wintypes.BOOL
            return bool(set_window_pos(
                wintypes.HWND(hwnd),
                wintypes.HWND(HWND_TOPMOST),
                x,
                y,
                width,
                height,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            ))
        except Exception:
            return False

    def start_cursor_highlight_overlay(self):
        """Один click-through overlay для подсветки и нестандартного курсора."""
        self.recording_custom_cursor_overlay_ready = False
        try:
            visible = bool(getattr(self, "recording_cursor_visible", True))
            size_percent = normalize_recording_cursor_size_percent(
                getattr(self, "recording_cursor_size_percent", 100)
            )
            highlight = bool(getattr(self, "recording_cursor_highlight", False))
            custom_cursor = visible and size_percent != 100
            if not highlight and not custom_cursor:
                self.stop_cursor_highlight_overlay()
                return False

            self.stop_cursor_highlight_overlay()
            highlight_size = max(
                20,
                min(200, int(getattr(self, "recording_cursor_highlight_size", 70))),
            )
            cursor_size = max(12, int(round(34 * size_percent / 100.0)))
            margin = max(3, int(round(cursor_size * 0.08)))
            ring_width = max(2, int(round(highlight_size * 0.08))) if highlight else 0
            ring_radius = max(10, highlight_size // 2) if highlight else 0
            left_extent = max(margin, ring_radius + ring_width + 2)
            top_extent = max(margin, ring_radius + ring_width + 2)
            right_extent = max(left_extent, cursor_size + margin)
            bottom_extent = max(top_extent, cursor_size + margin)
            overlay_width = left_extent + right_extent + 1
            overlay_height = top_extent + bottom_extent + 1

            transparent = "#ff00ff"
            win = tk.Toplevel(self.root)
            self._cursor_highlight_window = win
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=transparent)
            try:
                win.wm_attributes("-transparentcolor", transparent)
            except Exception as exc:
                raise RuntimeError("Tk не поддерживает прозрачный cursor overlay") from exc
            canvas = tk.Canvas(
                win,
                width=overlay_width,
                height=overlay_height,
                bg=transparent,
                highlightthickness=0,
                bd=0,
            )
            self._cursor_highlight_canvas = canvas
            canvas.pack(fill="both", expand=True)
            hotspot_x = left_extent
            hotspot_y = top_extent
            if highlight:
                canvas.create_oval(
                    hotspot_x - ring_radius,
                    hotspot_y - ring_radius,
                    hotspot_x + ring_radius,
                    hotspot_y + ring_radius,
                    outline="yellow",
                    width=ring_width,
                )
            if custom_cursor:
                scale = cursor_size / 34.0
                outline_points = ((1, 1), (1, 27), (8, 21), (13, 33), (19, 31), (14, 20), (25, 20))
                fill_points = ((4, 5), (4, 21), (9, 16), (14, 29), (16, 28), (11, 16), (19, 16))

                def scaled_points(points):
                    result = []
                    for point_x, point_y in points:
                        result.extend((
                            hotspot_x + point_x * scale,
                            hotspot_y + point_y * scale,
                        ))
                    return result

                canvas.create_polygon(
                    *scaled_points(outline_points),
                    fill="#111111",
                    outline="#111111",
                )
                canvas.create_polygon(
                    *scaled_points(fill_points),
                    fill="#ffffff",
                    outline="#ffffff",
                )

            position = self.get_cursor_position()
            if position is None:
                raise RuntimeError("Windows не вернула позицию курсора для overlay")
            cursor_x, cursor_y = position
            if not self.position_cursor_overlay_window(
                win,
                overlay_width,
                overlay_height,
                cursor_x - left_extent,
                cursor_y - top_extent,
            ):
                raise RuntimeError("Не удалось позиционировать cursor overlay")
            if not self.make_window_clickthrough(win):
                raise RuntimeError("Не удалось включить click-through для cursor overlay")
            self._cursor_overlay_offset_x = left_extent
            self._cursor_overlay_offset_y = top_extent
            self._cursor_overlay_width = overlay_width
            self._cursor_overlay_height = overlay_height
            self.recording_custom_cursor_overlay_ready = bool(custom_cursor)
            self._update_cursor_highlight_overlay()
            return bool(custom_cursor)
        except Exception as exc:
            self.stop_cursor_highlight_overlay()
            self.log_exception("start_cursor_highlight_overlay", exc)
            return False

    def _update_cursor_highlight_overlay(self):
        try:
            win = self._cursor_highlight_window
            if not win or not self.is_recording:
                self._cursor_highlight_job = None
                return
            position = self.get_cursor_position()
            if position is None:
                raise RuntimeError("Windows не вернула позицию курсора")
            cursor_x, cursor_y = position
            if not self.position_cursor_overlay_window(
                win,
                self._cursor_overlay_width,
                self._cursor_overlay_height,
                cursor_x - self._cursor_overlay_offset_x,
                cursor_y - self._cursor_overlay_offset_y,
            ):
                raise RuntimeError("Не удалось обновить позицию cursor overlay")
            win.attributes("-topmost", True)
            self._cursor_highlight_job = self.root.after(16, self._update_cursor_highlight_overlay)
        except Exception:
            try:
                if self._cursor_highlight_window is not None and self.is_recording:
                    self._cursor_highlight_job = self.root.after(80, self._update_cursor_highlight_overlay)
                else:
                    self._cursor_highlight_job = None
            except Exception:
                self._cursor_highlight_job = None

    def stop_cursor_highlight_overlay(self):
        self.recording_custom_cursor_overlay_ready = False
        try:
            if self._cursor_highlight_job is not None:
                try:
                    self.root.after_cancel(self._cursor_highlight_job)
                except Exception:
                    pass
                self._cursor_highlight_job = None
            if self._cursor_highlight_window is not None:
                try:
                    self._cursor_highlight_window.destroy()
                except Exception:
                    pass
            self._cursor_highlight_window = None
            self._cursor_highlight_canvas = None
        except Exception:
            pass

    def stop_keys_overlay(self):
        try:
            if self._keys_hook is not None:
                try:
                    import keyboard as _kb
                    _kb.unhook(self._keys_hook)
                except Exception:
                    pass
                self._keys_hook = None
            if self._keys_overlay is not None:
                try:
                    self._keys_overlay.destroy()
                except Exception:
                    pass
                self._keys_overlay = None
                self._keys_overlay_label = None
        except Exception:
            pass
