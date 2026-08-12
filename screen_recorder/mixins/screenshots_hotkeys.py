from ..shared import *


class ScreenshotsHotkeysMixin:
    def take_screenshot(self):
        """Скрывает служебный интерфейс и запускает выбор области снимка."""
        if getattr(self, "_exiting", False):
            self.diagnostic_log("screenshot_request_ignored", {"reason": "application_exiting"}, level="WARN")
            return
        if self._screenshot_in_progress:
            self.screenshot_status_var.set("Выбор области или копирование скриншота уже выполняется...")
            self.diagnostic_log("screenshot_request_ignored", {
                "reason": "already_in_progress",
                "snapshot_worker_alive": bool(
                    self.screenshot_prepare_thread and self.screenshot_prepare_thread.is_alive()
                ),
                "worker_alive": bool(self.screenshot_thread and self.screenshot_thread.is_alive()),
            }, level="WARN")
            return
        if not PIL_AVAILABLE or ImageGrab is None or Image is None or ImageTk is None:
            error_text = "Скриншоты недоступны: установи Pillow (pip install pillow)."
            self.screenshot_status_var.set(error_text)
            self.status_var.set(error_text)
            self.diagnostic_log("screenshot_unavailable", {
                "pil_available": bool(PIL_AVAILABLE),
                "image_grab_available": ImageGrab is not None,
                "image_available": Image is not None,
                "image_tk_available": ImageTk is not None,
            }, level="ERROR")
            if self.annotation_overlay is not None:
                self.annotation_overlay.show_screenshot_feedback(success=False)
            return

        self._screenshot_in_progress = True
        self._clear_screenshot_frozen_image()
        self.screenshot_status_var.set("Подготавливаю снимок экрана...")
        self.diagnostic_log("screenshot_requested", {
            "hotkey": self.screenshot_hotkey_var.get().strip(),
        })
        self._hide_ui_for_screenshot()
        # Сначала сохраняем экран, пока активное приложение ещё не потеряло фокус.
        # Только после этого открываем окно выбора области. Иначе временные панели
        # браузера, меню и стикеры закрываются раньше фактического ImageGrab.
        self.root.after(100, self._start_screenshot_snapshot_worker)

    def _clear_screenshot_frozen_image(self):
        image = getattr(self, "_screenshot_frozen_image", None)
        self._screenshot_frozen_image = None
        self._screenshot_frozen_screen_rect = None
        self._screenshot_snapshot_captured_perf = None
        if image is not None:
            try:
                image.close()
            except Exception:
                pass

    def _start_screenshot_snapshot_worker(self):
        if getattr(self, "_exiting", False):
            self._screenshot_in_progress = False
            self._clear_screenshot_frozen_image()
            self._restore_ui_after_screenshot()
            return
        screen_rect = tuple(int(value) for value in self.get_virtual_screen_rect())
        self.diagnostic_log("screenshot_snapshot_started", {
            "virtual_screen": list(screen_rect),
            "capture_stage": "before_selector_focus",
        })
        try:
            self.screenshot_prepare_thread = threading.Thread(
                target=self._capture_screenshot_snapshot_worker,
                args=(screen_rect,),
                daemon=True,
                name="ScreenshotSnapshotWorker",
            )
            self.screenshot_prepare_thread.start()
        except Exception as exc:
            self.log_exception("start_screenshot_snapshot_worker", exc)
            self._finish_screenshot_capture(False, None, str(exc))

    def _capture_screenshot_snapshot_worker(self, screen_rect):
        started = time.perf_counter()
        image = None
        try:
            if os.name == "nt":
                image = ImageGrab.grab(all_screens=True)
            else:
                image = ImageGrab.grab()
            if image.size[0] < 1 or image.size[1] < 1:
                raise RuntimeError("Не удалось получить предварительный снимок экрана.")
            captured_perf = time.perf_counter()
            expected_width = int(screen_rect[2])
            expected_height = int(screen_rect[3])
            size_matches = tuple(image.size) == (expected_width, expected_height)
            self.diagnostic_log("screenshot_snapshot_ready", {
                "virtual_screen": list(screen_rect),
                "snapshot_size": [int(image.size[0]), int(image.size[1])],
                "size_matches_virtual_screen": size_matches,
                "coordinate_scale": [
                    round(image.size[0] / float(expected_width), 6) if expected_width else None,
                    round(image.size[1] / float(expected_height), 6) if expected_height else None,
                ],
                "elapsed_sec": round(captured_perf - started, 3),
                "capture_stage": "before_selector_focus",
            }, level="INFO" if size_matches else "WARN")
            self.hotkey_action_queue.put((
                "screenshot_snapshot_ready",
                True,
                image,
                list(screen_rect),
                captured_perf,
                None,
            ))
            image = None  # Владение передано GUI-потоку через очередь.
        except Exception as exc:
            self.log_exception("capture_screenshot_snapshot", exc)
            self.diagnostic_log("screenshot_snapshot_failed", {
                "virtual_screen": list(screen_rect),
                "elapsed_sec": round(time.perf_counter() - started, 3),
                "error": repr(exc),
                "capture_stage": "before_selector_focus",
            }, level="ERROR")
            self.hotkey_action_queue.put((
                "screenshot_snapshot_ready",
                False,
                None,
                list(screen_rect),
                None,
                str(exc),
            ))
        finally:
            if image is not None:
                try:
                    image.close()
                except Exception:
                    pass

    def _hide_ui_for_screenshot(self):
        """Прячет настройки и плавающие окна, чтобы они не попали в снимок."""
        self._screenshot_restore_settings_window = False
        settings_window = getattr(self, "settings_window", None)
        try:
            if settings_window is not None and settings_window.winfo_exists() and settings_window.state() != "withdrawn":
                self._screenshot_restore_settings_window = True
                settings_window.withdraw()
        except Exception:
            self._screenshot_restore_settings_window = False

        overlay = getattr(self, "annotation_overlay", None)
        if overlay is None:
            return
        for window in (overlay.input_blocker, overlay.overlay, overlay.toolbar, overlay.bubble):
            try:
                if window is not None:
                    window.withdraw()
            except Exception:
                pass
        overlay.toolbar_visible = False
        try:
            overlay.update_control_rects_now()
        except Exception:
            pass

    def _restore_ui_after_screenshot(self, success=None):
        overlay = getattr(self, "annotation_overlay", None)
        if overlay is not None:
            try:
                if overlay.pen_active:
                    overlay.show_layer()
                else:
                    overlay.show_bubble_only()
                if success is not None:
                    overlay.show_screenshot_feedback(success=bool(success))
            except Exception:
                pass

        if self._screenshot_restore_settings_window:
            settings_window = getattr(self, "settings_window", None)
            try:
                if settings_window is not None and settings_window.winfo_exists():
                    settings_window.deiconify()
                    settings_window.lift()
                    settings_window.focus_force()
            except Exception:
                pass
        self._screenshot_restore_settings_window = False

    def _handle_screenshot_snapshot_ready(
        self,
        success,
        image,
        screen_rect,
        captured_perf,
        error_text,
    ):
        self.screenshot_prepare_thread = None
        if getattr(self, "_exiting", False):
            if image is not None:
                try:
                    image.close()
                except Exception:
                    pass
            self._screenshot_in_progress = False
            return
        if not success or image is None:
            self._finish_screenshot_capture(
                False,
                None,
                error_text or "не удалось сохранить экран до открытия рамки",
            )
            return
        self._clear_screenshot_frozen_image()
        self._screenshot_frozen_image = image
        self._screenshot_frozen_screen_rect = [int(value) for value in screen_rect]
        self._screenshot_snapshot_captured_perf = float(captured_perf or time.perf_counter())
        self.screenshot_status_var.set("Выдели нужную область мышью. Esc — отмена.")
        self._open_screenshot_region_selector()

    def _open_screenshot_region_selector(self):
        if getattr(self, "_exiting", False):
            self._screenshot_in_progress = False
            self._clear_screenshot_frozen_image()
            self._restore_ui_after_screenshot()
            return
        frozen_image = getattr(self, "_screenshot_frozen_image", None)
        frozen_screen_rect = getattr(self, "_screenshot_frozen_screen_rect", None)
        if frozen_image is None or not frozen_screen_rect:
            self._finish_screenshot_capture(False, None, "предварительный снимок экрана потерян")
            return
        self.select_capture_region(
            allow_while_recording=True,
            hint_text="Выдели область или сначала добавь пометки.  Esc — отмена.",
            on_result=self._after_screenshot_region_selected,
            purpose="screenshot",
            background_image=frozen_image,
            screen_rect=frozen_screen_rect,
            enable_annotations=True,
        )

    def _after_screenshot_region_selected(self, region, selection_info=None):
        selection_info = dict(selection_info or {})
        if not region:
            self._screenshot_in_progress = False
            self._clear_screenshot_frozen_image()
            reason = str(selection_info.get("status") or "cancelled")
            if reason == "too_small":
                message = (
                    "Скриншот не создан: выделенная область слишком мала "
                    f"({int(selection_info.get('width', 0))}×{int(selection_info.get('height', 0))}). "
                    f"Минимум — {int(selection_info.get('minimum_size', 16))}×"
                    f"{int(selection_info.get('minimum_size', 16))}."
                )
            elif reason == "selector_error":
                self._finish_screenshot_capture(
                    False,
                    None,
                    str(selection_info.get("error") or "ошибка окна выбора области"),
                )
                return
            else:
                message = "Создание скриншота отменено."
            self.screenshot_status_var.set(message)
            self.status_var.set(message)
            self.diagnostic_log("screenshot_cancelled", {
                "reason": reason,
                "start": selection_info.get("start"),
                "end": selection_info.get("end"),
                "width": selection_info.get("width"),
                "height": selection_info.get("height"),
                "minimum_size": selection_info.get("minimum_size"),
                "selector_elapsed_sec": selection_info.get("elapsed_sec"),
            }, level="WARN" if reason == "release_without_press" else "INFO")
            self._restore_ui_after_screenshot()
            return
        try:
            x, y, width, height = [int(value) for value in region]
            if width < 1 or height < 1:
                raise ValueError("Выбрана пустая область.")
            region = [x, y, width, height]
        except Exception as exc:
            self._finish_screenshot_capture(False, None, str(exc))
            return

        annotations = list(selection_info.get("annotations") or [])
        self.screenshot_status_var.set("Копирую выбранную область в буфер обмена...")
        # Повторно экран не читаем: копируем область из кадра, сохранённого до
        # того, как окно выбора забрало фокус у браузера или панели стикеров.
        self._start_screenshot_worker(region, annotations=annotations)

    def _start_screenshot_worker(self, region, annotations=None):
        if getattr(self, "_exiting", False):
            self._screenshot_in_progress = False
            self._clear_screenshot_frozen_image()
            self._restore_ui_after_screenshot()
            return
        snapshot = getattr(self, "_screenshot_frozen_image", None)
        screen_rect = getattr(self, "_screenshot_frozen_screen_rect", None)
        captured_perf = getattr(self, "_screenshot_snapshot_captured_perf", None)
        self._screenshot_frozen_image = None
        self._screenshot_frozen_screen_rect = None
        self._screenshot_snapshot_captured_perf = None
        if snapshot is None or not screen_rect:
            self._finish_screenshot_capture(False, None, "предварительный снимок экрана потерян")
            return
        try:
            self.screenshot_thread = threading.Thread(
                target=self._take_screenshot_worker,
                args=(region, snapshot, list(screen_rect), captured_perf, list(annotations or [])),
                daemon=True,
                name="ScreenshotWorker",
            )
            self.screenshot_thread.start()
        except Exception as exc:
            try:
                snapshot.close()
            except Exception:
                pass
            self.log_exception("start_screenshot_worker", exc)
            self._finish_screenshot_capture(False, None, str(exc))

    @staticmethod
    def get_screenshot_snapshot_crop_box(region, screen_rect, image_size):
        """Переводит область виртуального экрана в пиксели сохранённого кадра."""
        x, y, width, height = [int(value) for value in region]
        vx, vy, virtual_width, virtual_height = [int(value) for value in screen_rect]
        image_width, image_height = [int(value) for value in image_size]
        if width < 1 or height < 1 or virtual_width < 1 or virtual_height < 1:
            raise ValueError("Некорректный размер области или виртуального экрана.")
        if image_width < 1 or image_height < 1:
            raise ValueError("Предварительный снимок экрана пуст.")
        scale_x = image_width / float(virtual_width)
        scale_y = image_height / float(virtual_height)
        left = max(0, min(image_width, int(round((x - vx) * scale_x))))
        top = max(0, min(image_height, int(round((y - vy) * scale_y))))
        right = max(0, min(image_width, int(round((x + width - vx) * scale_x))))
        bottom = max(0, min(image_height, int(round((y + height - vy) * scale_y))))
        if right <= left or bottom <= top:
            raise ValueError("Выбранная область находится вне сохранённого экрана.")
        return left, top, right, bottom

    @staticmethod
    def apply_screenshot_annotations(image, annotations, screen_rect):
        """Наносит карандаш и стрелки на сохранённый кадр до обрезки области."""
        if image is None or ImageDraw is None or not annotations:
            return 0
        vx, vy, virtual_width, virtual_height = [int(value) for value in screen_rect]
        image_width, image_height = [int(value) for value in image.size]
        if virtual_width < 1 or virtual_height < 1 or image_width < 1 or image_height < 1:
            raise ValueError("Некорректная геометрия кадра для аннотаций.")
        scale_x = image_width / float(virtual_width)
        scale_y = image_height / float(virtual_height)
        width_scale = max(0.25, (scale_x + scale_y) / 2.0)
        painter = ImageDraw.Draw(image)

        def map_point(point):
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                return None
            try:
                x, y = point[:2]
                return (
                    int(round((float(x) - vx) * scale_x)),
                    int(round((float(y) - vy) * scale_y)),
                )
            except (TypeError, ValueError, OverflowError):
                return None

        applied = 0
        for command in list(annotations)[:1000]:
            if not isinstance(command, dict):
                continue
            tool = str(command.get("tool") or "")
            color = str(command.get("color") or "#ff3b30")
            try:
                width = max(1, min(96, int(round(float(command.get("width", 5)) * width_scale))))
            except (TypeError, ValueError, OverflowError):
                width = max(1, int(round(5 * width_scale)))
            if tool == "draw":
                raw_points = command.get("points") or []
                if not isinstance(raw_points, (list, tuple)):
                    continue
                points = []
                for raw_point in raw_points[:20000]:
                    point = map_point(raw_point)
                    if point is not None:
                        points.append(point)
                if not points:
                    continue
                if len(points) == 1:
                    x, y = points[0]
                    radius = max(1, width // 2)
                    painter.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
                else:
                    painter.line(points, fill=color, width=width, joint="curve")
                applied += 1
            elif tool == "arrow":
                start = map_point(command.get("start"))
                end = map_point(command.get("end"))
                if start is None or end is None:
                    continue
                dx = float(end[0] - start[0])
                dy = float(end[1] - start[1])
                length = (dx * dx + dy * dy) ** 0.5
                if length < 2.0:
                    continue
                painter.line((start, end), fill=color, width=width)
                ux, uy = dx / length, dy / length
                head_length = min(length * 0.45, max(12.0 * width_scale, width * 3.5))
                wing = head_length * 0.48
                base_x = end[0] - ux * head_length
                base_y = end[1] - uy * head_length
                perp_x, perp_y = -uy, ux
                painter.polygon(
                    (
                        end,
                        (int(round(base_x + perp_x * wing)), int(round(base_y + perp_y * wing))),
                        (int(round(base_x - perp_x * wing)), int(round(base_y - perp_y * wing))),
                    ),
                    fill=color,
                )
                applied += 1
        return applied

    def _take_screenshot_worker(self, region, snapshot, screen_rect, captured_perf=None, annotations=None):
        started = time.perf_counter()
        image = None
        try:
            annotation_count = self.apply_screenshot_annotations(snapshot, annotations or [], screen_rect)
            crop_box = self.get_screenshot_snapshot_crop_box(region, screen_rect, snapshot.size)
            image = snapshot.crop(crop_box)
            if image.size[0] < 1 or image.size[1] < 1:
                raise RuntimeError("Не удалось получить изображение выбранной области.")
            self._copy_image_to_windows_clipboard(image)

            self.diagnostic_log("screenshot_copied_to_clipboard", {
                "region": region,
                "snapshot_crop_box": list(crop_box),
                "captured_width": image.size[0],
                "captured_height": image.size[1],
                "elapsed_sec": round(time.perf_counter() - started, 3),
                "source": "frozen_snapshot_before_selector_focus",
                "annotation_backend": "screenshot_canvas_v3",
                "annotation_count": annotation_count,
                "annotation_tools": sorted({
                    str(item.get("tool")) for item in (annotations or []) if isinstance(item, dict)
                }),
                "annotation_colors": sorted({
                    str(item.get("color"))
                    for item in (annotations or [])
                    if isinstance(item, dict) and item.get("color")
                }),
                "annotation_sizes": sorted({
                    int(item.get("width"))
                    for item in (annotations or [])
                    if isinstance(item, dict) and item.get("width")
                }),
                "snapshot_size": [int(snapshot.size[0]), int(snapshot.size[1])],
                "snapshot_age_sec": (
                    round(max(0.0, time.perf_counter() - float(captured_perf)), 3)
                    if captured_perf is not None else None
                ),
            })
            self.hotkey_action_queue.put(("screenshot_finished", True, region, None))
        except Exception as exc:
            self.log_exception("take_screenshot", exc)
            self.hotkey_action_queue.put(("screenshot_finished", False, region, str(exc)))
        finally:
            if image is not None:
                try:
                    image.close()
                except Exception:
                    pass
            try:
                snapshot.close()
            except Exception:
                pass

    @staticmethod
    def _copy_image_to_windows_clipboard(image):
        """Кладёт PIL-изображение в буфер Windows как CF_DIB без временного файла."""
        if os.name != "nt":
            raise RuntimeError("Копирование изображения в буфер реализовано только для Windows.")

        converted = image.convert("RGB")
        try:
            with io.BytesIO() as bmp_stream:
                converted.save(bmp_stream, format="BMP")
                bmp_data = bmp_stream.getvalue()
        finally:
            converted.close()
        if len(bmp_data) <= 14:
            raise RuntimeError("Не удалось подготовить изображение для буфера обмена.")
        dib_data = bmp_data[14:]

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        CF_DIB = 8
        GMEM_MOVEABLE = 0x0002
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL

        memory_handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib_data))
        if not memory_handle:
            raise MemoryError("Windows не выделил память для скриншота.")
        ownership_transferred = False
        try:
            memory_pointer = kernel32.GlobalLock(memory_handle)
            if not memory_pointer:
                raise OSError("Windows не заблокировал память буфера обмена.")
            try:
                ctypes.memmove(memory_pointer, dib_data, len(dib_data))
            finally:
                kernel32.GlobalUnlock(memory_handle)

            clipboard_open = False
            for _attempt in range(12):
                if user32.OpenClipboard(None):
                    clipboard_open = True
                    break
                time.sleep(0.05)
            if not clipboard_open:
                raise OSError("Буфер обмена Windows занят другой программой.")
            try:
                if not user32.EmptyClipboard():
                    raise OSError("Windows не очистил буфер обмена.")
                if not user32.SetClipboardData(CF_DIB, memory_handle):
                    raise OSError("Windows не принял изображение в буфер обмена.")
                ownership_transferred = True
            finally:
                user32.CloseClipboard()
        finally:
            if not ownership_transferred and memory_handle:
                kernel32.GlobalFree(memory_handle)

    def _finish_screenshot_capture(self, success, region=None, error_text=None):
        self._screenshot_in_progress = False
        self.screenshot_prepare_thread = None
        self.screenshot_thread = None
        self._clear_screenshot_frozen_image()
        if success:
            width = int(region[2]) if region and len(region) == 4 else 0
            height = int(region[3]) if region and len(region) == 4 else 0
            size_text = f" ({width}×{height})" if width and height else ""
            message = f"Скриншот{size_text} скопирован в буфер обмена. Вставка: Ctrl+V."
            self.screenshot_status_var.set(message)
            self.status_var.set(message)
        else:
            message = f"Не удалось скопировать скриншот: {error_text or 'неизвестная ошибка'}"
            self.screenshot_status_var.set(message)
            self.status_var.set(message)
            try:
                if self.tray_icon is not None and hasattr(self.tray_icon, "notify"):
                    self.tray_icon.notify(message, "Screen Recorder Pro")
            except Exception:
                pass
        self._restore_ui_after_screenshot(success=success)

    def enqueue_hotkey_action(self, action):
        """Поток keyboard только кладёт действие в очередь и не обращается к Tk."""
        if getattr(self, "_exiting", False):
            return
        try:
            if action in {"record", "screenshot"}:
                try:
                    counts = getattr(self, "hotkey_callback_counts", None)
                    if not isinstance(counts, dict):
                        counts = {"record": 0, "screenshot": 0}
                        self.hotkey_callback_counts = counts
                    counts[action] = int(counts.get(action, 0)) + 1
                    self.hotkey_last_callback_perf = time.perf_counter()
                except Exception:
                    pass
                self.diagnostic_log("hotkey_callback_received", {
                    "action": action,
                    "registration_generation": getattr(self, "hotkey_registration_generation", None),
                    "backend": (
                        getattr(self, "screenshot_hotkey_backend", None)
                        if action == "screenshot" else "keyboard"
                    ),
                    "native_thread_id": (
                        getattr(self, "native_screenshot_hotkey_thread_id", None)
                        if action == "screenshot" else None
                    ),
                })
            self.hotkey_action_queue.put_nowait(action)
        except Exception:
            pass

    def process_hotkey_actions(self):
        """Выполняет глобальные горячие клавиши безопасно в GUI-потоке."""
        self.hotkey_poll_job = None
        for _index in range(20):
            try:
                action = self.hotkey_action_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if action == "record":
                    self.handle_record_hotkey()
                elif action == "screenshot":
                    self.handle_screenshot_hotkey()
                elif action == "escape":
                    overlay = getattr(self, "annotation_overlay", None)
                    if overlay is not None:
                        overlay.put_down_pencil()
                elif isinstance(action, tuple) and action and action[0] == "screenshot_finished":
                    self._finish_screenshot_capture(*action[1:])
                elif isinstance(action, tuple) and action and action[0] == "screenshot_snapshot_ready":
                    self._handle_screenshot_snapshot_ready(*action[1:])
                elif isinstance(action, tuple) and action and action[0] == "screenshot_hotkey_captured":
                    self._finish_screenshot_hotkey_capture(*action[1:])
            except Exception as exc:
                self.log_exception("process_hotkey_action", exc)
        if self.running and not getattr(self, "_exiting", False):
            self.hotkey_poll_job = self.root.after(75, self.process_hotkey_actions)

    def handle_record_hotkey(self):
        """Одна горячая клавиша: если записи нет — начать, если запись идёт — остановить и сохранить."""
        now = time.monotonic()
        if now - self.last_record_toggle_hotkey_time < 0.8:
            return
        self.last_record_toggle_hotkey_time = now

        if getattr(self, "is_starting", False) or self.is_finalizing:
            return
        if self.is_recording:
            self.status_var.set("Горячая клавиша нажата: останавливаю и сохраняю запись...")
            self.stop_recording()
        else:
            self.status_var.set("Горячая клавиша нажата: начинаю запись...")
            self.start_recording()

    def handle_screenshot_hotkey(self):
        now = time.monotonic()
        if now - self.last_screenshot_hotkey_time < 0.8:
            return
        self.last_screenshot_hotkey_time = now
        self.take_screenshot()

    @staticmethod
    def _normalize_screenshot_capture_key_name(name):
        """Приводит названия keyboard к форме, пригодной для add_hotkey()."""
        normalized = re.sub(r"\s+", " ", str(name or "").strip().lower())
        aliases = {
            "left ctrl": "ctrl",
            "right ctrl": "ctrl",
            "control": "ctrl",
            "left shift": "shift",
            "right shift": "shift",
            "left alt": "alt",
            "right alt": "alt",
            "left windows": "windows",
            "right windows": "windows",
            "win": "windows",
            "printscreen": "print screen",
            "prtsc": "print screen",
            "prtscn": "print screen",
            "snapshot": "print screen",
            "escape": "esc",
            "return": "enter",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def hotkey_uses_print_screen(hotkey):
        """Определяет Print Screen в одиночной клавише или сочетании."""
        for part in str(hotkey or "").lower().split("+"):
            compact = re.sub(r"[\s_-]+", "", part)
            if compact in {"printscreen", "prtsc", "prtscn", "snapshot"}:
                return True
        return False

    @staticmethod
    def parse_native_print_screen_hotkey(hotkey):
        """Готовит Print Screen и его модификаторы для WinAPI RegisterHotKey."""
        parts = [
            ScreenshotsHotkeysMixin._normalize_screenshot_capture_key_name(part)
            for part in str(hotkey or "").split("+")
            if str(part).strip()
        ]
        if not parts:
            return None
        modifier_flags = {
            "alt": 0x0001,
            "ctrl": 0x0002,
            "shift": 0x0004,
            "windows": 0x0008,
        }
        modifiers = 0
        main_keys = []
        normalized_parts = []
        for part in parts:
            if part in modifier_flags:
                modifiers |= modifier_flags[part]
                normalized_parts.append(part)
            else:
                main_keys.append(part)
        if len(main_keys) != 1 or main_keys[0] != "print screen":
            return None
        normalized_parts.append("print screen")
        return {
            "modifiers": modifiers,
            "virtual_key": 0x2C,  # VK_SNAPSHOT
            "normalized_hotkey": "+".join(normalized_parts),
        }

    def _is_native_screenshot_hotkey_healthy(self):
        thread = getattr(self, "native_screenshot_hotkey_thread", None)
        return bool(
            getattr(self, "native_screenshot_hotkey_registered", False)
            and thread is not None
            and thread.is_alive()
            and getattr(self, "native_screenshot_hotkey_thread_id", None)
        )

    def _is_screenshot_hotkey_registered(self):
        backend = str(getattr(self, "screenshot_hotkey_backend", "") or "")
        if backend == "windows_register_hotkey":
            return self._is_native_screenshot_hotkey_healthy()
        return getattr(self, "screenshot_hotkey_handle", None) is not None

    def _native_screenshot_hotkey_worker(
        self,
        config,
        ready_event,
        stop_event,
        result,
        source,
        generation,
    ):
        """Отдельная очередь Windows-сообщений для надёжного глобального Print Screen."""
        registered = False
        hotkey_id = 0x5343
        WM_HOTKEY = 0x0312
        WM_QUIT = 0x0012
        PM_NOREMOVE = 0x0000
        MOD_NOREPEAT = 0x4000
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            user32.RegisterHotKey.restype = wintypes.BOOL
            user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.UnregisterHotKey.restype = wintypes.BOOL
            user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            user32.GetMessageW.restype = wintypes.BOOL
            user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
            user32.PeekMessageW.restype = wintypes.BOOL
            kernel32.GetCurrentThreadId.argtypes = []
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD

            message = wintypes.MSG()
            # Создаём очередь до публикации thread_id, чтобы PostThreadMessageW
            # при остановке не попал в ещё не готовый поток.
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_NOREMOVE)
            thread_id = int(kernel32.GetCurrentThreadId())
            self.native_screenshot_hotkey_thread_id = thread_id

            ctypes.set_last_error(0)
            registered = bool(user32.RegisterHotKey(
                None,
                hotkey_id,
                int(config["modifiers"]) | MOD_NOREPEAT,
                int(config["virtual_key"]),
            ))
            windows_error = int(ctypes.get_last_error()) if not registered else 0
            self.native_screenshot_hotkey_registered = registered
            result.update({
                "registered": registered,
                "thread_id": thread_id,
                "windows_error": windows_error,
            })
            ready_event.set()
            self.diagnostic_log(
                "native_screenshot_hotkey_ready" if registered else "native_screenshot_hotkey_failed",
                {
                    "source": source,
                    "generation": generation,
                    "hotkey": config["normalized_hotkey"],
                    "thread_id": thread_id,
                    "registered": registered,
                    "windows_error": windows_error,
                    "windows_error_text": (
                        ctypes.FormatError(windows_error).strip() if windows_error else ""
                    ),
                    "backend": "windows_register_hotkey",
                },
                level="INFO" if registered else "ERROR",
            )
            if not registered:
                return

            while not stop_event.is_set():
                message_result = int(user32.GetMessageW(ctypes.byref(message), None, 0, 0))
                if message_result == 0 or stop_event.is_set() or int(message.message) == WM_QUIT:
                    break
                if message_result == -1:
                    error_code = int(ctypes.get_last_error())
                    raise OSError(error_code, ctypes.FormatError(error_code))
                if int(message.message) == WM_HOTKEY and int(message.wParam) == hotkey_id:
                    self.enqueue_hotkey_action("screenshot")
        except Exception as exc:
            result.setdefault("registered", False)
            result["error"] = repr(exc)
            self.native_screenshot_hotkey_registered = False
            if not ready_event.is_set():
                ready_event.set()
            self.log_exception("native_screenshot_hotkey_worker", exc)
        finally:
            if registered:
                try:
                    user32.UnregisterHotKey(None, hotkey_id)
                except Exception:
                    pass
            self.native_screenshot_hotkey_registered = False
            self.diagnostic_log("native_screenshot_hotkey_stopped", {
                "source": source,
                "generation": generation,
                "hotkey": config.get("normalized_hotkey"),
                "stop_requested": stop_event.is_set(),
                "backend": "windows_register_hotkey",
            })

    def _stop_native_screenshot_hotkey(self, reason="reconfigure", join_timeout=1.5):
        thread = getattr(self, "native_screenshot_hotkey_thread", None)
        thread_id = getattr(self, "native_screenshot_hotkey_thread_id", None)
        stop_event = getattr(self, "native_screenshot_hotkey_stop_event", None)
        existed = thread is not None
        if stop_event is not None:
            stop_event.set()
        post_result = None
        windows_error = 0
        if os.name == "nt" and thread_id:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                user32.PostThreadMessageW.restype = wintypes.BOOL
                ctypes.set_last_error(0)
                post_result = bool(user32.PostThreadMessageW(int(thread_id), 0x0012, 0, 0))
                if not post_result:
                    windows_error = int(ctypes.get_last_error())
            except Exception as exc:
                self.log_exception("stop_native_screenshot_hotkey.post_quit", exc)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(join_timeout)))
        thread_alive = bool(thread is not None and thread.is_alive())
        if existed:
            self.diagnostic_log("native_screenshot_hotkey_stop_result", {
                "reason": reason,
                "thread_id": thread_id,
                "post_quit_result": post_result,
                "windows_error": windows_error,
                "thread_alive_after_join": thread_alive,
            }, level="WARN" if thread_alive else "INFO")
        if not thread_alive:
            self.native_screenshot_hotkey_thread = None
            self.native_screenshot_hotkey_thread_id = None
            self.native_screenshot_hotkey_ready_event = None
            self.native_screenshot_hotkey_stop_event = None
            self.native_screenshot_hotkey_registered = False
        return not thread_alive

    def _start_native_screenshot_hotkey(self, hotkey, source, generation):
        config = self.parse_native_print_screen_hotkey(hotkey)
        if os.name != "nt" or not config:
            return False, {"reason": "unsupported_hotkey_or_platform"}
        if not self._stop_native_screenshot_hotkey(reason="before_register"):
            return False, {"reason": "previous_native_thread_still_alive"}
        ready_event = threading.Event()
        stop_event = threading.Event()
        result = {}
        thread = threading.Thread(
            target=self._native_screenshot_hotkey_worker,
            args=(config, ready_event, stop_event, result, source, generation),
            daemon=True,
            name="NativeScreenshotHotkey",
        )
        self.native_screenshot_hotkey_ready_event = ready_event
        self.native_screenshot_hotkey_stop_event = stop_event
        self.native_screenshot_hotkey_thread = thread
        try:
            thread.start()
        except Exception as exc:
            self.native_screenshot_hotkey_thread = None
            self.native_screenshot_hotkey_ready_event = None
            self.native_screenshot_hotkey_stop_event = None
            self.native_screenshot_hotkey_last_result = {
                "registered": False,
                "reason": "thread_start_failed",
                "error": repr(exc),
            }
            self.log_exception("start_native_screenshot_hotkey_thread", exc)
            return False, dict(self.native_screenshot_hotkey_last_result)
        ready = ready_event.wait(timeout=1.5)
        if not ready:
            result.update({"registered": False, "reason": "registration_timeout"})
            self._stop_native_screenshot_hotkey(reason="registration_timeout")
        elif not result.get("registered"):
            self._stop_native_screenshot_hotkey(reason="registration_failed")
        self.native_screenshot_hotkey_last_result = dict(result)
        return bool(ready and result.get("registered") and self._is_native_screenshot_hotkey_healthy()), result

    def _remove_registered_hotkeys(self):
        """Снимает только постоянные горячие клавиши этой программы."""
        for attr_name in ("hotkey_handle", "screenshot_hotkey_handle"):
            handle = getattr(self, attr_name, None)
            if handle is None:
                continue
            if HOTKEY_AVAILABLE:
                try:
                    keyboard.remove_hotkey(handle)
                except Exception:
                    pass
            setattr(self, attr_name, None)
        self._stop_native_screenshot_hotkey(reason="remove_registered_hotkeys")
        self.screenshot_hotkey_backend = None
        self.native_screenshot_hotkey_last_result = None

    def on_screenshot_hotkey_field_clicked(self, _event=None):
        """Начинает выбор, когда пользователь нажал прямо на поле клавиши."""
        if not getattr(self, "screenshot_hotkey_capture_active", False):
            self.root.after_idle(self.begin_screenshot_hotkey_capture)
        return "break"

    def begin_screenshot_hotkey_capture(self):
        """Переводит поле в режим выбора следующей реально нажатой клавиши."""
        if not HOTKEY_AVAILABLE:
            message = "Выбор нажатием недоступен: установи pip install keyboard"
            self.screenshot_status_var.set(message)
            self.status_var.set(message)
            return
        if getattr(self, "screenshot_hotkey_capture_active", False):
            return

        self._remove_registered_hotkeys()
        with self.screenshot_hotkey_capture_lock:
            self.screenshot_hotkey_capture_active = True
            self.screenshot_hotkey_capture_pressed = []
            self.screenshot_hotkey_capture_result_sent = False

        self.screenshot_hotkey_display_var.set("Нажмите клавишу…  Esc — отмена")

        try:
            # suppress=True особенно важен для Print Screen: системные «Ножницы»
            # не успевают открыться, пока программа запоминает выбранную клавишу.
            self.screenshot_hotkey_capture_hook = keyboard.hook(
                self._on_screenshot_hotkey_capture_event,
                suppress=True,
            )
            self.screenshot_status_var.set("Ожидаю нажатие клавиши для скриншота...")
        except Exception as exc:
            self._close_screenshot_hotkey_capture(restore_hotkeys=False)
            self.register_hotkey()
            self.screenshot_hotkey_display_var.set(self.screenshot_hotkey_var.get())
            message = f"Не удалось включить выбор клавиши: {exc}"
            self.screenshot_status_var.set(message)
            self.status_var.set(message)
            self.diagnostic_log("screenshot_hotkey_capture_failed", {
                "error": repr(exc),
            }, level="ERROR")

    def _on_screenshot_hotkey_capture_event(self, event):
        """Callback keyboard-потока: собирает сочетание и отправляет его GUI."""
        name = self._normalize_screenshot_capture_key_name(getattr(event, "name", ""))
        event_type = str(getattr(event, "event_type", "")).lower()
        if not name or event_type not in {"down", "up"}:
            return

        modifier_order = ("ctrl", "shift", "alt", "windows")
        result = None
        should_send = False
        with self.screenshot_hotkey_capture_lock:
            if not self.screenshot_hotkey_capture_active or self.screenshot_hotkey_capture_result_sent:
                return

            if event_type == "up":
                if name in modifier_order:
                    self.screenshot_hotkey_capture_pressed = [
                        item for item in self.screenshot_hotkey_capture_pressed if item != name
                    ]
                return

            if name == "esc":
                self.screenshot_hotkey_capture_result_sent = True
                should_send = True
            elif name in modifier_order:
                if name not in self.screenshot_hotkey_capture_pressed:
                    self.screenshot_hotkey_capture_pressed.append(name)
                return
            else:
                names = [item for item in modifier_order if item in self.screenshot_hotkey_capture_pressed]
                names.append(name)
                try:
                    result = str(keyboard.get_hotkey_name(names) or "").strip().lower()
                except Exception:
                    result = "+".join(names)
                if not result:
                    result = "+".join(names)
                self.screenshot_hotkey_capture_result_sent = True
                should_send = True

        if should_send:
            self.enqueue_hotkey_action(("screenshot_hotkey_captured", result, None))

    def _close_screenshot_hotkey_capture(self, restore_hotkeys=True):
        """Снимает временный keyboard-hook и закрывает окно выбора."""
        with self.screenshot_hotkey_capture_lock:
            self.screenshot_hotkey_capture_active = False
            self.screenshot_hotkey_capture_pressed = []
            self.screenshot_hotkey_capture_result_sent = False

        hook = self.screenshot_hotkey_capture_hook
        self.screenshot_hotkey_capture_hook = None
        if hook is not None and HOTKEY_AVAILABLE:
            try:
                keyboard.unhook(hook)
            except Exception:
                pass

        if restore_hotkeys and not getattr(self, "_exiting", False):
            self.register_hotkey()

    def cancel_screenshot_hotkey_capture(self):
        if not getattr(self, "screenshot_hotkey_capture_active", False):
            return
        current = self.screenshot_hotkey_var.get().strip()
        self._close_screenshot_hotkey_capture(restore_hotkeys=True)
        self.screenshot_hotkey_display_var.set(current)
        message = f"Выбор отменён. Текущая клавиша скриншота: {current or 'не назначена'}"
        self.screenshot_status_var.set(message)

    def _finish_screenshot_hotkey_capture(self, hotkey, error_text=None):
        """Принимает результат keyboard-потока уже в безопасном GUI-потоке."""
        if not getattr(self, "screenshot_hotkey_capture_active", False):
            return
        self._close_screenshot_hotkey_capture(restore_hotkeys=False)

        if error_text:
            self.register_hotkey()
            self.screenshot_hotkey_display_var.set(self.screenshot_hotkey_var.get())
            message = f"Не удалось распознать клавишу: {error_text}"
            self.screenshot_status_var.set(message)
            self.status_var.set(message)
            return
        if not hotkey:
            self.register_hotkey()
            current = self.screenshot_hotkey_var.get().strip()
            self.screenshot_hotkey_display_var.set(current)
            self.screenshot_status_var.set(
                f"Выбор отменён. Текущая клавиша скриншота: {current or 'не назначена'}"
            )
            return

        normalized_record = re.sub(r"\s+", "", self.hotkey_var.get().strip().lower())
        normalized_screenshot = re.sub(r"\s+", "", str(hotkey).strip().lower())
        if normalized_record and normalized_screenshot == normalized_record:
            self.register_hotkey()
            self.screenshot_hotkey_display_var.set(self.screenshot_hotkey_var.get())
            message = "Эта клавиша уже запускает запись. Выбери для скриншота другую."
            self.screenshot_status_var.set(message)
            self.status_var.set(message)
            return

        self.screenshot_hotkey_var.set(str(hotkey).strip().lower())
        self.screenshot_hotkey_display_var.set(self.screenshot_hotkey_var.get())
        if self.hotkey_job:
            try:
                self.root.after_cancel(self.hotkey_job)
            except Exception:
                pass
            self.hotkey_job = None
        self.register_hotkey()
        self.schedule_save_settings()
        self.diagnostic_log("screenshot_hotkey_captured", {
            "hotkey": self.screenshot_hotkey_var.get().strip(),
            "print_screen_suppressed": self.hotkey_uses_print_screen(hotkey),
        })

    def _cancel_hotkey_recovery_jobs(self):
        jobs = list(getattr(self, "hotkey_recovery_jobs", []) or [])
        self.hotkey_recovery_jobs = []
        for job in jobs:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass

    def schedule_startup_hotkey_recovery(self):
        """Повторно поднимает global hook после входа в Windows.

        При автозапуске Explorer, Ножницы, оверлеи и драйверы клавиатуры ещё
        могут инициализироваться. Одна ранняя регистрация keyboard.add_hotkey()
        иногда остаётся формально успешной, но фактически не получает Print Screen.
        """
        self._cancel_hotkey_recovery_jobs()
        if not getattr(self, "started_from_windows_startup", False) or not HOTKEY_AVAILABLE:
            return
        delays = (3000, 10000, 30000)
        for attempt, delay_ms in enumerate(delays, start=1):
            def retry(current_attempt=attempt, current_delay=delay_ms):
                try:
                    self.hotkey_recovery_jobs = [
                        job for job in getattr(self, "hotkey_recovery_jobs", [])
                        if job != getattr(self, "_active_hotkey_recovery_job", None)
                    ]
                    if getattr(self, "_exiting", False) or not getattr(self, "running", True):
                        return
                    self.register_hotkey(
                        source="windows_startup_recovery",
                        recovery_attempt=current_attempt,
                    )
                except Exception as exc:
                    self.log_exception("startup_hotkey_recovery", exc)
            job = self.root.after(delay_ms, retry)
            self.hotkey_recovery_jobs.append(job)

        def write_startup_health_summary():
            if getattr(self, "_exiting", False) or not getattr(self, "running", True):
                return
            explorer_ready = None
            if os.name == "nt" and PSUTIL_AVAILABLE:
                try:
                    explorer_ready = any(
                        str(proc.info.get("name") or "").lower() == "explorer.exe"
                        for proc in psutil.process_iter(["name"])
                    )
                except Exception:
                    explorer_ready = None
            screenshot_registered = self._is_screenshot_hotkey_registered()
            self.diagnostic_log("startup_hotkey_health_summary", {
                "started_from_windows_startup": True,
                "registration_generation": getattr(self, "hotkey_registration_generation", 0),
                "expected_generation_after_recovery": 4,
                "record_registered": getattr(self, "hotkey_handle", None) is not None,
                "screenshot_registered": screenshot_registered,
                "screenshot_backend": getattr(self, "screenshot_hotkey_backend", None),
                "native_thread_id": getattr(self, "native_screenshot_hotkey_thread_id", None),
                "native_thread_alive": bool(
                    getattr(self, "native_screenshot_hotkey_thread", None)
                    and self.native_screenshot_hotkey_thread.is_alive()
                ),
                "native_registered": bool(getattr(self, "native_screenshot_hotkey_registered", False)),
                "screenshot_hotkey": self.screenshot_hotkey_var.get().strip(),
                "print_screen_suppressed": self.hotkey_uses_print_screen(self.screenshot_hotkey_var.get()),
                "callback_counts_since_start": dict(getattr(self, "hotkey_callback_counts", {}) or {}),
                "explorer_ready": explorer_ready,
                "note_for_ai": (
                    "Для backend=windows_register_hotkey registered=true означает успешный ответ WinAPI "
                    "и живой поток сообщений. Для keyboard окончательная проверка — hotkey_callback_received."
                ),
            }, level="INFO" if screenshot_registered else "WARN")
            if not screenshot_registered and self.screenshot_hotkey_var.get().strip():
                self.diagnostic_log("startup_hotkey_health_repair_requested", {
                    "registration_generation": getattr(self, "hotkey_registration_generation", 0),
                    "screenshot_backend": getattr(self, "screenshot_hotkey_backend", None),
                    "native_last_result": getattr(self, "native_screenshot_hotkey_last_result", None),
                }, level="WARN")
                self.register_hotkey(source="startup_health_repair", recovery_attempt=4)

        health_job = self.root.after(32000, write_startup_health_summary)
        self.hotkey_recovery_jobs.append(health_job)
        self.diagnostic_log("hotkey_startup_recovery_scheduled", {
            "delays_ms": list(delays),
            "health_summary_delay_ms": 32000,
            "screenshot_hotkey": self.screenshot_hotkey_var.get().strip(),
            "started_from_windows_startup": True,
        })

    def register_hotkey(self, source="normal", recovery_attempt=None):
        if self.initializing:
            return False
        if getattr(self, "screenshot_hotkey_capture_active", False):
            self.diagnostic_log("hotkey_registration_skipped", {
                "source": source,
                "reason": "screenshot_hotkey_capture_active",
                "recovery_attempt": recovery_attempt,
            }, level="WARN")
            return False
        if not HOTKEY_AVAILABLE:
            message = "Горячие клавиши недоступны: установи pip install keyboard"
            self.status_var.set(message)
            self.screenshot_status_var.set(message)
            self.diagnostic_log("hotkey_registration_unavailable", {"source": source}, level="ERROR")
            return False

        self.hotkey_registration_generation = int(getattr(self, "hotkey_registration_generation", 0)) + 1
        generation = self.hotkey_registration_generation
        started = time.perf_counter()
        self.diagnostic_log("hotkey_registration_start", {
            "source": source,
            "recovery_attempt": recovery_attempt,
            "generation": generation,
            "record_hotkey": self.hotkey_var.get().strip(),
            "screenshot_hotkey": self.screenshot_hotkey_var.get().strip(),
            "started_from_windows_startup": getattr(self, "started_from_windows_startup", False),
        })

        self._remove_registered_hotkeys()
        self.native_screenshot_hotkey_last_result = None
        record_hotkey = self.hotkey_var.get().strip()
        screenshot_hotkey = self.screenshot_hotkey_var.get().strip()
        normalized_record = re.sub(r"\s+", "", record_hotkey.lower())
        normalized_screenshot = re.sub(r"\s+", "", screenshot_hotkey.lower())
        messages = []
        failures = []

        if record_hotkey:
            try:
                self.hotkey_handle = keyboard.add_hotkey(
                    record_hotkey,
                    lambda: self.enqueue_hotkey_action("record"),
                )
                messages.append(f"запись — {record_hotkey}")
            except Exception as exc:
                failures.append("record")
                messages.append(f"ошибка клавиши записи «{record_hotkey}»: {exc}")
                self.diagnostic_log("record_hotkey_registration_failed", {
                    "hotkey": record_hotkey, "error": repr(exc), "source": source,
                    "recovery_attempt": recovery_attempt, "generation": generation,
                }, level="ERROR")

        if screenshot_hotkey and normalized_screenshot == normalized_record and normalized_record:
            failures.append("screenshot_conflict")
            message = "Клавиша скриншота совпадает с клавишей записи. Выбери другое сочетание."
            self.screenshot_status_var.set(message)
            messages.append(message)
        elif screenshot_hotkey:
            try:
                suppress = self.hotkey_uses_print_screen(screenshot_hotkey)
                native_result = None
                native_success = False
                if suppress and os.name == "nt":
                    native_success, native_result = self._start_native_screenshot_hotkey(
                        screenshot_hotkey,
                        source,
                        generation,
                    )
                if native_success:
                    self.screenshot_hotkey_backend = "windows_register_hotkey"
                else:
                    self.screenshot_hotkey_handle = keyboard.add_hotkey(
                        screenshot_hotkey,
                        lambda: self.enqueue_hotkey_action("screenshot"),
                        suppress=suppress,
                    )
                    self.screenshot_hotkey_backend = (
                        "keyboard_fallback" if suppress and os.name == "nt" else "keyboard"
                    )
                    if native_result is not None:
                        self.diagnostic_log("native_screenshot_hotkey_fallback", {
                            "source": source,
                            "generation": generation,
                            "hotkey": screenshot_hotkey,
                            "native_result": native_result,
                            "fallback_backend": "keyboard",
                        }, level="WARN")
                self.screenshot_status_var.set(f"Горячая клавиша скриншота: {screenshot_hotkey}")
                messages.append(f"скриншот — {screenshot_hotkey}")
            except Exception as exc:
                failures.append("screenshot")
                message = f"Не удалось назначить клавишу скриншота «{screenshot_hotkey}»: {exc}"
                self.screenshot_status_var.set(message)
                messages.append(message)
                self.diagnostic_log("screenshot_hotkey_registration_failed", {
                    "hotkey": screenshot_hotkey, "error": repr(exc), "source": source,
                    "recovery_attempt": recovery_attempt, "generation": generation,
                }, level="ERROR")

        self.status_var.set("Горячие клавиши: " + ("; ".join(messages) if messages else "не назначены"))
        self.hotkey_last_registration_perf = time.perf_counter()
        success = not failures
        self.diagnostic_log("hotkey_registration_success" if success else "hotkey_registration_partial", {
            "source": source,
            "recovery_attempt": recovery_attempt,
            "generation": generation,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "record_hotkey": record_hotkey,
            "record_registered": self.hotkey_handle is not None,
            "screenshot_hotkey": screenshot_hotkey,
            "screenshot_registered": self._is_screenshot_hotkey_registered(),
            "screenshot_backend": getattr(self, "screenshot_hotkey_backend", None),
            "native_thread_id": getattr(self, "native_screenshot_hotkey_thread_id", None),
            "native_thread_alive": bool(
                getattr(self, "native_screenshot_hotkey_thread", None)
                and self.native_screenshot_hotkey_thread.is_alive()
            ),
            "native_registered": bool(getattr(self, "native_screenshot_hotkey_registered", False)),
            "native_last_result": getattr(self, "native_screenshot_hotkey_last_result", None),
            "print_screen_suppressed": self.hotkey_uses_print_screen(screenshot_hotkey),
            "failures": failures,
            "note_for_ai": (
                "Print Screen в Windows использует RegisterHotKey с отдельным потоком сообщений. "
                "При автозапуске backend полностью пересоздаётся через 3/10/30 секунд."
            ),
        }, level="INFO" if success else "WARN")
        return success
