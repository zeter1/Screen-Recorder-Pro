import math

from ..shared import *


class FinalizeMixin:
    @staticmethod
    def normalize_audio_bitrate_value(value, default="192k"):
        try:
            m = re.fullmatch(r"\s*(\d+)\s*k?\s*", str(value or ""), re.IGNORECASE)
            if m:
                kbps = int(m.group(1))
                if 32 <= kbps <= 512:
                    return f"{kbps}k"
        except Exception:
            pass
        return default

    def get_recording_audio_bitrate_safe(self):
        # Валидируем значение из settings.json: оно уходит прямо в -b:a FFmpeg.
        # Принимаем только "<число>k" в разумном диапазоне, иначе 192k.
        return self.normalize_audio_bitrate_value(getattr(self, "recording_audio_bitrate", "192k"))

    def recover_orphan_segments(self):
        """Ищет сегменты от упавшего прошлого сеанса и предлагает собрать их."""
        if (getattr(self, "is_recording", False) or getattr(self, "is_finalizing", False)
                or getattr(self, "is_starting", False) or getattr(self, "_exiting", False)):
            return
        try:
            roots = []
            try:
                folder = Path(self.output_folder.get().strip() or os.getcwd())
                roots.append(folder / ".recording_temp")
            except Exception:
                pass
            roots.append(TEMP_RECORDINGS_DIR)
            roots.append(DATA_DIR / "recording_temp_local")

            current = getattr(self, "temp_dir", None)
            orphans = []
            seen = set()
            for root in roots:
                try:
                    if not root or not root.exists():
                        continue
                    root_key = str(Path(root).resolve(strict=False)).casefold()
                    if root_key in seen:
                        continue
                    seen.add(root_key)
                    for session_dir in sorted(root.iterdir()):
                        if not session_dir.is_dir() or session_dir == current:
                            continue
                        try:
                            segs = self.select_recovery_segments(session_dir)
                        except Exception:
                            # Let the worker report this folder's exact validation error,
                            # without hiding it or skipping other recoverable sessions.
                            orphans.append((session_dir, ()))
                            continue
                        if segs:
                            orphans.append((session_dir, segs))
                except Exception:
                    continue

            if not orphans:
                return
            if not messagebox.askyesno(
                "Найдены незавершённые записи",
                f"Обнаружены недописанные записи из прошлого сеанса: {len(orphans)} шт.\n\n"
                "Собрать их в готовые видеофайлы?",
            ):
                return

            self.start_orphan_recovery(orphans)
        except Exception as exc:
            self.log_exception("recover_orphan_segments", exc)

    def start_orphan_recovery(self, orphans):
        # Confirmation dialogs process Tk events: recheck state after the dialog closed.
        if (self.is_recording or self.is_finalizing or getattr(self, "is_starting", False)
                or getattr(self, "_exiting", False)):
            return
        output_folder = Path(self.output_folder.get().strip() or os.getcwd())
        results = queue.Queue(maxsize=1)
        self._orphan_recovery_results = results
        self.is_finalizing = True
        self.status_var.set("Восстанавливаю запись; исходные файлы будут сохранены...")
        worker = threading.Thread(target=self.orphan_recovery_worker,
                                  args=(tuple(orphans), output_folder, results),
                                  name="orphan_recording_recovery", daemon=True)
        self._orphan_recovery_thread = worker
        try:
            worker.start()
        except Exception:
            self.is_finalizing = False
            self._orphan_recovery_results = None
            raise
        self._orphan_recovery_poll_job = self.root.after(100, lambda: self.poll_orphan_recovery(results))

    def orphan_recovery_worker(self, orphans, output_folder, results):
        recovered, errors = [], []
        for session_dir, segs in orphans:
            try:
                recovered.append(self.assemble_recovered_session(session_dir, segs, output_folder=output_folder))
            except Exception as exc:
                errors.append(f"{Path(session_dir).name}: {exc}")
                try:
                    self.log_exception("assemble_recovered_session", exc)
                except Exception:
                    pass
        results.put_nowait((recovered, errors))

    def poll_orphan_recovery(self, results):
        # Only the Tk thread owns this poller and UI changes; an old result cannot finish a new job.
        if getattr(self, "_orphan_recovery_results", None) is not results:
            return
        self._orphan_recovery_poll_job = None
        try:
            recovered, errors = results.get_nowait()
        except queue.Empty:
            self._orphan_recovery_poll_job = self.root.after(100, lambda: self.poll_orphan_recovery(results))
            return
        self._orphan_recovery_results = None
        self._orphan_recovery_thread = None
        self.is_finalizing = False
        self.status_var.set("Восстановление завершено." if not errors else "Восстановление завершено с ошибками; исходники сохранены.")
        if getattr(self, "_exit_after_finalize", False):
            self.exit_app()
            return
        message = "Исходные сегменты и звук сохранены в папках .recording_temp."
        if recovered:
            message += "\n\nВосстановлены файлы:\n" + "\n".join(str(path) for path in recovered)
        if errors:
            message += "\n\nНе удалось восстановить:\n" + "\n".join(errors)
            messagebox.showwarning("Восстановление", message)
        else:
            messagebox.showinfo("Восстановление завершено", message)

    @staticmethod
    def select_recovery_segments(session_dir):
        """Only numbered originals, never previously mixed/aligned derivatives."""
        by_index = {}
        for path in Path(session_dir).iterdir():
            match = re.fullmatch(r"segment_(\d+)\.(mp4|mkv|nut)", path.name, re.IGNORECASE)
            if not match or not path.is_file():
                continue
            if path.is_symlink():
                raise RuntimeError("Ссылка вместо исходного сегмента; восстановление остановлено.")
            index = int(match.group(1))
            if index in by_index:
                raise RuntimeError(f"Несколько исходников сегмента {index}; файлы сохранены.")
            by_index[index] = path
        if by_index and sorted(by_index) != list(range(1, max(by_index) + 1)):
            raise RuntimeError("Отсутствуют исходные сегменты; неполная запись не будет выдана за полную.")
        return [by_index[key] for key in sorted(by_index)]

    def inspect_recovery_video(self, path):
        self.validate_media_file(path, label="сегмент восстановления")
        result = self.run_managed_process(
            [self.get_ffprobe_path(), "-v", "error", "-count_frames", "-show_entries",
             "stream=codec_type,codec_name,width,height,nb_read_frames", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
            creationflags=self.creation_flags(),
        )
        if result.returncode != 0 or (result.stderr or "").strip():
            raise RuntimeError(f"Повреждён поток восстановления: {result.stderr}")
        streams = json.loads(result.stdout).get("streams") or []
        video = next(item for item in streams if item.get("codec_type") == "video")
        frames = int(video.get("nb_read_frames") or 0)
        timing = self.probe_av_stream_timing(path)
        duration = float((timing or {}).get("video_duration") or 0)
        if frames <= 0 or not math.isfinite(duration) or duration <= 0:
            raise RuntimeError("Не удалось проверить полноту сегмента восстановления.")
        return {"frames": frames, "duration": duration, "timing": timing,
                "audio": any(item.get("codec_type") == "audio" for item in streams),
                "signature": (video.get("codec_name"), video.get("width"), video.get("height"))}

    def load_recovery_loopback_plan(self, source, wav):
        sidecar = wav.with_suffix(".sync.json")
        if sidecar.exists():
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            if payload.get("schema") != "screen_recorder_loopback_recovery_v1":
                raise RuntimeError("Неизвестный формат данных восстановления звука.")
            for key, path in (("video", source), ("wav", wav)):
                stat = path.stat()
                expected = {"name": path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
                if payload.get(key) != expected:
                    raise RuntimeError("Сегмент или WAV изменён после сохранения синхронизации; исходники сохранены.")
        else:
            # Older sessions may carry the same anchors in their existing diagnostic report.
            report_path = LOGS_DIR / source.parent.name / "15_синхронизация_системного_звука.json"
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
            matches = [item for item in report.get("segments", [])
                       if Path(item.get("source_segment_path") or item.get("segment_path") or "").resolve() == source.resolve()
                       and Path(item.get("wav_path") or "").resolve() == wav.resolve()
                       and item.get("wav_size_bytes") == wav.stat().st_size
                       and item.get("status") not in {"incomplete", "error"} and not item.get("stop_error")]
            if len(matches) != 1:
                raise RuntimeError("Нет достоверной синхронизации системного звука. Видео и WAV сохранены для восстановления.")
            payload = matches[0]
        anchors = [payload.get("loopback_capture_start_perf"), payload.get("video_capture_start_perf")]
        if any(value is None or not math.isfinite(float(value)) for value in anchors):
            raise RuntimeError("Нет достоверных временных якорей системного звука; исходники сохранены.")
        return self.build_python_loopback_sync_plan(*anchors)

    def assemble_recovered_session(self, session_dir, segs, output_folder=None):
        session_dir = Path(session_dir)
        # Re-enumerate at the execution boundary: callers cannot inject a derivative or omit an original.
        segs = self.select_recovery_segments(session_dir)
        if not segs or not self.get_ffprobe_path():
            raise RuntimeError("Для проверки восстановления нужны исходные сегменты и ffprobe.")
        details = [self.inspect_recovery_video(path) for path in segs]
        if len({item["signature"] for item in details}) != 1:
            raise RuntimeError("Форматы сегментов различаются; исходники сохранены для отдельного восстановления.")
        wavs = [path.with_suffix(".system_loopback.wav") for path in segs]
        if any(wav.with_suffix(".sync.json").exists() and not wav.exists() for wav in wavs):
            raise RuntimeError("Отсутствует системный WAV, указанный в данных восстановления; исходники сохранены.")
        plans = [self.load_recovery_loopback_plan(path, wav) if wav.exists() else None
                 for path, wav in zip(segs, wavs)]
        needs_audio = any(item["audio"] for item in details) or any(plan is not None for plan in plans)
        out_dir = Path(output_folder) if output_folder is not None else Path(self.output_folder.get().strip() or os.getcwd())
        out_dir.mkdir(parents=True, exist_ok=True)
        # Stage on the destination volume; the recording temp root may be on another drive.
        stage = Path(tempfile.mkdtemp(prefix=".recovery_candidate_", dir=out_dir))
        prepared = []
        for index, (source, info, wav, plan) in enumerate(zip(segs, details, wavs, plans)):
            output = stage / f"part_{index:04d}.mkv"
            cmd = [self.ffmpeg_path, "-n", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source)]
            if plan is not None:
                from ..components.audio_loopback import WasapiLoopbackWaveRecorder
                if not WasapiLoopbackWaveRecorder.inspect_wav_file(wav).get("valid"):
                    raise RuntimeError("Повреждён системный WAV; исходники сохранены.")
                wav_duration = float(self.get_media_duration(wav))
                effective = max(0.0, wav_duration - plan["trim_loopback_start_seconds"]) + plan["delay_loopback_start_seconds"]
                if abs(effective - info["duration"]) > 1.0:
                    raise RuntimeError("Системный WAV не покрывает видеосегмент; исходники сохранены.")
                cmd += ["-i", str(wav)]
                audio_filter = self.build_python_loopback_audio_filter(plan)
                if info["audio"]:
                    graph = (f"[1:a:0]{audio_filter}[sys];"
                             "[0:a:0][sys]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95,apad[out]")
                else:
                    graph = f"[1:a:0]{audio_filter},apad[out]"
                cmd += ["-filter_complex", graph, "-map", "0:v:0", "-map", "[out]"]
            elif needs_audio and not info["audio"]:
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v:0", "-map", "1:a:0"]
            else:
                cmd += ["-map", "0:v:0"] + (["-map", "0:a:0", "-af", "apad"] if needs_audio else [])
            cmd += ["-c:v", "copy"]
            if needs_audio:
                cmd += ["-c:a", "aac", "-b:a", self.get_recording_audio_bitrate_safe(), "-ar", "48000", "-ac", "2", "-shortest"]
            cmd += [str(output)]
            result = self.run_managed_process(cmd, capture_output=True, text=True, timeout=1800, creationflags=self.creation_flags())
            if result.returncode != 0:
                raise RuntimeError(f"Не удалось подготовить сегмент восстановления: {result.stderr}")
            checked = self.inspect_recovery_video(output)
            if checked["frames"] != info["frames"] or abs(checked["duration"] - info["duration"]) > 0.15 or checked["audio"] != needs_audio:
                raise RuntimeError("Сегмент восстановления неполон; исходники сохранены.")
            prepared.append(output)
        list_path = stage / "segments.txt"
        with list_path.open("w", encoding="utf-8") as file:
            for path in prepared:
                safe = str(path).replace("\\", "/").replace("'", "'\\''")
                file.write(f"file '{safe}'\n")
        partial = stage / "result.mkv"
        result = self.run_managed_process(
            [self.ffmpeg_path, "-n", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(list_path), "-map", "0", "-c", "copy", str(partial)],
            capture_output=True, text=True, timeout=1800, creationflags=self.creation_flags())
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg не восстановил запись: {result.stderr}")
        final = self.inspect_recovery_video(partial)
        if (final["frames"] != sum(item["frames"] for item in details)
                or abs(final["duration"] - sum(item["duration"] for item in details)) > max(0.2, 0.1 * len(details))
                or final["audio"] != needs_audio):
            raise RuntimeError("Восстановленная запись не прошла проверку полноты; исходники сохранены.")
        if needs_audio:
            timing = final["timing"]
            if timing.get("audio_end") is None or abs(timing["audio_end"] - timing["video_end"]) > max(0.2, 0.1 * len(details)):
                raise RuntimeError("Звук восстановленной записи не покрывает видео; исходники сохранены.")
        out_dir = Path(output_folder) if output_folder is not None else Path(self.output_folder.get().strip() or os.getcwd())
        out_dir.mkdir(parents=True, exist_ok=True)
        base = f"Восстановленная запись {session_dir.name}"
        out_path = out_dir / f"{base}.mkv"
        counter = 2
        while out_path.exists():
            out_path = out_dir / f"{base} ({counter}).mkv"
            counter += 1
        # Windows rename refuses to overwrite an existing target, including a late collision.
        os.rename(partial, out_path)
        self.diagnostic_log("recovered_session_sources_retained", {"session_dir": session_dir, "output_path": out_path})
        return out_path

    @staticmethod
    def build_capture_recovery_plan(video_duration, target_duration, fps):
        """Конечное число clone-кадров; неизвестную длительность не угадываем."""
        video, target, rate = (float(value) for value in (video_duration, target_duration, fps))
        if not all(math.isfinite(value) and value > 0 for value in (video, target, rate)):
            raise ValueError("Неизвестна длительность или FPS сегмента для восстановления.")
        padding_frames = max(0, math.ceil((target - video) * rate - 1e-6))
        return {
            "source_video_seconds": video,
            "target_seconds": max(video, target),
            "fps": rate,
            "padding_frames": padding_frames,
            "expected_video_seconds": video + padding_frames / rate,
        }

    @staticmethod
    def capture_recovery_encoder_options(recording_command):
        # Берём уже выбранный кодек и его параметры из фактической команды,
        # а не из Tk-переменных в фоне. Границы принадлежат append_encoder_options.
        command = list(recording_command)
        start = command.index("-c:v")
        end = command.index("-max_muxing_queue_size", start)
        options = command[start:end]
        if options[1] not in {"h264_nvenc", "hevc_nvenc", "libx264", "libx265"}:
            raise ValueError("Неизвестный кодек исходного сегмента.")
        return options

    def build_capture_recovery_command(self, source, output, plan, recording_command):
        options = self.capture_recovery_encoder_options(recording_command)
        command = [
            self.ffmpeg_path, "-n", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
            "-vf", f"tpad=stop_mode=clone:stop={int(plan['padding_frames'])}",
            *options, "-fps_mode", "passthrough", "-c:a", "copy",
        ]
        if Path(output).suffix.lower() in {".mp4", ".mov"}:
            command += ["-movflags", "+faststart", "-video_track_timescale", str(self.MP4_VIDEO_TRACK_TIMESCALE)]
            if options[1] in {"hevc_nvenc", "libx265"}:
                command += ["-tag:v", "hvc1"]
        return command + [str(output)]

    def read_capture_recovery_tail(self, path, timing, fps):
        """Небольшой декодированный последний кадр, а не только PTS/код возврата."""
        seek = max(0.0, float(timing["video_end"]) - 3.0 / float(fps))
        result = self.run_managed_process(
            [self.ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error",
             "-ss", f"{seek:.6f}", "-i", str(path), "-map", "0:v:0",
             "-vf", "scale=64:36,format=gray", "-frames:v", "4",
             "-an", "-sn", "-f", "rawvideo", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            creationflags=self.creation_flags(),
        )
        data = result.stdout or b""
        if result.returncode != 0 or not data or len(data) % (64 * 36):
            error = (result.stderr or b"").decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"Не удалось декодировать последний кадр {path}: {error}")
        return data[-64 * 36:]

    def prepare_segments_with_capture_recovery(self, segments):
        """Дополняет только доказанные DXGI-разрывы, до -shortest при смешивании."""
        prepared = []
        recovery_segments = getattr(self, "capture_recovery_segments", {}) or {}
        starts = getattr(self, "recording_segment_start_perfs", {}) or {}
        for segment in segments:
            source_key = str(segment)
            metadata = recovery_segments.get(source_key)
            if metadata is None:
                prepared.append(segment)
                continue
            start = metadata.get("capture_start_perf")
            resume_path = metadata.get("resume_segment_path")
            end = starts.get(resume_path) if resume_path else metadata.get("stop_requested_perf")
            if start is None or end is None or float(end) <= float(start):
                raise RuntimeError("Нет достоверных границ DXGI-разрыва; исходные сегменты оставлены.")
            timing = self.probe_av_stream_timing(segment)
            if not timing:
                raise RuntimeError("Не удалось проверить видеодорожку до восстановления.")
            plan = self.build_capture_recovery_plan(
                timing.get("video_duration"), float(end) - float(start), metadata.get("fps"),
            )
            output = Path(segment)
            if plan["padding_frames"]:
                # Уникальная staged-копия: не перезаписываем ни исходник, ни
                # остаток прошлой попытки. На любой ошибке сохраняем оба файла.
                fd, output_name = tempfile.mkstemp(
                    prefix="capture_recovery_", suffix=Path(segment).suffix, dir=Path(segment).parent,
                )
                os.close(fd)
                output = Path(output_name)
                # Только что созданный пустой файл принадлежит этой операции.
                output.unlink()
                command = self.build_capture_recovery_command(segment, output, plan, metadata["ffmpeg_args"])
                self.append_ffmpeg_problem_log("finite capture recovery start", command=command, extra=plan)
                result = self.run_managed_process(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=max(180, int(plan["expected_video_seconds"] * 2 + 120)),
                    creationflags=self.creation_flags(),
                )
                self.append_ffmpeg_problem_log("finite capture recovery finish", command=command, extra={
                    "return_code": result.returncode, "stderr": (result.stderr or "")[-4000:],
                })
                if result.returncode != 0:
                    raise RuntimeError(f"Не удалось дополнить DXGI-разрыв: {(result.stderr or '')[-2000:]}")
                self.validate_media_file(output, label="сегмент с конечным стоп-кадром")
                verified = self.probe_av_stream_timing(output)
                actual = float((verified or {}).get("video_duration") or 0)
                if abs(actual - plan["expected_video_seconds"]) > max(0.12, 2.0 / plan["fps"]):
                    raise RuntimeError(f"Неверная длительность stop-frame: {actual}; ожидалось {plan['expected_video_seconds']}")
                before_tail = self.read_capture_recovery_tail(segment, timing, plan["fps"])
                after_tail = self.read_capture_recovery_tail(output, verified, plan["fps"])
                tail_error = sum(abs(a - b) for a, b in zip(before_tail, after_tail)) / len(before_tail)
                if tail_error > 6.0:
                    raise RuntimeError(f"Стоп-кадр не совпал с последним исходным кадром: mean_error={tail_error:.3f}")
            else:
                actual = float(timing["video_duration"])
                tail_error = 0.0

            # Только проверенная копия допускается к смешиванию/склейке. WAV
            # сохраняет исходный якорь, а не привязывается к новому имени файла.
            output_key = str(output)
            if output_key != source_key:
                wav_path = self.python_loopback_audio_segments.get(source_key)
                if wav_path is not None:
                    self.python_loopback_audio_segments[output_key] = wav_path
                sync_metadata = self.python_loopback_sync_metadata.pop(source_key, None)
                if sync_metadata is not None:
                    sync_metadata["source_segment_path"] = source_key
                    sync_metadata["segment_path"] = output_key
                    self.python_loopback_sync_metadata[output_key] = sync_metadata
            old_media = metadata.get("accounted_media_seconds", metadata.get("committed_media_seconds", 0.0))
            old_wall = metadata.get("accounted_wall_seconds", metadata.get("committed_wall_seconds", 0.0))
            self.recorded_seconds += actual - float(old_media)
            self.recorded_wall_seconds += (float(end) - float(start)) - float(old_wall)
            metadata.update({
                "status": "validated", "output": output_key, "plan": plan,
                "tail_mean_absolute_error": tail_error,
                "accounted_media_seconds": actual,
                "accounted_wall_seconds": float(end) - float(start),
            })
            self.problem_log_event("capture_recovery_segment_validated", {
                "source": source_key, "output": output_key, "plan": plan,
                "tail_mean_absolute_error": tail_error,
                "audio_policy": "keep captured audio; restart gaps cannot be recovered",
            }, level="WARN")
            prepared.append(output)
        return prepared

    def merge_segments(self):
        valid_segments = []
        invalid_segments = []
        for path in self.segments:
            try:
                self.validate_media_file(path, label="сегмент записи")
                valid_segments.append(path)
            except Exception as exc:
                invalid_segments.append((path, str(exc)))
                self.log_exception(f"invalid segment {path}", exc)

        if invalid_segments:
            details = "\n".join(f"{p}: {err}" for p, err in invalid_segments)
            self.append_problem_error("invalid_segments_before_merge", details)
            raise RuntimeError(
                "Один или несколько сегментов записи повреждены. Итоговый файл НЕ собран, "
                "чтобы не сохранить неполное видео. Временная папка оставлена для восстановления.\n"
                f"Лог: {self.current_log_path}\n{details}"
            )
        if not valid_segments:
            raise RuntimeError(f"Нет валидных записанных сегментов. Проверь лог: {self.current_log_path}")

        valid_segments = self.prepare_segments_with_capture_recovery(valid_segments)
        valid_segments = self.prepare_segments_with_python_loopback_audio(valid_segments)
        valid_segments = self.prepare_segments_with_aligned_audio(valid_segments)

        if len(valid_segments) == 1:
            self.finalize_single_segment(valid_segments[0])
            return

        list_path = self.temp_dir / "segments.txt"
        with open(list_path, "w", encoding="utf-8") as file:
            for segment in valid_segments:
                # Экранируем апостроф по правилам concat-демиксера: ' -> '\''
                safe_path = str(segment).replace("\\", "/").replace("'", "'\\''")
                file.write(f"file '{safe_path}'\n")

        ext = self.output_path.suffix.lower().replace(".", "")
        # +genpts на входе concat и -avoid_negative_ts на выходе убирают разрыв
        # PTS на стыке сегментов (каждый сегмент стартует с PTS≈0 — пауза/resume).
        command = [self.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning",
                   "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", str(list_path)]

        if ext == "avi":
            command += ["-c:v", "mpeg4", "-q:v", "3", "-c:a", "mp3", "-b:a", self.get_recording_audio_bitrate_safe(), "-ac", "2"]
        elif ext in ("mp4", "mov"):
            # Для MP4/MOV обязательно пересобираем контейнер и переносим moov в начало,
            # чтобы файл нормально открывался в проигрывателях Windows.
            command += [
                "-c", "copy",
                "-movflags", "+faststart",
                "-video_track_timescale", str(self.MP4_VIDEO_TRACK_TIMESCALE),
                "-avoid_negative_ts", "make_zero",
            ]
            if self.should_use_hevc():
                command += ["-tag:v", "hvc1"]  # иначе HEVC в MP4 не играет в плеерах Windows/Apple
        else:
            command += ["-c", "copy", "-avoid_negative_ts", "make_zero"]

        command += [str(self.output_path)]
        self.run_merge_command(command)

    def finalize_single_segment(self, segment):
        ext = self.output_path.suffix.lower().replace(".", "")
        segment_ext = Path(segment).suffix.lower().replace(".", "")

        # Надёжное сохранение важнее скорости: даже MKV теперь ремультиплексируем
        # через FFmpeg. Так финальный файл получает заново записанный контейнер,
        # а не просто переименованный временный сегмент с возможными проблемами
        # таймингов/trailer после аварийной остановки.
        command = [self.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning", "-fflags", "+genpts", "-i", str(segment)]

        if ext == "avi":
            command += ["-c:v", "mpeg4", "-q:v", "3", "-c:a", "mp3", "-b:a", self.get_recording_audio_bitrate_safe(), "-ac", "2"]
        elif ext in ("mp4", "mov"):
            command += [
                "-c", "copy",
                "-movflags", "+faststart",
                "-video_track_timescale", str(self.MP4_VIDEO_TRACK_TIMESCALE),
                "-avoid_negative_ts", "make_zero",
            ]
            if self.should_use_hevc():
                command += ["-tag:v", "hvc1"]
        else:
            command += ["-c", "copy"]

        command += [str(self.output_path)]
        self.run_merge_command(command)

    def run_merge_command(self, command):
        log_path = self.get_current_recording_log_path()
        merge_timeout = 1800
        self.append_ffmpeg_problem_log("merge command start", command=command, extra={
            "output_path": self.output_path,
            "segments": [str(p) for p in self.segments],
            "timeout_sec": merge_timeout,
        })
        self.diagnostic_log("merge_command_start", {
            "command": self.command_to_log_text(command),
            "output_path": self.output_path,
            "segments": [str(p) for p in self.segments],
            "timeout_sec": merge_timeout,
        })
        with open(log_path, "a", encoding="utf-8", errors="ignore") as log:
            log.write("\n\n--- MERGE ---\n")
            log.write(self.command_to_log_text(command) + "\n")
            log.write(f"merge_timeout_sec={merge_timeout}\n")
            log.flush()
            merge_started = time.perf_counter()
            process = self.start_managed_process(
                command,
                stdout=subprocess.DEVNULL,
                stderr=log,
                creationflags=self.creation_flags(),
            )
            try:
                return_code = process.wait(timeout=merge_timeout)
            except subprocess.TimeoutExpired as exc:
                log.write(f"merge_timeout_after={merge_timeout}s; terminating FFmpeg process tree\n")
                log.flush()
                self.terminate_process_tree(process, timeout=3.0, name="ffmpeg_merge")
                self.diagnostic_log("merge_command_timeout", {
                    "timeout_sec": merge_timeout,
                    "command": self.command_to_log_text(command),
                    "output_path": self.output_path,
                }, level="ERROR")
                raise RuntimeError(
                    f"FFmpeg завис при финальной сборке дольше {merge_timeout} секунд. "
                    f"Итоговый файл не считается сохранённым. Лог: {log_path}"
                ) from exc
            finally:
                self.unregister_child_process(process)
            log.write(f"merge_elapsed={time.perf_counter() - merge_started:.2f}s\n")
        self.append_ffmpeg_problem_log("merge command finish", command=command, extra={"return_code": return_code, "elapsed_sec": round(time.perf_counter() - merge_started, 3), "output_path": self.output_path})
        if return_code != 0:
            self.diagnostic_log("merge_command_failed", {
                "return_code": return_code,
                "command": self.command_to_log_text(command),
                "output_path": self.output_path,
            }, level="ERROR")
            self.append_problem_error("merge_command_failed", f"return_code={return_code}\ncommand={self.command_to_log_text(command)}\nlog={log_path}")
            raise RuntimeError(f"FFmpeg завершился с кодом {return_code}. Лог: {log_path}")
        self.diagnostic_log("merge_command_finish", {
            "return_code": return_code,
            "output_path": self.output_path,
            "exists": bool(self.output_path and Path(self.output_path).exists()),
            "size_bytes": Path(self.output_path).stat().st_size if self.output_path and Path(self.output_path).exists() else None,
        })
