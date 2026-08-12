from ..shared import *


class SettingsMixin:
    def load_settings(self):
        # Главный файл настроек хранится рядом с программой. Резервная копия
        # нужна на случай повреждения JSON после аварийного выключения Windows.
        for candidate, is_backup in (
            (SETTINGS_PATH, False),
            (SETTINGS_BACKUP_PATH, True),
        ):
            try:
                if not candidate.exists():
                    continue
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Корень settings.json должен быть объектом JSON")
                if is_backup:
                    self.diagnostic_log(
                        "settings_recovered_from_backup",
                        {"backup_path": candidate, "settings_path": SETTINGS_PATH},
                        level="WARN",
                    )
                    try:
                        atomic_write_text(
                            SETTINGS_PATH,
                            json.dumps(data, ensure_ascii=False, indent=2),
                        )
                    except Exception as restore_exc:
                        self.diagnostic_log(
                            "settings_backup_restore_failed",
                            {"error": repr(restore_exc)},
                            level="ERROR",
                        )
                return data
            except Exception as exc:
                self.diagnostic_log(
                    "load_settings_failed",
                    {"settings_path": candidate, "is_backup": is_backup, "error": repr(exc)},
                    level="ERROR",
                )

        # Мягкая миграция со старой версии, где настройки лежали в AppData.
        try:
            old_path = Path(os.getenv("APPDATA", str(Path.home()))) / APP_NAME / "settings.json"
            if old_path.exists():
                data = json.loads(old_path.read_text(encoding="utf-8"))
                try:
                    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(
                        SETTINGS_PATH,
                        json.dumps(data, ensure_ascii=False, indent=2),
                    )
                except Exception:
                    pass
                self.diagnostic_log("settings_migrated_from_appdata", {"old_path": old_path, "settings_path": SETTINGS_PATH})
                return data
        except Exception as exc:
            self.diagnostic_log("load_old_settings_failed", {"error": repr(exc)}, level="WARN")

        return {}

    def save_settings(self):
        if self.initializing:
            return
        data = {
            "output_folder": self.output_folder.get(),
            "format": self.format_var.get(),
            "fps": self.fps_var.get(),
            "auto_adjust_fps": bool(self.auto_adjust_fps_var.get()),
            "video_bitrate": str(normalize_video_bitrate_mbps(self.video_bitrate_var.get())),
            "capture_method": self.capture_method_var.get(),
            "encoder": self.encoder_var.get(),
            "webcam_device": self.webcam_device_var.get().strip() or WEBCAM_AUTO,
            "audio_bitrate": self.audio_bitrate_var.get(),
            "mic_device": self.mic_device_var.get(),
            "system_device": self.system_device_var.get(),
            "mic_volume": int(self.mic_volume_var.get()),
            "system_volume": int(self.system_volume_var.get()),
            "draw_enabled": True,
            "floating_panel_size": normalize_floating_panel_size(self.floating_panel_size_var.get()),
            "cursor_visible": bool(self.cursor_visible_var.get()),
            "cursor_highlight": bool(self.cursor_highlight_var.get()),
            "cursor_highlight_size": int(self.cursor_highlight_size_var.get()),
            "startup_tray": bool(self.startup_tray_var.get()),
            "hotkey": self.hotkey_var.get().strip(),
            "screenshot_hotkey": self.screenshot_hotkey_var.get().strip(),
            "monitor_index": self.monitor_index_var.get().strip() or "1",
            "auto_stop_minutes": self.auto_stop_minutes_var.get().strip() or "0",
            "countdown_enabled": bool(self.countdown_enabled_var.get()),
            "show_keys_overlay": bool(self.show_keys_overlay_var.get()),
            "open_folder_after_stop": bool(self.open_folder_after_stop_var.get()),
            "problem_logs_enabled": bool(self.problem_logs_enabled_var.get()),
            "problem_logs_retention_days": str(self.problem_logs_retention_days_var.get()).strip() or "120",
            "problem_logs_error_retention_days": str(self.problem_logs_error_retention_days_var.get()).strip() or "120",
            "problem_logs_max_file_mb": str(self.problem_logs_max_file_mb_var.get()).strip() or "2",
            "problem_logs_cleanup_on_start": bool(self.problem_logs_cleanup_on_start_var.get()),
            "problem_logs_keep_successful": bool(self.problem_logs_keep_successful_var.get()),
        }
        # Позиция плавающей панели сохраняется рядом с остальными настройками.
        try:
            if "floating_panel_x" in self.settings:
                data["floating_panel_x"] = int(self.settings.get("floating_panel_x"))
            if "floating_panel_y" in self.settings:
                data["floating_panel_y"] = int(self.settings.get("floating_panel_y"))
            for key in ("webcam_preview_x", "webcam_preview_y", "webcam_preview_width", "webcam_preview_height"):
                if key in self.settings:
                    data[key] = int(self.settings.get(key))
        except Exception:
            pass
        try:
            SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(data, ensure_ascii=False, indent=2)
            # Обновляем backup только из заведомо корректного старого JSON. Если
            # основной файл уже повреждён, хорошую резервную копию не затираем.
            try:
                if SETTINGS_PATH.exists():
                    previous_text = SETTINGS_PATH.read_text(encoding="utf-8")
                    previous_data = json.loads(previous_text)
                    if isinstance(previous_data, dict):
                        atomic_write_text(SETTINGS_BACKUP_PATH, previous_text)
            except Exception as backup_exc:
                self.diagnostic_log(
                    "settings_backup_update_skipped",
                    {"error": repr(backup_exc)},
                    level="WARN",
                )
            atomic_write_text(SETTINGS_PATH, serialized)
            self.diagnostic_log("settings_saved", {"settings_path": SETTINGS_PATH, "settings": data})
        except Exception as exc:
            self.log_exception("save_settings", exc)

    def update_cursor_state_text(self):
        try:
            if not hasattr(self, "cursor_state_text"):
                return
            visible = "виден" if self.cursor_visible_var.get() else "скрыт"
            if self.cursor_highlight_var.get():
                self.cursor_state_text.set(f"{visible}, подсветка {int(self.cursor_highlight_size_var.get())}")
            else:
                self.cursor_state_text.set(f"{visible}, без подсветки")
        except Exception:
            pass

    def update_auto_adjust_fps_text(self):
        try:
            if not hasattr(self, "auto_adjust_fps_text"):
                return
            self.auto_adjust_fps_text.set("включена" if self.auto_adjust_fps_var.get() else "выключена")
        except Exception:
            pass

    def on_cursor_setting_changed(self, *_args):
        self.update_cursor_state_text()
        self.schedule_save_settings()

    def on_floating_panel_setting_changed(self, *_args):
        self.schedule_save_settings()
        if self.initializing:
            return
        try:
            # В новой логике плавающая панель — единственный основной интерфейс.
            # Не даём старым настройкам или случайному изменению переменной
            # выключить её и оставить программу только в фоне.
            if not self.draw_enabled_var.get():
                self.draw_enabled_var.set(True)
                return
            self.show_annotation_overlay(open_toolbar=True)
        except Exception as exc:
            self.log_exception("on_floating_panel_setting_changed", exc)

    def on_floating_panel_size_changed(self, *_args):
        self.schedule_save_settings()
        if self.initializing:
            return
        try:
            if self.annotation_overlay is not None:
                self.annotation_overlay.apply_bubble_size()
        except Exception as exc:
            self.log_exception("on_floating_panel_size_changed", exc)

    def schedule_save_settings(self, *_args):
        if self.initializing:
            return
        self.update_cursor_state_text()
        self.update_auto_adjust_fps_text()
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(400, self.save_settings)

    def bind_setting_traces(self):
        general_vars = [
            self.output_folder,
            self.format_var,
            self.fps_var,
            self.auto_adjust_fps_var,
            self.video_bitrate_var,
            self.capture_method_var,
            self.encoder_var,
            self.audio_bitrate_var,
            self.mic_volume_var,
            self.system_volume_var,
            self.cursor_visible_var,
            self.cursor_highlight_var,
            self.cursor_highlight_size_var,
            self.problem_logs_enabled_var,
            self.problem_logs_retention_days_var,
            self.problem_logs_error_retention_days_var,
            self.problem_logs_max_file_mb_var,
            self.problem_logs_cleanup_on_start_var,
            self.problem_logs_keep_successful_var,
        ]
        for var in general_vars:
            var.trace_add("write", self.schedule_save_settings)

        for var in (
            self.problem_logs_enabled_var,
            self.problem_logs_retention_days_var,
            self.problem_logs_error_retention_days_var,
            self.problem_logs_max_file_mb_var,
            self.problem_logs_cleanup_on_start_var,
            self.problem_logs_keep_successful_var,
        ):
            var.trace_add("write", self.on_problem_logs_setting_changed)

        self.draw_enabled_var.trace_add("write", self.on_floating_panel_setting_changed)
        self.floating_panel_size_var.trace_add("write", self.on_floating_panel_size_changed)
        self.mic_device_var.trace_add("write", self.on_audio_device_changed)
        self.system_device_var.trace_add("write", self.on_audio_device_changed)
        self.webcam_device_var.trace_add("write", self.on_webcam_device_changed)
        self.hotkey_var.trace_add("write", self.on_hotkey_changed)
        self.screenshot_hotkey_var.trace_add("write", self.on_hotkey_changed)
        self.startup_tray_var.trace_add("write", self.on_startup_tray_changed)

    def on_audio_device_changed(self, *_args):
        self.schedule_save_settings()
        self.schedule_meter_restart()

    def on_webcam_device_changed(self, *_args):
        self.schedule_save_settings()
        if self.initializing:
            return
        preview = getattr(self, "webcam_preview", None)
        try:
            if preview is not None and preview.is_open():
                preview.close()
                self.root.after(120, self.toggle_webcam_preview)
        except Exception:
            self.webcam_preview = None

    def on_hotkey_changed(self, *_args):
        self.schedule_save_settings()
        if self.hotkey_job:
            self.root.after_cancel(self.hotkey_job)
        self.hotkey_job = self.root.after(600, self.register_hotkey)

    def on_startup_tray_changed(self, *_args):
        self.schedule_save_settings()
        if self.initializing:
            return
        if self.sync_startup_tray_setting(show_errors=True, source="settings_change"):
            # Не ждём отложенного сохранения: состояние файла настроек и Run
            # должны измениться вместе даже при быстром закрытии программы.
            self.save_settings()
