from ..shared import *


class TimingMixin:
    @staticmethod
    def is_effective_fps_validation_applicable(video_duration, frame_count, target_fps):
        try:
            duration_seconds = max(0.0, float(video_duration))
            frames = max(0.0, float(frame_count))
            fps = max(0.0, float(target_fps))
        except Exception:
            return False
        if duration_seconds < 2.0 or fps <= 0.0:
            return False
        return frames >= max(2.0, fps * 1.5)

    def scan_recording_log_for_timing_warnings(self):
        """Ищет проблемы PTS/DTS во всех логах записи и в progress JSONL.

        Раньше проверялся только один текстовый файл. Из-за этого серьёзное
        предупреждение могло остаться в 02_ffmpeg... или в progress-потоке и не
        попасть в итоговый отчёт/04_ошибки_и_трейсы.txt.
        """
        result = {
            "source_files_scanned": [],
            "counts_by_file": {},
            "non_monotonic_dts_count": 0,
            "backward_timestamp_count": 0,
            "timestamp_discontinuity_count": 0,
            "invalid_timestamp_count": 0,
            "output_duplicate_count": 0,
            "output_drop_count": 0,
            "sample_lines": [],
            "severe_warning_count": 0,
        }
        try:
            candidate_paths = []
            for value in (
                getattr(self, "current_log_path", None),
                getattr(self, "session_ffmpeg_path", None),
            ):
                if value:
                    path = Path(value)
                    if path not in candidate_paths:
                        candidate_paths.append(path)

            patterns = {
                "non_monotonic_dts_count": re.compile(
                    r"non[- ]monotonic dts|non monotonically increasing dts",
                    re.IGNORECASE,
                ),
                "backward_timestamp_count": re.compile(
                    r"queue input is backward in time|backward timestamp|timestamp.*went backwards",
                    re.IGNORECASE,
                ),
                "timestamp_discontinuity_count": re.compile(
                    r"timestamp discontinuity|discontinuity in timestamps",
                    re.IGNORECASE,
                ),
                "invalid_timestamp_count": re.compile(
                    r"timestamps are unset|invalid dts|invalid pts|pts has no value|dts has no value",
                    re.IGNORECASE,
                ),
            }

            duplicate_values = []
            drop_values = []
            interesting = []
            max_bytes = 32 * 1024 * 1024

            for path in candidate_paths:
                if not path.exists() or not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                    with open(path, "rb") as file:
                        if size > max_bytes:
                            file.seek(size - max_bytes)
                        raw = file.read(max_bytes)
                    log_text = raw.decode("utf-8", errors="ignore")
                except Exception:
                    continue

                result["source_files_scanned"].append(str(path))
                file_counts = {}
                for key, pattern in patterns.items():
                    count = len(pattern.findall(log_text))
                    file_counts[key] = count
                    result[key] += count
                result["counts_by_file"][str(path)] = file_counts

                duplicate_values.extend(
                    int(value)
                    for value in re.findall(r"(?:^|\s)dup=\s*(\d+)", log_text)
                )
                drop_values.extend(
                    int(value)
                    for value in re.findall(r"(?:^|\s)drop=\s*(\d+)", log_text)
                )

                for line in log_text.splitlines():
                    low = line.lower()
                    if (
                        "non-monotonic dts" in low
                        or "non monotonically increasing dts" in low
                        or "backward in time" in low
                        or "timestamp discontinuity" in low
                        or "invalid dts" in low
                        or "invalid pts" in low
                        or "timestamps are unset" in low
                        or " dup=" in low
                        or " drop=" in low
                    ):
                        cleaned = line.strip()
                        if cleaned and cleaned not in interesting:
                            interesting.append(cleaned[:1500])
                        if len(interesting) >= 30:
                            break

            # Progress идёт по stdout, а FFmpeg warnings — по stderr. Счётчики
            # dup/drop поэтому надёжнее дополнительно брать из памяти и JSONL.
            try:
                with self.recording_progress_lock:
                    progress_samples = list(self.recording_progress_samples)
                for sample in progress_samples:
                    duplicate_values.append(int(sample.get("dup_frames") or 0))
                    drop_values.append(int(sample.get("drop_frames") or 0))
            except Exception:
                pass

            progress_path = getattr(self, "session_ffmpeg_progress_path", None)
            if progress_path:
                progress_path = Path(progress_path)
                if progress_path.exists() and progress_path.is_file():
                    result["source_files_scanned"].append(str(progress_path))
                    try:
                        with open(progress_path, "r", encoding="utf-8", errors="ignore") as file:
                            for line in file:
                                try:
                                    item = json.loads(line)
                                except Exception:
                                    continue
                                duplicate_values.append(int(item.get("dup_frames") or 0))
                                drop_values.append(int(item.get("drop_frames") or 0))
                    except Exception:
                        pass

            if duplicate_values:
                result["output_duplicate_count"] = max(duplicate_values)
            if drop_values:
                result["output_drop_count"] = max(drop_values)
            result["sample_lines"] = interesting
            result["severe_warning_count"] = int(
                result["non_monotonic_dts_count"]
                + result["backward_timestamp_count"]
                + result["timestamp_discontinuity_count"]
                + result["invalid_timestamp_count"]
            )

            # Серьёзные таймштамп-предупреждения должны быть видны сразу в 04,
            # а не прятаться только в длинном FFmpeg-логе.
            if result["severe_warning_count"] > 0:
                signature = (
                    f"non_monotonic={result['non_monotonic_dts_count']};"
                    f"backward={result['backward_timestamp_count']};"
                    f"discontinuity={result['timestamp_discontinuity_count']};"
                    f"invalid={result['invalid_timestamp_count']}"
                )
                details = json.dumps(result, ensure_ascii=False, indent=2)
                self.append_problem_error(
                    "ffmpeg_timestamp_warnings_detected",
                    f"signature={signature}\n{details}",
                )
        except Exception as exc:
            result["scan_error"] = repr(exc)
        return result

    def log_video_timing_summary(
        self,
        path,
        label="видео",
        expected_wall_seconds=None,
        expected_media_seconds=None,
        requested_wall_seconds=None,
    ):
        """Пишет диагностику FPS, PTS/DTS, разрывов и синхронизации аудио.

        ``expected_wall_seconds`` — сумма реального времени активных сегментов.
        ``expected_media_seconds`` — сумма ``out_time`` FFmpeg, то есть длительность
        таймлайна, которую показывает таймер программы и должен получить файл.
        ``requested_wall_seconds`` — полное время от нажатия «Запись» до «Стоп».
        Эти три шкалы намеренно не смешиваются.
        """
        try:
            path = Path(path)
            ffprobe = self.get_ffprobe_path()
            if not ffprobe or not path.exists():
                return None

            stream_cmd = [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,time_base,start_time,duration,duration_ts,nb_frames,sample_rate,channels",
                "-of",
                "json",
                str(path),
            ]
            stream_result = self.run_managed_process(
                stream_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=20,
                creationflags=self.creation_flags(),
            )
            try:
                parsed = json.loads(stream_result.stdout or "{}")
                streams = parsed.get("streams") or []
                video_streams = [item for item in streams if item.get("codec_type") == "video"]
                audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
                stream_data = video_streams[0] if video_streams else (streams[0] if streams else {})
                format_data = parsed.get("format") or {}
            except Exception:
                stream_data = {"parse_error": self._safe_log_value(stream_result.stdout, max_text=2000)}
                audio_streams = []
                format_data = {}

            packet_cmd = [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-read_intervals",
                "%+#30000",
                "-show_entries",
                "packet=pts_time,dts_time,duration_time",
                "-of",
                "csv=p=0",
                str(path),
            ]
            packet_result = self.run_managed_process(
                packet_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=45,
                creationflags=self.creation_flags(),
            )

            pts = []
            dts = []
            durations = []
            for line in (packet_result.stdout or "").splitlines():
                parts = [part.strip() for part in line.split(",")]
                if not parts:
                    continue
                try:
                    pts_value = float(parts[0])
                except Exception:
                    continue
                pts.append(pts_value)
                if len(parts) > 1:
                    try:
                        dts.append(float(parts[1]))
                    except Exception:
                        dts.append(None)
                if len(parts) > 2:
                    try:
                        durations.append(float(parts[2]))
                    except Exception:
                        pass

            intervals_ms = [
                round((pts[index] - pts[index - 1]) * 1000.0, 3)
                for index in range(1, len(pts))
            ]
            dts_intervals_ms = []
            numeric_dts = [value for value in dts if value is not None]
            if len(numeric_dts) == len(dts) and len(dts) > 1:
                dts_intervals_ms = [
                    round((dts[index] - dts[index - 1]) * 1000.0, 3)
                    for index in range(1, len(dts))
                ]

            def fraction_to_float(value):
                try:
                    value_text = str(value or "").strip()
                    if "/" in value_text:
                        left, right = value_text.split("/", 1)
                        denominator = float(right)
                        return float(left) / denominator if denominator else None
                    return float(value_text)
                except Exception:
                    return None

            def safe_float(value):
                try:
                    return float(value)
                except Exception:
                    return None

            target_fps = safe_float(getattr(self, "recording_effective_fps", None))
            requested_fps = safe_float(getattr(self, "recording_requested_fps", None))
            r_frame_rate = fraction_to_float(stream_data.get("r_frame_rate"))
            avg_frame_rate = fraction_to_float(stream_data.get("avg_frame_rate"))
            expected_fps = target_fps or avg_frame_rate or r_frame_rate
            expected_ms = (1000.0 / expected_fps) if expected_fps and expected_fps > 0 else None

            long_threshold = expected_ms * 1.5 if expected_ms else None
            severe_gap_threshold = max(50.0, expected_ms * 3.5) if expected_ms else 100.0
            short_threshold = expected_ms * 0.50 if expected_ms else None
            long_intervals = []
            severe_gap_intervals = []
            very_short_intervals = []
            non_positive_intervals = []
            for index, interval in enumerate(intervals_ms, start=1):
                item = {
                    "packet_index": index,
                    "prev_pts": pts[index - 1],
                    "pts": pts[index],
                    "interval_ms": interval,
                }
                if interval <= 0:
                    non_positive_intervals.append(item)
                if short_threshold is not None and 0 < interval < short_threshold:
                    very_short_intervals.append(item)
                if long_threshold is not None and interval > long_threshold:
                    if len(long_intervals) < 20:
                        long_intervals.append(item)
                if interval > severe_gap_threshold:
                    if len(severe_gap_intervals) < 20:
                        severe_gap_intervals.append(item)

            histogram = {}
            for interval in intervals_ms:
                key = f"{interval:.3f}"
                histogram[key] = histogram.get(key, 0) + 1
            top_intervals = [
                {"interval_ms": key, "count": count}
                for key, count in sorted(histogram.items(), key=lambda item: item[1], reverse=True)[:10]
            ]

            video_duration = safe_float(stream_data.get("duration"))
            format_duration = safe_float(format_data.get("duration"))
            frame_count = safe_float(stream_data.get("nb_frames"))
            effective_fps = (
                frame_count / video_duration
                if frame_count and video_duration and video_duration > 0
                else avg_frame_rate
            )
            fps_error_percent = (
                abs(effective_fps - target_fps) / target_fps * 100.0
                if effective_fps and target_fps and target_fps > 0
                else None
            )

            video_start = safe_float(stream_data.get("start_time")) or 0.0
            video_end = (video_start + video_duration) if video_duration is not None else None
            audio_video_timing = []
            for audio_stream in audio_streams:
                audio_start = safe_float(audio_stream.get("start_time")) or 0.0
                audio_duration = safe_float(audio_stream.get("duration"))
                audio_end = (audio_start + audio_duration) if audio_duration is not None else None
                start_offset = audio_start - video_start
                end_gap = (video_end - audio_end) if video_end is not None and audio_end is not None else None
                warning = None
                if abs(start_offset) > 0.10:
                    warning = "audio_and_video_start_times_differ"
                if end_gap is not None and end_gap > 0.25:
                    warning = "audio_ends_before_video"
                elif end_gap is not None and end_gap < -0.25:
                    warning = "audio_ends_after_video"
                audio_video_timing.append({
                    "audio_stream_index": audio_stream.get("index"),
                    "audio_codec": audio_stream.get("codec_name"),
                    "audio_start_seconds": round(audio_start, 3),
                    "audio_duration_seconds": round(audio_duration, 3) if audio_duration is not None else None,
                    "start_offset_from_video_seconds": round(start_offset, 3),
                    "video_minus_audio_end_seconds": round(end_gap, 3) if end_gap is not None else None,
                    "warning": warning,
                })

            wall_clock_check = None
            try:
                active_wall_seconds = float(expected_wall_seconds) if expected_wall_seconds is not None else None
            except Exception:
                active_wall_seconds = None
            try:
                active_media_seconds = float(expected_media_seconds) if expected_media_seconds is not None else None
            except Exception:
                active_media_seconds = None
            try:
                request_wall_seconds = float(requested_wall_seconds) if requested_wall_seconds is not None else None
            except Exception:
                request_wall_seconds = None

            duration_for_ratio = video_duration or format_duration
            if (
                (active_wall_seconds and active_wall_seconds > 0)
                or (active_media_seconds and active_media_seconds > 0)
                or (request_wall_seconds and request_wall_seconds > 0)
            ):
                wall_ratio = (
                    duration_for_ratio / active_wall_seconds
                    if duration_for_ratio and active_wall_seconds and active_wall_seconds > 0
                    else None
                )
                media_ratio = (
                    duration_for_ratio / active_media_seconds
                    if duration_for_ratio and active_media_seconds and active_media_seconds > 0
                    else None
                )
                wall_difference = (
                    duration_for_ratio - active_wall_seconds
                    if duration_for_ratio is not None and active_wall_seconds is not None
                    else None
                )
                media_difference = (
                    duration_for_ratio - active_media_seconds
                    if duration_for_ratio is not None and active_media_seconds is not None
                    else None
                )
                non_capture_overhead = (
                    request_wall_seconds - active_wall_seconds
                    if request_wall_seconds is not None and active_wall_seconds is not None
                    else None
                )
                first_frame_delay = None
                try:
                    if self.recording_first_frame_perf is not None and self.recording_start_requested_perf is not None:
                        first_frame_delay = float(self.recording_first_frame_perf) - float(self.recording_start_requested_perf)
                except Exception:
                    first_frame_delay = None

                warning = None
                interpretation = "Шкалы времени согласованы либо расхождение находится в пределах обычной погрешности старта/остановки."
                if media_ratio is not None and abs(media_ratio - 1.0) > 0.015:
                    warning = "output_duration_differs_from_ffmpeg_media_counter"
                    interpretation = (
                        "Файл отличается от накопленного out_time FFmpeg. Проверить паузы, concat, "
                        "финальный progress=end и длительности отдельных сегментов."
                    )
                elif wall_ratio is not None and wall_ratio < 0.985:
                    warning = "encoded_timeline_shorter_than_active_wall_clock"
                    interpretation = (
                        "Медиатаймлайн идёт медленнее монотонных часов. Это не повреждение контейнера, "
                        "но при устойчивом расхождении на длинных записях может означать, что источник "
                        "фактически отдаёт меньше кадров в секунду, чем задано setpts."
                    )
                elif wall_ratio is not None and wall_ratio > 1.015:
                    warning = "encoded_timeline_longer_than_active_wall_clock"
                    interpretation = (
                        "Медиатаймлайн длиннее реального времени активных сегментов. Проверить дублирование, "
                        "паузы и корректность первого/последнего progress-сэмпла."
                    )

                wall_clock_check = {
                    "time_reference": (
                        "separate_clocks: media=ffmpeg_progress_out_time; "
                        "active_wall=monotonic_clock_between_first_frame_and_stop; "
                        "request_wall=record_button_to_stop"
                    ),
                    "active_media_seconds_counter": round(active_media_seconds, 3) if active_media_seconds is not None else None,
                    "active_capture_wall_seconds": round(active_wall_seconds, 3) if active_wall_seconds is not None else None,
                    "recorded_wall_seconds": round(active_wall_seconds, 3) if active_wall_seconds is not None else None,
                    "recording_request_to_stop_seconds": round(request_wall_seconds, 3) if request_wall_seconds is not None else None,
                    "record_button_to_first_frame_seconds": round(first_frame_delay, 3) if first_frame_delay is not None else None,
                    "non_capture_overhead_seconds": round(non_capture_overhead, 3) if non_capture_overhead is not None else None,
                    "video_duration_seconds": round(video_duration, 3) if video_duration is not None else None,
                    "format_duration_seconds": round(format_duration, 3) if format_duration is not None else None,
                    "output_to_media_counter_ratio": round(media_ratio, 6) if media_ratio is not None else None,
                    "output_minus_media_counter_seconds": round(media_difference, 6) if media_difference is not None else None,
                    "duration_to_wall_ratio": round(wall_ratio, 6) if wall_ratio is not None else None,
                    "output_minus_active_wall_seconds": round(wall_difference, 6) if wall_difference is not None else None,
                    "duration_difference_seconds": round(wall_difference, 6) if wall_difference is not None else None,
                    "duration_difference_percent": round((wall_ratio - 1.0) * 100.0, 3) if wall_ratio is not None else None,
                    "timeline_vs_wall_drift_percent": round((1.0 - wall_ratio) * 100.0, 3) if wall_ratio is not None else None,
                    # Старое поле оставлено для совместимости, но больше не называется ошибкой скорости в пояснении.
                    "speed_error_percent": round((1.0 - wall_ratio) * 100.0, 3) if wall_ratio is not None else None,
                    "warning": warning,
                    "interpretation": interpretation,
                    "do_not_mark_file_corrupt_from_wall_clock_only": True,
                }

            ffmpeg_timing_warnings = self.scan_recording_log_for_timing_warnings()
            clock_alignment = self.summarize_capture_clock_alignment()
            if wall_clock_check is not None:
                wall_clock_check["ffmpeg_progress_clock_alignment"] = clock_alignment
            health_errors = []
            health_warnings = []
            if int(ffmpeg_timing_warnings.get("severe_warning_count") or 0) > 0:
                health_errors.append("ffmpeg_reported_non_monotonic_or_backward_timestamps")

            output_dup = int(ffmpeg_timing_warnings.get("output_duplicate_count") or 0)
            output_drop = int(ffmpeg_timing_warnings.get("output_drop_count") or 0)
            cadence_limit = max(5, int((frame_count or len(pts) or 1) * 0.005))
            # dup/drop — диагностический сигнал, но сам по себе не означает
            # повреждённый контейнер. Старый код из-за этих счётчиков переносил
            # полностью читаемый MP4 в «неполные результаты». Фатальными остаются
            # реальные нарушения PTS, большие разрывы и неверный итоговый FPS.
            if output_dup > cadence_limit:
                health_warnings.append("many_output_cfr_duplicates")
            if output_drop > cadence_limit:
                health_warnings.append("many_output_cfr_drops")
            fps_validation_applicable = self.is_effective_fps_validation_applicable(
                video_duration,
                frame_count,
                target_fps,
            )
            if fps_error_percent is not None and fps_error_percent > 3.0:
                if fps_validation_applicable:
                    health_errors.append("effective_fps_differs_from_target")
                else:
                    health_warnings.append("short_recording_effective_fps_inconclusive")
            if non_positive_intervals:
                health_errors.append("non_increasing_video_pts")
            if severe_gap_intervals:
                health_errors.append("large_video_frame_gaps")
            # Несколько необычных стартовых пакетов допустимы. Сотни сверхкоротких
            # интервалов, как в проблемной записи, означают пачку кадров в одной точке.
            short_limit = max(5, int(max(1, len(intervals_ms)) * 0.01))
            if len(very_short_intervals) > short_limit:
                health_errors.append("too_many_subframe_intervals")

            if health_errors:
                health_status = "error"
            elif health_warnings:
                health_status = "warning"
            else:
                health_status = "ok"

            timing_health = {
                "status": health_status,
                "errors": health_errors,
                "warnings": health_warnings,
                "target_fps": round(target_fps, 3) if target_fps else None,
                "requested_fps": round(requested_fps, 3) if requested_fps else None,
                "reported_r_frame_rate": round(r_frame_rate, 3) if r_frame_rate else None,
                "reported_avg_frame_rate": round(avg_frame_rate, 3) if avg_frame_rate else None,
                "effective_fps": round(effective_fps, 3) if effective_fps else None,
                "fps_error_percent": round(fps_error_percent, 3) if fps_error_percent is not None else None,
                "effective_fps_validation_applicable": fps_validation_applicable,
                "non_positive_interval_count": len(non_positive_intervals),
                "very_short_interval_count": len(very_short_intervals),
                "severe_gap_count": len(severe_gap_intervals),
                "severe_gap_threshold_ms": round(severe_gap_threshold, 3),
                "ffmpeg_timing_warnings": ffmpeg_timing_warnings,
                "output_cfr_duplicate_count": output_dup,
                "output_cfr_drop_count": output_drop,
                "output_cadence_warning_limit": cadence_limit,
            }

            summary = {
                "label": label,
                "path": str(path),
                "stream": stream_data,
                "audio_streams": audio_streams,
                "format": format_data,
                "target_fps": round(target_fps, 3) if target_fps else None,
                "requested_fps": round(requested_fps, 3) if requested_fps else None,
                "reported_r_frame_rate": round(r_frame_rate, 3) if r_frame_rate else None,
                "reported_avg_frame_rate": round(avg_frame_rate, 3) if avg_frame_rate else None,
                "effective_fps_from_frames_and_duration": round(effective_fps, 3) if effective_fps else None,
                "fps_error_percent": round(fps_error_percent, 3) if fps_error_percent is not None else None,
                "timing_health": timing_health,
                "wall_clock_check": wall_clock_check,
                "ffmpeg_progress_clock_alignment": clock_alignment,
                "audio_video_timing": audio_video_timing,
                "sampled_packets": len(pts),
                "expected_interval_ms": round(expected_ms, 3) if expected_ms else None,
                "min_interval_ms": min(intervals_ms) if intervals_ms else None,
                "max_interval_ms": max(intervals_ms) if intervals_ms else None,
                "non_positive_intervals": non_positive_intervals[:20],
                "very_short_intervals": very_short_intervals[:20],
                "severe_gap_intervals": severe_gap_intervals,
                "first_intervals_ms": intervals_ms[:20],
                "first_dts_intervals_ms": dts_intervals_ms[:20],
                "top_intervals": top_intervals,
                "long_intervals_over_1_5x": long_intervals,
                "first_packet_durations_ms": [round(value * 1000.0, 3) for value in durations[:20]],
                "content_cadence_note": (
                    "Ровные PTS подтверждают тайминг контейнера, но сами по себе не "
                    "доказывают отсутствие одинаковых изображений. Для ddagrab "
                    "дополнительно проверяются накопительные dup/drop счётчики FFmpeg."
                ),
            }
            self.last_video_timing_summary = summary
            try:
                timing_path = getattr(self, "session_timing_detail_path", None)
                if timing_path:
                    Path(timing_path).write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            except Exception as exc:
                self.diagnostic_log("video_timing_detail_write_failed", {
                    "error": repr(exc),
                    "path": getattr(self, "session_timing_detail_path", None),
                }, level="WARN")
            compact_summary = self._compact_timing_summary(summary) if hasattr(self, "_compact_timing_summary") else summary
            if isinstance(compact_summary, dict):
                compact_summary = dict(compact_summary)
                compact_summary["details_file"] = str(getattr(self, "session_timing_detail_path", None) or "")
            self.diagnostic_log("video_timing_summary", compact_summary)
            try:
                log_path = self.get_current_recording_log_path()
                with open(log_path, "a", encoding="utf-8", errors="ignore") as log:
                    log.write("\n\n--- VIDEO TIMING SUMMARY ---\n")
                    log.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            except Exception:
                pass

            if health_errors:
                details = json.dumps(timing_health, ensure_ascii=False, indent=2)
                self.append_problem_error("video_timing_validation_failed", details)
                self.problem_log_event(
                    "video_timing_validation_failed",
                    timing_health,
                    level="ERROR",
                )
            elif health_warnings:
                self.problem_log_event(
                    "video_timing_cadence_warning",
                    timing_health,
                    level="WARN",
                )
            return summary
        except Exception as exc:
            self.log_exception("log_video_timing_summary", exc)
            return None

    def validate_final_timing_summary(self, summary):
        """Не помечает запись успешной при сломанных FPS, PTS/DTS или звуке."""
        try:
            if not summary:
                return True

            for audio_check in summary.get("audio_video_timing") or []:
                warning = audio_check.get("warning")
                end_gap = audio_check.get("video_minus_audio_end_seconds")
                start_offset = audio_check.get("start_offset_from_video_seconds")
                if warning:
                    raise RuntimeError(
                        "Итоговый файл имеет рассинхрон видео и аудио: "
                        f"warning={warning}, start_offset={start_offset}, end_gap={end_gap}. "
                        "Файл не помечен как успешно сохранён, чтобы не скрыть обрыв звука."
                    )

            timing_health = summary.get("timing_health") or {}
            health_errors = list(timing_health.get("errors") or [])
            health_warnings = list(timing_health.get("warnings") or [])
            if timing_health.get("status") == "error" or health_errors:
                target_fps = timing_health.get("target_fps")
                effective_fps = timing_health.get("effective_fps")
                fps_error = timing_health.get("fps_error_percent")
                ffmpeg_warnings = (
                    (timing_health.get("ffmpeg_timing_warnings") or {}).get("severe_warning_count")
                )
                max_gap = summary.get("max_interval_ms")
                raise RuntimeError(
                    "Итоговое видео имеет повреждённый тайминг кадров: "
                    f"errors={health_errors}, warnings={health_warnings}, "
                    f"target_fps={target_fps}, effective_fps={effective_fps}, "
                    f"fps_error_percent={fps_error}, max_interval_ms={max_gap}, "
                    f"ffmpeg_timestamp_warnings={ffmpeg_warnings}. "
                    "Файл перемещён в неполные результаты, чтобы программа не выдала "
                    "реально повреждённую запись за успешно сохранённую."
                )

            wall = summary.get("wall_clock_check") or {}
            ratio = wall.get("duration_to_wall_ratio")
            recorded = wall.get("active_capture_wall_seconds")
            if recorded is None:
                recorded = wall.get("recorded_wall_seconds")
            video = wall.get("video_duration_seconds") or wall.get("format_duration_seconds")
            if ratio is None or recorded is None or video is None:
                return True
            recorded = float(recorded)
            video = float(video)
            ratio = float(ratio)
            if recorded < 5.0:
                return True
            diff = abs(recorded - video)
            # Подготовка и обратный отсчёт уже исключены. Оставляем 1 секунду
            # на погрешность старта источника/закрытия контейнера. Для длинных
            # файлов допускаем не более 2%, но максимум 5 секунд.
            allowed_diff = max(1.0, min(5.0, recorded * 0.02))
            if diff > allowed_diff:
                # Wall-clock начинается около запуска процесса, а первый реальный
                # кадр ddagrab может появиться позже. Это полезный диагностический
                # сигнал, но не доказательство повреждения читаемого MP4. Старый
                # код из-за одной стартовой задержки переносил нормальное видео в
                # «неполные результаты». Теперь сохраняем файл и подробно пишем
                # расхождение в AI-отчёт.
                warning_payload = {
                    "warning": "video_duration_differs_from_active_wall_clock",
                    "active_capture_seconds": recorded,
                    "video_duration_seconds": video,
                    "ratio": ratio,
                    "difference_seconds": diff,
                    "allowed_difference_seconds": allowed_diff,
                    "first_frame_perf_known": getattr(self, "recording_first_frame_perf", None) is not None,
                    "interpretation": (
                        "Не считать файл повреждённым только по wall-clock. "
                        "Проверять FFmpeg progress, PTS и одинаковые изображения."
                    ),
                }
                self.problem_log_event("duration_mismatch_warning", warning_payload, level="WARN")
                # После перехода на source-clock CFR устойчивый дрейф от 1.5%
                # уже является полезным диагностическим предупреждением. Файл
                # сохраняется, но запись в 04 позволяет сразу увидеть регрессию,
                # даже если PTS контейнера формально монотонны и ровны.
                drift_percent = abs(float(wall.get("timeline_vs_wall_drift_percent") or 0.0))
                media_ratio = wall.get("output_to_media_counter_ratio")
                try:
                    media_mismatch_percent = abs(float(media_ratio) - 1.0) * 100.0 if media_ratio is not None else 0.0
                except Exception:
                    media_mismatch_percent = 0.0
                warning_payload["absolute_timeline_drift_percent"] = drift_percent
                warning_payload["media_counter_mismatch_percent"] = media_mismatch_percent
                if drift_percent >= 5.0 or media_mismatch_percent >= 2.0:
                    self.append_problem_error(
                        "significant_duration_or_media_counter_mismatch",
                        json.dumps(warning_payload, ensure_ascii=False, indent=2),
                    )
                elif drift_percent >= 1.5:
                    self.append_problem_error(
                        "nonfatal_timeline_drift_warning",
                        json.dumps(warning_payload, ensure_ascii=False, indent=2),
                    )
        except RuntimeError:
            raise
        except Exception as exc:
            self.log_exception("validate_final_timing_summary", exc)
        return True
