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
