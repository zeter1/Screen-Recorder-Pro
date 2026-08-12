from ..shared import *


class AudioDevicesMixin:
    @staticmethod
    def normalize_saved_audio_choice(value, kind):
        """Мягкая миграция старых настроек к новым пунктам «по умолчанию»."""
        value = str(value or "").strip()
        if not value:
            return MIC_AUDIO_DEFAULT if kind == "mic" else SYSTEM_AUDIO_DEFAULT
        if kind == "system" and value == SYSTEM_AUDIO_WASAPI:
            return SYSTEM_AUDIO_DEFAULT
        return value

    @staticmethod
    def is_wasapi_render_choice(value):
        return str(value or "").startswith(WASAPI_RENDER_PREFIX)

    @staticmethod
    def strip_wasapi_render_prefix(value):
        value = str(value or "").strip()
        if value.startswith(WASAPI_RENDER_PREFIX):
            return value[len(WASAPI_RENDER_PREFIX):].strip()
        return value

    def refresh_audio_devices(self, silent=False):
        dshow_devices = self.get_dshow_audio_devices()
        self.dshow_audio_devices = list(dshow_devices)
        wasapi_capture, wasapi_render = self.get_wasapi_audio_devices()
        self.wasapi_capture_devices = list(wasapi_capture)
        self.wasapi_render_devices = list(wasapi_render)

        old_mic = self.normalize_saved_audio_choice(self.mic_device_var.get(), "mic")
        old_system = self.normalize_saved_audio_choice(self.system_device_var.get(), "system")

        # Для микрофона оставляем понятный пункт Windows default + реальные dshow-устройства.
        # WASAPI input тоже добавляем, если FFmpeg смог их перечислить.
        mic_wasapi = []
        for name in wasapi_capture:
            choice = f"WASAPI input: {name}"
            if choice not in mic_wasapi:
                mic_wasapi.append(choice)
        self.mic_audio_devices = [NO_AUDIO, MIC_AUDIO_DEFAULT] + mic_wasapi + dshow_devices

        # Для звука компьютера dshow обычно НЕ показывает обычные наушники/динамики.
        # Поэтому добавляем настоящие WASAPI render endpoint'ы: так можно выбрать именно
        # HUAWEI/Realtek/Focusrite и писать то, что играет в этом выходе Windows.
        render_choices = []
        for name in wasapi_render:
            choice = f"{WASAPI_RENDER_PREFIX}{name}"
            if choice not in render_choices:
                render_choices.append(choice)
        dshow_system_sources = [name for name in dshow_devices if self.is_valid_dshow_system_audio_source(name)]
        self.system_audio_devices = [NO_AUDIO, SYSTEM_AUDIO_DEFAULT, SYSTEM_AUDIO_COMMUNICATION] + render_choices + dshow_system_sources
        self.audio_devices = self.system_audio_devices

        # Комбобоксы есть только когда открыто окно «Настройки».
        # После закрытия окна ссылки сбрасываются, чтобы автообновление устройств не падало.
        try:
            if self.mic_combo is not None and self.mic_combo.winfo_exists():
                self.mic_combo.configure(values=self.mic_audio_devices)
        except Exception:
            pass
        try:
            if self.system_combo is not None and self.system_combo.winfo_exists():
                self.system_combo.configure(values=self.system_audio_devices)
        except Exception:
            pass

        if old_mic not in self.mic_audio_devices:
            self.mic_device_var.set(MIC_AUDIO_DEFAULT)
        elif old_mic != self.mic_device_var.get():
            self.mic_device_var.set(old_mic)

        if old_system not in self.system_audio_devices:
            # Старые версии могли сохранить обычный вход Focusrite/микрофон как
            # «звук компьютера». Такой выбор не пишет Telegram/YouTube/игры,
            # поэтому автоматически возвращаем системный звук на Windows default.
            self.system_device_var.set(SYSTEM_AUDIO_DEFAULT)
        elif old_system != self.system_device_var.get():
            self.system_device_var.set(old_system)

        if not silent:
            wasapi_note = "WASAPI loopback доступен" if self.supports_wasapi_loopback() else "WASAPI loopback FFmpeg недоступен, будет использован Python CoreAudio loopback"
            default_render = self.resolve_default_wasapi_render_device("console") or "не определён"
            comm_render = self.resolve_default_wasapi_render_device("communications") or "не определён"
            self.status_var.set(
                f"Аудиоустройства обновлены. dshow: {len(dshow_devices)}, WASAPI output: {len(wasapi_render)}. "
                f"{wasapi_note}. По умолчанию: {default_render}; связь: {comm_render}."
            )
        self.diagnostic_log("audio_devices_refreshed", {
            "silent": silent,
            "dshow_devices": dshow_devices,
            "wasapi_capture_devices": wasapi_capture,
            "wasapi_render_devices": wasapi_render,
            "selected_mic": self.mic_device_var.get(),
            "selected_system": self.system_device_var.get(),
            "mic_choices": self.mic_audio_devices,
            "system_choices": self.system_audio_devices,
        })

    def is_audio_settings_visible(self):
        """True только когда пользователь реально видит окно настроек звука."""
        try:
            window = getattr(self, "settings_window", None)
            return bool(
                window is not None
                and window.winfo_exists()
                and str(window.state()) != "withdrawn"
            )
        except Exception:
            return False

    def cancel_audio_device_refresh(self):
        job = getattr(self, "audio_device_refresh_job", None)
        self.audio_device_refresh_job = None
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass

    def schedule_audio_device_refresh(self, delay_ms=90000):
        """Планирует редкий probe только пока открыты настройки.

        Раньше `ffmpeg -list_devices` запускался каждые 15 секунд даже в трее.
        Теперь в фоне probe отсутствует; окно настроек обновляется при открытии,
        вручную кнопкой и страховочно раз в 90 секунд пока остаётся открытым.
        """
        self.cancel_audio_device_refresh()
        if not getattr(self, "running", False) or not self.is_audio_settings_visible():
            return
        try:
            self.audio_device_refresh_job = self.root.after(
                max(15000, int(delay_ms)),
                self.auto_refresh_audio_devices,
            )
        except Exception:
            self.audio_device_refresh_job = None

    def auto_refresh_audio_devices(self):
        self.audio_device_refresh_job = None
        if not getattr(self, "running", False) or not self.is_audio_settings_visible():
            return
        # Во время записи настройки и так заблокированы; probe FFmpeg не должен
        # конкурировать с основным захватом.
        if not self.is_recording and not self.is_finalizing:
            self.refresh_audio_devices(silent=True)
        self.schedule_audio_device_refresh(90000)

    def get_dshow_audio_devices(self):
        try:
            result = self.run_managed_process(
                [self.ffmpeg_path, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=10,
                creationflags=self.creation_flags(),
                expected_returncodes=(0, 1),
            )
            text = (result.stderr or "") + "\n" + (result.stdout or "")
        except Exception:
            return []

        devices = []
        for line in text.splitlines():
            match = re.search(r'"(.+?)"\s*\(audio\)', line)
            if match:
                name = match.group(1).strip()
                if name and name not in devices:
                    devices.append(name)
        return devices

    def get_wasapi_audio_devices(self):
        """Возвращает (capture_devices, render_devices), которые видит FFmpeg WASAPI.

        dshow часто показывает только устройства записи, поэтому для системного
        звука обычные динамики/наушники надо брать именно из WASAPI render list.
        """
        if not self.supports_wasapi_loopback():
            return [], []
        try:
            result = self.run_managed_process(
                [self.ffmpeg_path, "-hide_banner", "-list_devices", "true", "-f", "wasapi", "-i", "dummy"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=10,
                creationflags=self.creation_flags(),
                expected_returncodes=(0, 1),
            )
            raw_text = (result.stderr or "") + "\n" + (result.stdout or "")
        except Exception:
            return [], []

        capture_devices = []
        render_devices = []
        all_devices = []
        for line in raw_text.splitlines():
            match = re.search(r'"(.+?)"', line)
            if not match:
                continue
            name = match.group(1).strip()
            if not name or name.lower() in ("default", "dummy"):
                continue
            low = line.lower()
            if name not in all_devices:
                all_devices.append(name)
            if any(word in low for word in ("render", "output", "вывод", "playback", "speaker", "headphone", "динамик", "науш")):
                if name not in render_devices:
                    render_devices.append(name)
            elif any(word in low for word in ("capture", "input", "record", "микроф", "microphone", "ввод")):
                if name not in capture_devices:
                    capture_devices.append(name)

        # Разные сборки FFmpeg печатают список WASAPI по-разному. Если тип не
        # удалось понять, считаем список render-кандидатами: для loopback это
        # полезнее, чем вообще не показывать устройства вывода.
        if not render_devices and all_devices:
            render_devices = list(all_devices)
        return capture_devices, render_devices

    @staticmethod
    def normalize_device_name_for_match(name):
        return re.sub(r"\s+", " ", str(name or "").lower()).strip()

    def get_cached_wasapi_render_devices(self):
        devices = list(getattr(self, "wasapi_render_devices", []) or [])
        if not devices:
            _capture, devices = self.get_wasapi_audio_devices()
            self.wasapi_render_devices = list(devices)
        return devices

    def get_windows_default_render_device_name(self, role="console"):
        """Читает имя текущего устройства вывода Windows через CoreAudio.

        Это нужно потому, что `-f wasapi -loopback 1 -i default` на некоторых
        сборках FFmpeg либо пишет не тот endpoint, либо даёт тишину. Надёжнее
        получить реальное имя устройства вывода Windows и передать его в FFmpeg.
        """
        if os.name != "nt":
            return None
        try:
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

                @classmethod
                def from_string(cls, value):
                    return cls.from_buffer_copy(uuid.UUID(value).bytes_le)

            class PROPERTYKEY(ctypes.Structure):
                _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

            class PROPVARIANT(ctypes.Structure):
                _fields_ = [
                    ("vt", wintypes.USHORT),
                    ("wReserved1", wintypes.USHORT),
                    ("wReserved2", wintypes.USHORT),
                    ("wReserved3", wintypes.USHORT),
                    ("p", ctypes.c_void_p),
                    ("p2", ctypes.c_void_p),
                ]

            def release_com(ptr):
                try:
                    if ptr:
                        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
                        release(ptr)
                except Exception:
                    pass

            role_map = {
                "console": 0,
                "multimedia": 1,
                "communications": 2,
                "communication": 2,
            }
            role_value = role_map.get(str(role or "console").lower(), 0)
            e_render = 0
            stgm_read = 0
            clsctx_inproc_server = 0x1
            vt_lpwstr = 31

            ole32 = ctypes.OleDLL("ole32")
            initialized = False
            try:
                hr_init = ole32.CoInitialize(None)
                initialized = hr_init in (0, 1)
            except Exception:
                initialized = False

            p_enumerator = ctypes.c_void_p()
            p_device = ctypes.c_void_p()
            p_store = ctypes.c_void_p()
            try:
                clsid = GUID.from_string("BCDE0395-E52F-467C-8E3D-C4579291692E")
                iid = GUID.from_string("A95664D2-9614-4F35-A746-DE8DB63617E6")
                hr = ole32.CoCreateInstance(
                    ctypes.byref(clsid),
                    None,
                    clsctx_inproc_server,
                    ctypes.byref(iid),
                    ctypes.byref(p_enumerator),
                )
                if hr != 0 or not p_enumerator.value:
                    return None

                enum_vtbl = ctypes.cast(p_enumerator, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                get_default = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p),
                )(enum_vtbl[4])
                hr = get_default(p_enumerator, e_render, role_value, ctypes.byref(p_device))
                if hr != 0 or not p_device.value:
                    return None

                dev_vtbl = ctypes.cast(p_device, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                open_store = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.POINTER(ctypes.c_void_p),
                )(dev_vtbl[4])
                hr = open_store(p_device, stgm_read, ctypes.byref(p_store))
                if hr != 0 or not p_store.value:
                    return None

                store_vtbl = ctypes.cast(p_store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                get_value = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.POINTER(PROPERTYKEY),
                    ctypes.POINTER(PROPVARIANT),
                )(store_vtbl[5])
                key = PROPERTYKEY(GUID.from_string("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14)
                prop = PROPVARIANT()
                hr = get_value(p_store, ctypes.byref(key), ctypes.byref(prop))
                if hr == 0 and prop.vt == vt_lpwstr and prop.p:
                    name = ctypes.wstring_at(prop.p).strip()
                    try:
                        ctypes.OleDLL("ole32").PropVariantClear(ctypes.byref(prop))
                    except Exception:
                        pass
                    return name or None
                return None
            finally:
                release_com(p_store)
                release_com(p_device)
                release_com(p_enumerator)
                if initialized:
                    try:
                        ole32.CoUninitialize()
                    except Exception:
                        pass
        except Exception as exc:
            try:
                self.log_message(f"CoreAudio default render lookup failed ({role}): {exc}")
            except Exception:
                pass
            return None

    def match_wasapi_render_device(self, preferred_name):
        devices = self.get_cached_wasapi_render_devices()
        if not preferred_name:
            return None
        target = self.normalize_device_name_for_match(preferred_name)
        for name in devices:
            if self.normalize_device_name_for_match(name) == target:
                return name
        for name in devices:
            low = self.normalize_device_name_for_match(name)
            if target and (target in low or low in target):
                return name
        return None

    def resolve_default_wasapi_render_device(self, role="console"):
        preferred = self.get_windows_default_render_device_name(role=role)
        matched = self.match_wasapi_render_device(preferred)
        if matched:
            return matched
        return preferred

    @staticmethod
    def find_default_mic(devices):
        for name in devices:
            low = name.lower()
            if "microphone" in low or "mic" in low or "микроф" in low:
                return name
        return devices[0] if devices else NO_AUDIO

    @staticmethod
    def is_valid_dshow_system_audio_source(name):
        """True только для dshow-источников, которые реально могут писать звук ПК.

        Обычные dshow-устройства вроде Focusrite Analogue 1+2, микрофона или
        линейного входа — это ВХОДЫ, а не звук компьютера. Если их оставить в
        выпадающем списке «Звук компьютера», пользователь выбирает их логично,
        но в записи получается тишина от приложений. Для системного звука через
        dshow оставляем только настоящие loopback/virtual capture endpoints.
        """
        low = str(name or "").lower()
        positive_keywords = [
            "stereo mix",
            "стерео микшер",
            "what u hear",
            "wave out",
            "loopback",
            "virtual-audio-capturer",
            "cable output",          # VB-Audio Cable recording endpoint
            "vb-audio virtual cable",
            "virtual output",
        ]
        negative_keywords = [
            "microphone",
            "микроф",
            "analogue",
            "analog",
            "line in",
            "focusrite",
        ]
        if any(k in low for k in negative_keywords) and not any(k in low for k in positive_keywords):
            return False
        return any(k in low for k in positive_keywords)

    @classmethod
    def find_default_system_audio_device(cls, devices):
        """Возвращает первый настоящий источник системного звука dshow.

        Метод является classmethod, потому что использует другой метод класса.
        В модульной версии здесь раньше ошибочно использовался ``self`` внутри
        ``@staticmethod``, из-за чего старт записи падал с NameError.
        """
        for name in devices:
            if cls.is_valid_dshow_system_audio_source(name):
                return name
        return NO_AUDIO

    @staticmethod
    def find_default_system_audio(devices, wasapi_available=False):
        # Для интерфейса всегда возвращаем понятный пункт «по умолчанию».
        # Реальный способ захвата выбирается ниже: WASAPI loopback, либо dshow fallback.
        return SYSTEM_AUDIO_DEFAULT

    def get_cached_dshow_audio_devices(self):
        devices = list(getattr(self, "dshow_audio_devices", []) or [])
        if not devices:
            devices = self.get_dshow_audio_devices()
            self.dshow_audio_devices = list(devices)
        return devices

    def resolve_default_mic_dshow_device(self):
        return self.find_default_mic(self.get_cached_dshow_audio_devices())

    def resolve_default_system_dshow_device(self):
        return self.find_default_system_audio_device(self.get_cached_dshow_audio_devices())

    def schedule_meter_restart(self):
        if self.initializing or not self.running or not self.is_audio_settings_visible():
            return
        if self.meter_restart_job:
            try:
                self.root.after_cancel(self.meter_restart_job)
            except Exception:
                pass
        self.meter_restart_job = self.root.after(800, self.start_audio_meters)

    def start_audio_meters(self):
        # Перезапуск индикаторов должен сначала полностью убить старые ffmpeg.
        # Раньше старый поток мог не успеть выйти, флаг снова становился True,
        # и в системе копились несколько ffmpeg.exe.
        self.stop_audio_meters(join_timeout=0.35, cancel_restart=False)
        if (
            not self.running
            or self.is_recording
            or self.is_finalizing
            or not self.is_audio_settings_visible()
        ):
            self.meter_queue.put(("mic", 0))
            self.meter_queue.put(("system", 0))
            return

        self.audio_meters_running = True
        self.audio_meter_generation += 1
        generation = self.audio_meter_generation

        selections = {
            "mic": self.mic_device_var.get(),
            "system": self.system_device_var.get(),
        }
        for source, device in selections.items():
            if device and device != NO_AUDIO:
                thread = threading.Thread(
                    target=self.audio_meter_loop,
                    args=(source, device, generation),
                    daemon=True,
                )
                self.audio_meter_threads[source] = thread
                thread.start()
            else:
                self.meter_queue.put((source, 0))

    def stop_audio_meters(self, join_timeout=0.8, cancel_restart=True):
        self.audio_meters_running = False
        self.audio_meter_generation += 1

        if cancel_restart and self.meter_restart_job:
            try:
                self.root.after_cancel(self.meter_restart_job)
            except Exception:
                pass
            self.meter_restart_job = None

        processes = list(self.audio_meter_processes.values())
        self.audio_meter_processes.clear()

        # Самая частая причина «Python не отвечает» при старте с плавающей
        # панели — синхронная остановка ffmpeg-процессов аудио-индикаторов
        # прямо в Tkinter-потоке. Если ffmpeg/taskkill подвисает, подвисает всё
        # окно. Поэтому из GUI-потока только отдаём завершение в daemon-потоки.
        gui_thread = self.is_gui_thread()
        for process in processes:
            if gui_thread:
                try:
                    threading.Thread(
                        target=self.terminate_process_tree,
                        args=(process, 0.35, "audio_meter_ffmpeg"),
                        name="stop_audio_meter_ffmpeg",
                        daemon=True,
                    ).start()
                except Exception:
                    pass
            else:
                self.terminate_process_tree(process, timeout=0.8, name="audio_meter_ffmpeg")

        current = threading.current_thread()
        threads = list(self.audio_meter_threads.values())
        if not gui_thread:
            for thread in threads:
                try:
                    if thread is not current and thread.is_alive():
                        thread.join(timeout=join_timeout)
                except Exception:
                    pass
        # Из GUI-потока не ждём meter-потоки: они daemon и сами выйдут после
        # смены generation/audio_meters_running.
        self.audio_meter_threads.clear()
        self.meter_queue.put(("mic", 0))
        self.meter_queue.put(("system", 0))

    def audio_meter_loop(self, source, device_name, generation):
        def meter_should_run():
            return bool(
                self.running
                and self.audio_meters_running
                and generation == self.audio_meter_generation
            )

        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
        ]
        try:
            cmd += self.build_audio_input_args(source, device_name, for_meter=True)
        except Exception:
            self.meter_queue.put((source, 0))
            return
        cmd += [
            "-filter_complex",
            "astats=metadata=1:reset=0.25,ametadata=print:key=lavfi.astats.Overall.RMS_level",
            "-metadata",
            f"comment={APP_NAME}",
            "-f",
            "null",
            "-",
        ]
        process = None
        try:
            process = self.start_managed_process(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                expected_returncodes=(0, 1),
                creationflags=self.creation_flags(),
            )
            if not meter_should_run():
                self.terminate_process_tree(process, timeout=0.5, name="audio_meter_ffmpeg_cancelled")
                return
            self.audio_meter_processes[source] = process
        except Exception:
            self.meter_queue.put((source, 0))
            return

        try:
            for line in process.stderr:
                if not meter_should_run():
                    break
                match = re.search(r"RMS_level=([-+]?inf|-?\d+(?:\.\d+)?)", line)
                if not match:
                    continue
                raw = match.group(1).lower()
                if "inf" in raw:
                    level = 0
                else:
                    db = float(raw)
                    level = int(max(0, min(100, (db + 60) / 60 * 100)))
                self.meter_queue.put((source, level))
        except Exception:
            pass
        finally:
            self.meter_queue.put((source, 0))
            try:
                if self.audio_meter_processes.get(source) is process:
                    self.audio_meter_processes.pop(source, None)
            except Exception:
                pass
            self.terminate_process_tree(process, timeout=0.8, name="audio_meter_ffmpeg_finally")

    def update_audio_levels_from_queue(self):
        try:
            while True:
                source, level = self.meter_queue.get_nowait()
                if source == "mic":
                    self.mic_level_var.set(level)
                    self.mic_level_text.set(f"{int(level)}%")
                elif source == "system":
                    self.system_level_var.set(level)
                    self.system_level_text.set(f"{int(level)}%")
        except queue.Empty:
            pass

        if self.running:
            self.root.after(100, self.update_audio_levels_from_queue)
