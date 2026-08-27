from ..shared import *


class DxcamCaptureMixin:
    def refresh_recording_cursor_cache(self):
        """Снимает Tk-параметры курсора в обычные поля до запуска записи."""
        try:
            self.recording_cursor_visible = bool(self.cursor_visible_var.get())
        except Exception:
            self.recording_cursor_visible = True
        try:
            self.recording_cursor_size_percent = normalize_recording_cursor_size_percent(
                self.cursor_size_percent_var.get()
            )
        except Exception:
            self.recording_cursor_size_percent = 100
        try:
            self.recording_cursor_highlight = bool(self.cursor_highlight_var.get())
        except Exception:
            self.recording_cursor_highlight = False
        try:
            size = int(self.cursor_highlight_size_var.get())
        except Exception:
            size = 70
        self.recording_cursor_highlight_size = max(20, min(200, size))

    def should_draw_native_recording_cursor(self):
        """Native FFmpeg cursor используется, пока custom overlay не готов."""
        if not bool(getattr(self, "recording_cursor_visible", True)):
            return False
        size_percent = normalize_recording_cursor_size_percent(
            getattr(self, "recording_cursor_size_percent", 100)
        )
        if size_percent == 100:
            return True
        return not bool(getattr(self, "recording_custom_cursor_overlay_ready", False))

    def get_recording_cursor_render_mode(self):
        if not bool(getattr(self, "recording_cursor_visible", True)):
            return "hidden"
        if self.should_draw_native_recording_cursor():
            size_percent = normalize_recording_cursor_size_percent(
                getattr(self, "recording_cursor_size_percent", 100)
            )
            return "system" if size_percent == 100 else "system_fallback"
        return "custom_overlay"

    def start_dxcam_segment(self, segment_path):
        if not DXCAM_AVAILABLE or os.name != "nt":
            raise RuntimeError("DXcam недоступен. Установи: pip install dxcam")
        self.refresh_recording_cursor_cache()

        fps_int = self.get_recording_fps_int()
        click_perf = self.recording_start_requested_perf or time.perf_counter()

        restore_annotation_controls = self.prepare_clean_annotation_capture()
        try:
            warm_camera, warm_frames, warm_ready, warm_error = self.get_instant_buffer_snapshot()
            # Используем прогретый DXcam-буфер всегда, если он есть.
            # В старой логике буфер отключался, когда уже была создана плавающая
            # панель/overlay; из-за этого старт снова ждал создание новой камеры
            # и первые 1–2 секунды после клика могли не попасть в файл. Сейчас
            # панель по текущему поведению должна быть видна в записи, поэтому
            # отключать мгновенный буфер ради overlay больше нельзя.
            prebuffer_used = warm_camera is not None

            # Важный фикс: если горячий буфер уже запустил DXcam, но мы НЕ можем
            # использовать его кадры (например, кадры ещё не накопились или уже
            # видна плавающая панель), надо сначала остановить буфер. Иначе
            # dxcam.create().start(...) может упасть с ошибкой:
            # "Capture is already running. Call stop() first."
            if warm_camera is not None and not prebuffer_used:
                self.stop_instant_dxcam_buffer(release_camera=True, join_timeout=1.2)
                warm_camera = None
                warm_frames = []

            if prebuffer_used:
                # Не вызываем camera.get_latest_frame()/grab() из GUI-потока.
                # Именно этот путь зависал, когда пользователь нажимал «Запись»
                # на плавающей панели в момент, когда DXcam-камера уже создана,
                # но буфер ещё не успел положить первый кадр. В таком случае
                # безопаснее мгновенно уйти на ddagrab/gdigrab, чем подвесить окно.
                if not warm_frames:
                    raise RuntimeError("DXcam-буфер ещё не накопил первый кадр; включаю безопасный fallback без зависания GUI.")
                first_frame = warm_frames[-1][1]
                if first_frame is None:
                    raise RuntimeError("DXcam-буфер вернул пустой кадр; включаю безопасный fallback без зависания GUI.")
                height, width = first_frame.shape[:2]
            else:
                # Не создаём новую DXcam-камеру прямо в обработчике Start. Именно
                # этот путь на повторном старте мог подвесить Tkinter, когда dxcam
                # возвращал старый singleton-instance. Если горячий буфер ещё не
                # готов, ниже сработает безопасный fallback на ddagrab/gdigrab.
                raise RuntimeError("DXcam-буфер ещё не готов к мгновенному старту.")

            try:
                self.annotation_toolbar_clean_frame = first_frame.copy()
            except Exception:
                self.annotation_toolbar_clean_frame = None
        finally:
            restore_annotation_controls()

        command = self.build_dxcam_ffmpeg_command(segment_path, width, height)
        startup_delay = time.perf_counter() - click_perf
        self.log_handle.write(self.command_to_log_text(command) + "\n")
        self.log_handle.write(f"DXcam frame_size={width}x{height}, fps={fps_int}\n")
        self.log_handle.write(f"instant_prebuffer_ready={warm_ready}, used={prebuffer_used}, warm_snapshot_frames={len(warm_frames)}\n")
        if warm_error:
            self.log_handle.write(f"instant_prebuffer_error={warm_error}\n")
        self.log_handle.write(f"start_delay_before_ffmpeg={startup_delay:.3f}\n")
        self.log_handle.flush()

        process = self.start_managed_process(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self.log_handle,
            creationflags=self.recording_creation_flags(),
        )

        if prebuffer_used:
            camera, buffered_frames = self.take_instant_buffer_for_recording()
            if camera is None:
                try:
                    if process.stdin:
                        process.stdin.close()
                except Exception:
                    pass
                try:
                    self.terminate_process_tree(process, timeout=1.0, name="dxcam_no_camera_ffmpeg")
                except Exception:
                    pass
                raise RuntimeError("DXcam-камера была потеряна при передаче из буфера; включаю безопасный fallback без зависания GUI.")
        else:
            buffered_frames = [(click_perf, first_frame, self.get_cursor_position())]

        handoff_perf = time.perf_counter()
        selected_start_frames = self.select_frames_from_click(buffered_frames, click_perf, fallback_frame=first_frame)
        selected_start_frames = self.append_timing_guard_frame(selected_start_frames, click_perf, handoff_perf, fallback_frame=first_frame)
        try:
            if self.log_handle:
                first_ts = selected_start_frames[0][0] if selected_start_frames else None
                last_ts = selected_start_frames[-1][0] if selected_start_frames else None
                span = (last_ts - first_ts) if first_ts is not None and last_ts is not None else 0
                self.log_handle.write(
                    f"instant_prebuffer_taken_frames={len(buffered_frames)}, "
                    f"selected_start_frames={len(selected_start_frames)}, "
                    f"selected_span={span:.3f}s\n"
                )
                self.log_handle.flush()
        except Exception:
            pass

        self.dxcam_stop_event = threading.Event()
        self.dxcam_camera = camera
        self.current_segment_engine = "dxcam"
        with self.process_lock:
            self.process = process

        self.segments.append(segment_path)
        self.current_segment_media_seconds = 0.0
        self.current_segment_last_progress_perf = None
        self.segment_started_at = click_perf

        self.dxcam_thread = threading.Thread(
            target=self.dxcam_capture_loop,
            args=(camera, process, first_frame, fps_int, click_perf, selected_start_frames),
            daemon=True,
        )
        self.dxcam_thread.start()

        self.root.after(450, lambda p=process: self.check_recording_process_after_start(p))

    def check_recording_process_after_start(self, process):
        try:
            if not self.is_recording:
                return
            with self.process_lock:
                if self.process is not process:
                    return
            code = process.poll()
            if code is not None and code != 0:
                self.status_var.set(f"FFmpeg завершился сразу с кодом {code}. Проверь лог: {self.current_log_path}")
        except Exception:
            pass

    def get_cursor_position(self):
        """Текущая позиция курсора в координатах основного экрана."""
        if os.name != "nt":
            return None
        try:
            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            point = POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                return int(point.x), int(point.y)
        except Exception:
            return None
        return None

    def get_cursor_bitmap(self):
        """Маленькая стрелка курсора как BGR+alpha для вшивания в DXcam-кадр."""
        if self._cursor_bitmap_cache is not None:
            return self._cursor_bitmap_cache
        if not (NUMPY_AVAILABLE and PIL_AVAILABLE and Image is not None and ImageDraw is not None):
            self._cursor_bitmap_cache = (None, None)
            return self._cursor_bitmap_cache
        try:
            img = Image.new("RGBA", (34, 34), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            outline = [(1, 1), (1, 27), (8, 21), (13, 33), (19, 31), (14, 20), (25, 20)]
            fill = [(4, 5), (4, 21), (9, 16), (14, 29), (16, 28), (11, 16), (19, 16)]
            draw.polygon(outline, fill=(0, 0, 0, 235))
            draw.polygon(fill, fill=(255, 255, 255, 255))
            arr = np.array(img, dtype=np.uint8)
            bgr = arr[:, :, :3][:, :, ::-1].copy()
            alpha = (arr[:, :, 3:4].astype(np.float32) / 255.0)
            self._cursor_bitmap_cache = (bgr, alpha)
            return self._cursor_bitmap_cache
        except Exception:
            self._cursor_bitmap_cache = (None, None)
            return self._cursor_bitmap_cache

    def alpha_blend_patch(self, frame, patch_bgr, patch_alpha, x, y):
        if patch_bgr is None or patch_alpha is None:
            return frame
        h, w = frame.shape[:2]
        ph, pw = patch_bgr.shape[:2]
        x1 = max(0, int(x))
        y1 = max(0, int(y))
        x2 = min(w, int(x) + pw)
        y2 = min(h, int(y) + ph)
        if x1 >= x2 or y1 >= y2:
            return frame
        px1 = x1 - int(x)
        py1 = y1 - int(y)
        px2 = px1 + (x2 - x1)
        py2 = py1 + (y2 - y1)
        roi = frame[y1:y2, x1:x2].astype(np.float32)
        patch = patch_bgr[py1:py2, px1:px2].astype(np.float32)
        alpha = patch_alpha[py1:py2, px1:px2]
        blended = roi * (1.0 - alpha) + patch * alpha
        frame[y1:y2, x1:x2] = blended.astype(np.uint8)
        return frame

    def draw_cursor_highlight_on_frame(self, frame, x, y):
        if not NUMPY_AVAILABLE:
            return frame
        try:
            diameter = int(getattr(self, "recording_cursor_highlight_size", 70))
        except Exception:
            diameter = 70
        diameter = max(20, min(200, diameter))
        radius = max(10, diameter // 2)
        ring_width = max(3, min(8, radius // 5))
        h, w = frame.shape[:2]
        cx = int(x)
        cy = int(y)
        x1 = max(0, cx - radius)
        y1 = max(0, cy - radius)
        x2 = min(w, cx + radius + 1)
        y2 = min(h, cy + radius + 1)
        if x1 >= x2 or y1 >= y2:
            return frame
        yy, xx = np.ogrid[y1:y2, x1:x2]
        dist2 = (xx - cx) * (xx - cx) + (yy - cy) * (yy - cy)
        outer = radius * radius
        inner = max(1, radius - ring_width)
        inner2 = inner * inner
        ring_mask = (dist2 <= outer) & (dist2 >= inner2)
        fill_mask = dist2 < inner2
        roi = frame[y1:y2, x1:x2]
        ring_color = np.array([0, 255, 255], dtype=np.float32)  # жёлтый в BGR
        fill_color = np.array([0, 255, 255], dtype=np.float32)
        if fill_mask.any():
            fill = roi[fill_mask].astype(np.float32)
            roi[fill_mask] = (fill * 0.88 + fill_color * 0.12).astype(np.uint8)
        if ring_mask.any():
            ring = roi[ring_mask].astype(np.float32)
            roi[ring_mask] = (ring * 0.25 + ring_color * 0.75).astype(np.uint8)
        return frame

    def get_annotation_control_rects_for_recording(self):
        overlay = getattr(self, "annotation_overlay", None)
        if overlay is None:
            return []
        try:
            return overlay.get_control_rects_for_capture()
        except Exception:
            return []

    def remove_annotation_toolbar_from_frame(self, frame):
        """Не удаляет плавающую панель из кадра.

        По текущему требованию индикатор и раскрытая панель должны быть видны
        во время записи экрана и попадать в итоговое видео. Функция оставлена
        как безопасная заглушка, чтобы старые вызовы не ломали программу.
        """
        return frame

    def decorate_dxcam_frame(self, frame, cursor_pos=None):
        """Добавляет в кадр курсор/подсветку. Плавающая панель не вырезается."""
        need_cursor = bool(getattr(self, "recording_cursor_visible", True) or getattr(self, "recording_cursor_highlight", False))
        need_clean_controls = False
        if not need_cursor and not need_clean_controls:
            try:
                self.annotation_toolbar_clean_frame = frame.copy()
            except Exception:
                pass
            return frame

        try:
            if not frame.flags["C_CONTIGUOUS"]:
                frame = frame.copy()
            else:
                frame = frame.copy()
        except Exception:
            pass

        if need_clean_controls:
            frame = self.remove_annotation_toolbar_from_frame(frame)

        if need_cursor:
            position = cursor_pos if cursor_pos is not None else self.get_cursor_position()
            if position is not None:
                x, y = position
                try:
                    h, w = frame.shape[:2]
                    # Для обычного одного монитора координаты уже совпадают с кадром DXcam.
                    if not (x < -250 or y < -250 or x > w + 250 or y > h + 250):
                        if getattr(self, "recording_cursor_highlight", False):
                            self.draw_cursor_highlight_on_frame(frame, x, y)
                        if getattr(self, "recording_cursor_visible", True):
                            cursor_bgr, cursor_alpha = self.get_cursor_bitmap()
                            self.alpha_blend_patch(frame, cursor_bgr, cursor_alpha, x, y)
                except Exception:
                    pass

        return frame

    def dxcam_capture_loop(self, camera, process, first_frame, fps_int, click_perf=None, buffered_start_frames=None):
        frame_interval = 1.0 / max(1, fps_int)
        frames_written = 0
        prebuffer_frames_written = 0
        duplicated_frames = 0
        late_ticks = 0
        write_errors = 0
        last_frame = first_frame
        started_at = time.perf_counter()
        click_perf = click_perf or started_at
        buffered_start_frames = buffered_start_frames or []

        def write_one_frame(raw_frame, cursor_pos=None):
            nonlocal frames_written, write_errors, last_frame
            try:
                frame = self.decorate_dxcam_frame(raw_frame, cursor_pos=cursor_pos)
                if not frame.flags["C_CONTIGUOUS"]:
                    frame = frame.copy()
                process.stdin.write(frame.tobytes())
                frames_written += 1
                last_frame = raw_frame
                return True
            except Exception:
                write_errors += 1
                return False

        try:
            if buffered_start_frames:
                normalized = []
                for ts, frame, cursor_pos in sorted(buffered_start_frames, key=lambda item: item[0]):
                    try:
                        ts = float(ts)
                    except Exception:
                        ts = click_perf
                    if ts < click_perf - frame_interval:
                        continue
                    normalized.append((max(click_perf, ts), frame, cursor_pos))

                if not normalized:
                    normalized = [(click_perf, first_frame, self.get_cursor_position())]

                last_output_ts = click_perf
                previous_frame = None
                previous_cursor = None
                min_step = frame_interval * 0.35
                for ts, frame, cursor_pos in normalized:
                    if previous_frame is None:
                        if not write_one_frame(frame, cursor_pos=cursor_pos):
                            return
                        prebuffer_frames_written += 1
                        previous_frame = frame
                        previous_cursor = cursor_pos
                        last_output_ts = click_perf
                        continue

                    # Если буфер был прорежен из-за ограничения памяти, не
                    # сжимаем первые секунды в короткий рывок. Дублируем
                    # ближайший предыдущий кадр до timestamp следующего кадра.
                    while last_output_ts + frame_interval < ts - min_step:
                        if not write_one_frame(previous_frame, cursor_pos=previous_cursor):
                            return
                        prebuffer_frames_written += 1
                        duplicated_frames += 1
                        last_output_ts += frame_interval

                    if ts - last_output_ts >= min_step:
                        if not write_one_frame(frame, cursor_pos=cursor_pos):
                            return
                        prebuffer_frames_written += 1
                        last_output_ts = max(ts, last_output_ts + frame_interval)
                    previous_frame = frame
                    previous_cursor = cursor_pos
            else:
                if not write_one_frame(first_frame, cursor_pos=self.get_cursor_position()):
                    return

            next_frame_time = time.perf_counter() + frame_interval
            max_catch_up_frames = 3
            stop_loop = False

            while self.dxcam_stop_event is not None and not self.dxcam_stop_event.is_set() and not stop_loop:
                now = time.perf_counter()
                sleep_for = next_frame_time - now
                if sleep_for > 0:
                    time.sleep(min(sleep_for, 0.004))
                    continue

                now = time.perf_counter()
                ticks_due = 1
                if now > next_frame_time + frame_interval:
                    try:
                        ticks_due = int((now - next_frame_time) / frame_interval) + 1
                    except Exception:
                        ticks_due = 1
                    if ticks_due > max_catch_up_frames:
                        late_ticks += ticks_due - max_catch_up_frames
                        ticks_due = max_catch_up_frames
                        # Не сжимаем время, но и не пытаемся бесконечно догонять.
                        # Дальше пишем несколько дубликатов и возвращаем таймер
                        # к реальному времени. Так видео не ускоряется рывками,
                        # а максимум получает короткую заморозку кадра при перегрузе.
                        next_frame_time = now - (ticks_due - 1) * frame_interval

                frame = self.get_dxcam_recording_frame_fast(camera)

                if frame is None:
                    frame = last_frame
                    duplicated_frames += 1
                    cursor_pos = self.get_cursor_position()
                else:
                    cursor_pos = self.get_cursor_position()

                for duplicate_index in range(max(1, ticks_due)):
                    if duplicate_index:
                        duplicated_frames += 1
                    if not write_one_frame(frame, cursor_pos=cursor_pos):
                        stop_loop = True
                        break

                next_frame_time += max(1, ticks_due) * frame_interval
        finally:
            elapsed = max(0.001, time.perf_counter() - started_at)
            total_from_click = max(0.001, time.perf_counter() - click_perf)
            self.dxcam_stats = {
                "frames_written": frames_written,
                "prebuffer_frames_written": prebuffer_frames_written,
                "duplicated_frames": duplicated_frames,
                "late_ticks": late_ticks,
                "write_errors": write_errors,
                "elapsed": round(elapsed, 2),
                "from_click_elapsed": round(total_from_click, 2),
                "actual_write_fps": round(frames_written / elapsed, 2),
            }
            self.release_dxcam_camera_safely(camera, label="dxcam_recording_camera")
            try:
                if process.stdin:
                    process.stdin.close()
            except Exception:
                pass
