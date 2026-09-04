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
        if getattr(self, "is_recording", False) or getattr(self, "is_finalizing", False):
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
                        segs = sorted(
                            list(session_dir.glob("segment_*.mkv"))
                            + list(session_dir.glob("segment_*.mp4"))
                            + list(session_dir.glob("segment_*.nut"))
                        )
                        segs = [s for s in segs if s.stat().st_size > 0]
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

            recovered = []
            for session_dir, segs in orphans:
                try:
                    out = self.assemble_recovered_session(session_dir, segs)
                    if out:
                        recovered.append(out)
                except Exception as exc:
                    self.log_exception("assemble_recovered_session", exc)
            if recovered:
                messagebox.showinfo(
                    "Восстановление завершено",
                    "Восстановлены файлы:\n" + "\n".join(str(p) for p in recovered),
                )
            else:
                messagebox.showwarning("Восстановление", "Не удалось собрать ни одного файла. Сегменты оставлены на месте.")
        except Exception as exc:
            self.log_exception("recover_orphan_segments", exc)

    def assemble_recovered_session(self, session_dir, segs):
        out_dir = Path(self.output_folder.get().strip() or os.getcwd())
        out_dir.mkdir(parents=True, exist_ok=True)
        base_name = f"Восстановленная запись {session_dir.name}"
        out_path = out_dir / f"{base_name}.mkv"
        counter = 2
        while out_path.exists() or out_path.with_name(out_path.stem + ".partial" + out_path.suffix).exists():
            out_path = out_dir / f"{base_name} ({counter}).mkv"
            counter += 1
        partial_path = out_path.with_name(out_path.stem + ".partial" + out_path.suffix)

        if len(segs) == 1:
            cmd = [
                self.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning",
                "-fflags", "+genpts", "-i", str(segs[0]),
                "-c", "copy", "-avoid_negative_ts", "make_zero", str(partial_path),
            ]
        else:
            list_path = session_dir / "recover_list.txt"
            with open(list_path, "w", encoding="utf-8") as file:
                for seg in segs:
                    safe = str(seg).replace("\\", "/").replace("'", "'\\''")
                    file.write(f"file '{safe}'\n")
            cmd = [
                self.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning",
                "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", str(list_path),
                "-c", "copy", "-avoid_negative_ts", "make_zero", str(partial_path),
            ]

        result = self.run_managed_process(
            cmd, capture_output=True, text=True, timeout=180, creationflags=self.creation_flags()
        )
        if result.returncode != 0:
            raise RuntimeError(
                "FFmpeg не восстановил запись: "
                f"код {result.returncode}; {(result.stderr or '').strip()}"
            )
        self.validate_media_file(partial_path, label="восстановленная запись")
        os.replace(partial_path, out_path)
        self.validate_media_file(out_path, label="восстановленная запись")

        # Исходные сегменты удаляем только после успешной сборки, проверки и
        # атомарного появления готового файла под окончательным именем.
        try:
            shutil.rmtree(session_dir)
        except Exception as exc:
            self.diagnostic_log(
                "recovered_session_cleanup_failed",
                {"session_dir": session_dir, "output_path": out_path, "error": repr(exc)},
                level="WARN",
            )
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
