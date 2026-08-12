from ..shared import *


class WasapiLoopbackWaveRecorder:
    """Запись системного звука Windows напрямую через CoreAudio/WASAPI loopback.

    Нужна как запасной путь, когда установленный ffmpeg не умеет `-f wasapi`.
    FFmpeg в этом режиме пишет видео и микрофон, а этот поток параллельно пишет
    звук текущего устройства вывода Windows в WAV. После остановки WAV
    подмешивается в сегмент через FFmpeg.
    """

    def __init__(self, output_path, role="console", volume=1.0, log_callback=None):
        self.output_path = Path(output_path)
        self.role = role or "console"
        self.volume = max(0.0, float(volume if volume is not None else 1.0))
        self.log_callback = log_callback
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None
        self.started = threading.Event()
        self.finished = threading.Event()
        self.capture_start_perf = None

    def log(self, text):
        try:
            if self.log_callback:
                self.log_callback(str(text))
        except Exception:
            pass

    def start(self, startup_wait=0.0, start_perf=None):
        if os.name != "nt":
            raise RuntimeError("CoreAudio loopback доступен только на Windows.")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.capture_start_perf = float(start_perf) if start_perf is not None else time.perf_counter()
        except Exception:
            self.capture_start_perf = time.perf_counter()
        self.thread = threading.Thread(target=self._run, name="coreaudio_loopback_recorder", daemon=True)
        self.thread.start()
        # Раньше здесь ожидание было до 1.5 секунды. При выбранном системном
        # звуке это блокировало старт видео: экран начинал писаться только после
        # подготовки CoreAudio, и первые секунды после клика пропадали.
        # Теперь ждём только короткое окно для мгновенной ошибки, а сама запись
        # loopback поднимается параллельно с подготовкой видео.
        try:
            wait_time = max(0.0, float(startup_wait))
        except Exception:
            wait_time = 0.15
        if wait_time:
            self.started.wait(timeout=wait_time)
        if self.error:
            raise RuntimeError(str(self.error))

    def stop(self, timeout=3.0):
        self.stop_event.set()
        try:
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=timeout)
        except Exception:
            pass
        if self.error:
            self.log(f"CoreAudio loopback stopped with error: {self.error}")
        return self.output_path

    def _make_guid_class(self):
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

        return GUID

    @staticmethod
    def _release_com(ptr):
        try:
            if ptr and getattr(ptr, "value", None):
                vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
                release(ptr)
        except Exception:
            pass

    def _run(self):
        ole32 = None
        initialized = False
        p_enumerator = ctypes.c_void_p()
        p_device = ctypes.c_void_p()
        p_audio_client = ctypes.c_void_p()
        p_capture_client = ctypes.c_void_p()
        p_mix_format = ctypes.c_void_p()
        wav_file = None
        try:
            GUID = self._make_guid_class()

            class WAVEFORMATEX(ctypes.Structure):
                _fields_ = [
                    ("wFormatTag", wintypes.WORD),
                    ("nChannels", wintypes.WORD),
                    ("nSamplesPerSec", wintypes.DWORD),
                    ("nAvgBytesPerSec", wintypes.DWORD),
                    ("nBlockAlign", wintypes.WORD),
                    ("wBitsPerSample", wintypes.WORD),
                    ("cbSize", wintypes.WORD),
                ]

            role_map = {
                "console": 0,
                "multimedia": 1,
                "communications": 2,
                "communication": 2,
            }
            role_value = role_map.get(str(self.role or "console").lower(), 0)

            CLSCTX_ALL = 0x17
            eRender = 0
            AUDCLNT_SHAREMODE_SHARED = 0
            AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
            AUDCLNT_BUFFERFLAGS_SILENT = 0x00000002
            REFTIMES_PER_SEC = 10_000_000

            ole32 = ctypes.OleDLL("ole32")
            try:
                hr_init = ole32.CoInitialize(None)
                initialized = hr_init in (0, 1)
            except Exception:
                initialized = False

            clsid_mmdevice = GUID.from_string("BCDE0395-E52F-467C-8E3D-C4579291692E")
            iid_immdevice_enumerator = GUID.from_string("A95664D2-9614-4F35-A746-DE8DB63617E6")
            iid_iaudio_client = GUID.from_string("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
            iid_iaudio_capture_client = GUID.from_string("C8ADBD64-E71E-48A0-A4DE-185C395CD317")

            hr = ole32.CoCreateInstance(
                ctypes.byref(clsid_mmdevice),
                None,
                CLSCTX_ALL,
                ctypes.byref(iid_immdevice_enumerator),
                ctypes.byref(p_enumerator),
            )
            if hr != 0 or not p_enumerator.value:
                raise RuntimeError(f"CoCreateInstance IMMDeviceEnumerator failed: 0x{hr & 0xffffffff:08x}")

            enum_vtbl = ctypes.cast(p_enumerator, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            get_default = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_void_p),
            )(enum_vtbl[4])
            hr = get_default(p_enumerator, eRender, role_value, ctypes.byref(p_device))
            if hr != 0 or not p_device.value:
                raise RuntimeError(f"GetDefaultAudioEndpoint failed: 0x{hr & 0xffffffff:08x}")

            dev_vtbl = ctypes.cast(p_device, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            activate = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(GUID),
                wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )(dev_vtbl[3])
            hr = activate(p_device, ctypes.byref(iid_iaudio_client), CLSCTX_ALL, None, ctypes.byref(p_audio_client))
            if hr != 0 or not p_audio_client.value:
                raise RuntimeError(f"IMMDevice.Activate(IAudioClient) failed: 0x{hr & 0xffffffff:08x}")

            ac_vtbl = ctypes.cast(p_audio_client, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            get_mix_format = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )(ac_vtbl[8])
            hr = get_mix_format(p_audio_client, ctypes.byref(p_mix_format))
            if hr != 0 or not p_mix_format.value:
                raise RuntimeError(f"IAudioClient.GetMixFormat failed: 0x{hr & 0xffffffff:08x}")

            fmt = ctypes.cast(p_mix_format, ctypes.POINTER(WAVEFORMATEX)).contents
            channels = int(fmt.nChannels or 2)
            sample_rate = int(fmt.nSamplesPerSec or 48000)
            bits = int(fmt.wBitsPerSample or 32)
            block_align = int(fmt.nBlockAlign or max(1, channels * bits // 8))
            format_tag = int(fmt.wFormatTag)
            cb_size = int(fmt.cbSize or 0)
            fmt_blob = ctypes.string_at(p_mix_format, ctypes.sizeof(WAVEFORMATEX) + cb_size)
            subformat = fmt_blob[24:40] if format_tag == 0xFFFE and len(fmt_blob) >= 40 else b""

            initialize = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.c_int,
                wintypes.DWORD,
                ctypes.c_longlong,
                ctypes.c_longlong,
                ctypes.c_void_p,
                ctypes.c_void_p,
            )(ac_vtbl[3])
            hr = initialize(
                p_audio_client,
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK,
                ctypes.c_longlong(REFTIMES_PER_SEC),
                ctypes.c_longlong(0),
                p_mix_format,
                None,
            )
            if hr != 0:
                raise RuntimeError(f"IAudioClient.Initialize(loopback) failed: 0x{hr & 0xffffffff:08x}")

            get_service = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(GUID),
                ctypes.POINTER(ctypes.c_void_p),
            )(ac_vtbl[14])
            hr = get_service(p_audio_client, ctypes.byref(iid_iaudio_capture_client), ctypes.byref(p_capture_client))
            if hr != 0 or not p_capture_client.value:
                raise RuntimeError(f"IAudioClient.GetService(IAudioCaptureClient) failed: 0x{hr & 0xffffffff:08x}")

            cc_vtbl = ctypes.cast(p_capture_client, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            get_buffer = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
            )(cc_vtbl[3])
            release_buffer = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.c_uint32,
            )(cc_vtbl[4])
            get_next_packet_size = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
            )(cc_vtbl[5])
            start = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(ac_vtbl[10])
            stop = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(ac_vtbl[11])

            wav_file = wave.open(str(self.output_path), "wb")
            wav_file.setnchannels(2)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)

            self.log(
                f"CoreAudio loopback started: role={self.role}, "
                f"format_tag={format_tag}, channels={channels}, rate={sample_rate}, bits={bits}, block_align={block_align}"
            )
            hr = start(p_audio_client)
            if hr != 0:
                raise RuntimeError(f"IAudioClient.Start failed: 0x{hr & 0xffffffff:08x}")
            self.started.set()

            # Учёт тишины: WASAPI shared loopback не отдаёт пакеты, когда ничего
            # не играет. Без добивки нулями длительность WAV = только звучащим
            # интервалам, и после каждой паузы звук уезжает вперёд видео.
            try:
                silence_start_perf = float(self.capture_start_perf or time.perf_counter())
            except Exception:
                silence_start_perf = time.perf_counter()
            frames_written = 0
            out_frame_bytes = 2 * 2  # выходной WAV всегда стерео 16-бит

            try:
                while not self.stop_event.is_set():
                    packet_frames = ctypes.c_uint32(0)
                    hr = get_next_packet_size(p_capture_client, ctypes.byref(packet_frames))
                    if hr != 0:
                        raise RuntimeError(f"GetNextPacketSize failed: 0x{hr & 0xffffffff:08x}")
                    if packet_frames.value == 0:
                        expected = int((time.perf_counter() - silence_start_perf) * sample_rate)
                        if expected > frames_written:
                            gap = expected - frames_written
                            wav_file.writeframesraw(b"\x00" * gap * out_frame_bytes)
                            frames_written = expected
                        time.sleep(0.005)
                        continue
                    while packet_frames.value and not self.stop_event.is_set():
                        data_ptr = ctypes.c_void_p()
                        num_frames = ctypes.c_uint32(0)
                        flags = wintypes.DWORD(0)
                        dev_pos = ctypes.c_uint64(0)
                        qpc_pos = ctypes.c_uint64(0)
                        hr = get_buffer(
                            p_capture_client,
                            ctypes.byref(data_ptr),
                            ctypes.byref(num_frames),
                            ctypes.byref(flags),
                            ctypes.byref(dev_pos),
                            ctypes.byref(qpc_pos),
                        )
                        if hr != 0:
                            raise RuntimeError(f"IAudioCaptureClient.GetBuffer failed: 0x{hr & 0xffffffff:08x}")
                        try:
                            frames = int(num_frames.value)
                            if frames > 0:
                                if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data_ptr.value:
                                    pcm = b"\x00" * frames * 2 * 2
                                else:
                                    raw = ctypes.string_at(data_ptr, frames * block_align)
                                    pcm = self._convert_to_pcm16_stereo(raw, frames, channels, bits, format_tag, subformat)
                                if pcm:
                                    wav_file.writeframesraw(pcm)
                                    frames_written += frames
                        finally:
                            release_buffer(p_capture_client, num_frames)

                        packet_frames = ctypes.c_uint32(0)
                        hr = get_next_packet_size(p_capture_client, ctypes.byref(packet_frames))
                        if hr != 0:
                            raise RuntimeError(f"GetNextPacketSize after release failed: 0x{hr & 0xffffffff:08x}")
            finally:
                try:
                    stop(p_audio_client)
                except Exception:
                    pass
        except Exception as exc:
            self.error = exc
            self.log(f"CoreAudio loopback error: {exc}")
            self.started.set()
        finally:
            try:
                if wav_file:
                    wav_file.close()
            except Exception:
                pass
            try:
                if p_mix_format and p_mix_format.value and ole32:
                    ole32.CoTaskMemFree(p_mix_format)
            except Exception:
                pass
            self._release_com(p_capture_client)
            self._release_com(p_audio_client)
            self._release_com(p_device)
            self._release_com(p_enumerator)
            if initialized and ole32:
                try:
                    ole32.CoUninitialize()
                except Exception:
                    pass
            self.finished.set()

    def _convert_to_pcm16_stereo(self, raw, frames, channels, bits, format_tag, subformat):
        if frames <= 0:
            return b""
        ieee_float_guid = b"\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
        pcm_guid = b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
        is_float = format_tag == 3 or subformat == ieee_float_guid
        is_pcm = format_tag == 1 or subformat == pcm_guid
        channels = max(1, int(channels or 2))

        try:
            if NUMPY_AVAILABLE and np is not None:
                if is_float and bits == 32:
                    arr = np.frombuffer(raw, dtype="<f4")
                    arr = arr[: frames * channels].reshape((-1, channels))
                    stereo = self._numpy_to_stereo(arr, channels)
                    if self.volume != 1.0:
                        stereo = stereo * self.volume
                    stereo = np.clip(stereo, -1.0, 1.0)
                    return (stereo * 32767.0).astype("<i2").tobytes()
                if is_pcm and bits == 16:
                    arr = np.frombuffer(raw, dtype="<i2")
                    arr = arr[: frames * channels].reshape((-1, channels))
                    stereo = self._numpy_to_stereo(arr, channels).astype(np.float32)
                    if self.volume != 1.0:
                        stereo = stereo * self.volume
                    stereo = np.clip(stereo, -32768, 32767)
                    return stereo.astype("<i2").tobytes()
                if is_pcm and bits == 32:
                    arr = np.frombuffer(raw, dtype="<i4")
                    arr = arr[: frames * channels].reshape((-1, channels)).astype(np.float32) / 2147483648.0
                    stereo = self._numpy_to_stereo(arr, channels)
                    if self.volume != 1.0:
                        stereo = stereo * self.volume
                    stereo = np.clip(stereo, -1.0, 1.0)
                    return (stereo * 32767.0).astype("<i2").tobytes()
                if is_pcm and bits == 24:
                    b = np.frombuffer(raw, dtype=np.uint8)
                    usable = (len(b) // 3) * 3
                    b = b[:usable].reshape((-1, 3))
                    vals = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
                    vals = (vals ^ 0x800000) - 0x800000
                    vals = vals[: frames * channels].reshape((-1, channels)).astype(np.float32) / 8388608.0
                    stereo = self._numpy_to_stereo(vals, channels)
                    if self.volume != 1.0:
                        stereo = stereo * self.volume
                    stereo = np.clip(stereo, -1.0, 1.0)
                    return (stereo * 32767.0).astype("<i2").tobytes()
        except Exception as exc:
            self.log(f"CoreAudio conversion fallback: {exc}")

        # Без numpy поддерживаем два самых частых варианта. Неизвестный формат
        # лучше заменить тишиной, чем уронить всю запись.
        try:
            import struct
            if is_float and bits == 32:
                samples = struct.unpack("<" + "f" * (len(raw) // 4), raw)
                out = bytearray()
                for i in range(0, min(len(samples), frames * channels), channels):
                    left = samples[i]
                    right = samples[i + 1] if channels > 1 and i + 1 < len(samples) else left
                    for value in (left, right):
                        value = max(-1.0, min(1.0, float(value) * self.volume))
                        out += int(value * 32767.0).to_bytes(2, "little", signed=True)
                return bytes(out)
            if is_pcm and bits == 16:
                samples = memoryview(raw).cast("h")
                out = bytearray()
                for i in range(0, min(len(samples), frames * channels), channels):
                    left = int(samples[i])
                    right = int(samples[i + 1]) if channels > 1 and i + 1 < len(samples) else left
                    for value in (left, right):
                        value = int(max(-32768, min(32767, value * self.volume)))
                        out += value.to_bytes(2, "little", signed=True)
                return bytes(out)
        except Exception:
            pass
        return b"\x00" * frames * 2 * 2

    @staticmethod
    def _numpy_to_stereo(arr, channels):
        if channels <= 1 or arr.shape[1] <= 1:
            return np.repeat(arr[:, :1], 2, axis=1)
        return arr[:, :2]
