from ..shared import *


class CaptureCommandsMixin:
    def get_virtual_screen_rect(self):
        """Возвращает прямоугольник всего виртуального рабочего стола Windows."""
        if os.name == "nt":
            try:
                user32 = ctypes.windll.user32
                left = int(user32.GetSystemMetrics(76))
                top = int(user32.GetSystemMetrics(77))
                width = int(user32.GetSystemMetrics(78))
                height = int(user32.GetSystemMetrics(79))
                if width > 0 and height > 0:
                    return left, top, width, height
            except Exception:
                pass
        try:
            return 0, 0, max(1, int(self.root.winfo_screenwidth())), max(1, int(self.root.winfo_screenheight()))
        except Exception:
            return 0, 0, 1920, 1080

    def get_monitor_rects(self):
        """Список мониторов как (left, top, width, height), отсортированный слева направо."""
        if os.name != "nt":
            return [self.get_virtual_screen_rect()]
        rects = []
        try:
            user32 = ctypes.windll.user32

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            MONITORENUMPROC = ctypes.WINFUNCTYPE(
                ctypes.c_int,
                wintypes.HMONITOR,
                wintypes.HDC,
                ctypes.POINTER(RECT),
                wintypes.LPARAM,
            )

            def callback(_monitor, _hdc, rect_ptr, _data):
                rect = rect_ptr.contents
                width = int(rect.right - rect.left)
                height = int(rect.bottom - rect.top)
                if width > 0 and height > 0:
                    rects.append((int(rect.left), int(rect.top), width, height))
                return 1

            user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(callback), 0)
        except Exception:
            rects = []
        if not rects:
            rects = [self.get_virtual_screen_rect()]
        return sorted(rects, key=lambda r: (r[0], r[1]))

    def get_selected_monitor_rect_for_ddagrab(self):
        rects = self.get_monitor_rects()
        index = self.get_ddagrab_output_index(rects=rects)
        if index >= len(rects):
            index = 0
        return rects[index]

    def get_capture_region_monitor_intersections(self, rects=None):
        """Возвращает пересечения выбранной области с мониторами."""
        region = getattr(self, "capture_region", None)
        if not region or len(region) != 4:
            return []
        try:
            rx, ry, rw, rh = (int(value) for value in region)
            if rw <= 0 or rh <= 0:
                return []
            rects = list(rects or self.get_monitor_rects())
            intersections = []
            for index, (mx, my, mw, mh) in enumerate(rects):
                left = max(rx, mx)
                top = max(ry, my)
                right = min(rx + rw, mx + mw)
                bottom = min(ry + rh, my + mh)
                area = max(0, right - left) * max(0, bottom - top)
                if area > 0:
                    intersections.append((index, area))
            return intersections
        except Exception as exc:
            self.log_exception("get_capture_region_monitor_intersections", exc)
            return []

    def capture_region_spans_multiple_monitors(self):
        return len(self.get_capture_region_monitor_intersections()) > 1

    def get_ddagrab_output_index(self, rects=None):
        """Для записи области автоматически выбирает монитор, где она находится."""
        rects = list(rects or self.get_monitor_rects())
        intersections = self.get_capture_region_monitor_intersections(rects=rects)
        if intersections:
            return max(intersections, key=lambda item: item[1])[0]
        try:
            index = max(0, int(self.monitor_index_var.get()) - 1)
        except Exception:
            index = 0
        if index >= len(rects):
            index = 0
        return index

    def get_ddagrab_poll_fps(self, output_fps):
        """Частота опроса Desktop Duplication отдельно от FPS готового файла.

        На мониторе 144 Гц при выходе 72 FPS прежний код опрашивал ddagrab
        только 72 раза/с. При локальной задержке одного опроса в файл попадал
        повтор старого изображения, хотя PTS и средний FPS оставались идеальными.

        Если герцовка является целым кратным выходного FPS, опрашиваем экран
        вдвое чаще, но не выше герцовки. Затем единственный fps-фильтр выбирает
        ровные выходные кадры. Для 144 Гц / 72 FPS получается 144 -> 72.
        """
        try:
            output_fps = max(1, int(output_fps))
        except Exception:
            output_fps = 30
        try:
            refresh_hz = max(1, int(self.recording_refresh_hz or detect_primary_refresh_hz()))
        except Exception:
            refresh_hz = output_fps

        poll_fps = output_fps
        if refresh_hz >= output_fps * 2 and refresh_hz % output_fps == 0:
            poll_fps = min(refresh_hz, output_fps * 2)

        self.recording_ddagrab_poll_fps = int(poll_fps)
        return int(poll_fps)

    def build_ddagrab_source_expression(self, fps_int, draw_mouse, output_idx):
        """Строит источник ddagrab и обрезает область прямо в Desktop Duplication.

        Это важно для плавности: кадры остаются в D3D11/GPU-памяти и сразу
        передаются NVENC. Старый путь сначала скачивал каждый кадр на CPU через
        ``hwdownload``, а затем снова загружал его на GPU. У ddagrab нет фонового
        буфера, поэтому такой лишний круг мог задерживать опрос Desktop
        Duplication и давать пропущенные реальные состояния экрана, хотя PTS
        готового файла выглядели идеально ровными.
        """
        try:
            fps_int = max(1, int(fps_int))
        except Exception:
            fps_int = 30
        try:
            output_idx = max(0, int(output_idx))
        except Exception:
            output_idx = 0

        options = [
            f"framerate={fps_int}",
            f"draw_mouse={1 if draw_mouse else 0}",
            f"output_idx={output_idx}",
            "dup_frames=1",
        ]

        region = getattr(self, "capture_region", None)
        if region and len(region) == 4:
            try:
                rx, ry, rw, rh = (int(v) for v in region)
                ox, oy, sw, sh = self.get_selected_monitor_rect_for_ddagrab()
                x1 = max(0, rx - ox)
                y1 = max(0, ry - oy)
                x2 = min(sw, rx + rw - ox)
                y2 = min(sh, ry + rh - oy)
                width = int(x2 - x1)
                height = int(y2 - y1)
                # H.264/H.265 4:2:0 требуют чётные размеры.
                width -= width % 2
                height -= height % 2
                if width >= 2 and height >= 2:
                    options.extend([
                        f"video_size={width}x{height}",
                        f"offset_x={int(x1)}",
                        f"offset_y={int(y1)}",
                    ])
            except Exception as exc:
                self.log_exception("build_ddagrab_source_expression.region", exc)

        return "ddagrab=" + ":".join(options)

    def build_capture_crop_prefix(self, capture_backend="gdigrab", raw_pipe=False):
        """Возвращает crop-фильтр для выбранной области с учётом мультимонитора."""
        # ddagrab обрезается непосредственно параметрами video_size/offset_x/
        # offset_y. Так кадры не скачиваются на CPU только ради crop.
        if capture_backend == "ddagrab":
            return ""

        region = getattr(self, "capture_region", None)
        if not region or len(region) != 4 or raw_pipe:
            return ""
        try:
            rx, ry, rw, rh = (int(v) for v in region)
            if rw < 2 or rh < 2:
                return ""
            ox, oy, sw, sh = self.get_virtual_screen_rect()
            x1 = max(0, rx - ox)
            y1 = max(0, ry - oy)
            x2 = min(sw, rx + rw - ox)
            y2 = min(sh, ry + rh - oy)
            cw = int(x2 - x1)
            ch = int(y2 - y1)
            cw -= cw % 2
            ch -= ch % 2
            if cw < 2 or ch < 2:
                return ""
            return f"crop={cw}:{ch}:{int(x1)}:{int(y1)},"
        except Exception as exc:
            self.log_exception("build_capture_crop_prefix", exc)
            return ""

    def build_smooth_video_filter(self, fps_int, use_nvenc, raw_pipe=False, capture_backend="gdigrab"):
        """Создаёт ровный CFR без ускорения и без зависимости от рваных PTS ddagrab.

        Последняя версия сначала применяла fps к исходным PTS Desktop Duplication.
        Глобальная длительность получалась правильной, но локальная неравномерность
        PTS могла заставлять fps-фильтр выбирать повторяющиеся состояния экрана.

        Для ddagrab каждому входному кадру сначала назначается время его фактического
        прибытия по часам системы. Затем единственный fps-фильтр формирует ровный
        выходной поток. Это сохраняет реальную длительность и не возвращает схему
        setpts=N*ticks, которая ускоряла длинную запись.

        Фильтры меняют только метаданные кадров. В режиме ddagrab + NVENC кадры
        остаются D3D11-frame в видеопамяти; hwdownload не используется.
        """
        try:
            fps_int = max(1, int(fps_int))
        except Exception:
            fps_int = 30

        pixel_format = "nv12" if use_nvenc else "yuv420p"
        timescale = int(getattr(self, "MP4_VIDEO_TRACK_TIMESCALE", 39600))

        source_rebase = (
            f"fps={fps_int}:round=near,"
            f"settb=expr=1/{timescale},"
            "setpts=PTS-STARTPTS"
        )

        # RTCTIME/RTCSTART выражены в микросекундах. При time base 1/1_000_000
        # их разность становится прямым PTS по монотонно идущим реальным часам.
        # Финальный setpts только обнуляет начало после CFR-нормализации.
        ddagrab_wallclock_cfr = (
            "settb=expr=1/1000000,"
            "setpts=RTCTIME-RTCSTART,"
            f"fps={fps_int}:round=near,"
            f"settb=expr=1/{timescale},"
            "setpts=PTS-STARTPTS"
        )

        crop = self.build_capture_crop_prefix(
            capture_backend=capture_backend,
            raw_pipe=raw_pipe,
        )

        if not raw_pipe and capture_backend == "ddagrab":
            if use_nvenc:
                return ddagrab_wallclock_cfr
            return f"{ddagrab_wallclock_cfr},hwdownload,format=bgra,format=yuv420p"

        if raw_pipe:
            return (
                f"settb=expr=1/{timescale},"
                f"setpts=PTS-STARTPTS,format={pixel_format}"
            )

        return f"{crop}{source_rebase},format={pixel_format}"

    @staticmethod
    def _scale_bitrate(value, factor):
        """Масштабирует строку битрейта вида '16M'/'16000k' в '24000k'."""
        try:
            s = str(value).strip().lower()
            if s.endswith("m"):
                kbit = float(s[:-1]) * 1000.0
            elif s.endswith("k"):
                kbit = float(s[:-1])
            else:
                kbit = float(s) / 1000.0
            return f"{int(round(kbit * factor))}k"
        except Exception:
            return value

    def append_encoder_options(self, cmd, fps_int, video_bitrate, bufsize, use_nvenc, raw_pipe=False, capture_backend="gdigrab"):
        smooth_filter = self.build_smooth_video_filter(
            fps_int,
            use_nvenc,
            raw_pipe=raw_pipe,
            capture_backend=capture_backend,
        )
        if smooth_filter:
            cmd += ["-vf", smooth_filter]

        use_hevc = False
        try:
            use_hevc = self.should_use_hevc()
        except Exception:
            use_hevc = False
        gop = str(max(30, fps_int * 4))
        maxrate = self._scale_bitrate(video_bitrate, 1.5)
        bufsize2 = self._scale_bitrate(video_bitrate, 2.0)

        if use_nvenc:
            # Для захвата экрана важнее равномерно и быстро принимать кадры, чем
            # держать большую очередь lookahead. Нулевой lookahead и отключённый
            # temporal AQ уменьшают задержку/нагрузку GPU; битрейт уже имеет запас.
            cmd += [
                "-c:v",
                "hevc_nvenc" if use_hevc else "h264_nvenc",
                "-preset",
                "fast",
                "-rc",
                "vbr",
                "-b:v",
                video_bitrate,
                "-maxrate",
                maxrate,
                "-bufsize",
                bufsize2,
                "-rc-lookahead",
                "0",
                "-spatial_aq",
                "1",
                "-temporal_aq",
                "0",
                "-profile:v",
                "main" if use_hevc else "high",
                "-bf",
                "0",
                "-g",
                gop,
            ]
        else:
            cmd += [
                "-c:v",
                "libx265" if use_hevc else "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "22" if use_hevc else "20",
                "-maxrate",
                maxrate,
                "-bufsize",
                bufsize2,
                "-g",
                gop,
            ]

        cmd += ["-max_muxing_queue_size", "4096"]

        # Экранные захваты к этому моменту уже нормализованы одним fps-фильтром
        # по исходным часам. rawvideo имеет входной -framerate и только обнулённый
        # PTS. Поэтому на выходе используем passthrough и не запускаем второй
        # механизм, который снова мог бы массово добавлять/удалять кадры.
        cmd += ["-fps_mode", "passthrough"]
        return cmd

    def append_segment_container_options(self, cmd, segment_path):
        suffix = Path(segment_path).suffix.lower()
        if suffix in (".mp4", ".mov"):
            cmd += [
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                "-video_track_timescale",
                str(self.MP4_VIDEO_TRACK_TIMESCALE),
            ]
        return cmd

    def build_ffmpeg_command(self, segment_path, capture_backend=None):
        fps_int, video_bitrate, bufsize = self.get_video_settings_for_ffmpeg()
        fps = str(fps_int)
        audio_bitrate = self.get_recording_audio_bitrate_safe()
        capture_backend = capture_backend or self.choose_capture_backend()
        use_nvenc = self.should_use_nvenc()

        cmd = [
            self.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-stats",
            "-stats_period",
            "0.5",
            "-progress",
            "pipe:1",
        ]

        if capture_backend == "ddagrab":
            output_idx = self.get_ddagrab_output_index()
            capture_poll_fps = self.get_ddagrab_poll_fps(fps_int)
            try:
                if self.log_handle:
                    self.log_handle.write(
                        f"ddagrab_poll_fps={capture_poll_fps}, output_fps={fps_int}, "
                        f"monitor_refresh_hz={self.recording_refresh_hz}\n"
                    )
                    self.log_handle.flush()
            except Exception:
                pass
            cmd += [
                "-thread_queue_size",
                "4096",
                "-f",
                "lavfi",
                "-i",
                self.build_ddagrab_source_expression(
                    capture_poll_fps,
                    bool(self.cursor_visible_var.get()),
                    output_idx,
                ),
            ]
        else:
            # gdigrab получает входные PTS через genpts, а затем общий фильтр
            # переводит поток на строгую CFR-шкалу времени.
            cmd += [
                "-fflags",
                "+genpts",
                "-rtbufsize",
                "2048M",
                "-thread_queue_size",
                "4096",
                "-f",
                "gdigrab",
                "-framerate",
                fps,
                "-draw_mouse",
                "1" if self.cursor_visible_var.get() else "0",
                "-i",
                "desktop",
            ]

        audio_sources = self.collect_audio_sources()
        self.append_audio_inputs_and_filters(cmd, audio_sources, first_audio_input_index=1)
        self.append_encoder_options(cmd, fps_int, video_bitrate, bufsize, use_nvenc, raw_pipe=False, capture_backend=capture_backend)

        if audio_sources:
            cmd += ["-c:a", "aac", "-b:a", audio_bitrate, "-ar", "48000", "-ac", "2"]
        else:
            cmd += ["-an"]

        cmd += ["-metadata", "encoder=ScreenRecorderProWin11"]
        self.append_segment_container_options(cmd, segment_path)

        cmd += [str(segment_path)]
        return cmd

    def build_dxcam_ffmpeg_command(self, segment_path, width, height):
        fps_int, video_bitrate, bufsize = self.get_video_settings_for_ffmpeg()
        audio_bitrate = self.get_recording_audio_bitrate_safe()
        use_nvenc = self.should_use_nvenc()

        cmd = [
            self.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-stats",
            "-stats_period",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            str(fps_int),
            "-thread_queue_size",
            "4096",
            "-i",
            "pipe:0",
        ]

        audio_sources = self.collect_audio_sources()
        self.append_audio_inputs_and_filters(cmd, audio_sources, first_audio_input_index=1)
        self.append_encoder_options(cmd, fps_int, video_bitrate, bufsize, use_nvenc, raw_pipe=True, capture_backend="dxcam")

        if audio_sources:
            cmd += ["-c:a", "aac", "-b:a", audio_bitrate, "-ar", "48000", "-ac", "2"]
            # Важно для DXcam + звук: после закрытия видеопотока аудио dshow
            # может продолжать идти бесконечно. Из-за этого FFmpeg долго висел
            # при остановке, а при принудительном завершении файл мог получаться
            # без нормальных свойств/длительности в Windows. -shortest заставляет
            # FFmpeg корректно закрыть контейнер сразу после конца видео.
            cmd += ["-shortest"]
        else:
            cmd += ["-an"]

        # Явная метка кодировщика. Основные свойства видео берутся из корректно
        # закрытого контейнера, а не из этого поля, но метка помогает диагностике.
        cmd += ["-metadata", "encoder=ScreenRecorderProWin11"]
        cmd += ["-avoid_negative_ts", "make_zero"]
        self.append_segment_container_options(cmd, segment_path)

        cmd += [str(segment_path)]
        return cmd

    def prepare_clean_annotation_capture(self):
        """Коротко прячет кнопки плавающей панели, чтобы снять чистый фон.

        Нужен при продолжении записи после паузы: overlay уже существует, и
        первый DXcam-кадр иначе содержал бы круг/панель, которые потом нечем
        заменить. Нарисованные линии не прячем — в видео они должны остаться.
        """
        overlay = getattr(self, "annotation_overlay", None)
        if overlay is None:
            return lambda: None
        try:
            bubble_visible = bool(overlay.bubble and str(overlay.bubble.state()) != "withdrawn")
            toolbar_visible = bool(overlay.toolbar_visible)
            if overlay.toolbar:
                overlay.toolbar.withdraw()
            if overlay.bubble:
                overlay.bubble.withdraw()
            overlay.toolbar_visible = False
            overlay.update_control_rects_now()
            self.root.update_idletasks()
        except Exception as exc:
            self.log_exception("prepare_clean_annotation_capture.hide", exc)
            return lambda: None

        def restore():
            try:
                if overlay.bubble and bubble_visible:
                    overlay.bubble.deiconify()
                    overlay.bubble.attributes("-topmost", True)
                    overlay.bubble.lift()
                if toolbar_visible:
                    overlay.show_toolbar()
                overlay.update_control_rects_now()
                self.root.update_idletasks()
            except Exception as exc:
                self.log_exception("prepare_clean_annotation_capture.restore", exc)
        return restore
