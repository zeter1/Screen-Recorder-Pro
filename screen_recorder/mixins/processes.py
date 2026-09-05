from ..shared import *


class ProcessMixin:
    DDAGRAB_FIRST_FRAME_TIMEOUT_SECONDS = 12.0
    DDAGRAB_VIDEO_STALL_SECONDS = 6.0
    COREAUDIO_SEGMENT_ROLLOVER_SECONDS = 4.0 * 60.0 * 60.0
    MAX_CAPTURE_RECOVERY_ATTEMPTS = 3
    DDAGRAB_ACCESS_LOST_MARKERS = (
        "acquirenextframe failed: 887a0026",
        "dxgi_error_access_lost",
    )

    @staticmethod
    def classify_recording_video_progress_health(
        capture_backend,
        now_perf,
        segment_started_perf,
        last_frame_advance_perf,
        last_frame_value,
        first_frame_timeout_seconds=12.0,
        stall_seconds=6.0,
    ):
        if str(capture_backend or "").strip().lower() != "ddagrab":
            return {"status": "not_applicable", "stalled_for_seconds": None}
        try:
            now_value = float(now_perf)
            started_value = float(segment_started_perf)
        except Exception:
            return {"status": "waiting_for_segment_clock", "stalled_for_seconds": None}
        if last_frame_value is None or last_frame_advance_perf is None:
            elapsed = max(0.0, now_value - started_value)
            return {
                "status": "first_frame_timeout" if elapsed >= float(first_frame_timeout_seconds) else "waiting_for_first_frame",
                "stalled_for_seconds": round(elapsed, 6),
            }
        try:
            stalled_for = max(0.0, now_value - float(last_frame_advance_perf))
        except Exception:
            return {"status": "waiting_for_frame_clock", "stalled_for_seconds": None}
        return {
            "status": "frame_stalled" if stalled_for >= float(stall_seconds) else "healthy",
            "stalled_for_seconds": round(stalled_for, 6),
        }

    @classmethod
    def classify_ffmpeg_capture_stderr(cls, text, capture_backend):
        """Распознаёт только доказанную потерю Desktop Duplication."""
        if str(capture_backend or "").strip().lower() != "ddagrab":
            return None
        normalized = str(text or "").lower()
        if any(marker in normalized for marker in cls.DDAGRAB_ACCESS_LOST_MARKERS):
            return "dxgi_access_lost"
        return None

    @staticmethod
    def classify_input_desktop_name(name, open_succeeded=True):
        if not open_succeeded:
            return "unavailable"
        normalized = str(name or "").strip().casefold()
        if normalized == "default":
            return "default"
        if normalized:
            return "non_default"
        return "unknown"

    @classmethod
    def get_input_desktop_state(cls):
        """Возвращает активный Windows desktop без переключения или GUI-вызовов."""
        if os.name != "nt":
            return {"status": "not_windows", "name": None, "winerror": None}

        desktop_handle = None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            open_input_desktop = user32.OpenInputDesktop
            open_input_desktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_input_desktop.restype = wintypes.HANDLE
            get_user_object_information = user32.GetUserObjectInformationW
            get_user_object_information.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ]
            get_user_object_information.restype = wintypes.BOOL
            close_desktop = user32.CloseDesktop
            close_desktop.argtypes = [wintypes.HANDLE]
            close_desktop.restype = wintypes.BOOL

            # DESKTOP_READOBJECTS нужен только для чтения имени; desktop не
            # активируется и пользовательский ввод не затрагивается.
            desktop_handle = open_input_desktop(0, False, 0x0001)
            if not desktop_handle:
                return {
                    "status": "unavailable",
                    "name": None,
                    "winerror": int(ctypes.get_last_error() or 0),
                }

            needed = wintypes.DWORD(0)
            get_user_object_information(desktop_handle, 2, None, 0, ctypes.byref(needed))
            wchar_count = max(
                2,
                int(needed.value // ctypes.sizeof(ctypes.c_wchar)) + 1,
            )
            buffer = ctypes.create_unicode_buffer(wchar_count)
            if not get_user_object_information(
                desktop_handle,
                2,
                buffer,
                ctypes.sizeof(buffer),
                ctypes.byref(needed),
            ):
                return {
                    "status": "unknown",
                    "name": None,
                    "winerror": int(ctypes.get_last_error() or 0),
                }
            name = buffer.value
            return {
                "status": cls.classify_input_desktop_name(name, open_succeeded=True),
                "name": name,
                "winerror": None,
            }
        except Exception as exc:
            return {
                "status": "unknown",
                "name": None,
                "winerror": None,
                "error": repr(exc),
            }
        finally:
            if desktop_handle:
                try:
                    close_desktop(desktop_handle)
                except Exception:
                    pass

    @staticmethod
    def should_restart_after_capture_access_lost(desktop_status, waited_seconds=0.0):
        status = str(desktop_status or "").strip().lower()
        if status in {"default", "not_windows"}:
            return True
        if status in {"non_default", "unavailable"}:
            return False
        # Если API проверки desktop недоступен, не зависаем навсегда: даём
        # Windows короткое окно на возврат и затем используем обычный recovery.
        try:
            return float(waited_seconds or 0.0) >= 2.0
        except Exception:
            return False

    @staticmethod
    def capture_signal_matches(signal, session_id, segment_index, process_generation, pid):
        if not isinstance(signal, dict):
            return False
        try:
            return (
                str(signal.get("recording_session_id")) == str(session_id)
                and int(signal.get("segment_index", -1)) == int(segment_index)
                and int(signal.get("process_generation", -1)) == int(process_generation)
                and int(signal.get("ffmpeg_pid", -1)) == int(pid)
            )
        except Exception:
            return False

    def start_ffmpeg_stderr_reader(
        self,
        process,
        log_path,
        segment_path,
        capture_backend,
        process_generation,
    ):
        """Дренирует stderr, пишет его в лог и передаёт только data-сигнал GUI."""
        stream = getattr(process, "stderr", None)
        if process is None or stream is None:
            return None

        session_id = self.recording_session_id
        segment_index = self.segment_index
        ffmpeg_pid = getattr(process, "pid", None)
        signal_queue = self.recording_capture_signal_queue

        def worker():
            scan_tail = ""
            signal_sent = False
            last_flush_perf = 0.0
            try:
                # Бинарный append сохраняет stderr побайтно, включая исходные
                # CR/LF и возможную локальную кодировку FFmpeg. Декодирование
                # ниже используется только для поиска ASCII DXGI-маркера.
                with open(log_path, "ab", buffering=0) as stderr_log:
                    read_chunk = getattr(stream, "read1", stream.read)
                    while True:
                        raw = read_chunk(4096)
                        if not raw:
                            break
                        if isinstance(raw, bytes):
                            raw_bytes = raw
                            text = raw.decode("utf-8", errors="replace")
                        else:
                            text = str(raw)
                            raw_bytes = text.encode("utf-8", errors="replace")
                        stderr_log.write(raw_bytes)

                        combined = scan_tail + text
                        classification = self.classify_ffmpeg_capture_stderr(
                            combined,
                            capture_backend,
                        )
                        if classification and not signal_sent:
                            detected_perf = time.perf_counter()
                            signal = {
                                "kind": classification,
                                "recording_session_id": session_id,
                                "segment_index": segment_index,
                                "segment_path": str(segment_path),
                                "capture_backend": str(capture_backend),
                                "process_generation": int(process_generation),
                                "ffmpeg_pid": ffmpeg_pid,
                                "detected_perf": detected_perf,
                                "detected_at": datetime.now().isoformat(timespec="milliseconds"),
                                "stderr_excerpt": combined[-600:],
                            }
                            try:
                                signal_queue.put_nowait(signal)
                                signal_sent = True
                            except queue.Full:
                                self.log_message(
                                    "Capture signal queue is full; DXGI access-lost signal was not queued."
                                )

                        scan_tail = combined[-256:]
                        now_perf = time.perf_counter()
                        if classification or now_perf - last_flush_perf >= 1.0:
                            stderr_log.flush()
                            last_flush_perf = now_perf
            except Exception as exc:
                self.log_exception("ffmpeg_stderr_reader", exc)

        thread = threading.Thread(
            target=worker,
            name=f"ffmpeg_stderr_reader_{ffmpeg_pid or 'unknown'}",
            daemon=True,
        )
        self.recording_stderr_threads.append({
            "process": process,
            "pid": ffmpeg_pid,
            "generation": int(process_generation),
            "thread": thread,
        })
        thread.start()
        return thread

    def wait_for_ffmpeg_stderr_reader(self, process, timeout=1.0):
        """Ограниченно ждёт EOF stderr после завершения принадлежащего процесса."""
        matching = []
        for entry in list(getattr(self, "recording_stderr_threads", []) or []):
            if entry.get("process") is process:
                matching.append(entry)
        for entry in matching:
            thread = entry.get("thread")
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, float(timeout or 0.0)))
            if thread and thread.is_alive():
                self.diagnostic_log(
                    "ffmpeg_stderr_reader_join_timeout",
                    {
                        "recording_session_id": self.recording_session_id,
                        "pid": entry.get("pid"),
                        "process_generation": entry.get("generation"),
                        "timeout_seconds": timeout,
                    },
                    level="WARN",
                )
        self.recording_stderr_threads = [
            entry
            for entry in list(getattr(self, "recording_stderr_threads", []) or [])
            if (entry.get("thread") and entry["thread"].is_alive())
        ]

    def _consume_current_capture_signal(self, process):
        selected = None
        stale_count = 0
        signal_queue = getattr(self, "recording_capture_signal_queue", None)
        if signal_queue is None:
            return None
        while True:
            try:
                signal = signal_queue.get_nowait()
            except queue.Empty:
                break
            if self.capture_signal_matches(
                signal,
                self.recording_session_id,
                self.segment_index,
                self.recording_process_generation,
                getattr(process, "pid", None),
            ):
                selected = signal
            else:
                stale_count += 1
        if stale_count:
            self.diagnostic_log(
                "stale_capture_signals_ignored",
                {
                    "recording_session_id": self.recording_session_id,
                    "count": stale_count,
                    "segment_index": self.segment_index,
                    "process_generation": self.recording_process_generation,
                    "pid": getattr(process, "pid", None),
                },
                level="INFO",
            )
        return selected

    def _request_ddagrab_capture_recovery(
        self,
        process,
        video_health,
        reason_kind,
        event_name,
        reason,
        extra_details=None,
        append_problem=True,
    ):
        self.current_segment_video_stall_detected = True
        attempts = int(getattr(self, "recording_capture_recovery_attempts", 0) or 0)
        details = {
            "recording_session_id": self.recording_session_id,
            "segment_index": self.segment_index,
            "segment_path": str(self.segments[-1]) if self.segments else None,
            "capture_backend": self.recording_capture_backend,
            "pid": getattr(process, "pid", None),
            "process_generation": self.recording_process_generation,
            "video_health": dict(video_health or {}),
            "last_video_frame": self.current_segment_last_video_frame_value,
            "last_video_frame_out_time_seconds": self.current_segment_last_video_frame_out_time_seconds,
            "recovery_attempts_before": attempts,
            "recovery_attempt_limit": self.MAX_CAPTURE_RECOVERY_ATTEMPTS,
        }
        details.update(dict(extra_details or {}))
        if attempts >= self.MAX_CAPTURE_RECOVERY_ATTEMPTS:
            exhausted_reason = (
                "ddagrab не удалось восстановить после нескольких последовательных "
                "попыток. Сохраняю доступные сегменты."
            )
            self.recording_failure_reason = exhausted_reason
            details["reason"] = exhausted_reason
            self.diagnostic_log(
                "recording_video_stall_recovery_exhausted",
                details,
                level="ERROR",
            )
            self.append_problem_error(
                "recording_video_stall_recovery_exhausted",
                exhausted_reason,
            )
            try:
                self.status_var.set(exhausted_reason)
            except Exception:
                pass
            self.stop_recording()
            return False

        self.recording_capture_recovery_attempts = attempts + 1
        self.recording_failure_reason = reason
        details["reason"] = reason
        details["recovery_attempt"] = self.recording_capture_recovery_attempts
        self.diagnostic_log(event_name, details, level="WARN")
        if append_problem:
            self.append_problem_error(event_name, reason)
        return self.request_automatic_segment_restart(reason_kind, details)

    @staticmethod
    def calculate_coreaudio_segment_rollover_seconds(
        sample_rate,
        safe_data_bytes=3_500_000_000,
        maximum_seconds=14400.0,
        safety_ratio=0.80,
    ):
        try:
            rate = max(1.0, float(sample_rate or 48000.0))
            safe_bytes = max(1.0, float(safe_data_bytes))
            ratio = min(0.95, max(0.10, float(safety_ratio)))
            maximum = max(60.0, float(maximum_seconds))
        except Exception:
            return 14400.0
        pcm_bytes_per_second = rate * 2.0 * 2.0
        riff_limited_seconds = safe_bytes / pcm_bytes_per_second * ratio
        return max(60.0, min(maximum, riff_limited_seconds))

    @staticmethod
    def should_rollover_coreaudio_segment(segment_started_perf, now_perf, recorder_active, limit_seconds=14400.0):
        if not recorder_active:
            return False
        try:
            return max(0.0, float(now_perf) - float(segment_started_perf)) >= float(limit_seconds)
        except Exception:
            return False

    def is_gui_thread(self):
        """True, если код выполняется в основном Tkinter-потоке.

        В GUI-потоке нельзя делать долгие FFmpeg/DXcam/TaskKill-проверки:
        Windows помечает окно как «Не отвечает». Поэтому тяжёлые проверки
        либо берут уже прогретый кэш, либо выполняются в фоне.
        """
        try:
            gui_ident = getattr(self, "gui_thread_ident", None)
            return gui_ident is not None and threading.get_ident() == gui_ident
        except Exception:
            return False

    def cancel_recording_watchdog(self):
        job = getattr(self, "recording_watchdog_job", None)
        self.recording_watchdog_job = None
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass

    def schedule_recording_watchdog(self, delay_ms=500):
        """Следит, что FFmpeg не завершился сам, пока интерфейс показывает REC."""
        self.cancel_recording_watchdog()
        if (
            self.running
            and self.is_recording
            and not self.is_paused
            and not self.is_finalizing
            and not getattr(self, "is_pause_transitioning", False)
        ):
            self.recording_watchdog_job = self.root.after(
                max(100, int(delay_ms)),
                self._recording_watchdog_tick,
            )

    def _recording_watchdog_tick(self):
        self.recording_watchdog_job = None
        if (
            not self.running
            or not self.is_recording
            or self.is_paused
            or self.is_finalizing
            or getattr(self, "is_pause_transitioning", False)
        ):
            return

        with self.process_lock:
            process = self.process

        capture_signal = None
        if process is not None:
            capture_signal = self._consume_current_capture_signal(process)
        if capture_signal and capture_signal.get("kind") == "dxgi_access_lost":
            self.remember_capture_recovery_segment(capture_signal)
            self.current_capture_access_lost = dict(capture_signal)
            self.current_capture_access_lost_wait_logged = False
            self.current_segment_video_stall_detected = True
            self.diagnostic_log(
                "ddagrab_access_lost_detected",
                {
                    **dict(capture_signal),
                    "stop_frame_padding": "finite_padding_pending_after_segment_close",
                    "worker_touched_tkinter": False,
                },
                level="WARN",
            )
            self.append_problem_error(
                "ddagrab_access_lost_detected",
                "Windows временно сделал Desktop Duplication недоступным. "
                "При сохранении недоступный интервал будет дополнен последним кадром.",
            )

        pending_access_lost = getattr(self, "current_capture_access_lost", None)
        if process is not None and pending_access_lost:
            now_perf = time.perf_counter()
            try:
                detected_perf = float(pending_access_lost.get("detected_perf") or now_perf)
            except Exception:
                detected_perf = now_perf
            waited_seconds = max(0.0, now_perf - detected_perf)
            desktop_state = self.get_input_desktop_state()
            if not self.should_restart_after_capture_access_lost(
                desktop_state.get("status"),
                waited_seconds,
            ):
                if not self.current_capture_access_lost_wait_logged:
                    self.current_capture_access_lost_wait_logged = True
                    self.diagnostic_log(
                        "ddagrab_recovery_waiting_for_default_desktop",
                        {
                            "recording_session_id": self.recording_session_id,
                            "segment_index": self.segment_index,
                            "pid": getattr(process, "pid", None),
                            "process_generation": self.recording_process_generation,
                            "input_desktop": desktop_state,
                            "stop_frame_padding": "pending_finalization",
                        },
                        level="WARN",
                    )
                    try:
                        self.status_var.set(
                            "Windows открыл защищённый экран. Ожидаю возврата; "
                            "недоступный интервал будет дополнен стоп-кадром при сохранении."
                        )
                    except Exception:
                        pass
                self.schedule_recording_watchdog(delay_ms=250)
                return

            with self.recording_progress_lock:
                last_frame = self.current_segment_last_video_frame_value
                last_frame_out_time = self.current_segment_last_video_frame_out_time_seconds
            details = {
                "trigger_source": "ffmpeg_stderr",
                "input_desktop": desktop_state,
                "capture_unavailable_seconds": round(waited_seconds, 6),
                "last_video_frame": last_frame,
                "last_video_frame_out_time_seconds": last_frame_out_time,
                "stop_frame_padding": "finite_padding_pending_after_segment_close",
            }
            self.current_capture_access_lost = None
            self.current_capture_access_lost_wait_logged = False
            reason = (
                "Windows временно переключил запись на защищённый рабочий стол. "
                "Недоступный интервал будет дополнен стоп-кадром при сохранении. "
                "После возврата обычного рабочего стола захват продолжится в новом сегменте."
            )
            self._request_ddagrab_capture_recovery(
                process,
                {
                    "status": "dxgi_access_lost",
                    "stalled_for_seconds": round(waited_seconds, 6),
                },
                "ddagrab_access_lost",
                "ddagrab_secure_desktop_recovery_requested",
                reason,
                extra_details=details,
                append_problem=False,
            )
            return

        return_code = None
        try:
            if process is not None:
                return_code = process.poll()
        except Exception as exc:
            self.log_exception("recording_watchdog.poll", exc)
            self.schedule_recording_watchdog()
            return

        if process is not None and return_code is None:
            now_perf = time.perf_counter()
            with self.recording_progress_lock:
                video_health = self.classify_recording_video_progress_health(
                    self.recording_capture_backend,
                    now_perf,
                    self.segment_started_at,
                    self.current_segment_last_video_frame_advance_perf,
                    self.current_segment_last_video_frame_value,
                    first_frame_timeout_seconds=self.DDAGRAB_FIRST_FRAME_TIMEOUT_SECONDS,
                    stall_seconds=self.DDAGRAB_VIDEO_STALL_SECONDS,
                )
                last_frame = self.current_segment_last_video_frame_value
                last_frame_out_time = self.current_segment_last_video_frame_out_time_seconds

            if video_health.get("status") in {"first_frame_timeout", "frame_stalled"}:
                reason = (
                    "ddagrab перестал отдавать новые видеокадры; текущий сегмент "
                    "закрывается, запись продолжится в новом сегменте."
                )
                self._request_ddagrab_capture_recovery(
                    process,
                    video_health,
                    "ddagrab_video_stall",
                    "recording_video_frames_stalled",
                    reason,
                    extra_details={
                        "trigger_source": "ffmpeg_progress",
                        "last_video_frame": last_frame,
                        "last_video_frame_out_time_seconds": last_frame_out_time,
                    },
                )
                return

            attempts = int(getattr(self, "recording_capture_recovery_attempts", 0) or 0)
            try:
                healthy_segment_seconds = max(
                    0.0,
                    now_perf - float(self.segment_started_at or now_perf),
                )
            except Exception:
                healthy_segment_seconds = 0.0
            if (
                video_health.get("status") == "healthy"
                and attempts > 0
                and healthy_segment_seconds >= 3.0
                # Process age and audio out_time do not prove video recovery.
                and float(last_frame or 0) >= max(
                    2.0, 3.0 * float(getattr(self, "recording_effective_fps", None)
                                    or getattr(self, "recording_requested_fps", None) or 30)
                )
                and now_perf - float(self.current_segment_last_video_frame_advance_perf or 0) <= 1.0
            ):
                self.recording_capture_recovery_attempts = 0
                self.diagnostic_log(
                    "capture_recovery_attempt_counter_reset",
                    {
                        "recording_session_id": self.recording_session_id,
                        "segment_index": self.segment_index,
                        "previous_attempts": attempts,
                        "healthy_segment_seconds": round(healthy_segment_seconds, 6),
                    },
                    level="INFO",
                )

            coreaudio_recorder = getattr(self, "current_python_loopback_recorder", None)
            coreaudio_rollover_seconds = self.calculate_coreaudio_segment_rollover_seconds(
                getattr(coreaudio_recorder, "output_sample_rate", None),
                maximum_seconds=self.COREAUDIO_SEGMENT_ROLLOVER_SECONDS,
            )
            if self.should_rollover_coreaudio_segment(
                self.segment_started_at,
                now_perf,
                coreaudio_recorder is not None,
                limit_seconds=coreaudio_rollover_seconds,
            ):
                details = {
                    "recording_session_id": self.recording_session_id,
                    "segment_index": self.segment_index,
                    "segment_path": str(self.segments[-1]) if self.segments else None,
                    "elapsed_seconds": round(max(0.0, now_perf - float(self.segment_started_at)), 6),
                    "limit_seconds": coreaudio_rollover_seconds,
                    "sample_rate": getattr(coreaudio_recorder, "output_sample_rate", None),
                }
                self.diagnostic_log("coreaudio_segment_rollover_requested", details)
                self.request_automatic_segment_restart("coreaudio_wav_size_guard", details)
                return
            self.schedule_recording_watchdog()
            return

        if process is None:
            reason = "Процесс записи FFmpeg неожиданно отсутствует."
        else:
            reason = f"FFmpeg неожиданно завершил запись с кодом {return_code}."
        self.recording_failure_reason = reason
        self.diagnostic_log(
            "recording_process_died_unexpectedly",
            {
                "recording_session_id": self.recording_session_id,
                "pid": getattr(process, "pid", None),
                "return_code": return_code,
                "segments": [str(path) for path in self.segments],
                "reason": reason,
            },
            level="ERROR",
        )
        self.append_problem_error("recording_process_died_unexpectedly", reason)
        try:
            self.status_var.set(reason + " Сохраняю доступную часть записи...")
        except Exception:
            pass
        # Обычный путь остановки проверит сегмент и сохранит всё, что FFmpeg
        # успел корректно записать. Пользователь не останется с ложным REC.
        self.stop_recording()

    @staticmethod
    def creation_flags():
        if os.name == "nt":
            return subprocess.CREATE_NO_WINDOW
        return 0

    @staticmethod
    def recording_creation_flags():
        if os.name == "nt":
            return subprocess.CREATE_NO_WINDOW | getattr(subprocess, "HIGH_PRIORITY_CLASS", 0)
        return 0

    def subprocess_cwd(self):
        """Безопасная рабочая папка для FFmpeg.

        Если FFmpeg запускается без cwd, он наследует текущую папку Python.
        Из-за этого даже фоновые индикаторы звука могут держать папку проекта
        или папку сохранения заблокированной в File Locksmith. Все дочерние
        процессы теперь стартуют из служебной папки данных.
        """
        for folder in (DATA_DIR, Path(tempfile.gettempdir())):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                return str(folder)
            except Exception:
                continue
        return None

    def register_child_process(self, process):
        if process is None:
            return process
        try:
            with self.child_processes_lock:
                self.child_processes.add(process)
        except Exception:
            pass
        return process

    def unregister_child_process(self, process):
        if process is None:
            return
        meta = None
        try:
            with self.child_processes_lock:
                self.child_processes.discard(process)
                meta = self._process_meta.pop(id(process), None)
        except Exception:
            pass
        if meta:
            try:
                returncode = process.poll()
                expected_returncodes = meta.get("expected_returncodes", (0,))
                expected = returncode is None or returncode in expected_returncodes
                self.diagnostic_log("subprocess_popen_finish", {
                    "process_log_id": meta.get("process_log_id"),
                    "pid": getattr(process, "pid", None),
                    "returncode": returncode,
                    "expected_returncode": expected,
                    "expected_returncodes": expected_returncodes,
                    "elapsed_sec": round(time.perf_counter() - float(meta.get("started_perf", time.perf_counter())), 3),
                    "command": meta.get("command"),
                    "name": meta.get("name"),
                }, level="INFO" if expected else "WARN")
            except Exception:
                pass

    def start_managed_process(self, command, **kwargs):
        """Запускает subprocess.Popen под контролем приложения.

        Важно: cwd задаётся всегда, а процесс регистрируется для последующего
        taskkill/kill при выходе. Это главный фикс зависших ffmpeg.exe.
        """
        expected_returncodes = kwargs.pop("expected_returncodes", (0,))
        try:
            expected_returncodes = tuple(int(code) for code in expected_returncodes)
        except Exception:
            expected_returncodes = (0,)
        kwargs.setdefault("cwd", self.subprocess_cwd())
        self._process_seq += 1
        process_log_id = self._process_seq
        started_perf = time.perf_counter()
        command_text = self.command_to_log_text(command)
        self.diagnostic_log("subprocess_popen_start", {
            "process_log_id": process_log_id,
            "command": command_text,
            "cwd": kwargs.get("cwd"),
            "stdin": self.stream_to_log_text(kwargs.get("stdin")),
            "stdout": self.stream_to_log_text(kwargs.get("stdout")),
            "stderr": self.stream_to_log_text(kwargs.get("stderr")),
            "text": kwargs.get("text"),
            "encoding": kwargs.get("encoding"),
            "expected_returncodes": expected_returncodes,
            "creationflags": kwargs.get("creationflags"),
        })
        if self.is_ffmpeg_related_command(command):
            self.append_ffmpeg_problem_log("Popen start", command=command, extra={
                "process_log_id": process_log_id,
                "cwd": kwargs.get("cwd"),
                "stdin": self.stream_to_log_text(kwargs.get("stdin")),
                "stdout": self.stream_to_log_text(kwargs.get("stdout")),
                "stderr": self.stream_to_log_text(kwargs.get("stderr")),
                "expected_returncodes": expected_returncodes,
            })
        try:
            process = subprocess.Popen(command, **kwargs)
        except Exception as exc:
            error_details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self.diagnostic_log("subprocess_popen_failed", {
                "process_log_id": process_log_id,
                "command": command_text,
                "cwd": kwargs.get("cwd"),
                "error": error_details,
            }, level="ERROR")
            if self.is_ffmpeg_related_command(command):
                self.append_ffmpeg_problem_log("Popen failed", command=command, stderr=error_details, extra={"process_log_id": process_log_id})
            self.append_problem_error("subprocess_popen_failed", error_details)
            raise
        try:
            with self.child_processes_lock:
                self._process_meta[id(process)] = {
                    "process_log_id": process_log_id,
                    "started_perf": started_perf,
                    "command": command_text,
                    "expected_returncodes": expected_returncodes,
                    "name": Path(str(command[0])).name if isinstance(command, (list, tuple)) and command else str(command)[:80],
                }
        except Exception:
            pass
        self.diagnostic_log("subprocess_popen_started", {
            "process_log_id": process_log_id,
            "pid": getattr(process, "pid", None),
            "elapsed_start_sec": round(time.perf_counter() - started_perf, 3),
        })
        self.register_child_process(process)
        return process

    def run_managed_process(self, command, **kwargs):
        expected_returncodes = kwargs.pop("expected_returncodes", (0,))
        try:
            expected_returncodes = tuple(int(code) for code in expected_returncodes)
        except Exception:
            expected_returncodes = (0,)
        kwargs.setdefault("cwd", self.subprocess_cwd())
        self._process_seq += 1
        process_log_id = self._process_seq
        started_perf = time.perf_counter()
        command_text = self.command_to_log_text(command)
        expected_device_probe = self.is_expected_ffmpeg_device_probe_command(command)
        self.diagnostic_log("subprocess_run_start", {
            "process_log_id": process_log_id,
            "command": command_text,
            "cwd": kwargs.get("cwd"),
            "timeout": kwargs.get("timeout"),
            "expected_returncodes": expected_returncodes,
            "capture_output": kwargs.get("capture_output"),
            "stdout": self.stream_to_log_text(kwargs.get("stdout")),
            "stderr": self.stream_to_log_text(kwargs.get("stderr")),
            "text": kwargs.get("text"),
            "encoding": kwargs.get("encoding"),
            "creationflags": kwargs.get("creationflags"),
            "expected_device_probe": expected_device_probe,
        })
        try:
            result = subprocess.run(command, **kwargs)
            expected = result.returncode in expected_returncodes
            stdout_for_log = getattr(result, "stdout", None)
            stderr_for_log = getattr(result, "stderr", None)
            if expected_device_probe:
                stderr_for_log = self.normalize_expected_ffmpeg_probe_stderr(stderr_for_log)
            self.diagnostic_log("subprocess_run_finish", {
                "process_log_id": process_log_id,
                "returncode": result.returncode,
                "expected_returncode": expected,
                "expected_device_probe": expected_device_probe,
                "expected_device_probe_note": self.expected_ffmpeg_probe_note(command) if expected_device_probe else None,
                "elapsed_sec": round(time.perf_counter() - started_perf, 3),
                "command": command_text,
                "stdout": "<omitted: expected device probe>" if expected_device_probe and expected else self._safe_log_value(stdout_for_log, max_text=3000),
                "stderr": "<omitted: expected device probe; device list is in audio_devices_refreshed>" if expected_device_probe and expected else self._safe_log_value(stderr_for_log, max_text=3000),
            }, level="INFO" if expected else "WARN")
            if self.is_ffmpeg_related_command(command):
                extra = {
                    "process_log_id": process_log_id,
                    "returncode": result.returncode,
                    "expected_returncode": expected,
                    "expected_device_probe": expected_device_probe,
                    "elapsed_sec": round(time.perf_counter() - started_perf, 3),
                }
                if expected_device_probe:
                    extra["note_for_ai"] = self.expected_ffmpeg_probe_note(command)
                self.append_ffmpeg_problem_log("subprocess.run finish", command=command,
                    stdout=stdout_for_log, stderr=stderr_for_log, extra=extra)
            return result
        except subprocess.TimeoutExpired as exc:
            self.diagnostic_log("subprocess_run_timeout", {
                "process_log_id": process_log_id,
                "elapsed_sec": round(time.perf_counter() - started_perf, 3),
                "timeout": kwargs.get("timeout"),
                "command": command_text,
                "stdout": self._safe_log_value(getattr(exc, "stdout", None), max_text=8000),
                "stderr": self._safe_log_value(getattr(exc, "stderr", None), max_text=8000),
            }, level="ERROR")
            if self.is_ffmpeg_related_command(command):
                self.append_ffmpeg_problem_log("subprocess.run timeout", command=command,
                    stdout=getattr(exc, "stdout", None), stderr=getattr(exc, "stderr", None),
                    extra={"process_log_id": process_log_id, "timeout": kwargs.get("timeout"), "elapsed_sec": round(time.perf_counter() - started_perf, 3)})
            self.append_problem_error("subprocess_run_timeout", f"command={command_text}\nstdout={getattr(exc, 'stdout', None)}\nstderr={getattr(exc, 'stderr', None)}")
            raise
        except Exception as exc:
            error_details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self.diagnostic_log("subprocess_run_failed", {
                "process_log_id": process_log_id,
                "elapsed_sec": round(time.perf_counter() - started_perf, 3),
                "command": command_text,
                "error": error_details,
            }, level="ERROR")
            if self.is_ffmpeg_related_command(command):
                self.append_ffmpeg_problem_log("subprocess.run failed", command=command, stderr=error_details, extra={"process_log_id": process_log_id})
            self.append_problem_error("subprocess_run_failed", error_details)
            raise

    def terminate_process_tree(self, process, timeout=2.0, name="process"):
        """Корректно, а затем принудительно завершает процесс и его дерево."""
        if process is None:
            return
        try:
            if process.poll() is not None:
                self.unregister_child_process(process)
                return
        except Exception:
            pass

        try:
            if getattr(process, "stdin", None):
                try:
                    process.stdin.close()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            process.terminate()
            process.wait(timeout=timeout)
            self.unregister_child_process(process)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=max(0.5, min(2.0, float(timeout or 0.8))),
                    creationflags=self.creation_flags(),
                    cwd=self.subprocess_cwd(),
                )
            except Exception:
                pass

        try:
            if process.poll() is None:
                process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=1.5)
        except Exception:
            pass
        self.unregister_child_process(process)

    def force_shutdown_child_processes(self):
        """Финальная зачистка всех дочерних FFmpeg/ffprobe перед выходом."""
        try:
            with self.child_processes_lock:
                processes = list(self.child_processes)
        except Exception:
            processes = []
        for process in processes:
            self.terminate_process_tree(process, timeout=0.8, name="shutdown_child")
        try:
            with self.child_processes_lock:
                self.child_processes.clear()
        except Exception:
            pass

    def cleanup_stale_ffmpeg_processes_from_previous_runs(self):
        """Убирает старые ffmpeg.exe, оставленные предыдущими версиями программы."""
        if os.name != "nt":
            return
        self.diagnostic_log("cleanup_stale_ffmpeg_start")
        try:
            ps_script = (
                "Get-CimInstance Win32_Process -Filter \"name = 'ffmpeg.exe'\" | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=6,
                creationflags=self.creation_flags(),
                cwd=self.subprocess_cwd(),
            )
            raw = (result.stdout or "").strip()
            if not raw:
                return
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            # Только уникальные маркеры нашей программы, чтобы не убить чужой
            # ffmpeg. Голую подстроку "recording_temp" убрали — она слишком общая.
            markers = [
                "screenrecorderprowin11",            # -metadata encoder=... нашей записи
                "lavfi.astats.overall.rms_level",    # наши аудио-индикаторы
                "astats=metadata=1:reset=0.25",
                str(TEMP_RECORDINGS_DIR).lower().replace("\\", "\\\\"),
                str(TEMP_RECORDINGS_DIR).lower(),
            ]
            for item in data:
                try:
                    pid = int(item.get("ProcessId"))
                    cmd = str(item.get("CommandLine") or "").lower()
                    if not cmd:
                        continue
                    if any(marker and marker in cmd for marker in markers):
                        self.diagnostic_log("cleanup_stale_ffmpeg_kill", {
                            "pid": pid,
                            "command": item.get("CommandLine"),
                        }, level="WARN")
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=4,
                            creationflags=self.creation_flags(),
                            cwd=self.subprocess_cwd(),
                        )
                except Exception:
                    pass
            self.diagnostic_log("cleanup_stale_ffmpeg_finish", {"checked_processes": len(data)})
        except Exception as exc:
            self.diagnostic_log("cleanup_stale_ffmpeg_failed", {
                "error": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            }, level="WARN")
