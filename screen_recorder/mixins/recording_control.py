from ..shared import *


class RecordingControlMixin:
    def get_current_segment_duration_counters(self, segment_perf_end=None):
        """Возвращает (media_seconds, wall_seconds, source) для текущего сегмента.

        ``media_seconds`` берётся из ``-progress out_time`` и поэтому совпадает с
        длительностью, которую пользователь увидит в готовом файле. ``wall_seconds``
        сохраняется отдельно для диагностики дрейфа источника относительно часов ПК.
        Раньше одно wall-clock число одновременно использовалось как таймер файла и
        как доказательство потери кадров, из-за чего нормальная запись выглядела
        повреждённой.
        """
        try:
            end_perf = float(segment_perf_end) if segment_perf_end is not None else time.perf_counter()
        except Exception:
            end_perf = time.perf_counter()
        try:
            start_perf = float(self.segment_capture_started_perf or self.segment_started_at or end_perf)
        except Exception:
            start_perf = end_perf
        wall_seconds = max(0.0, end_perf - start_perf)

        # Дожидаемся чтения финального progress=end после process.wait().
        try:
            for thread in list(getattr(self, "recording_progress_threads", []) or [])[-2:]:
                if thread.is_alive() and thread is not threading.current_thread():
                    thread.join(timeout=0.35)
        except Exception:
            pass

        media_seconds = 0.0
        try:
            media_seconds = max(0.0, float(self.current_segment_media_seconds or 0.0))
        except Exception:
            media_seconds = 0.0

        # После process.wait() progress-поток обычно уже прочитал progress=end,
        # но на медленном диске он может дописать последнюю строку чуть позже.
        # Берём максимум из временного ряда текущего сегмента как страховку.
        try:
            current_path = str(self.segments[-1]) if self.segments else None
            with self.recording_progress_lock:
                for sample in self.recording_progress_samples:
                    if current_path and str(sample.get("segment_path")) != current_path:
                        continue
                    value = sample.get("out_time_seconds")
                    if value is not None:
                        media_seconds = max(media_seconds, max(0.0, float(value)))
        except Exception:
            pass

        source = "ffmpeg_progress_out_time"
        if media_seconds <= 0.001:
            media_seconds = wall_seconds
            source = "wall_clock_fallback_no_progress"
        return media_seconds, wall_seconds, source

    def commit_current_segment_duration(self, segment_perf_end=None, reason="segment_stop"):
        """Добавляет длительность сегмента в общий таймер и пишет объяснимый лог."""
        media_seconds, wall_seconds, source = self.get_current_segment_duration_counters(segment_perf_end)
        self.recorded_seconds += max(0.0, media_seconds)
        self.recorded_wall_seconds += max(0.0, wall_seconds)
        payload = {
            "reason": reason,
            "segment_index": self.segment_index,
            "segment_path": str(self.segments[-1]) if self.segments else None,
            "duration_source": source,
            "segment_media_seconds": round(media_seconds, 6),
            "segment_wall_seconds": round(wall_seconds, 6),
            "segment_media_minus_wall_seconds": round(media_seconds - wall_seconds, 6),
            "total_media_seconds": round(self.recorded_seconds, 6),
            "total_wall_seconds": round(self.recorded_wall_seconds, 6),
        }
        self.problem_log_event("recording_segment_duration_committed", payload)
        self.segment_started_at = None
        self.segment_capture_started_perf = None
        self.current_segment_media_seconds = 0.0
        self.current_segment_last_progress_perf = None
        return payload

    def toggle_pause(self):
        if not self.is_recording or self.is_finalizing or getattr(self, "is_pause_transitioning", False):
            return
        if not self.is_paused:
            self.pause_recording()
        else:
            self.resume_recording()

    def pause_recording(self):
        if not self.is_recording or self.is_paused or getattr(self, "is_pause_transitioning", False):
            return
        self.diagnostic_log("pause_recording_requested", {
            "recording_session_id": self.recording_session_id,
            "segments": [str(p) for p in self.segments],
            "recorded_seconds": self.recorded_seconds,
        })
        self.is_pause_transitioning = True
        self.cancel_recording_watchdog()
        try:
            self.pause_button.configure(state="disabled", text="⏳ Ставлю паузу...")
            self.stop_button.configure(state="disabled")
            if self.annotation_overlay:
                self.annotation_overlay.update_record_controls()
            self.set_rec_state("saving")
            self.status_var.set("Ставлю запись на паузу: корректно закрываю текущий сегмент FFmpeg...")
        except Exception:
            pass
        self.pause_transition_thread = threading.Thread(
            target=self._pause_recording_worker,
            name="pause_recording_worker",
            daemon=True,
        )
        self.pause_transition_thread.start()

    def _pause_recording_worker(self):
        ok = False
        error_text = None
        try:
            segment_perf_end = time.perf_counter()
            self.stop_current_segment()
            if self.segment_started_at is not None:
                self.commit_current_segment_duration(segment_perf_end, reason="pause")
            ok = True
        except Exception as exc:
            self.log_exception("pause_recording_worker", exc)
            error_text = str(exc)
        finally:
            try:
                self.root.after(0, lambda: self._finish_pause_recording(ok, error_text))
            except Exception as exc:
                self.log_exception("pause_recording_worker.schedule_finish_ui", exc)

    def _finish_pause_recording(self, ok, error_text=None):
        self.is_pause_transitioning = False
        if not ok:
            self.schedule_recording_watchdog()
            try:
                self.pause_button.configure(state="normal", text="⏸ Пауза")
                self.stop_button.configure(state="normal")
                if self.annotation_overlay:
                    self.annotation_overlay.update_record_controls()
                self.set_rec_state("recording")
                messagebox.showerror("Ошибка паузы", error_text or "Не удалось корректно поставить запись на паузу.")
                self.status_var.set("Ошибка паузы. Лучше останови запись и проверь лог.")
            except Exception:
                pass
            return
        self.is_paused = True
        try:
            self.pause_button.configure(state="normal", text="▶ Продолжить")
            self.stop_button.configure(state="normal")
            if self.annotation_overlay:
                self.annotation_overlay.update_pause_button_text()
            self.set_rec_state("paused")
            self.status_var.set("Пауза. Нажми «Продолжить», чтобы писать дальше.")
        except Exception:
            pass

    def resume_recording(self):
        if not self.is_recording or not self.is_paused or getattr(self, "is_pause_transitioning", False):
            return
        self.diagnostic_log("resume_recording_requested", {
            "recording_session_id": self.recording_session_id,
            "segments_before_resume": [str(p) for p in self.segments],
        })
        self.is_pause_transitioning = True
        try:
            self.pause_button.configure(state="disabled", text="⏳ Продолжаю...")
            self.stop_button.configure(state="disabled")
            if self.annotation_overlay:
                self.annotation_overlay.update_record_controls()
            self.status_var.set("Продолжаю запись: запускаю новый сегмент...")
        except Exception:
            pass
        try:
            self.start_new_segment()
        except Exception as exc:
            self.log_exception("resume_recording.start_new_segment", exc)
            self.is_pause_transitioning = False
            try:
                self.pause_button.configure(state="normal", text="▶ Продолжить")
                self.stop_button.configure(state="normal")
                if self.annotation_overlay:
                    self.annotation_overlay.update_pause_button_text()
            except Exception:
                pass
            messagebox.showerror("Ошибка продолжения записи", str(exc))
            return
        self.is_paused = False
        self.is_pause_transitioning = False
        self.schedule_recording_watchdog()
        try:
            self.pause_button.configure(state="normal", text="⏸ Пауза")
            self.stop_button.configure(state="normal")
            if self.annotation_overlay:
                self.annotation_overlay.update_pause_button_text()
            self.set_rec_state("recording")
            self.status_var.set("Запись продолжена...")
        except Exception:
            pass

    def stop_recording(self):
        """Останавливает запись без подвешивания GUI.

        Тяжёлая часть — остановка DXcam/FFmpeg, проверка сегментов и сборка
        итогового файла — выполняется в отдельном потоке. Tkinter остаётся
        отзывчивым, а FFmpeg получает корректный EOF вместо принудительного kill.
        """
        if not self.is_recording or self.is_finalizing or getattr(self, "is_pause_transitioning", False):
            self.diagnostic_log("stop_recording_ignored", {
                "is_recording": self.is_recording,
                "is_finalizing": self.is_finalizing,
                "is_pause_transitioning": getattr(self, "is_pause_transitioning", False),
            }, level="WARN")
            try:
                if getattr(self, "is_pause_transitioning", False):
                    self.status_var.set("Дождись завершения паузы/продолжения, потом нажми Стоп.")
            except Exception:
                pass
            return

        self.cancel_auto_stop()
        self.cancel_recording_watchdog()
        self.recording_stop_requested_perf = time.perf_counter()
        self.is_finalizing = True
        was_paused = bool(self.is_paused)
        self.recording_audio_bitrate = self.normalize_audio_bitrate_value(self.audio_bitrate_var.get())
        self.output_path = self.make_output_path_at_save_time()
        self.diagnostic_log("stop_recording_requested", {
            "recording_session_id": self.recording_session_id,
            "was_paused": was_paused,
            "output_path": self.output_path,
            "segments": [str(p) for p in self.segments],
            "recorded_media_seconds": self.recorded_seconds,
            "recorded_wall_seconds": self.recorded_wall_seconds,
            "settings": self.collect_settings_snapshot(),
        })

        # Сразу блокируем кнопки, но не уничтожаем overlay: поток DXcam ещё может
        # читать кэш прямоугольников панели. Закроем overlay уже после остановки.
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="disabled", text="⏸ Пауза")
        self.stop_button.configure(state="disabled")
        self.set_settings_window_enabled(False)
        self.set_rec_state("saving")
        self.status_var.set("Останавливаю запись и сохраняю файл...")

        self.finalize_thread = threading.Thread(
            target=self._stop_recording_worker,
            args=(was_paused,),
            daemon=True,
        )
        self.finalize_thread.start()

    def _stop_recording_worker(self, was_paused):
        save_success = False
        error_text = None
        debug_log = None
        post_diagnostics_context = None
        self.diagnostic_log("stop_recording_worker_start", {
            "recording_session_id": self.recording_session_id,
            "was_paused": was_paused,
            "segments": [str(p) for p in self.segments],
            "output_path": self.output_path,
            "recording_failure_reason": self.recording_failure_reason,
        })
        try:
            if not was_paused:
                segment_perf_end = time.perf_counter()
                self.stop_current_segment()
                if self.segment_started_at is not None:
                    self.commit_current_segment_duration(segment_perf_end, reason="stop")

            try:
                self.root.after(0, self.stop_cursor_highlight_overlay)
            except Exception as exc:
                self.log_exception("stop_recording_worker.schedule_cursor_overlay_stop", exc)

            self.merge_segments()
            self.validate_media_file(self.output_path, label="итоговый файл")
            request_to_stop_seconds = None
            try:
                if self.recording_start_requested_perf is not None and self.recording_stop_requested_perf is not None:
                    request_to_stop_seconds = max(
                        0.0,
                        float(self.recording_stop_requested_perf) - float(self.recording_start_requested_perf),
                    )
            except Exception:
                request_to_stop_seconds = None
            self.stop_recording_performance_sampler()
            timing_summary = self.log_video_timing_summary(
                self.output_path,
                label="итоговый файл",
                expected_wall_seconds=self.recorded_wall_seconds,
                expected_media_seconds=self.recorded_seconds,
                requested_wall_seconds=request_to_stop_seconds,
            )
            self.last_video_timing_summary = timing_summary
            self.validate_final_timing_summary(timing_summary)
            save_success = True
            post_diagnostics_context = self.build_post_save_diagnostics_context(
                timing_summary,
                outcome="saved",
            )
            # Пользовательский результат уже готов. Тяжёлый декод/анализ кадров
            # запускается позже в фоне и больше не задерживает окно «Сохранено».
            self.write_pending_post_diagnostics_report(post_diagnostics_context)
            debug_log = self.copy_debug_log_to_output()
        except Exception as exc:
            self.log_exception("stop_recording_worker", exc)
            error_text = str(exc)
            incomplete_path = self.quarantine_incomplete_output()
            if incomplete_path:
                error_text += f"\nНеполный результат сохранён отдельно: {incomplete_path}"
        finally:
            try:
                self.stop_recording_performance_sampler()
            except Exception:
                pass
            try:
                if save_success and self.recording_failure_reason:
                    outcome = "Запись сохранена с предупреждением; доступная корректная часть сохранена."
                elif save_success:
                    outcome = "Файл успешно сохранён."
                else:
                    outcome = "Ошибка сохранения или сборки итогового видео."
                self.write_ai_problem_summary(
                    outcome=outcome,
                    error_text=error_text or self.recording_failure_reason,
                )
            except Exception:
                pass
            self._pending_post_diagnostics_context = post_diagnostics_context
            self.embed_recording_log_in_diagnostics(self.current_log_path, max_chars=20000)
            self.diagnostic_log("stop_recording_worker_finish", {
                "recording_session_id": self.recording_session_id,
                "save_success": save_success,
                "error_text": error_text,
                "debug_log": debug_log,
                "output_path": self.output_path,
                "recording_failure_reason": self.recording_failure_reason,
                "incomplete_output_path": self.incomplete_output_path,
            }, level="WARN" if save_success and self.recording_failure_reason else ("INFO" if save_success else "ERROR"))
            try:
                self.root.after(0, lambda: self._finish_stop_recording(save_success, error_text, debug_log))
            except Exception as exc:
                self.log_exception("stop_recording_worker.schedule_finish_ui", exc)

    def show_saved_popup(self, file_path, log_note=""):
        """Поп-ап «Запись сохранена» с кнопкой открытия папки.

        Возвращает True, если пользователь открыл папку прямо из поп-апа.
        """
        opened = {"v": False}
        try:
            win = tk.Toplevel(self.root)
            win.title("Запись сохранена")
            win.configure(bg="#1e1e1e")
            win.attributes("-topmost", True)
            win.resizable(False, False)
            frm = ttk.Frame(win, padding=16)
            frm.pack(fill="both", expand=True)
            ttk.Label(frm, text="Файл сохранён:", font=("Segoe UI", 10, "bold")).pack(anchor="w")
            ttk.Label(frm, text=str(file_path), wraplength=480, foreground="#cfcfcf").pack(anchor="w", pady=(2, 0))
            if log_note:
                ttk.Label(frm, text=log_note.strip(), wraplength=480, foreground="#9a9a9a").pack(anchor="w", pady=(6, 0))

            btns = ttk.Frame(frm)
            btns.pack(fill="x", pady=(14, 0))

            def open_and_close():
                opened["v"] = True
                try:
                    self.reveal_in_file_manager(file_path)
                except Exception as exc:
                    self.log_exception("show_saved_popup.open", exc)
                win.destroy()

            ttk.Button(btns, text="📁 Открыть папку с видео", command=open_and_close).pack(side="left")
            ttk.Button(btns, text="OK", command=win.destroy, width=8).pack(side="right")

            win.update_idletasks()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            w, h = win.winfo_width(), win.winfo_height()
            win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")
            win.grab_set()
            win.focus_force()
            win.wait_window()
        except Exception as exc:
            self.log_exception("show_saved_popup", exc)
            messagebox.showinfo("Запись сохранена", f"Файл сохранён:\n{file_path}{log_note}")
        return opened["v"]

    def _finish_stop_recording(self, save_success, error_text, debug_log):
        exit_after_finalize = bool(getattr(self, "_exit_after_finalize", False))
        failure_reason = getattr(self, "recording_failure_reason", None)
        self.diagnostic_log("finish_stop_recording_ui", {
            "recording_session_id": self.recording_session_id,
            "save_success": save_success,
            "error_text": error_text,
            "debug_log": debug_log,
            "output_path": self.output_path,
            "exit_after_finalize": exit_after_finalize,
            "recording_failure_reason": failure_reason,
        }, level="INFO" if save_success else "ERROR")
        try:
            self.is_recording = False
            self.is_paused = False
            self.is_finalizing = False
            self.is_pause_transitioning = False
            self.stop_keys_overlay()
            self.stop_cursor_highlight_overlay()
            if self.annotation_overlay and self.draw_enabled_var.get():
                self.annotation_overlay.reset_after_recording_stopped()
            else:
                self.close_annotation_overlay()
            self.annotation_toolbar_clean_frame = None
            self.start_button.configure(state="normal")
            self.pause_button.configure(state="disabled", text="⏸ Пауза")
            self.stop_button.configure(state="disabled")
            self.set_settings_window_enabled(True)
            self.set_rec_state("ready")
            self.timer_var.set("00:00:00")
            # DXcam-буфер в стабильной сборке отключён: повторный старт должен
            # идти через FFmpeg ddagrab/gdigrab без Python-DXcam singleton.

            if save_success:
                try:
                    self.last_output_path = Path(self.output_path).resolve(strict=False)
                except Exception:
                    self.last_output_path = Path(self.output_path)
                self.last_debug_log_path = debug_log or self.current_session_log_dir or self.current_log_path
                self.update_after_recording_buttons()
                try:
                    self.cleanup_temp_dir()
                except Exception as exc:
                    self.log_exception("cleanup_temp_dir", exc)
                logs_deleted_after_success = (
                    False if failure_reason else self.maybe_delete_successful_session_logs()
                )
                if logs_deleted_after_success:
                    debug_log = None
                    self.last_debug_log_path = None
                    self._pending_post_diagnostics_context = None
                    self.update_after_recording_buttons()
                diagnostic_latest = self.diagnostic_log_paths[0] if getattr(self, "diagnostic_log_paths", None) else None
                log_note = ""
                if self.current_session_log_dir:
                    log_note += f"\n\nПапка логов проблем:\n{self.current_session_log_dir}"
                elif debug_log:
                    log_note += f"\n\nЛог записи:\n{debug_log}"
                if diagnostic_latest:
                    log_note += f"\n\nОбщий лог запуска:\n{diagnostic_latest}"
                if not logs_deleted_after_success:
                    try:
                        context = getattr(self, "_pending_post_diagnostics_context", None)
                        if context:
                            self.start_post_save_diagnostics(context)
                            self._pending_post_diagnostics_context = None
                    except Exception as exc:
                        self.log_exception("finish_stop_recording.start_post_save_diagnostics", exc)
                if failure_reason:
                    self.status_var.set(
                        f"Запись сохранена с предупреждением: {self.output_path}"
                    )
                else:
                    self.status_var.set(f"Готово. Файл сохранён: {self.output_path}")

                if not exit_after_finalize:
                    if failure_reason:
                        messagebox.showwarning(
                            "Запись сохранена с предупреждением",
                            f"{failure_reason}\n\n"
                            "Программа сохранила доступную корректную часть записи. "
                            "Проверь указанный в предупреждении источник:\n"
                            f"{self.last_output_path}{log_note}",
                        )
                        opened = False
                    else:
                        opened = self.show_saved_popup(self.last_output_path, log_note)
                    # Автооткрытие по галке — только если в поп-апе не открыли вручную.
                    if not opened and self.open_folder_after_stop_var.get():
                        try:
                            self.reveal_in_file_manager(self.last_output_path)
                        except Exception as exc:
                            self.log_exception("open_folder_after_stop", exc)
            else:
                log_path = self.current_log_path or ""
                log_folder = self.current_session_log_dir or ""
                self.last_debug_log_path = Path(log_folder) if log_folder else (Path(log_path) if log_path else None)
                self.update_after_recording_buttons()
                diagnostic_latest = self.diagnostic_log_paths[0] if getattr(self, "diagnostic_log_paths", None) else ""
                if not exit_after_finalize:
                    messagebox.showerror("Ошибка сохранения", f"Не удалось собрать итоговый файл.\n\n{error_text}\n\nПапка логов проблем: {log_folder}\nЛог записи: {log_path}\nОбщий лог: {diagnostic_latest}")
                self.status_var.set(f"Ошибка сохранения. Папка логов: {log_folder or diagnostic_latest or log_path}")
        finally:
            if exit_after_finalize and self.running:
                self._exit_after_finalize = False
                try:
                    self.root.after_idle(self.exit_app)
                except Exception as exc:
                    self.log_exception("finish_stop_recording.exit_after_finalize", exc)
            elif self.running:
                self.start_audio_meters()

    def stop_current_segment(self):
        with self.process_lock:
            process = self.process
            self.process = None

        if process is None:
            # После успешного сохранения или при штатном закрытии программы активного
            # FFmpeg-процесса уже нет. Это не проблема записи, поэтому не пишем WARN,
            # чтобы папка «Логи проблем» не пугала ложным предупреждением. WARN
            # оставляем только для странной ситуации: программа считает, что запись
            # идёт, но процесса сегмента нет.
            idle_stop = bool(getattr(self, "_exiting", False) or not getattr(self, "is_recording", False) or getattr(self, "is_finalizing", False))
            self.diagnostic_log("stop_current_segment_no_active_process", {
                "recording_session_id": self.recording_session_id,
                "current_segment_engine": self.current_segment_engine,
                "is_recording": getattr(self, "is_recording", None),
                "is_paused": getattr(self, "is_paused", None),
                "is_finalizing": getattr(self, "is_finalizing", None),
                "is_exiting": getattr(self, "_exiting", None),
                "classification": "normal_idle_or_shutdown" if idle_stop else "unexpected_missing_process",
            }, level="INFO" if idle_stop else "WARN")
            if not idle_stop:
                self.problem_log_event("stop_current_segment_missing_process_unexpected", {
                    "recording_session_id": self.recording_session_id,
                    "current_segment_engine": self.current_segment_engine,
                    "is_recording": getattr(self, "is_recording", None),
                    "is_paused": getattr(self, "is_paused", None),
                    "is_finalizing": getattr(self, "is_finalizing", None),
                }, level="WARN")
            return

        segment_stop_started = time.perf_counter()
        self.diagnostic_log("stop_current_segment_start", {
            "recording_session_id": self.recording_session_id,
            "current_segment_engine": self.current_segment_engine,
            "pid": getattr(process, "pid", None),
            "segments": [str(p) for p in self.segments],
        })

        if self.current_segment_engine == "dxcam":
            stop_started = time.perf_counter()
            try:
                if self.dxcam_stop_event:
                    self.dxcam_stop_event.set()
            except Exception:
                pass

            # Даём потоку записи самому выйти и закрыть pipe. Если он завис на
            # записи кадра/обработке служебных окон, принудительно закрываем
            # stdin из GUI-потока: FFmpeg получает EOF, дописывает trailer и файл
            # остаётся открываемым вместо принудительного kill.
            thread_finished = True
            try:
                if self.dxcam_thread and self.dxcam_thread.is_alive():
                    self.dxcam_thread.join(timeout=2.0)
                thread_finished = not (self.dxcam_thread and self.dxcam_thread.is_alive())
            except Exception:
                thread_finished = False

            if not thread_finished:
                try:
                    if self.log_handle:
                        self.log_handle.write("\nDXCAM stop: capture thread did not finish in 2s; closing FFmpeg stdin from main thread.\n")
                        self.log_handle.flush()
                except Exception:
                    pass
                try:
                    if process.stdin:
                        process.stdin.close()
                except Exception:
                    pass
                try:
                    if self.dxcam_thread and self.dxcam_thread.is_alive():
                        self.dxcam_thread.join(timeout=2.0)
                except Exception:
                    pass

            try:
                process.wait(timeout=10)
                self.unregister_child_process(process)
            except subprocess.TimeoutExpired:
                try:
                    if self.log_handle:
                        self.log_handle.write("\nDXCAM stop: FFmpeg did not finish after stdin EOF; terminating process tree. File metadata may be incomplete.\n")
                        self.log_handle.flush()
                except Exception:
                    pass
                self.terminate_process_tree(process, timeout=2.0, name="dxcam_recording_ffmpeg")
            try:
                if self.log_handle:
                    self.log_handle.write(f"\n--- DXCAM STATS ---\n{json.dumps(self.dxcam_stats, ensure_ascii=False, indent=2)}\n")
                    self.log_handle.write(f"dxcam_thread_finished={not (self.dxcam_thread and self.dxcam_thread.is_alive())}\n")
                    self.log_handle.write(f"dxcam_stop_elapsed={time.perf_counter() - stop_started:.2f}s\n")
            except Exception:
                pass
            self.dxcam_stop_event = None
            self.dxcam_thread = None
            self.dxcam_camera = None
        else:
            try:
                if process.stdin:
                    process.stdin.write(b"q\n")
                    process.stdin.flush()
                    # Явный EOF: без close() FFmpeg может ждать stdin и дольше
                    # держать незакрытый контейнер.
                    process.stdin.close()
            except Exception:
                pass

            try:
                return_code = process.wait(timeout=12)
                self.unregister_child_process(process)
                if return_code not in (0, None):
                    self.log_message(f"FFmpeg segment stopped with return code {return_code}; segment will be validated before final merge.")
            except subprocess.TimeoutExpired:
                self.log_message("FFmpeg segment did not finish after stdin EOF; terminating process tree. Segment will be validated before final merge.")
                self.terminate_process_tree(process, timeout=2.0, name="ffmpeg_recording_segment")

        try:
            self.stop_python_loopback_for_current_segment()
        except Exception as exc:
            self.log_exception("stop_python_loopback_for_current_segment", exc)
            raise

        if self.log_handle:
            try:
                self.log_handle.flush()
                self.log_handle.close()
            except Exception:
                pass
            self.log_handle = None
        self.diagnostic_log("stop_current_segment_finish", {
            "recording_session_id": self.recording_session_id,
            "current_segment_engine": self.current_segment_engine,
            "elapsed_sec": round(time.perf_counter() - segment_stop_started, 3),
            "segments": [str(p) for p in self.segments],
        })

    def copy_debug_log_to_output(self):
        """Возвращает папку логов проблем текущей записи.

        Внутри папки лежит краткое резюме для нейросети, события, FFmpeg-вывод,
        подробный лог записи, ошибки и снимок настроек.
        """
        try:
            if self.current_session_log_dir and Path(self.current_session_log_dir).exists():
                return Path(self.current_session_log_dir)
            if self.current_log_path and Path(self.current_log_path).exists():
                return Path(self.current_log_path)
        except Exception:
            pass
        return None

    def fast_move_segment_to_output(self, segment):
        """Самое быстрое сохранение для одной записи без пауз.

        Если контейнер временного сегмента уже совпадает с выбранным форматом,
        не запускаем FFmpeg и не копируем гигабайты заново — просто
        переименовываем файл в итоговое имя. На одном диске это почти мгновенно.
        """
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = self.get_current_recording_log_path()
        with open(log_path, "a", encoding="utf-8", errors="ignore") as log:
            log.write("\n\n--- FAST SAVE ---\n")
            log.write(f"move {segment} -> {self.output_path}\n")
            log.flush()
        try:
            if self.output_path.exists():
                self.output_path.unlink()
        except Exception:
            pass
        try:
            os.replace(str(segment), str(self.output_path))
        except OSError:
            shutil.move(str(segment), str(self.output_path))

    def cleanup_temp_dir(self):
        try:
            if self.temp_dir and Path(self.temp_dir).exists():
                shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception as exc:
            self.log_exception("cleanup_temp_dir", exc)

    def validate_media_file(self, path, label="видео"):
        """Проверяет, что файл не просто существует, а реально читается FFmpeg.

        Раньше сегмент считался валидным, если его размер больше нуля. Но MP4/MOV
        или аварийно закрытый контейнер может быть непустым и при этом не
        открываться. Здесь используем ffprobe, а если его нет — ffmpeg -f null.
        """
        path = Path(path)
        self.diagnostic_log("validate_media_file_start", {"label": label, "path": path})
        if not path.exists() or path.stat().st_size <= 0:
            raise RuntimeError(f"{label}: файл пустой или не найден: {path}")

        ffprobe = self.get_ffprobe_path()
        probe_details = None
        if ffprobe:
            cmd = [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=index,codec_type,width,height,duration,nb_frames",
                "-of",
                "json",
                str(path),
            ]
            result = self.run_managed_process(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=20,
                creationflags=self.creation_flags(),
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"{label}: FFprobe не смог прочитать файл {path}. "
                    f"{(result.stderr or '').strip()}"
                )
            try:
                probe_details = json.loads(result.stdout or "{}")
            except Exception as exc:
                raise RuntimeError(
                    f"{label}: FFprobe вернул повреждённое описание файла {path}: {exc}"
                ) from exc
            streams = probe_details.get("streams") or []
            video_streams = [item for item in streams if item.get("codec_type") == "video"]
            if not video_streams:
                raise RuntimeError(f"{label}: в файле нет видеодорожки: {path}")
            video_stream = video_streams[0]
            try:
                width = int(video_stream.get("width") or 0)
                height = int(video_stream.get("height") or 0)
            except Exception:
                width = height = 0
            if width < 1 or height < 1:
                raise RuntimeError(
                    f"{label}: видеодорожка имеет неверный размер {width}x{height}: {path}"
                )
            duration_value = (probe_details.get("format") or {}).get("duration")
            if duration_value in (None, "", "N/A"):
                duration_value = video_stream.get("duration")
            try:
                duration_seconds = float(duration_value)
            except Exception:
                duration_seconds = 0.0
            if duration_seconds <= 0.0:
                raise RuntimeError(f"{label}: длительность видео не определена или равна нулю: {path}")

        # FFprobe проверяет контейнер и потоки, а эта короткая команда реально
        # декодирует первый видеокадр. Аудиофайл без видео здесь не пройдёт.
        cmd = [
            self.ffmpeg_path,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ]
        result = self.run_managed_process(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore", timeout=30, creationflags=self.creation_flags())
        if result.returncode != 0:
            details = (result.stderr or "").strip()
            raise RuntimeError(f"{label}: FFmpeg не смог декодировать видеокадр {path}. {details}")
        self.diagnostic_log("validate_media_file_ok", {
            "label": label,
            "path": path,
            "validator": "ffprobe_and_ffmpeg_frame" if ffprobe else "ffmpeg_video_frame",
            "probe": probe_details,
        })
        return True
