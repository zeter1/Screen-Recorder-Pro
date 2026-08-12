from ..shared import *


class AnnotationOverlay:
    """Плавающая панель записи поверх экрана с инструментами рисования.

    Панель раскрывается при наведении/клике на маленький индикатор,
    умеет запускать/останавливать запись, ставить запись на паузу,
    продолжать запись и включать карандаш для пометок на экране.
    Маленький индикатор можно перетаскивать в любое место; положение
    сохраняется в settings.json.
    """

    TRANSPARENT_COLOR = "#ff00ff"
    WDA_MONITOR = 0x00000001
    WDA_EXCLUDEFROMCAPTURE = 0x00000011

    def __init__(self, app):
        self.app = app
        self.root = app.root

        self.overlay = None          # слой с видимыми линиями
        self.input_blocker = None    # прозрачный перехватчик кликов во время рисования
        self.bubble = None           # маленькая плавающая кнопка
        self.bubble_label = None
        self.bubble_menu = None
        self.bubble_size = 34
        self.toolbar = None          # раскрывающаяся панель
        self.canvas = None
        self.toolbar_record_button = None
        self.toolbar_pause_button = None
        self.toolbar_stop_button = None
        self.toolbar_open_folder_button = None
        self.toolbar_settings_button = None
        self.toolbar_webcam_button = None
        self._esc_hotkey_handle = None

        self.last_x = None
        self.last_y = None
        self.pen_color = "yellow"
        self.pen_width = 5
        self.pen_active = False
        self.mouse_is_down = False

        self.bubble_rect = (0, 0, 0, 0)
        self.toolbar_rect = (0, 0, 0, 0)
        self._rect_lock = threading.Lock()
        self.toolbar_visible = False
        self.bubble_hover = False
        self.toolbar_hover = False
        self._toolbar_hide_job = None
        self._topmost_job = None
        self._rect_job = None
        self._bubble_feedback_job = None
        self._drag_start = None
        self._drag_window_start = None
        self._drag_moved = False

        self.create_overlay()
        self.create_input_blocker()
        self.create_bubble()
        self.create_toolbar()
        self.install_escape_shortcut()
        self.update_control_rects()
        self.keep_controls_on_top()

    # -------------------- Windows helpers --------------------

    def is_windows(self):
        return os.name == "nt"

    def make_window_not_recorded(self, window):
        """Просим Windows не показывать служебное окно в захвате экрана.

        Используем только для невидимого окна-перехватчика мыши.
        Индикатор и плавающая панель не исключаются: они должны быть видны в записи.
        """
        if not self.is_windows() or not window:
            return False
        try:
            window.update_idletasks()
            hwnd = int(window.winfo_id())
            user32 = ctypes.windll.user32

            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            try:
                get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
                set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
                get_long.restype = ctypes.c_ssize_t
                get_long.argtypes = [wintypes.HWND, ctypes.c_int]
                set_long.restype = ctypes.c_ssize_t
                set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
                style = get_long(hwnd, GWL_EXSTYLE)
                style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
                set_long(hwnd, GWL_EXSTYLE, style)
            except Exception:
                pass

            try:
                user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
                user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
            except Exception:
                pass

            result = user32.SetWindowDisplayAffinity(hwnd, self.WDA_EXCLUDEFROMCAPTURE)
            if not result:
                result = user32.SetWindowDisplayAffinity(hwnd, self.WDA_MONITOR)
            return bool(result)
        except Exception:
            return False

    # -------------------- UI windows --------------------

    def create_overlay(self):
        """Прозрачный слой, где видны только нарисованные линии."""
        self.overlay = tk.Toplevel(self.root)
        self.overlay.title("Слой рисования")
        self.overlay.overrideredirect(True)
        self.overlay.configure(bg=self.TRANSPARENT_COLOR)
        self.overlay.attributes("-topmost", True)

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.overlay.geometry(f"{screen_w}x{screen_h}+0+0")

        try:
            self.overlay.wm_attributes("-transparentcolor", self.TRANSPARENT_COLOR)
        except tk.TclError as exc:
            try:
                self.overlay.destroy()
            except Exception:
                pass
            raise RuntimeError(
                "Tkinter на этой системе не включил прозрачность окна. "
                "Обнови Python/Tkinter или запусти программу через обычный python.exe."
            ) from exc

        self.canvas = tk.Canvas(
            self.overlay,
            bg=self.TRANSPARENT_COLOR,
            highlightthickness=0,
            bd=0,
            cursor="pencil",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.start_draw_event)
        self.canvas.bind("<B1-Motion>", self.move_draw_event)
        self.canvas.bind("<ButtonRelease-1>", self.end_draw_event)
        self.canvas.bind("<ButtonPress-2>", self.block_event)
        self.canvas.bind("<ButtonPress-3>", self.block_event)
        self.canvas.bind("<MouseWheel>", self.block_event)
        self.overlay.bind("<Escape>", self.handle_escape_key)
        self.overlay.withdraw()

    def create_input_blocker(self):
        """Прозрачное окно-перехватчик кликов.

        Оно включается только когда карандаш выбран. Пока карандаш не выбран,
        кликать по экрану можно как обычно.
        """
        self.input_blocker = tk.Toplevel(self.root)
        self.input_blocker.title("Перехват мыши для карандаша")
        self.input_blocker.overrideredirect(True)
        self.input_blocker.configure(bg="black")
        self.input_blocker.attributes("-topmost", True)
        try:
            self.input_blocker.attributes("-alpha", 0.01)
        except Exception:
            pass

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.input_blocker.geometry(f"{screen_w}x{screen_h}+0+0")
        self.input_blocker.configure(cursor="pencil")
        self.input_blocker.bind("<ButtonPress-1>", self.start_draw_event)
        self.input_blocker.bind("<B1-Motion>", self.move_draw_event)
        self.input_blocker.bind("<ButtonRelease-1>", self.end_draw_event)
        self.input_blocker.bind("<ButtonPress-2>", self.block_event)
        self.input_blocker.bind("<ButtonPress-3>", self.block_event)
        self.input_blocker.bind("<MouseWheel>", self.block_event)
        self.input_blocker.bind("<Escape>", self.handle_escape_key)
        self.input_blocker.update_idletasks()
        self.make_window_not_recorded(self.input_blocker)
        self.input_blocker.withdraw()

    def get_bubble_size(self):
        try:
            if hasattr(self.app, "floating_panel_size_var"):
                return normalize_floating_panel_size(self.app.floating_panel_size_var.get())
        except Exception:
            pass
        try:
            return normalize_floating_panel_size(getattr(self.app, "settings", {}).get("floating_panel_size", 34))
        except Exception:
            return 34

    @staticmethod
    def get_bubble_font_size(size):
        try:
            return max(11, min(34, int(round(int(size) * 0.48))))
        except Exception:
            return 16

    def apply_bubble_size(self):
        """Применяет новый размер плавающей кнопки без пересоздания панели."""
        if not self.bubble:
            return
        size = self.get_bubble_size()
        try:
            self.bubble.update_idletasks()
            x = int(self.bubble.winfo_rootx())
            y = int(self.bubble.winfo_rooty())
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = max(0, min(sw - size, x))
            y = max(0, min(sh - size, y))
            self.bubble.geometry(f"{size}x{size}+{x}+{y}")
            self.bubble_size = size
            if self.bubble_label:
                self.bubble_label.configure(font=("Segoe UI", self.get_bubble_font_size(size), "bold"))
            if self.toolbar_visible:
                self.place_toolbar_near_bubble()
            self.update_control_rects_now()
        except Exception:
            pass

    def create_bubble(self):
        self.bubble = tk.Toplevel(self.root)
        self.bubble.title("Плавающая панель записи")
        self.bubble.overrideredirect(True)
        self.bubble.attributes("-topmost", True)
        self.bubble.configure(bg="#202020")
        try:
            self.bubble.attributes("-alpha", 0.92)
        except Exception:
            pass

        size = self.get_bubble_size()
        self.bubble_size = size
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        default_x = max(0, screen_w - size - 22)
        default_y = 22
        try:
            x = int(getattr(self.app, "settings", {}).get("floating_panel_x", default_x))
            y = int(getattr(self.app, "settings", {}).get("floating_panel_y", default_y))
        except Exception:
            x, y = default_x, default_y
        x = max(0, min(screen_w - size, x))
        y = max(0, min(screen_h - size, y))
        self.bubble.geometry(f"{size}x{size}+{x}+{y}")

        # Tkinter не умеет настоящий круг без усложнений, поэтому делаем компактную
        # тёмную кнопку. Индикатор должен быть виден поверх экрана и доступен
        # даже когда основное окно свёрнуто в трей. Размер берётся из настроек.
        self.bubble_label = tk.Label(
            self.bubble,
            text="●",
            bg="#202020",
            fg="#ff4d4d",
            font=("Segoe UI", self.get_bubble_font_size(size), "bold"),
            cursor="hand2",
        )
        self.bubble_label.pack(fill="both", expand=True)
        self.bubble_menu = tk.Menu(self.bubble, tearoff=0)
        self.bubble_menu.add_command(label="Закрыть", command=self.exit_app_from_bubble)
        for widget in (self.bubble, self.bubble_label):
            widget.bind("<Enter>", self.on_bubble_enter)
            widget.bind("<Leave>", self.on_bubble_leave)
            widget.bind("<ButtonPress-1>", self.start_drag_controls)
            widget.bind("<B1-Motion>", self.drag_controls)
            widget.bind("<ButtonRelease-1>", self.end_drag_controls)
            widget.bind("<ButtonPress-3>", self.show_bubble_context_menu)

        self.bubble.update_idletasks()
        self.bubble.deiconify()

    def show_bubble_context_menu(self, event):
        """Показывает меню полного выхода по правому клику на плавающей кнопке."""
        if not self.bubble_menu:
            return "break"
        self.bubble_hover = True
        try:
            self.bubble_menu.tk_popup(int(event.x_root), int(event.y_root))
        finally:
            try:
                self.bubble_menu.grab_release()
            except Exception:
                pass
            self.bubble_hover = False
            self.schedule_toolbar_hide()
        return "break"

    def exit_app_from_bubble(self):
        """Закрывает не только панель, а всё приложение и его дочерние процессы."""
        try:
            self.hide_toolbar()
        except Exception:
            pass
        self.root.after_idle(self.app.exit_app)

    def show_screenshot_feedback(self, success=True):
        """Коротко подтверждает снимок на самой плавающей кнопке."""
        if not self.bubble_label:
            return
        if self._bubble_feedback_job:
            try:
                self.root.after_cancel(self._bubble_feedback_job)
            except Exception:
                pass
            self._bubble_feedback_job = None
        try:
            self.bubble_label.configure(
                text="✓" if success else "!",
                fg="#55dd77" if success else "#ffcc44",
            )
        except Exception:
            return

        def restore():
            self._bubble_feedback_job = None
            try:
                if self.bubble_label:
                    self.bubble_label.configure(text="●", fg="#ff4d4d")
            except Exception:
                pass

        self._bubble_feedback_job = self.root.after(900, restore)

    def create_toolbar(self):
        self.toolbar = tk.Toplevel(self.root)
        self.toolbar.title("Плавающая панель записи")
        self.toolbar.overrideredirect(True)
        self.toolbar.attributes("-topmost", True)
        self.toolbar.resizable(False, False)
        self.toolbar.configure(bg="#1f1f1f")
        try:
            self.toolbar.attributes("-alpha", 0.96)
        except Exception:
            pass
        self.toolbar.bind("<Escape>", self.handle_escape_key)
        self.toolbar.bind("<Enter>", self.on_toolbar_enter)
        self.toolbar.bind("<Leave>", self.on_toolbar_leave)
        self.toolbar.bind("<Configure>", lambda _event: self.update_control_rects_now())

        frame = tk.Frame(self.toolbar, bg="#1f1f1f")
        frame.pack(fill="both", expand=True, padx=8, pady=7)
        frame.bind("<ButtonPress-1>", self.start_drag_controls)
        frame.bind("<B1-Motion>", self.drag_controls)
        frame.bind("<ButtonRelease-1>", self.end_drag_controls)

        title = tk.Label(
            frame,
            text="Запись:",
            bg="#1f1f1f",
            fg="white",
            font=("Segoe UI", 9, "bold"),
            cursor="fleur",
        )
        title.pack(side="left", padx=(0, 6))
        title.bind("<ButtonPress-1>", self.start_drag_controls)
        title.bind("<B1-Motion>", self.drag_controls)
        title.bind("<ButtonRelease-1>", self.end_drag_controls)

        self.toolbar_record_button = tk.Button(
            frame,
            text="⏺ Запись",
            command=self.toggle_recording_from_toolbar,
            width=11,
        )
        self.toolbar_record_button.pack(side="left", padx=(0, 3))
        # Старое имя оставлено как alias, чтобы не ломать внутренние вызовы.
        self.toolbar_stop_button = self.toolbar_record_button

        self.toolbar_pause_button = tk.Button(
            frame,
            text="⏸ Пауза",
            command=self.toggle_recording_pause_from_toolbar,
            width=12,
            state="disabled",
        )
        self.toolbar_pause_button.pack(side="left", padx=3)

        self.toolbar_region_button = tk.Button(
            frame,
            text="⬚ Область",
            command=self.select_region_from_toolbar,
            width=10,
        )
        self.toolbar_region_button.pack(side="left", padx=(5, 3))

        self.toolbar_webcam_button = tk.Button(
            frame,
            text="📷 Вебкамера",
            command=self.toggle_webcam_preview_from_toolbar,
            width=13,
        )
        self.toolbar_webcam_button.pack(side="left", padx=(0, 3))

        self.toolbar_settings_button = tk.Button(
            frame,
            text="⚙ Настройки",
            command=self.open_settings_from_toolbar,
            width=11,
        )
        self.toolbar_settings_button.pack(side="left", padx=(0, 5))

        self.toolbar_open_folder_button = tk.Button(
            frame,
            text="📁 Папка",
            command=self.open_last_output_folder_from_toolbar,
            width=10,
            state="disabled",
        )
        self.toolbar_open_folder_button.pack(side="left", padx=(0, 3))

        tk.Label(frame, text="│", bg="#1f1f1f", fg="#444444", font=("Segoe UI", 11)).pack(side="left", padx=4)

        tk.Label(frame, text="Карандаш:", bg="#1f1f1f", fg="white", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(8, 4))
        colors = [
            ("Красный", "red"),
            ("Жёлтый", "yellow"),
            ("Зелёный", "lime"),
            ("Синий", "deepskyblue"),
            ("Белый", "white"),
        ]
        for text, color in colors:
            tk.Button(frame, text=text, command=lambda c=color: self.set_color(c), width=8).pack(side="left", padx=1)

        tk.Button(frame, text="Очистить", command=self.put_down_pencil, width=9).pack(side="left", padx=(6, 3))

        tk.Label(frame, text="Толщина", bg="#1f1f1f", fg="white", font=("Segoe UI", 8)).pack(side="left", padx=(7, 2))
        self.width_scale = tk.Scale(
            frame,
            from_=2,
            to=18,
            orient="horizontal",
            length=76,
            showvalue=True,
            bg="#1f1f1f",
            fg="white",
            troughcolor="#333333",
            highlightthickness=0,
            command=self.set_width,
        )
        self.width_scale.set(self.pen_width)
        self.width_scale.pack(side="left", padx=(0, 2))

        self.toolbar.update_idletasks()
        self.place_toolbar_near_bubble()
        # Панель не держим постоянно открытой: она раскрывается при
        # наведении/клике на индикатор ● и сама сворачивается обратно,
        # чтобы не занимать место на экране.
        self.toolbar.withdraw()
        self.toolbar_visible = False
        self.update_record_controls()

    # -------------------- Bubble / toolbar behaviour --------------------

    def on_bubble_enter(self, _event=None):
        self.bubble_hover = True
        self.show_toolbar()

    def on_bubble_leave(self, _event=None):
        self.bubble_hover = False
        self.schedule_toolbar_hide()

    def on_toolbar_enter(self, _event=None):
        self.toolbar_hover = True
        if self._toolbar_hide_job:
            try:
                self.root.after_cancel(self._toolbar_hide_job)
            except Exception:
                pass
            self._toolbar_hide_job = None

    def on_toolbar_leave(self, _event=None):
        self.toolbar_hover = False
        self.schedule_toolbar_hide()

    def schedule_toolbar_hide(self):
        # Автоскрытие работает и во время записи: пользователь видит маленький
        # индикатор ●, а раскрытая панель не занимает лишнее место на экране.
        if self._toolbar_hide_job:
            try:
                self.root.after_cancel(self._toolbar_hide_job)
            except Exception:
                pass
        self._toolbar_hide_job = self.root.after(650, self.hide_toolbar_if_not_hovered)

    def hide_toolbar_if_not_hovered(self):
        self._toolbar_hide_job = None
        if self.bubble_hover or self.toolbar_hover:
            return
        self.hide_toolbar()

    def show_toolbar(self):
        if not self.toolbar:
            return
        self.update_record_controls()
        self.place_toolbar_near_bubble()
        self.toolbar.deiconify()
        self.toolbar_visible = True
        self.toolbar.attributes("-topmost", True)
        self.toolbar.lift()
        if self.bubble:
            self.bubble.lift()
        self.update_control_rects_now()

    def hide_toolbar(self):
        if self.toolbar:
            self.toolbar.withdraw()
        self.toolbar_visible = False
        self.update_control_rects_now()

    def place_toolbar_near_bubble(self):
        if not self.toolbar or not self.bubble:
            return
        try:
            self.toolbar.update_idletasks()
            self.bubble.update_idletasks()
            bw = self.bubble.winfo_width() or self.bubble_size or 34
            bh = self.bubble.winfo_height() or self.bubble_size or 34
            bx = self.bubble.winfo_rootx()
            by = self.bubble.winfo_rooty()
            tw = self.toolbar.winfo_reqwidth()
            th = self.toolbar.winfo_reqheight()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            # По умолчанию панель раскрывается левее и немного выше кружка.
            x = min(max(8, bx + bw - tw), max(8, sw - tw - 8))
            y = min(max(8, by - th - 8), max(8, sh - th - 8))
            self.toolbar.geometry(f"{tw}x{th}+{x}+{y}")
        except Exception:
            pass

    def start_drag_controls(self, event):
        try:
            if self._toolbar_hide_job:
                try:
                    self.root.after_cancel(self._toolbar_hide_job)
                except Exception:
                    pass
                self._toolbar_hide_job = None
            self._drag_start = (int(event.x_root), int(event.y_root))
            self._drag_window_start = (self.bubble.winfo_rootx(), self.bubble.winfo_rooty())
            self._drag_moved = False
        except Exception:
            self._drag_start = None
            self._drag_window_start = None
            self._drag_moved = False

    def drag_controls(self, event):
        if not self._drag_start or not self._drag_window_start or not self.bubble:
            return
        try:
            dx = int(event.x_root) - self._drag_start[0]
            dy = int(event.y_root) - self._drag_start[1]
            if abs(dx) + abs(dy) > 4:
                self._drag_moved = True
            x = self._drag_window_start[0] + dx
            y = self._drag_window_start[1] + dy
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            bw = self.bubble.winfo_width() or self.bubble_size or 34
            bh = self.bubble.winfo_height() or self.bubble_size or 34
            x = max(0, min(sw - bw, x))
            y = max(0, min(sh - bh, y))
            self.bubble.geometry(f"+{x}+{y}")
            if self.toolbar_visible:
                self.place_toolbar_near_bubble()
            self.update_control_rects_now()
        except Exception:
            pass

    def end_drag_controls(self, _event=None):
        moved = bool(getattr(self, "_drag_moved", False))
        self._drag_start = None
        self._drag_window_start = None
        self._drag_moved = False
        self.update_control_rects_now()
        try:
            if self.bubble:
                x = int(self.bubble.winfo_rootx())
                y = int(self.bubble.winfo_rooty())
                if hasattr(self.app, "settings"):
                    self.app.settings["floating_panel_x"] = x
                    self.app.settings["floating_panel_y"] = y
                    self.app.schedule_save_settings()
        except Exception:
            pass
        # Короткий клик по индикатору открывает панель, а перетаскивание только двигает индикатор.
        if not moved:
            self.show_toolbar()

    def prevent_toolbar_close(self):
        try:
            if self.bubble:
                self.bubble.deiconify()
                self.bubble.attributes("-topmost", True)
                self.bubble.lift()
            if hasattr(self.app, "status_var"):
                self.app.status_var.set("Плавающая панель сворачивается в маленький индикатор. Наведи на него, чтобы снова открыть кнопки.")
        except Exception:
            pass
        return "break"

    # -------------------- Commands --------------------

    def install_escape_shortcut(self):
        for window in [self.overlay, self.input_blocker, self.toolbar, self.bubble]:
            try:
                if window:
                    window.bind("<Escape>", self.handle_escape_key)
            except Exception:
                pass
        if HOTKEY_AVAILABLE:
            try:
                self._esc_hotkey_handle = keyboard.add_hotkey(
                    "esc",
                    lambda: self.app.enqueue_hotkey_action("escape"),
                    suppress=False,
                )
            except Exception:
                self._esc_hotkey_handle = None

    def handle_escape_key(self, _event=None):
        self.put_down_pencil()
        return "break"

    def open_last_output_folder_from_toolbar(self):
        try:
            self.app.open_last_output_folder()
        finally:
            self.update_record_controls()
            self.show_toolbar()

    def select_region_from_toolbar(self):
        """Выбор области записи с панели → запись стартует сразу по этой области.

        Прячем панель и индикатор на время выбора, чтобы они не мешали и не
        попали в кадр/координаты.
        """
        if getattr(self.app, "is_recording", False):
            return
        try:
            if self.toolbar:
                self.toolbar.withdraw()
            self.toolbar_visible = False
            if self.bubble:
                self.bubble.withdraw()
        except Exception:
            pass
        self.root.after(50, lambda: self.app.select_capture_region(on_done=self._after_region_selected))

    def _after_region_selected(self, region):
        # Возвращаем индикатор...
        try:
            if self.bubble:
                self.bubble.deiconify()
                self.bubble.attributes("-topmost", True)
                self.bubble.lift()
        except Exception:
            pass
        # ...и если область выбрана — сразу запускаем запись по ней.
        if region:
            self.app._pending_region = region
            self.root.after(80, self.app.start_recording)

    def open_settings_from_toolbar(self):
        """Открывает настройки прямо из плавающей панели.

        Основное окно программы в минимальном режиме скрыто, поэтому настройки
        должны быть доступны с единственного видимого элемента интерфейса.
        """
        try:
            self.show_toolbar()
            self.app.open_settings_window()
        finally:
            self.update_record_controls()

    def toggle_webcam_preview_from_toolbar(self):
        try:
            self.root.after(0, self.app.toggle_webcam_preview)
            self.root.after(80, self.update_record_controls)
        except Exception as exc:
            try:
                self.app.status_var.set(f"Не удалось переключить вебкамеру: {exc}")
            except Exception:
                pass

    def toggle_recording_from_toolbar(self):
        if getattr(self.app, "is_finalizing", False) or getattr(self.app, "is_starting", False):
            return
        if getattr(self.app, "is_recording", False):
            self.stop_recording_from_toolbar()
            return
        try:
            if hasattr(self.app, "status_var"):
                self.app.status_var.set("Запускаю запись из плавающей панели...")
            self.update_record_controls()
            self.show_toolbar()
        except Exception:
            pass
        self.root.after(0, self.app.start_recording)

    def toggle_recording_pause_from_toolbar(self):
        if not getattr(self.app, "is_recording", False) or getattr(self.app, "is_finalizing", False):
            return
        self.root.after(0, self._toggle_recording_pause_from_toolbar)

    def _toggle_recording_pause_from_toolbar(self):
        try:
            self.app.toggle_pause()
        finally:
            self.update_record_controls()
            self.show_toolbar()

    def stop_recording_from_toolbar(self):
        if not getattr(self.app, "is_recording", False) or getattr(self.app, "is_finalizing", False):
            return
        try:
            if self.toolbar_record_button:
                self.toolbar_record_button.configure(state="disabled", text="Сохраняю...")
            if self.toolbar_pause_button:
                self.toolbar_pause_button.configure(state="disabled")
            if hasattr(self.app, "status_var"):
                self.app.status_var.set("Останавливаю запись из плавающей панели...")
        except Exception:
            pass

        # Перед остановкой не закрываем панель резко, чтобы не ловить подвисания Tk.
        # Сбрасываем только активное рисование, чтобы не оставалась зажатая линия.
        try:
            self.pen_active = False
            self.mouse_is_down = False
            self.last_x = None
            self.last_y = None
            self.update_control_rects_now()
            self.root.update_idletasks()
        except Exception:
            pass

        self.root.after(0, self.app.stop_recording)

    def reset_after_recording_stopped(self):
        """Оставляет плавающую панель на экране, но убирает линии и активный карандаш."""
        try:
            self.pen_active = False
            self.mouse_is_down = False
            self.last_x = None
            self.last_y = None
            self.clear()
            self.hide_layer()
            self.show_bubble_only()
            self.update_record_controls()
        except Exception:
            pass

    def update_pause_button_text(self):
        self.update_record_controls()

    def update_record_controls(self):
        try:
            recording = bool(getattr(self.app, "is_recording", False))
            paused = bool(getattr(self.app, "is_paused", False))
            finalizing = bool(getattr(self.app, "is_finalizing", False))
            starting = bool(getattr(self.app, "is_starting", False))
            pause_transitioning = bool(getattr(self.app, "is_pause_transitioning", False))
            busy = recording or finalizing or starting or pause_transitioning
            if self.toolbar_settings_button:
                self.toolbar_settings_button.configure(state="disabled" if busy else "normal")
            if getattr(self, "toolbar_region_button", None):
                # Область нельзя менять во время записи.
                self.toolbar_region_button.configure(state="disabled" if busy else "normal")
            if self.toolbar_record_button:
                if finalizing:
                    self.toolbar_record_button.configure(text="💾 Сохраняю...", state="disabled")
                elif starting:
                    self.toolbar_record_button.configure(text="⏳ Запускаю...", state="disabled")
                elif pause_transitioning:
                    self.toolbar_record_button.configure(text="⏳ Ждём...", state="disabled")
                elif recording:
                    self.toolbar_record_button.configure(text="⏹ Стоп", state="normal")
                else:
                    self.toolbar_record_button.configure(text="⏺ Запись", state="normal")
            if self.toolbar_pause_button:
                self.toolbar_pause_button.configure(
                    text="▶ Возобновить" if paused else "⏸ Пауза",
                    state="normal" if recording and not finalizing and not pause_transitioning else "disabled",
                )
            if self.toolbar_webcam_button:
                self.toolbar_webcam_button.configure(
                    text="📷 Вебкамера",
                    state="normal",
                )
            if self.toolbar_open_folder_button:
                output_exists = bool(getattr(self.app, "last_output_path", None) and Path(self.app.last_output_path).is_file())
                self.toolbar_open_folder_button.configure(state="normal" if output_exists else "disabled")
        except Exception:
            pass

    # -------------------- Drawing / blocking --------------------

    def start_draw_event(self, event):
        if not self.pen_active:
            return "break"
        x = int(getattr(event, "x_root", event.x))
        y = int(getattr(event, "y_root", event.y))
        if self.is_inside_controls_cached(x, y):
            return None
        self.mouse_is_down = True
        self.last_x = x
        self.last_y = y
        return "break"

    def move_draw_event(self, event):
        if not self.pen_active or not self.mouse_is_down:
            return "break"
        x = int(getattr(event, "x_root", event.x))
        y = int(getattr(event, "y_root", event.y))
        if self.is_inside_controls_cached(x, y):
            return "break"
        if self.last_x is not None and self.last_y is not None:
            if abs(x - self.last_x) + abs(y - self.last_y) < 3:
                return "break"
        self.draw_global(x, y)
        return "break"

    def end_draw_event(self, event=None):
        self.mouse_is_down = False
        self.last_x = None
        self.last_y = None
        return "break"

    def block_event(self, event=None):
        return "break" if self.pen_active else None

    def draw_global(self, x, y):
        if not self.canvas or not self.pen_active:
            return
        if self.last_x is None or self.last_y is None:
            self.last_x = x
            self.last_y = y
            return
        self.canvas.create_line(
            self.last_x,
            self.last_y,
            x,
            y,
            fill=self.pen_color,
            width=self.pen_width,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
            smooth=False,
        )
        self.last_x = x
        self.last_y = y

    def set_color(self, color):
        self.pen_color = color
        self.pen_active = True
        self.mouse_is_down = False
        self.last_x = None
        self.last_y = None
        self.show_layer()
        # После выбора цвета панель можно свернуть — маленький круг остаётся
        # видимым, а рисование продолжает работать.
        self.schedule_toolbar_hide()

    def put_down_pencil(self):
        self.pen_active = False
        self.clear()
        self.mouse_is_down = False
        self.last_x = None
        self.last_y = None
        self.hide_layer()
        self.show_bubble_only()
        self.schedule_toolbar_hide()

    def set_width(self, value):
        try:
            self.pen_width = int(float(value))
        except Exception:
            self.pen_width = 5

    def clear(self):
        if self.canvas:
            self.canvas.delete("all")
            try:
                self.canvas.update_idletasks()
            except Exception:
                pass

    def show_layer(self):
        if self.input_blocker:
            self.input_blocker.deiconify()
            self.input_blocker.attributes("-topmost", True)
            self.make_window_not_recorded(self.input_blocker)
            self.input_blocker.lift()
        if self.overlay:
            self.overlay.deiconify()
            self.overlay.attributes("-topmost", True)
            self.overlay.lift()
        self.show_bubble_only()
        if self.toolbar_visible and self.toolbar:
            self.toolbar.lift()

    def hide_layer(self):
        if self.input_blocker:
            self.input_blocker.withdraw()
        if self.overlay:
            self.overlay.withdraw()

    def show_bubble_only(self):
        if self.bubble:
            self.bubble.deiconify()
            self.bubble.attributes("-topmost", True)
            self.bubble.lift()
        self.update_control_rects_now()

    # -------------------- Geometry / capture exclusion --------------------

    def _window_rect(self, window):
        if not window:
            return None
        try:
            if not window.winfo_exists():
                return None
            if str(window.state()) == "withdrawn":
                return None
            window.update_idletasks()
            x = int(window.winfo_rootx())
            y = int(window.winfo_rooty())
            w = int(window.winfo_width())
            h = int(window.winfo_height())
            if w <= 1 or h <= 1:
                return None
            return (x, y, x + w, y + h)
        except Exception:
            return None

    def update_control_rects_now(self):
        # Важно: эту функцию можно вызывать только из Tkinter/GUI-потока.
        # DXcam-поток не должен делать winfo/update_idletasks: при остановке
        # из плавающей панели это иногда подвешивало поток записи, FFmpeg не
        # получал EOF по stdin, сохранение длилось десятки секунд, а файл мог
        # остаться без корректного trailer.
        try:
            gui_ident = getattr(self.app, "gui_thread_ident", None)
            if gui_ident is not None and threading.get_ident() != gui_ident:
                return
        except Exception:
            pass

        br = self._window_rect(self.bubble)
        tr = self._window_rect(self.toolbar) if self.toolbar_visible else None
        with self._rect_lock:
            self.bubble_rect = br or (0, 0, 0, 0)
            self.toolbar_rect = tr or (0, 0, 0, 0)

    def update_control_rects(self):
        self.update_control_rects_now()
        if self.root and self.running_safe():
            self._rect_job = self.root.after(160, self.update_control_rects)

    def running_safe(self):
        try:
            return bool(getattr(self.app, "running", True))
        except Exception:
            return True

    def is_inside_controls_cached(self, x, y):
        for rect in [self.toolbar_rect, self.bubble_rect]:
            try:
                x1, y1, x2, y2 = rect
                if x1 <= x <= x2 and y1 <= y <= y2:
                    return True
            except Exception:
                pass
        return False

    def get_control_rects_for_capture(self):
        """Прямоугольники служебных контролов, которые надо вырезать из DXcam-видео.

        Возвращает только кэш. Если вызов пришёл из GUI-потока, кэш можно
        предварительно обновить через Tkinter. Если вызов пришёл из потока
        записи DXcam, Tkinter не трогаем вообще.
        """
        try:
            gui_ident = getattr(self.app, "gui_thread_ident", None)
            if gui_ident is None or threading.get_ident() == gui_ident:
                self.update_control_rects_now()
        except Exception:
            pass

        rects = []
        try:
            with self._rect_lock:
                candidates = [self.bubble_rect, self.toolbar_rect]
        except Exception:
            candidates = []
        for rect in candidates:
            try:
                x1, y1, x2, y2 = rect
                if x2 - x1 > 2 and y2 - y1 > 2:
                    rects.append((int(x1), int(y1), int(x2), int(y2)))
            except Exception:
                pass
        return rects

    def keep_controls_on_top(self):
        try:
            if self.pen_active:
                if self.input_blocker:
                    self.input_blocker.attributes("-topmost", True)
                    self.make_window_not_recorded(self.input_blocker)
                    self.input_blocker.lift()
                if self.overlay:
                    self.overlay.attributes("-topmost", True)
                    self.overlay.lift()
            if self.toolbar_visible and self.toolbar:
                self.toolbar.attributes("-topmost", True)
                self.toolbar.lift()
            if self.bubble:
                self.bubble.attributes("-topmost", True)
                self.bubble.lift()
            self.update_record_controls()
        except Exception:
            return
        self._topmost_job = self.root.after(900, self.keep_controls_on_top)

    def destroy(self):
        self.pen_active = False
        self.mouse_is_down = False
        self.last_x = None
        self.last_y = None
        for job in [self._topmost_job, self._rect_job, self._toolbar_hide_job, self._bubble_feedback_job]:
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self._topmost_job = None
        self._rect_job = None
        self._toolbar_hide_job = None
        self._bubble_feedback_job = None
        if self._esc_hotkey_handle is not None and HOTKEY_AVAILABLE:
            try:
                keyboard.remove_hotkey(self._esc_hotkey_handle)
            except Exception:
                pass
            self._esc_hotkey_handle = None
        for window in [self.overlay, self.input_blocker, self.toolbar, self.bubble]:
            try:
                if window:
                    window.destroy()
            except Exception:
                pass
        self.bubble_menu = None
        self.overlay = None
        self.input_blocker = None
        self.toolbar = None
        self.bubble = None
        self.bubble_label = None
        self.canvas = None
