from ..shared import *
from ..components.audio_loopback import WasapiLoopbackWaveRecorder


class SegmentAudioMixin:
    def should_capture_system_audio_with_python_loopback(self, system_choice=None):
        """True, если системный звук надо писать не через FFmpeg, а напрямую CoreAudio.

        На компьютере пользователя FFmpeg видит dshow, но не видит WASAPI. dshow
        показывает в основном входы записи, а не звук приложений. Поэтому для
        пункта «Звук компьютера по умолчанию» используем нативный Windows
        CoreAudio loopback в Python и затем подмешиваем WAV к видео.
        """
        if os.name != "nt":
            return False
        system = self.normalize_saved_audio_choice(system_choice or self.system_device_var.get(), "system")
        if not system or system == NO_AUDIO:
            return False
        if self.supports_wasapi_loopback():
            return False
        if system in (SYSTEM_AUDIO_DEFAULT, SYSTEM_AUDIO_COMMUNICATION, SYSTEM_AUDIO_WASAPI):
            return True
        if self.is_wasapi_render_choice(system):
            return True
        # Если в старых настройках остался вход вместо системного звука, всё равно
        # пишем default output через CoreAudio, чтобы не получить тишину.
        if not self.is_valid_dshow_system_audio_source(system):
            return True
        return False

    def start_python_loopback_for_segment(self, segment_path):
        system = self.normalize_saved_audio_choice(self.system_device_var.get(), "system")
        role = "communications" if system == SYSTEM_AUDIO_COMMUNICATION else "console"
        volume = max(0, int(self.system_volume_var.get())) / 100.0
        wav_path = Path(segment_path).with_suffix(".system_loopback.wav")
        try:
            if self.log_handle:
                self.log_handle.write(f"python_coreaudio_loopback_start={wav_path}, role={role}, volume={volume}\n")
                self.log_handle.flush()
        except Exception:
            pass
        recorder = WasapiLoopbackWaveRecorder(
            wav_path,
            role=role,
            volume=volume,
            log_callback=self.log_message,
        )
        # Важно: CoreAudio loopback должен вести свою шкалу времени от фактического
        # старта сегмента, а не от клика по кнопке «Запись». Иначе при включённом
        # отсчёте 3-2-1 WAV начинался бы с лишней тишины и системный звук уезжал бы
        # относительно видео.
        recorder.start(startup_wait=0.0, start_perf=time.perf_counter())
        try:
            if self.log_handle:
                self.log_handle.write("python_coreaudio_loopback_start_wait=0.0s\n")
                self.log_handle.flush()
        except Exception:
            pass
        self.current_python_loopback_recorder = recorder
        self.current_python_loopback_segment = Path(segment_path)
        self.current_python_loopback_path = wav_path
        self.python_loopback_audio_segments[str(Path(segment_path))] = wav_path
        try:
            self.status_var.set("Системный звук пишется напрямую через Windows CoreAudio loopback.")
        except Exception:
            pass

    def stop_python_loopback_for_current_segment(self):
        recorder = self.current_python_loopback_recorder
        wav_path = self.current_python_loopback_path
        segment = self.current_python_loopback_segment
        self.current_python_loopback_recorder = None
        self.current_python_loopback_path = None
        self.current_python_loopback_segment = None
        if recorder is None:
            return None

        stop_error = None
        finished = False
        try:
            recorder.stop(timeout=10.0)
            try:
                finished = bool(getattr(recorder, "finished", None) and recorder.finished.wait(timeout=2.0))
            except Exception:
                finished = not (getattr(recorder, "thread", None) and recorder.thread.is_alive())
        except Exception as exc:
            stop_error = exc
            try:
                finished = bool(getattr(recorder, "finished", None) and recorder.finished.is_set())
            except Exception:
                finished = False
        finally:
            try:
                size = Path(wav_path).stat().st_size if wav_path and Path(wav_path).exists() else 0
                if self.log_handle:
                    self.log_handle.write(
                        f"python_coreaudio_loopback_stop={wav_path}, size={size}, "
                        f"finished={finished}, error={repr(stop_error) if stop_error else ''}\n"
                    )
                    self.log_handle.flush()
                if segment and wav_path and finished and size > 44:
                    self.python_loopback_audio_segments[str(Path(segment))] = Path(wav_path)
                elif segment:
                    self.python_loopback_audio_segments.pop(str(Path(segment)), None)
                    self.log_message(
                        f"CoreAudio loopback WAV is not complete; system audio will not be mixed for segment {segment}."
                    )
            except Exception:
                pass
        try:
            size = Path(wav_path).stat().st_size if wav_path and Path(wav_path).exists() else 0
        except Exception:
            size = 0
        if stop_error is not None:
            raise RuntimeError(f"CoreAudio loopback остановился с ошибкой: {stop_error}") from stop_error
        if not finished or size <= 44:
            raise RuntimeError(
                f"CoreAudio loopback WAV не был корректно завершён: {wav_path}, "
                f"finished={finished}, size={size}. Итоговый файл не будет собран молча без системного звука."
            )
        return wav_path

    def media_has_audio_stream(self, path):
        ffprobe = self.get_ffprobe_path()
        if ffprobe:
            try:
                result = self.run_managed_process(
                    [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=12,
                    creationflags=self.creation_flags(),
                )
            except Exception as exc:
                raise RuntimeError(f"Не удалось проверить аудиодорожку {path}: {exc}") from exc
            if result.returncode != 0:
                raise RuntimeError(
                    f"FFprobe не проверил аудиодорожку {path}: {(result.stderr or '').strip()}"
                )
            return bool((result.stdout or "").strip())

        # FFprobe может отсутствовать в урезанной поставке FFmpeg. Проверяем
        # реальное отображение первого аудиопотока вместо опасного return True.
        try:
            result = self.run_managed_process(
                [
                    self.ffmpeg_path,
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-map",
                    "0:a:0",
                    "-frames:a",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=12,
                creationflags=self.creation_flags(),
            )
        except Exception as exc:
            raise RuntimeError(f"Не удалось проверить аудиодорожку {path}: {exc}") from exc
        if result.returncode == 0:
            return True
        stderr_text = (result.stderr or "").lower()
        if "matches no streams" in stderr_text or "does not contain any stream" in stderr_text:
            return False
        raise RuntimeError(
            f"FFmpeg не проверил аудиодорожку {path}: {(result.stderr or '').strip()}"
        )

    def prepare_segments_with_python_loopback_audio(self, segments):
        prepared = []
        for segment in segments:
            wav_path = self.python_loopback_audio_segments.get(str(Path(segment)))
            if not wav_path or not Path(wav_path).exists() or Path(wav_path).stat().st_size <= 44:
                prepared.append(segment)
                continue
            mixed_path = Path(segment).with_name(Path(segment).stem + "_with_system_audio" + Path(segment).suffix)
            try:
                self.mix_python_loopback_audio_into_segment(Path(segment), Path(wav_path), mixed_path)
                self.validate_media_file(mixed_path, label="сегмент с системным звуком")
                prepared.append(mixed_path)
            except Exception as exc:
                # Не выдаём пользователю «успешно» с незаметно потерянной частью видео.
                # Если системный звук должен был подмешаться, но muxing сломался,
                # останавливаем финальную сборку и оставляем временную папку для диагностики.
                self.log_exception(f"mix_python_loopback_audio_failed {segment}", exc)
                raise RuntimeError(
                    f"Не удалось подмешать системный звук в сегмент {segment}. "
                    f"Итоговый файл не собран, чтобы не сохранить видео с потерянным/съехавшим звуком."
                ) from exc
        return prepared

    def mix_python_loopback_audio_into_segment(self, segment_path, wav_path, output_path):
        has_segment_audio = self.media_has_audio_stream(segment_path)
        audio_bitrate = self.get_recording_audio_bitrate_safe()

        # Сохраняем длительности ДО смешивания. В прошлых логах звук был длиннее
        # ускоренного видеопотока, а -shortest скрывал это, обрезая конец аудио.
        # Теперь такая ситуация остаётся видимой в JSONL даже если итоговый файл
        # после apad/-shortest формально имеет одинаковые границы дорожек.
        try:
            source_video_timing = self.probe_av_stream_timing(segment_path)
        except Exception as exc:
            source_video_timing = {"probe_error": repr(exc)}
        try:
            source_loopback_duration = self.get_media_duration(wav_path)
        except Exception as exc:
            source_loopback_duration = None
            source_video_timing["loopback_probe_error"] = repr(exc)
        try:
            video_duration_before = source_video_timing.get("video_duration")
            duration_difference_before = (
                float(source_loopback_duration) - float(video_duration_before)
                if source_loopback_duration is not None and video_duration_before is not None
                else None
            )
            self.problem_log_event("python_loopback_mix_timing_before", {
                "segment_path": segment_path,
                "wav_path": wav_path,
                "video_timing": source_video_timing,
                "loopback_wav_duration_seconds": source_loopback_duration,
                "audio_minus_video_seconds_before_mix": duration_difference_before,
                "has_segment_audio": has_segment_audio,
                "mix_policy": "audio is resampled, padded when short, and bounded by video with -shortest",
            })
        except Exception:
            pass

        command = [
            self.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(segment_path),
            "-i",
            str(wav_path),
        ]
        if has_segment_audio:
            command += [
                "-filter_complex",
                "[0:a:0]aresample=48000:async=1:first_pts=0,"
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a0];"
                "[1:a:0]aresample=48000:async=1:first_pts=0,"
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a1];"
                "[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
                "alimiter=limit=0.95,apad[aout]",
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                audio_bitrate,
                "-ar",
                "48000",
                "-ac",
                "2",
                "-shortest",
            ]
        else:
            command += [
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-af",
                "aresample=48000:async=1:first_pts=0,apad",
                "-c:a",
                "aac",
                "-b:a",
                audio_bitrate,
                "-ar",
                "48000",
                "-ac",
                "2",
                "-shortest",
            ]
        command += [str(output_path)]
        log_path = self.get_current_recording_log_path()
        with open(log_path, "a", encoding="utf-8", errors="ignore") as log:
            log.write("\n\n--- MIX PYTHON COREAUDIO LOOPBACK ---\n")
            log.write(self.command_to_log_text(command) + "\n")
            log.flush()
        result = self.run_managed_process(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=180,
            creationflags=self.creation_flags(),
        )
        if result.returncode != 0:
            raise RuntimeError(f"Не удалось подмешать системный звук CoreAudio. {(result.stderr or '').strip()}")
        try:
            mixed_timing = self.probe_av_stream_timing(output_path)
            self.problem_log_event("python_loopback_mix_timing_after", {
                "output_path": output_path,
                "mixed_timing": mixed_timing,
                "video_minus_audio_end_seconds": (
                    float(mixed_timing.get("video_end")) - float(mixed_timing.get("audio_end"))
                    if mixed_timing.get("video_end") is not None and mixed_timing.get("audio_end") is not None
                    else None
                ),
            })
        except Exception as exc:
            self.problem_log_event("python_loopback_mix_timing_after_probe_failed", {
                "output_path": output_path,
                "error": repr(exc),
            }, level="WARN")

    def probe_av_stream_timing(self, path):
        """Возвращает границы первой видео- и аудиодорожки через ffprobe."""
        ffprobe = self.get_ffprobe_path()
        if not ffprobe:
            return None
        result = self.run_managed_process(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=index,codec_type,start_time,duration,duration_ts,time_base:stream_tags=DURATION",
                "-of",
                "json",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=20,
            creationflags=self.creation_flags(),
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe не прочитал тайминги {path}: {(result.stderr or '').strip()}")
        parsed = json.loads(result.stdout or "{}")
        streams = parsed.get("streams") or []
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if video is None:
            raise RuntimeError(f"В сегменте отсутствует видеодорожка: {path}")

        def to_float(value):
            try:
                return float(value)
            except Exception:
                return None

        def duration_from_stream(stream):
            direct = to_float(stream.get("duration"))
            if direct is not None:
                return direct
            duration_ts = to_float(stream.get("duration_ts"))
            time_base = str(stream.get("time_base") or "")
            if duration_ts is not None and "/" in time_base:
                try:
                    numerator, denominator = time_base.split("/", 1)
                    denominator_value = float(denominator)
                    if denominator_value:
                        return duration_ts * float(numerator) / denominator_value
                except Exception:
                    pass
            tag_value = str((stream.get("tags") or {}).get("DURATION") or "").strip()
            if tag_value:
                try:
                    hours, minutes, seconds = tag_value.split(":", 2)
                    return int(hours) * 3600.0 + int(minutes) * 60.0 + float(seconds)
                except Exception:
                    pass
            return None

        format_duration = to_float((parsed.get("format") or {}).get("duration"))
        video_start = to_float(video.get("start_time")) or 0.0
        video_duration = duration_from_stream(video) or format_duration
        audio_start = (to_float(audio.get("start_time")) or 0.0) if audio else None
        audio_duration = duration_from_stream(audio) if audio else None
        return {
            "video_stream_index": video.get("index"),
            "audio_stream_index": audio.get("index") if audio else None,
            "video_start": video_start,
            "video_duration": video_duration,
            "video_end": (video_start + video_duration) if video_duration is not None else None,
            "audio_start": audio_start,
            "audio_duration": audio_duration,
            "audio_end": (audio_start + audio_duration) if audio_start is not None and audio_duration is not None else None,
        }

    def prepare_segments_with_aligned_audio(self, segments):
        """Выравнивает начало и конец аудио каждого сегмента по его видео."""
        prepared = []
        for segment in segments:
            timing = self.probe_av_stream_timing(segment)
            if not timing or timing.get("audio_stream_index") is None:
                prepared.append(segment)
                continue
            video_end = timing.get("video_end")
            audio_end = timing.get("audio_end")
            start_offset = (timing.get("audio_start") or 0.0) - (timing.get("video_start") or 0.0)
            end_gap = (video_end - audio_end) if video_end is not None and audio_end is not None else None
            if end_gap is not None and abs(end_gap) <= 0.08 and abs(start_offset) <= 0.08:
                prepared.append(segment)
                continue

            aligned_path = Path(segment).with_name(Path(segment).stem + "_av_aligned" + Path(segment).suffix)
            self.align_segment_audio_to_video(Path(segment), aligned_path, timing)
            self.validate_media_file(aligned_path, label="сегмент с выровненным звуком")
            verified = self.probe_av_stream_timing(aligned_path)
            verified_gap = None
            verified_start_offset = None
            if verified:
                if verified.get("video_end") is not None and verified.get("audio_end") is not None:
                    verified_gap = verified["video_end"] - verified["audio_end"]
                verified_start_offset = (verified.get("audio_start") or 0.0) - (verified.get("video_start") or 0.0)
            if verified_gap is None or abs(verified_gap) > 0.12 or abs(verified_start_offset or 0.0) > 0.12:
                raise RuntimeError(
                    "Не удалось выровнять звук сегмента: "
                    f"{segment}; gap={verified_gap}, start_offset={verified_start_offset}"
                )
            self.diagnostic_log("segment_audio_aligned", {
                "source": segment,
                "output": aligned_path,
                "before": timing,
                "after": verified,
            })
            prepared.append(aligned_path)
        return prepared

    def align_segment_audio_to_video(self, segment_path, output_path, timing=None):
        """Создаёт проверяемую копию сегмента с аудио точно по длине видео."""
        suffix = Path(output_path).suffix.lower()
        audio_bitrate = self.get_recording_audio_bitrate_safe()
        command = [
            self.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts",
            "-i",
            str(segment_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "copy",
            "-af",
            "aresample=48000:async=1:first_pts=0,apad",
        ]
        if suffix == ".avi":
            command += ["-c:a", "mp3", "-b:a", audio_bitrate, "-ar", "48000", "-ac", "2"]
        else:
            command += ["-c:a", "aac", "-b:a", audio_bitrate, "-ar", "48000", "-ac", "2"]
        command += ["-shortest", "-avoid_negative_ts", "make_zero"]
        if suffix in (".mp4", ".mov"):
            command += [
                "-movflags",
                "+faststart",
                "-video_track_timescale",
                str(self.MP4_VIDEO_TRACK_TIMESCALE),
            ]
            if self.should_use_hevc():
                command += ["-tag:v", "hvc1"]
        command += [str(output_path)]

        self.append_ffmpeg_problem_log("align segment audio start", command=command, extra={
            "source": segment_path,
            "output": output_path,
            "timing_before": timing,
        })
        result = self.run_managed_process(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=600,
            creationflags=self.creation_flags(),
        )
        self.append_ffmpeg_problem_log("align segment audio finish", command=command, extra={
            "return_code": result.returncode,
            "stderr": (result.stderr or "")[-4000:],
            "output": output_path,
        })
        if result.returncode != 0:
            raise RuntimeError(
                f"FFmpeg не выровнял звук сегмента {segment_path}. {(result.stderr or '').strip()}"
            )

    def collect_audio_sources(self):
        """Возвращает выбранные источники звука.

        Формат: (source_kind, device_name, volume_percent).
        Важно:
        - mic_default пишет микрофон Windows по умолчанию;
        - system_default пишет текущий звук компьютера через WASAPI loopback;
        - system_communication пишет устройство связи Windows для Telegram/звонков;
        - WASAPI loopback: <имя> пишет конкретный выход Windows.
        """
        audio_sources = []
        mic = self.normalize_saved_audio_choice(self.mic_device_var.get(), "mic")
        system = self.normalize_saved_audio_choice(self.system_device_var.get(), "system")

        if mic and mic != NO_AUDIO:
            if mic == MIC_AUDIO_DEFAULT:
                audio_sources.append(("mic_default", mic, self.mic_volume_var.get()))
            elif str(mic).startswith("WASAPI input: "):
                audio_sources.append(("mic_wasapi_device", str(mic).split(": ", 1)[1], self.mic_volume_var.get()))
            else:
                audio_sources.append(("mic", mic, self.mic_volume_var.get()))

        if system and system != NO_AUDIO:
            if self.should_capture_system_audio_with_python_loopback(system):
                audio_sources.append(("system_python_loopback", system, self.system_volume_var.get()))
            elif system in (SYSTEM_AUDIO_DEFAULT, SYSTEM_AUDIO_WASAPI):
                audio_sources.append(("system_default", system, self.system_volume_var.get()))
            elif system == SYSTEM_AUDIO_COMMUNICATION:
                audio_sources.append(("system_communication", system, self.system_volume_var.get()))
            elif self.is_wasapi_render_choice(system):
                audio_sources.append(("system_wasapi_device", self.strip_wasapi_render_prefix(system), self.system_volume_var.get()))
            elif system == mic:
                self.status_var.set("Микрофон и звук компьютера выбраны одним устройством. Для звука компьютера использую Windows default.")
                audio_sources.append(("system_default", SYSTEM_AUDIO_DEFAULT, self.system_volume_var.get()))
            elif self.is_valid_dshow_system_audio_source(system):
                audio_sources.append(("system", system, self.system_volume_var.get()))
            else:
                # Защита от старых сохранённых настроек: Focusrite Analogue 1+2,
                # микрофон и Line In — это не системный звук. Вместо тихой записи
                # автоматически берём WASAPI loopback по умолчанию.
                try:
                    self.system_device_var.set(SYSTEM_AUDIO_DEFAULT)
                except Exception:
                    pass
                self.status_var.set("Выбран вход вместо звука компьютера. Переключаю системный звук на Windows default.")
                audio_sources.append(("system_default", SYSTEM_AUDIO_DEFAULT, self.system_volume_var.get()))
        return audio_sources

    def build_wasapi_input_args(self, loopback=False, device_name="default"):
        device_name = str(device_name or "default").strip() or "default"
        args = [
            "-thread_queue_size",
            "2048",
            "-f",
            "wasapi",
        ]
        if loopback:
            args += ["-loopback", "1"]
        args += ["-i", device_name]
        return args

    def build_dshow_input_args(self, device_name, for_meter=False):
        args = [
            "-thread_queue_size",
            "2048",
            "-f",
            "dshow",
        ]
        if not for_meter:
            args += ["-audio_buffer_size", "200"]
        else:
            args += ["-audio_buffer_size", "50"]
        args += ["-i", f"audio={device_name}"]
        return args

    def build_audio_input_args(self, source_kind, device_name, for_meter=False):
        source_kind = str(source_kind or "")
        device_name = self.normalize_saved_audio_choice(device_name, "system" if "system" in source_kind else "mic")

        # Микрофон по умолчанию: сначала пробуем WASAPI default input.
        # Если FFmpeg без wasapi, откатываемся на найденный dshow-микрофон.
        if source_kind == "mic_default" or device_name == MIC_AUDIO_DEFAULT:
            if self.supports_wasapi_loopback():
                return self.build_wasapi_input_args(loopback=False, device_name="default")
            fallback = self.resolve_default_mic_dshow_device()
            if fallback and fallback != NO_AUDIO:
                return self.build_dshow_input_args(fallback, for_meter=for_meter)
            raise RuntimeError("Не найден микрофон по умолчанию.")

        if source_kind == "mic_wasapi_device":
            if self.supports_wasapi_loopback():
                return self.build_wasapi_input_args(loopback=False, device_name=device_name)
            raise RuntimeError("Выбран WASAPI-микрофон, но текущий FFmpeg не поддерживает wasapi.")

        # Конкретный WASAPI render endpoint. Это самый надёжный способ для
        # Bluetooth-наушников, Focusrite, Realtek и Telegram-звонков.
        if source_kind == "system_wasapi_device" or self.is_wasapi_render_choice(device_name):
            if self.is_wasapi_render_choice(device_name):
                device_name = self.strip_wasapi_render_prefix(device_name)
            if self.supports_wasapi_loopback():
                return self.build_wasapi_input_args(loopback=True, device_name=device_name)
            raise RuntimeError("Выбран WASAPI loopback-выход, но текущий FFmpeg не поддерживает wasapi.")

        # Звук компьютера по умолчанию: берём настоящее имя default render device
        # из Windows CoreAudio и передаём его в FFmpeg. Это надёжнее, чем просто
        # `-i default`, который на некоторых сборках FFmpeg даёт тишину.
        if source_kind in ("system_default", "system_wasapi") or device_name in (SYSTEM_AUDIO_DEFAULT, SYSTEM_AUDIO_WASAPI):
            if self.supports_wasapi_loopback():
                endpoint = self.resolve_default_wasapi_render_device("console") or "default"
                try:
                    self.log_message(f"System audio default Windows endpoint: {endpoint}; FFmpeg input: default")
                except Exception:
                    pass
                # Для пункта «по умолчанию Windows» используем именно default.
                # Это даёт FFmpeg актуальный render endpoint Windows даже после
                # переключения динамиков/наушников. Конкретные endpoints остаются
                # доступными отдельными пунктами WASAPI loopback.
                return self.build_wasapi_input_args(loopback=True, device_name="default")
            fallback = self.resolve_default_system_dshow_device()
            if fallback and fallback != NO_AUDIO:
                return self.build_dshow_input_args(fallback, for_meter=for_meter)
            raise RuntimeError(
                "Не найден источник системного звука. Для автозахвата нужен FFmpeg с wasapi "
                "или Stereo Mix / Стерео микшер / virtual-audio-capturer."
            )

        # Telegram и другие звонки иногда используют не обычное устройство вывода,
        # а Windows default communications render device. Даём отдельный пункт.
        if source_kind == "system_communication" or device_name == SYSTEM_AUDIO_COMMUNICATION:
            if self.supports_wasapi_loopback():
                endpoint = self.resolve_default_wasapi_render_device("communications") or self.resolve_default_wasapi_render_device("console") or "default"
                try:
                    self.log_message(f"System communication audio resolved to WASAPI endpoint: {endpoint}")
                except Exception:
                    pass
                return self.build_wasapi_input_args(loopback=True, device_name=endpoint)
            fallback = self.resolve_default_system_dshow_device()
            if fallback and fallback != NO_AUDIO:
                return self.build_dshow_input_args(fallback, for_meter=for_meter)
            raise RuntimeError("Не найдено устройство связи для системного звука.")

        return self.build_dshow_input_args(device_name, for_meter=for_meter)

    def append_audio_inputs_and_filters(self, cmd, audio_sources, first_audio_input_index=1):
        # system_python_loopback пишется отдельным Python-потоком в WAV и
        # подмешивается после остановки сегмента. В FFmpeg-входы его не добавляем.
        audio_sources = [s for s in audio_sources if str(s[0]) != "system_python_loopback"]

        for source_kind, device_name, _ in audio_sources:
            cmd += self.build_audio_input_args(source_kind, device_name, for_meter=False)

        cmd += ["-map", "0:v:0"]

        if audio_sources:
            filter_parts = []
            mixed_labels = []
            for offset, (source_kind, _, volume_percent) in enumerate(audio_sources):
                index = first_audio_input_index + offset
                volume = max(0, int(volume_percent)) / 100
                label = f"a{offset + 1}"

                if str(source_kind).startswith("mic"):
                    # Фикс бага «микрофон только в одном наушнике».
                    # Сначала приводим микрофон к mono, потом явно дублируем этот mono
                    # в левый и правый канал. Так голос всегда будет по центру.
                    filter_parts.append(
                        f"[{index}:a]aresample=48000:async=1:first_pts=0,"
                        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=mono,"
                        f"pan=stereo|c0=c0|c1=c0,"
                        f"volume={volume:.2f},alimiter=limit=0.95[{label}]"
                    )
                else:
                    # Системный звук не трогаем как mono: оставляем нормальное stereo.
                    filter_parts.append(
                        f"[{index}:a]aresample=48000:async=1:first_pts=0,"
                        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                        f"volume={volume:.2f},alimiter=limit=0.95[{label}]"
                    )
                mixed_labels.append(f"[{label}]")

            if len(audio_sources) == 1:
                filter_complex = filter_parts[0].replace("[a1]", "[aout]")
            else:
                filter_complex = ";".join(filter_parts)
                filter_complex += (
                    f";{''.join(mixed_labels)}"
                    f"amix=inputs={len(audio_sources)}:duration=longest:dropout_transition=0:normalize=0,"
                    f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[aout]"
                )

            cmd += ["-filter_complex", filter_complex, "-map", "[aout]"]

        return cmd
