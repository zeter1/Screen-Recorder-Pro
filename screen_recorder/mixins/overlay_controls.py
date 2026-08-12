from ..shared import *
from ..components.annotation_overlay import AnnotationOverlay
from ..components.webcam_preview import WebcamPreviewWindow


class OverlayControlsMixin:
    def start_minimal_panel_mode(self):
        """Запускает программу в новом минимальном режиме.

        Пользователь видит только плавающую панель записи. Главное окно
        остаётся скрытым, иконка находится в системном трее, а настройки
        открываются с кнопки на плавающей панели.
        """
        try:
            if not self.draw_enabled_var.get():
                self.draw_enabled_var.set(True)
        except Exception:
            pass

        panel_ok = False
        tray_ok = False
        try:
            panel_ok = bool(self.show_annotation_overlay(open_toolbar=True))
        except Exception as exc:
            self.log_exception("start_minimal_panel_mode.show_annotation_overlay", exc)

        try:
            tray_ok = bool(self.ensure_tray_icon())
        except Exception as exc:
            self.log_exception("start_minimal_panel_mode.ensure_tray_icon", exc)

        try:
            if panel_ok and tray_ok:
                self.root.withdraw()
            else:
                # Если хотя бы один обязательный элемент минимального режима не
                # создался, не оставляем приложение полностью невидимым.
                self.root.deiconify()
                self.root.lift()
        except Exception:
            pass
        try:
            if panel_ok and tray_ok:
                self.status_var.set("Программа работает в трее. Управление записью — только через плавающую панель.")
            else:
                missing = []
                if not panel_ok:
                    missing.append("плавающая кнопка")
                if not tray_ok:
                    missing.append("иконка в трее")
                self.status_var.set("Не запустились: " + ", ".join(missing))
        except Exception:
            pass
        self.diagnostic_log("minimal_panel_mode_started", {
            "started_from_windows_startup": self.started_from_windows_startup,
            "floating_button_ready": panel_ok,
            "tray_icon_ready": tray_ok,
            "fallback_main_window_visible": not (panel_ok and tray_ok),
        }, level="INFO" if panel_ok and tray_ok else "ERROR")

    def show_annotation_overlay(self, open_toolbar=True):
        try:
            if self.annotation_overlay is None:
                self.annotation_overlay = AnnotationOverlay(self)
            self.annotation_overlay.show_bubble_only()
            self.annotation_overlay.update_record_controls()
            if open_toolbar:
                # При включении/старте один раз показываем панель, затем она сама свернётся.
                self.annotation_overlay.show_toolbar()
                self.annotation_overlay.schedule_toolbar_hide()
            self.status_var.set("Плавающая панель включена: наведи на индикатор ●, чтобы открыть кнопки записи и карандаши.")
            return True
        except Exception as exc:
            self.annotation_overlay = None
            self.status_var.set(f"Не удалось открыть плавающую панель: {exc}")
            self.log_exception("show_annotation_overlay", exc)
            return False

    def close_annotation_overlay(self):
        if self.annotation_overlay:
            self.annotation_overlay.destroy()
            self.annotation_overlay = None

    def toggle_webcam_preview(self):
        preview = getattr(self, "webcam_preview", None)
        try:
            if preview is not None and preview.is_open():
                preview.close()
                return
        except Exception:
            self.webcam_preview = None

        try:
            preview = WebcamPreviewWindow(self)
            if preview.is_open():
                self.webcam_preview = preview
                preview.lift()
                self.status_var.set("Предпросмотр вебкамеры открыт.")
            else:
                self.webcam_preview = None
        except Exception as exc:
            self.webcam_preview = None
            self.status_var.set(f"Не удалось открыть вебкамеру: {exc}")
            try:
                messagebox.showerror("Вебкамера", f"Не удалось открыть вебкамеру:\n{exc}")
            except Exception:
                pass
        finally:
            try:
                if self.annotation_overlay is not None:
                    self.annotation_overlay.update_record_controls()
            except Exception:
                pass

    def close_webcam_preview(self):
        preview = getattr(self, "webcam_preview", None)
        self.webcam_preview = None
        if preview is not None:
            try:
                preview.close()
            except Exception:
                pass
        try:
            if self.annotation_overlay is not None:
                self.annotation_overlay.update_record_controls()
        except Exception:
            pass

    def on_webcam_preview_closed(self, preview=None):
        try:
            if preview is None or self.webcam_preview is preview:
                self.webcam_preview = None
            if hasattr(self, "status_var"):
                self.status_var.set("Предпросмотр вебкамеры закрыт.")
            if self.annotation_overlay is not None:
                self.annotation_overlay.update_record_controls()
        except Exception:
            pass
