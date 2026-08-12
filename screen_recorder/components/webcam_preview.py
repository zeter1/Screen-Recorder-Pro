from ..shared import *


class WebcamPreviewWindow:
    """Отдельное растягиваемое окно предпросмотра вебкамеры."""

    def __init__(self, app):
        self.app = app
        self.root = app.root
        self.window = None
        self.canvas = None
        self.fullscreen_button = None
        self.close_button = None
        self.button_frame = None
        self.cv2 = None
        self.capture = None
        self.ffmpeg_process = None
        self.ffmpeg_frame_width = 640
        self.ffmpeg_frame_height = 480
        self.capture_thread = None
        self.stop_event = threading.Event()
        self.frame_queue = queue.Queue(maxsize=1)
        self.latest_frame = None
        self.latest_photo = None
        self.frame_job = None
        self.fullscreen = False
        self.normal_geometry = None
        self._closing = False
        self.status_message = "Открываю вебкамеру..."
        self._drag_mode = None
        self._drag_start = None
        self._window_start = None
        self._resize_margin = 10
        self._min_width = 180
        self._min_height = 130
        self.open()

    def open(self):
        if not PIL_AVAILABLE or ImageTk is None:
            try:
                messagebox.showerror(
                    "Вебкамера",
                    "Для предпросмотра вебкамеры нужен Pillow.\n\n"
                    "Установи зависимость:\n"
                    "pip install pillow"
                )
            except Exception:
                pass
            return

        try:
            import cv2
            self.cv2 = cv2
        except Exception:
            self.cv2 = None

        self.window = tk.Toplevel(self.root)
        self.window.title("Предпросмотр вебкамеры")
        self.window.overrideredirect(True)
        self.window.geometry(self.get_saved_geometry())
        self.window.minsize(self._min_width, self._min_height)
        self.window.resizable(False, False)
        self.window.configure(bg="#111111")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", self.exit_fullscreen_or_close)
        self.window.bind("<F11>", lambda _event: self.toggle_fullscreen())
        try:
            self.window.lift()
            self.window.focus_force()
            self.window.attributes("-topmost", True)
        except Exception:
            pass

        self.canvas = tk.Canvas(self.window, bg="#050505", highlightthickness=1, highlightbackground="#202020", bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.render_latest_frame())
        self.canvas.bind("<Motion>", self.update_resize_cursor)
        self.canvas.bind("<ButtonPress-1>", self.start_window_drag_or_resize)
        self.canvas.bind("<B1-Motion>", self.drag_or_resize_window)
        self.canvas.bind("<ButtonRelease-1>", self.end_window_drag_or_resize)
        self.canvas.bind("<ButtonPress-3>", self.start_window_drag_or_resize)
        self.canvas.bind("<B3-Motion>", self.drag_or_resize_window)
        self.canvas.bind("<ButtonRelease-3>", self.end_window_drag_or_resize)
        self.canvas.bind("<Double-Button-1>", lambda _event: self.toggle_fullscreen())

        self.button_frame = tk.Frame(self.window, bg="#101010", highlightthickness=1, highlightbackground="#3d3d3d")
        self.button_frame.place(x=10, y=10, anchor="nw")
        overlay_button_style = {
            "width": 2,
            "height": 1,
            "bd": 0,
            "relief": "flat",
            "bg": "#151515",
            "fg": "#f4f4f4",
            "activebackground": "#2b2b2b",
            "activeforeground": "#ffffff",
            "font": ("Segoe UI Symbol", 10, "bold"),
            "takefocus": False,
            "cursor": "hand2",
        }
        self.close_button = tk.Button(
            self.button_frame,
            text="✕",
            command=self.close,
            **overlay_button_style,
        )
        self.close_button.pack(side="left", padx=(2, 1), pady=2)
        self.fullscreen_button = tk.Button(
            self.button_frame,
            text="⤢",
            command=self.toggle_fullscreen,
            **overlay_button_style,
        )
        self.fullscreen_button.pack(side="left", padx=(1, 2), pady=2)
        self.bind_overlay_button_hover(self.close_button)
        self.bind_overlay_button_hover(self.fullscreen_button)
        self.create_tooltip(self.close_button, "Закрыть вебкамеру")
        self.create_tooltip(self.fullscreen_button, "Во весь экран / вернуть обратно")
        self.render_status_message()

        self.capture_thread = threading.Thread(target=self.capture_loop, name="webcam_preview_loop", daemon=True)
        self.capture_thread.start()
        self.schedule_frame_update()

    def is_open(self):
        try:
            return bool(self.window is not None and self.window.winfo_exists())
        except Exception:
            return False

    def get_saved_geometry(self):
        try:
            settings = getattr(self.app, "settings", {}) or {}
            mx, my, sw, sh = self.get_current_monitor_rect()
            width = int(settings.get("webcam_preview_width", 420))
            height = int(settings.get("webcam_preview_height", 300))
            default_x = mx + max(0, sw - width - 80)
            default_y = my + max(0, sh - height - 80)
            x = int(settings.get("webcam_preview_x", default_x))
            y = int(settings.get("webcam_preview_y", default_y))
            too_large = width >= int(sw * 0.94) and height >= int(sh * 0.88)
            stuck_to_bottom_right = (
                width >= int(sw * 0.75)
                and height >= int(sh * 0.75)
                and x >= mx + max(0, sw - width) - 8
                and y >= my + max(0, sh - height) - 8
            )
            if too_large or stuck_to_bottom_right:
                width, height = 420, 300
                default_x = mx + max(0, sw - width - 80)
                default_y = my + max(0, sh - height - 80)
                x, y = default_x, default_y
            width = max(self._min_width, min(width, max(self._min_width, int(sw * 0.9))))
            height = max(self._min_height, min(height, max(self._min_height, int(sh * 0.9))))
            x = max(mx, min(x, mx + max(0, sw - width)))
            y = max(my, min(y, my + max(0, sh - height)))
            return f"{width}x{height}+{x}+{y}"
        except Exception:
            return "420x300"

    def get_current_monitor_rect(self):
        if os.name == "nt" and self.window is not None:
            try:
                user32 = ctypes.windll.user32
                hwnd = int(self.window.winfo_id())
                monitor = user32.MonitorFromWindow(hwnd, 2)

                class RECT(ctypes.Structure):
                    _fields_ = [
                        ("left", ctypes.c_long),
                        ("top", ctypes.c_long),
                        ("right", ctypes.c_long),
                        ("bottom", ctypes.c_long),
                    ]

                class MONITORINFO(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", ctypes.c_ulong),
                        ("rcMonitor", RECT),
                        ("rcWork", RECT),
                        ("dwFlags", ctypes.c_ulong),
                    ]

                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    left = int(info.rcMonitor.left)
                    top = int(info.rcMonitor.top)
                    width = int(info.rcMonitor.right - info.rcMonitor.left)
                    height = int(info.rcMonitor.bottom - info.rcMonitor.top)
                    if width > 0 and height > 0:
                        return left, top, width, height
            except Exception:
                pass
        return 0, 0, max(1, int(self.root.winfo_screenwidth())), max(1, int(self.root.winfo_screenheight()))

    def save_geometry(self, immediate=False):
        if self.fullscreen or self.window is None or self._closing:
            return
        try:
            self.window.update_idletasks()
            width = max(self._min_width, int(self.window.winfo_width()))
            height = max(self._min_height, int(self.window.winfo_height()))
            x = int(self.window.winfo_x())
            y = int(self.window.winfo_y())
            if not hasattr(self.app, "settings") or self.app.settings is None:
                self.app.settings = {}
            self.app.settings["webcam_preview_x"] = x
            self.app.settings["webcam_preview_y"] = y
            self.app.settings["webcam_preview_width"] = width
            self.app.settings["webcam_preview_height"] = height
            if immediate:
                self.app.save_settings()
            else:
                self.app.schedule_save_settings()
        except Exception:
            pass

    def set_window_rect_topmost(self, x, y, width, height):
        if self.window is None:
            return
        try:
            self.window.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
            self.window.attributes("-topmost", True)
            self.window.lift()
        except Exception:
            pass
        if os.name == "nt":
            try:
                hwnd = int(self.window.winfo_id())
                HWND_TOPMOST = -1
                SWP_SHOWWINDOW = 0x0040
                SWP_NOOWNERZORDER = 0x0200
                SWP_FRAMECHANGED = 0x0020
                ctypes.windll.user32.SetWindowPos(
                    hwnd,
                    HWND_TOPMOST,
                    int(x),
                    int(y),
                    int(width),
                    int(height),
                    SWP_SHOWWINDOW | SWP_NOOWNERZORDER | SWP_FRAMECHANGED,
                )
            except Exception:
                pass

    def set_borderless_mode(self, enabled):
        if self.window is None:
            return
        try:
            self.window.withdraw()
            self.window.overrideredirect(bool(enabled))
            self.window.update_idletasks()
            self.window.deiconify()
            self.window.attributes("-topmost", True)
            self.window.lift()
            self.window.focus_force()
        except Exception:
            try:
                self.window.overrideredirect(bool(enabled))
            except Exception:
                pass

    def bind_overlay_button_hover(self, button):
        try:
            button.bind("<Enter>", lambda _event, b=button: b.configure(bg="#2a2a2a"))
            button.bind("<Leave>", lambda _event, b=button: b.configure(bg="#151515"))
        except Exception:
            pass

    def create_tooltip(self, widget, text):
        tooltip = {"window": None, "job": None}

        def hide_tooltip(_event=None):
            job = tooltip.get("job")
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
            tooltip["job"] = None
            win = tooltip.get("window")
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
            tooltip["window"] = None

        def show_tooltip():
            if tooltip.get("window") is not None or not self.is_open():
                return
            try:
                x = widget.winfo_rootx()
                y = widget.winfo_rooty() + widget.winfo_height() + 8
                win = tk.Toplevel(self.window)
                tooltip["window"] = win
                win.overrideredirect(True)
                win.attributes("-topmost", True)
                label = tk.Label(
                    win,
                    text=text,
                    bg="#202020",
                    fg="#ffffff",
                    relief="solid",
                    bd=1,
                    padx=7,
                    pady=4,
                    font=("Segoe UI", 9),
                )
                label.pack()
                win.geometry(f"+{x}+{y}")
            except Exception:
                hide_tooltip()

        def schedule_tooltip(_event=None):
            hide_tooltip()
            try:
                tooltip["job"] = self.root.after(450, show_tooltip)
            except Exception:
                pass

        try:
            widget.bind("<Enter>", schedule_tooltip, add="+")
            widget.bind("<Leave>", hide_tooltip, add="+")
            widget.bind("<ButtonPress-1>", hide_tooltip, add="+")
        except Exception:
            pass

    def lift(self):
        if not self.is_open():
            return
        try:
            self.window.deiconify()
            self.window.attributes("-topmost", True)
            self.window.lift()
            self.window.focus_force()
        except Exception:
            pass

    def set_status(self, text):
        self.status_message = str(text or "")
        if self.latest_frame is None:
            self.render_status_message()

    def set_status_threadsafe(self, text):
        if self._closing:
            return
        try:
            self.root.after(0, lambda: self.set_status(text))
        except Exception:
            pass

    def render_status_message(self):
        if self.canvas is None or not self.is_open():
            return
        try:
            w = max(1, int(self.canvas.winfo_width()))
            h = max(1, int(self.canvas.winfo_height()))
            self.canvas.delete("all")
            self.canvas.create_text(
                w // 2,
                h // 2,
                text=self.status_message,
                fill="#cfcfcf",
                font=("Segoe UI", 11, "bold"),
                width=max(120, w - 34),
                justify="center",
            )
            if self.button_frame is not None:
                self.button_frame.lift()
        except Exception:
            pass

    def get_resize_zone(self, event):
        if self.fullscreen or self.window is None:
            return None
        try:
            w = max(1, int(self.window.winfo_width()))
            h = max(1, int(self.window.winfo_height()))
            m = int(self._resize_margin)
            left = event.x <= m
            right = event.x >= w - m
            top = event.y <= m
            bottom = event.y >= h - m
            if left and top:
                return "nw"
            if right and top:
                return "ne"
            if left and bottom:
                return "sw"
            if right and bottom:
                return "se"
            if left:
                return "w"
            if right:
                return "e"
            if top:
                return "n"
            if bottom:
                return "s"
        except Exception:
            pass
        return None

    def update_resize_cursor(self, event):
        if self.canvas is None:
            return
        zone = self.get_resize_zone(event)
        cursors = {
            "nw": "size_nw_se",
            "se": "size_nw_se",
            "ne": "size_ne_sw",
            "sw": "size_ne_sw",
            "w": "size_we",
            "e": "size_we",
            "n": "size_ns",
            "s": "size_ns",
        }
        try:
            self.canvas.configure(cursor=cursors.get(zone, "fleur"))
        except Exception:
            pass

    def start_window_drag_or_resize(self, event):
        if self.window is None:
            return
        try:
            self._drag_mode = self.get_resize_zone(event) or "move"
            self._drag_start = (int(event.x_root), int(event.y_root))
            self._window_start = (
                int(self.window.winfo_x()),
                int(self.window.winfo_y()),
                int(self.window.winfo_width()),
                int(self.window.winfo_height()),
            )
        except Exception:
            self._drag_mode = None
            self._drag_start = None
            self._window_start = None

    def drag_or_resize_window(self, event):
        if self.fullscreen or not self._drag_mode or not self._drag_start or not self._window_start or self.window is None:
            return
        try:
            sx, sy = self._drag_start
            x, y, w, h = self._window_start
            dx = int(event.x_root) - sx
            dy = int(event.y_root) - sy
            mode = self._drag_mode
            if mode == "move":
                self.window.geometry(f"+{x + dx}+{y + dy}")
                return
            new_x, new_y, new_w, new_h = x, y, w, h
            if "e" in mode:
                new_w = max(self._min_width, w + dx)
            if "s" in mode:
                new_h = max(self._min_height, h + dy)
            if "w" in mode:
                new_w = max(self._min_width, w - dx)
                new_x = x + (w - new_w)
            if "n" in mode:
                new_h = max(self._min_height, h - dy)
                new_y = y + (h - new_h)
            self.window.geometry(f"{new_w}x{new_h}+{new_x}+{new_y}")
        except Exception:
            pass

    def end_window_drag_or_resize(self, _event=None):
        self.save_geometry(immediate=False)
        self._drag_mode = None
        self._drag_start = None
        self._window_start = None

    def capture_loop(self):
        if self.cv2 is not None:
            opened = self.capture_loop_opencv()
            if opened or self.stop_event.is_set():
                return
            self.set_status_threadsafe("OpenCV не открыл камеру, пробую FFmpeg...")
        self.capture_loop_ffmpeg()

    def queue_frame(self, frame):
        try:
            self.frame_queue.put_nowait(frame)
        except queue.Full:
            try:
                self.frame_queue.get_nowait()
            except Exception:
                pass
            try:
                self.frame_queue.put_nowait(frame)
            except Exception:
                pass

    def capture_loop_opencv(self):
        cap = None
        try:
            camera_index = 0
            try:
                camera_index = int(self.app.get_selected_webcam_index())
            except Exception:
                camera_index = 0
            backend = None
            if os.name == "nt" and hasattr(self.cv2, "CAP_DSHOW"):
                backend = self.cv2.CAP_DSHOW
            try:
                cap = self.cv2.VideoCapture(camera_index, backend) if backend is not None else self.cv2.VideoCapture(camera_index)
            except Exception:
                cap = self.cv2.VideoCapture(camera_index)
            if not cap or not cap.isOpened():
                if cap:
                    try:
                        cap.release()
                    except Exception:
                        pass
                cap = self.cv2.VideoCapture(camera_index)
            self.capture = cap
            if not cap or not cap.isOpened():
                return False

            self.set_status_threadsafe("Вебкамера открыта.")
            while not self.stop_event.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    time.sleep(0.05)
                    continue
                try:
                    frame = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
                except Exception:
                    pass
                self.queue_frame(frame)
                time.sleep(0.005)
            return True
        except Exception as exc:
            self.set_status_threadsafe(f"Ошибка вебкамеры OpenCV: {exc}")
            return False
        finally:
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
            self.capture = None

    def find_ffmpeg_webcam_device(self):
        if os.name != "nt":
            return None
        try:
            selected = self.app.get_selected_webcam_device_name()
            if selected:
                return selected
        except Exception:
            pass
        try:
            devices = self.app.get_cached_webcam_devices()
        except Exception:
            devices = []
        return devices[0] if devices else None

    def capture_loop_ffmpeg(self):
        if os.name != "nt":
            self.set_status_threadsafe("Без OpenCV запасной предпросмотр через FFmpeg доступен только на Windows.")
            return False

        device_name = self.find_ffmpeg_webcam_device()
        if not device_name:
            self.set_status_threadsafe("Не удалось найти вебкамеру. Проверь, что камера подключена и не занята другой программой.")
            return False

        width = int(self.ffmpeg_frame_width)
        height = int(self.ffmpeg_frame_height)
        frame_size = width * height * 3
        process = None
        try:
            command = [
                self.app.ffmpeg_path,
                "-hide_banner",
                "-loglevel", "error",
                "-f", "dshow",
                "-i", f"video={device_name}",
                "-an",
                "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
                "-pix_fmt", "rgb24",
                "-f", "rawvideo",
                "pipe:1",
            ]
            process = self.app.start_managed_process(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=self.app.creation_flags(),
            )
            self.ffmpeg_process = process
            self.set_status_threadsafe("Вебкамера открыта.")
            while not self.stop_event.is_set():
                raw = process.stdout.read(frame_size)
                if not raw or len(raw) < frame_size:
                    break
                image = Image.frombytes("RGB", (width, height), raw)
                self.queue_frame(image)
            return True
        except Exception as exc:
            self.set_status_threadsafe(f"Ошибка вебкамеры FFmpeg: {exc}")
            return False
        finally:
            try:
                if process is not None:
                    if process.poll() is None:
                        self.app.terminate_process_tree(process, timeout=1.0, name="webcam_preview_ffmpeg")
                    else:
                        self.app.unregister_child_process(process)
            except Exception:
                pass
            self.ffmpeg_process = None

    def schedule_frame_update(self):
        if self._closing or not self.is_open():
            return
        got_frame = False
        while True:
            try:
                self.latest_frame = self.frame_queue.get_nowait()
                got_frame = True
            except queue.Empty:
                break
            except Exception:
                break
        if got_frame:
            self.render_latest_frame()
        try:
            self.frame_job = self.root.after(33, self.schedule_frame_update)
        except Exception:
            self.frame_job = None

    def render_latest_frame(self):
        if self.canvas is None or not self.is_open():
            return
        if self.latest_frame is None:
            self.render_status_message()
            return
        try:
            canvas_w = max(1, int(self.canvas.winfo_width()))
            canvas_h = max(1, int(self.canvas.winfo_height()))
            if hasattr(self.latest_frame, "shape"):
                frame_h, frame_w = self.latest_frame.shape[:2]
                image = Image.fromarray(self.latest_frame)
            else:
                image = self.latest_frame.copy()
                frame_w, frame_h = image.size
            if frame_w <= 0 or frame_h <= 0:
                return
            scale = min(canvas_w / frame_w, canvas_h / frame_h)
            new_w = max(1, int(frame_w * scale))
            new_h = max(1, int(frame_h * scale))
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BILINEAR)
            image = image.resize((new_w, new_h), resampling)
            self.latest_photo = ImageTk.PhotoImage(image)
            self.canvas.delete("all")
            self.canvas.create_image(canvas_w // 2, canvas_h // 2, image=self.latest_photo, anchor="center")
            if self.button_frame is not None:
                self.button_frame.place(x=10, y=10, anchor="nw")
                self.button_frame.lift()
        except Exception as exc:
            self.set_status(f"Ошибка отображения кадра: {exc}")

    def toggle_fullscreen(self):
        if not self.is_open():
            return
        try:
            if not self.fullscreen:
                self.save_geometry(immediate=False)
                self.normal_geometry = self.window.geometry()
                self.fullscreen = True
                self.set_borderless_mode(False)
                self.window.attributes("-fullscreen", True)
                self.window.attributes("-topmost", True)
                self.window.lift()
                self.window.focus_force()
                if self.fullscreen_button is not None:
                    self.fullscreen_button.configure(text="⤡")
            else:
                self.fullscreen = False
                self.window.attributes("-fullscreen", False)
                self.set_borderless_mode(True)
                if self.normal_geometry:
                    self.window.geometry(self.normal_geometry)
                if self.fullscreen_button is not None:
                    self.fullscreen_button.configure(text="⤢")
                self.window.after(60, self.save_geometry)
            self.window.attributes("-topmost", True)
            self.window.lift()
            if self.button_frame is not None:
                self.button_frame.place(x=10, y=10, anchor="nw")
                self.button_frame.lift()
            self.render_latest_frame()
        except Exception as exc:
            self.set_status(f"Не удалось переключить полноэкранный режим: {exc}")

    def exit_fullscreen_or_close(self, _event=None):
        if self.fullscreen:
            self.toggle_fullscreen()
        else:
            self.close()
        return "break"

    def force_restore_from_fullscreen(self):
        if self.window is None:
            return
        try:
            self.fullscreen = False
            self.window.attributes("-fullscreen", False)
            self.set_borderless_mode(True)
            if self.normal_geometry:
                self.window.geometry(self.normal_geometry)
            if self.fullscreen_button is not None:
                self.fullscreen_button.configure(text="⤢")
        except Exception:
            pass

    def close(self):
        if self._closing:
            return
        if self.fullscreen:
            self.force_restore_from_fullscreen()
        self.save_geometry(immediate=True)
        self._closing = True
        try:
            if self.frame_job is not None:
                self.root.after_cancel(self.frame_job)
        except Exception:
            pass
        self.frame_job = None
        self.stop_event.set()
        try:
            if self.capture is not None:
                self.capture.release()
        except Exception:
            pass
        try:
            if self.ffmpeg_process is not None:
                self.app.terminate_process_tree(self.ffmpeg_process, timeout=0.8, name="webcam_preview_ffmpeg")
        except Exception:
            pass
        try:
            if self.capture_thread is not None and self.capture_thread.is_alive():
                self.capture_thread.join(timeout=0.7)
        except Exception:
            pass
        try:
            if self.window is not None:
                self.window.destroy()
        except Exception:
            pass
        self.window = None
        self.canvas = None
        self.fullscreen_button = None
        self.close_button = None
        self.button_frame = None
        self.latest_photo = None
        try:
            self.app.on_webcam_preview_closed(self)
        except Exception:
            pass
