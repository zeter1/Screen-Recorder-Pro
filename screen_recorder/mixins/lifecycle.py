from ..shared import *


class LifecycleMixin:
    def choose_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_folder.set(folder)

    def on_close(self):
        # Крестик окна НЕ закрывает программу, а сворачивает её в системный трей.
        # Полное закрытие доступно через трей и через ПКМ по плавающей кнопке.
        # Именно exit_app() при закрытии из трея останавливает запись, индикаторы
        # звука и принудительно добивает все дочерние ffmpeg.exe.
        self.minimize_to_tray()
        return "break"

    def exit_app(self):
        # Реальное закрытие программы. Должно гарантированно убрать все дочерние
        # ffmpeg.exe, даже если закрытие пришло из трея или во время записи.
        if getattr(self, "_exiting", False):
            return "break"

        # Закрытие во время старта/записи/сборки теперь сначала безопасно
        # завершает текущую операцию. Так в папке назначения не останется
        # оборванный MP4 под обычным именем готового файла.
        if getattr(self, "is_starting", False):
            self.cancel_start_requested = True
            self._exit_after_finalize = True
            try:
                self.status_var.set("Отменяю запуск записи и закрываю программу...")
            except Exception:
                pass
            return "break"

        if getattr(self, "is_pause_transitioning", False):
            self._exit_after_finalize = True
            try:
                self.status_var.set("Завершаю паузу, затем сохраню запись и закрою программу...")
            except Exception:
                pass
            if not self._exit_retry_job:
                def retry_exit():
                    self._exit_retry_job = None
                    self.exit_app()
                self._exit_retry_job = self.root.after(250, retry_exit)
            return "break"

        if self.is_recording and not self.is_finalizing:
            self._exit_after_finalize = True
            self.diagnostic_log("exit_deferred_until_recording_saved", {
                "recording_session_id": self.recording_session_id,
                "is_paused": self.is_paused,
            })
            try:
                self.status_var.set("Сохраняю запись, затем программа полностью закроется...")
            except Exception:
                pass
            self.stop_recording()
            return "break"

        if self.is_finalizing:
            self._exit_after_finalize = True
            self.diagnostic_log("exit_deferred_until_finalize", {
                "recording_session_id": self.recording_session_id,
                "output_path": self.output_path,
            })
            try:
                self.status_var.set("Дожидаюсь безопасного сохранения и затем закрываю программу...")
            except Exception:
                pass
            return "break"

        self.diagnostic_log("exit_app_start", {
            "is_recording": self.is_recording,
            "is_finalizing": self.is_finalizing,
            "segments": [str(p) for p in self.segments],
            "child_processes": len(getattr(self, "child_processes", [])),
        })
        self._exiting = True
        self.running = False
        self.cancel_recording_watchdog()
        try:
            self.cancel_post_save_diagnostics(reason="application_exit")
        except Exception:
            pass
        try:
            self.cancel_audio_device_refresh()
        except Exception:
            pass

        for job_name in ("save_job", "hotkey_job", "hotkey_poll_job", "meter_restart_job", "_exit_retry_job"):
            try:
                job = getattr(self, job_name, None)
                if job:
                    self.root.after_cancel(job)
                    setattr(self, job_name, None)
            except Exception:
                pass

        try:
            self._cancel_hotkey_recovery_jobs()
        except Exception:
            pass

        try:
            self._close_screenshot_hotkey_capture(restore_hotkeys=False)
        except Exception as exc:
            self.log_exception("exit_app.close_screenshot_hotkey_capture", exc)

        try:
            self.save_settings()
        except Exception:
            pass

        try:
            self.close_annotation_overlay()
        except Exception:
            pass

        try:
            self.close_webcam_preview()
        except Exception as exc:
            self.log_exception("exit_app.close_webcam_preview", exc)

        # Сначала выключаем фоновые индикаторы звука — это самые частые висящие ffmpeg.
        try:
            self.stop_audio_meters(join_timeout=1.0)
        except Exception as exc:
            self.log_exception("exit_app.stop_audio_meters", exc)

        try:
            self.stop_instant_dxcam_buffer(release_camera=True, join_timeout=1.5)
        except Exception as exc:
            self.log_exception("exit_app.stop_instant_dxcam_buffer", exc)

        self._remove_registered_hotkeys()

        try:
            screenshot_prepare_thread = getattr(self, "screenshot_prepare_thread", None)
            if screenshot_prepare_thread is not None and screenshot_prepare_thread.is_alive():
                screenshot_prepare_thread.join(timeout=2.0)
            screenshot_thread = getattr(self, "screenshot_thread", None)
            if screenshot_thread is not None and screenshot_thread.is_alive():
                screenshot_thread.join(timeout=2.0)
            self._clear_screenshot_frozen_image()
        except Exception as exc:
            self.log_exception("exit_app.screenshot_thread", exc)

        # До этого места доходим только после безопасного завершения записи и
        # финальной сборки. Проверка ниже остаётся страховкой от несогласованного
        # состояния старой/аварийной сессии.
        try:
            with self.process_lock:
                has_active_recording_process = self.process is not None
            if has_active_recording_process:
                self.stop_current_segment()
            else:
                self.diagnostic_log("exit_app_no_active_recording_process", {
                    "is_recording": self.is_recording,
                    "is_paused": self.is_paused,
                    "is_finalizing": self.is_finalizing,
                    "recording_session_id": self.recording_session_id,
                    "note_for_ai": "Штатное закрытие: активного FFmpeg-сегмента уже нет, значит stop_current_segment не нужен.",
                }, level="INFO")
        except Exception as exc:
            self.log_exception("exit_app.stop_current_segment", exc)

        try:
            if self.finalize_thread and self.finalize_thread.is_alive():
                self.finalize_thread.join(timeout=2.0)
        except Exception:
            pass

        try:
            post_thread = getattr(self, "post_diagnostics_thread", None)
            if post_thread is not None and post_thread.is_alive():
                post_thread.join(timeout=1.5)
        except Exception:
            pass

        self.force_shutdown_child_processes()
        self.cleanup_stale_ffmpeg_processes_from_previous_runs()

        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None

        try:
            self.root.destroy()
        except Exception:
            pass
        self.diagnostic_log("exit_app_finish", {
            "child_processes": len(getattr(self, "child_processes", [])),
            "diagnostic_logs": [str(p) for p in getattr(self, "diagnostic_log_paths", [])],
        })
        return "break"
