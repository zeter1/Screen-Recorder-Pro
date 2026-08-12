from ..shared import *


class SmoothnessDiagnosticsMixin:
    def collect_smoothness_environment_snapshot(self):
        """Одноразовый снимок окружения, влияющего на плавность записи."""
        snapshot = {
            "collected_at": datetime.now().isoformat(timespec="milliseconds"),
            "cpu_logical_count": os.cpu_count(),
            "monitor_count": detect_monitor_count(),
            "primary_refresh_hz": detect_primary_refresh_hz(),
            "capture_region": getattr(self, "capture_region", None),
            "psutil_available": PSUTIL_AVAILABLE,
        }
        try:
            snapshot["monitor_rects"] = self.get_monitor_rects()
            snapshot["virtual_screen_rect"] = self.get_virtual_screen_rect()
            snapshot["ddagrab_selected_monitor_rect"] = self.get_selected_monitor_rect_for_ddagrab()
        except Exception as exc:
            snapshot["monitor_geometry_error"] = repr(exc)
        try:
            if os.name == "nt":
                user32 = ctypes.windll.user32
                try:
                    snapshot["system_dpi"] = int(user32.GetDpiForSystem())
                except Exception:
                    pass
                snapshot["remote_session"] = bool(user32.GetSystemMetrics(0x1000))
        except Exception:
            pass
        if PSUTIL_AVAILABLE:
            try:
                vm = psutil.virtual_memory()
                snapshot["memory"] = {
                    "total_bytes": int(vm.total),
                    "available_bytes": int(vm.available),
                    "percent": vm.percent,
                }
            except Exception:
                pass
            try:
                snapshot["cpu_frequency"] = self._safe_log_value(psutil.cpu_freq())
            except Exception:
                pass
            try:
                proc = psutil.Process(os.getpid())
                snapshot["python_process_priority"] = proc.nice()
            except Exception:
                pass
        try:
            output_root = Path(
                self.recording_output_folder_snapshot
                or self.output_folder.get().strip()
                or os.getcwd()
            )
            usage = shutil.disk_usage(output_root)
            snapshot["output_disk"] = {
                "path": str(output_root),
                "free_bytes": int(usage.free),
                "used_bytes": int(usage.used),
                "total_bytes": int(usage.total),
            }
        except Exception as exc:
            snapshot["output_disk_error"] = repr(exc)

        def version_line(executable):
            try:
                result = subprocess.run(
                    [str(executable), "-version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=4,
                    creationflags=self.creation_flags(),
                    cwd=self.subprocess_cwd(),
                )
                lines = (result.stdout or result.stderr or "").splitlines()
                return {
                    "returncode": result.returncode,
                    "first_lines": lines[:8],
                }
            except Exception as exc:
                return {"error": repr(exc)}

        snapshot["ffmpeg_version"] = version_line(self.ffmpeg_path)
        ffprobe = self.get_ffprobe_path()
        snapshot["ffprobe_version"] = version_line(ffprobe) if ffprobe else {"available": False}
        snapshot["nvidia_gpu_start"] = self._nvidia_smi_snapshot()

        if os.name == "nt":
            try:
                power = subprocess.run(
                    ["powercfg", "/getactivescheme"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=4,
                    creationflags=self.creation_flags(),
                    cwd=self.subprocess_cwd(),
                )
                snapshot["windows_active_power_plan"] = {
                    "returncode": power.returncode,
                    "stdout": (power.stdout or "").strip(),
                    "stderr": (power.stderr or "").strip(),
                }
            except Exception as exc:
                snapshot["windows_active_power_plan"] = {"error": repr(exc)}
            try:
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers",
                ) as key:
                    try:
                        value, value_type = winreg.QueryValueEx(key, "HwSchMode")
                        snapshot["windows_hardware_accelerated_gpu_scheduling"] = {
                            "registry_value": value,
                            "registry_type": value_type,
                            "interpretation": {
                                1: "forced_off",
                                2: "forced_on",
                            }.get(value, "system_default_or_unknown"),
                        }
                    except FileNotFoundError:
                        snapshot["windows_hardware_accelerated_gpu_scheduling"] = {
                            "registry_value": None,
                            "interpretation": "system_default",
                        }
            except Exception as exc:
                snapshot["windows_hardware_accelerated_gpu_scheduling"] = {"error": repr(exc)}
        return snapshot

    def _append_specialized_jsonl(self, path, payload, max_bytes=12_000_000):
        """Пишет одну JSON-строку без падения записи при ошибке диска/лога."""
        try:
            if not path:
                return
            safe_payload = self._safe_log_value(payload, max_text=20000)
            line = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            self.append_limited_text_file(path, line, max_bytes=max_bytes)
        except Exception:
            pass

    @staticmethod
    def _progress_number(value, integer=False):
        try:
            raw = str(value or "").strip()
            if not raw or raw.upper() == "N/A":
                return None
            if raw.endswith("x"):
                raw = raw[:-1]
            return int(float(raw)) if integer else float(raw)
        except Exception:
            return None

    def start_ffmpeg_progress_reader(self, process, segment_path, capture_backend, process_launch_perf):
        """Читает `-progress pipe:1` и сохраняет временной ряд FFmpeg.

        Это главный лог для рывков: он показывает, в какую секунду просели fps или
        speed, перестал расти out_time, появились dup/drop либо FFmpeg долго не
        получал новый кадр. Чтение идёт в отдельном daemon-потоке и не блокирует GUI.
        """
        if process is None or getattr(process, "stdout", None) is None:
            return

        def worker():
            current = {}
            first_sample_for_process = True
            last_file_write_perf = 0.0
            last_written_dup = None
            last_written_drop = None
            anomaly_detail_until = 0.0
            try:
                while True:
                    raw = process.stdout.readline()
                    if not raw:
                        break
                    try:
                        line = raw.decode("utf-8", errors="replace").strip()
                    except Exception:
                        line = str(raw).strip()
                    if not line or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    current[key.strip()] = value.strip()
                    if key.strip() != "progress":
                        continue

                    now_perf = time.perf_counter()
                    frame = self._progress_number(current.get("frame"), integer=True)
                    out_time_us = self._progress_number(current.get("out_time_us"), integer=True)
                    out_time_ms = self._progress_number(current.get("out_time_ms"), integer=True)
                    out_time_seconds = None
                    if out_time_us is not None:
                        out_time_seconds = out_time_us / 1_000_000.0
                    elif out_time_ms is not None:
                        # В разных сборках FFmpeg out_time_ms фактически хранит микросекунды.
                        out_time_seconds = out_time_ms / 1_000_000.0
                    sample = {
                        "event": "ffmpeg_progress",
                        "wall_time": datetime.now().isoformat(timespec="milliseconds"),
                        "perf_counter": round(now_perf, 6),
                        "elapsed_from_record_button_seconds": round(
                            now_perf - float(self.recording_start_requested_perf or now_perf), 6
                        ),
                        "elapsed_from_ffmpeg_launch_seconds": round(now_perf - process_launch_perf, 6),
                        "recording_session_id": self.recording_session_id,
                        "segment_index": self.segment_index,
                        "segment_path": str(segment_path),
                        "capture_backend": capture_backend,
                        "ffmpeg_pid": getattr(process, "pid", None),
                        "frame": frame,
                        "fps": self._progress_number(current.get("fps")),
                        "stream_0_0_q": self._progress_number(current.get("stream_0_0_q")),
                        "bitrate_text": current.get("bitrate"),
                        "total_size_bytes": self._progress_number(current.get("total_size"), integer=True),
                        "out_time_seconds": round(out_time_seconds, 6) if out_time_seconds is not None else None,
                        "out_time_text": current.get("out_time"),
                        "dup_frames": self._progress_number(current.get("dup_frames"), integer=True),
                        "drop_frames": self._progress_number(current.get("drop_frames"), integer=True),
                        "speed": self._progress_number(current.get("speed")),
                        "progress": current.get("progress"),
                    }
                    with self.recording_progress_lock:
                        self.recording_progress_samples.append(sample)
                        # Ограничиваем RAM, сам JSONL остаётся полным до файлового лимита.
                        if len(self.recording_progress_samples) > 30000:
                            del self.recording_progress_samples[:5000]
                        self.recording_progress_latest = dict(sample)
                        if out_time_seconds is not None:
                            try:
                                self.current_segment_media_seconds = max(
                                    float(self.current_segment_media_seconds or 0.0),
                                    max(0.0, float(out_time_seconds)),
                                )
                                self.current_segment_last_progress_perf = now_perf
                            except Exception:
                                pass

                    if frame is not None and frame > 0:
                        # `-progress` приходит периодически, поэтому момент чтения
                        # первой строки позже первого кадра. Оцениваем реальный
                        # старт как now - уже накопленный out_time. Это устраняет
                        # ложные ошибки 16.0 сек wall-clock против 14.7 сек файла.
                        elapsed_video = out_time_seconds
                        if elapsed_video is None:
                            try:
                                elapsed_video = frame / float(self.recording_effective_fps or 60)
                            except Exception:
                                elapsed_video = 0.0
                        try:
                            elapsed_video = max(0.0, float(elapsed_video or 0.0))
                        except Exception:
                            elapsed_video = 0.0
                        estimated_capture_start_perf = max(
                            process_launch_perf,
                            now_perf - elapsed_video,
                        )
                        if self.recording_first_frame_perf is None:
                            self.recording_first_frame_perf = estimated_capture_start_perf
                        self.recording_last_frame_perf = now_perf
                        if self.segment_capture_started_perf is None:
                            self.segment_capture_started_perf = estimated_capture_start_perf
                            self.segment_first_progress_out_time = out_time_seconds
                            self.problem_log_event("ffmpeg_first_real_frame", {
                                "segment_path": str(segment_path),
                                "segment_index": self.segment_index,
                                "ffmpeg_pid": getattr(process, "pid", None),
                                "progress_observed_after_launch_seconds": round(now_perf - process_launch_perf, 6),
                                "estimated_capture_start_after_launch_seconds": round(
                                    estimated_capture_start_perf - process_launch_perf, 6
                                ),
                                "estimated_capture_start_after_record_button_seconds": round(
                                    estimated_capture_start_perf - float(
                                        self.recording_start_requested_perf or estimated_capture_start_perf
                                    ), 6
                                ),
                                "frame": frame,
                                "out_time_seconds": out_time_seconds,
                            })
                    # В файл пишем адаптивно: старт и аномалии подробно, обычный
                    # устойчивый участок примерно раз в 2 секунды. В RAM оставляем
                    # исходные progress samples для итоговых агрегатов.
                    try:
                        elapsed_launch = float(sample.get("elapsed_from_ffmpeg_launch_seconds") or 0.0)
                        speed_value = sample.get("speed")
                        fps_value = sample.get("fps")
                        dup_value = int(sample.get("dup_frames") or 0)
                        drop_value = int(sample.get("drop_frames") or 0)
                        target_value = float(self.recording_effective_fps or 0.0)
                        counter_changed = (
                            (last_written_dup is not None and dup_value != last_written_dup)
                            or (last_written_drop is not None and drop_value != last_written_drop)
                        )
                        anomaly = bool(
                            (speed_value is not None and float(speed_value) < 0.95)
                            or (target_value and fps_value is not None and elapsed_launch > 2.0 and float(fps_value) < target_value * 0.90)
                            or counter_changed
                        )
                        if anomaly:
                            anomaly_detail_until = max(anomaly_detail_until, now_perf + 10.0)
                        should_write_file = bool(
                            first_sample_for_process
                            or elapsed_launch <= 10.0
                            or current.get("progress") == "end"
                            or anomaly
                            or now_perf <= anomaly_detail_until
                            or now_perf - last_file_write_perf >= 2.0
                        )
                    except Exception:
                        should_write_file = True
                        dup_value = sample.get("dup_frames")
                        drop_value = sample.get("drop_frames")
                    if should_write_file:
                        self._append_specialized_jsonl(self.session_ffmpeg_progress_path, sample)
                        last_file_write_perf = now_perf
                        last_written_dup = dup_value
                        last_written_drop = drop_value
                    if first_sample_for_process:
                        first_sample_for_process = False
                        self.problem_log_event("ffmpeg_progress_started", sample)
                    current = {}
            except Exception as exc:
                self.log_exception("ffmpeg_progress_reader", exc)
            finally:
                end_payload = {
                    "event": "ffmpeg_progress_reader_finished",
                    "wall_time": datetime.now().isoformat(timespec="milliseconds"),
                    "segment_path": str(segment_path),
                    "ffmpeg_pid": getattr(process, "pid", None),
                    "returncode": process.poll(),
                }
                self._append_specialized_jsonl(self.session_ffmpeg_progress_path, end_payload)

        thread = threading.Thread(
            target=worker,
            name=f"ffmpeg_progress_reader_{getattr(process, 'pid', 'unknown')}",
            daemon=True,
        )
        self.recording_progress_threads.append(thread)
        thread.start()

    def _get_psutil_process(self, pid):
        if not PSUTIL_AVAILABLE or not pid:
            return None
        try:
            pid = int(pid)
            proc = self._psutil_processes.get(pid)
            if proc is None or not proc.is_running():
                proc = psutil.Process(pid)
                try:
                    proc.cpu_percent(None)
                except Exception:
                    pass
                self._psutil_processes[pid] = proc
            return proc
        except Exception:
            return None

    def _process_metrics(self, pid):
        proc = self._get_psutil_process(pid)
        if proc is None:
            return {"pid": pid, "available": False}
        result = {"pid": pid, "available": True}
        try:
            result["cpu_percent"] = proc.cpu_percent(None)
        except Exception:
            result["cpu_percent"] = None
        try:
            mem = proc.memory_info()
            result.update({
                "rss_bytes": int(mem.rss),
                "vms_bytes": int(mem.vms),
            })
        except Exception:
            pass
        try:
            io_data = proc.io_counters()
            result.update({
                "read_bytes_total": int(io_data.read_bytes),
                "write_bytes_total": int(io_data.write_bytes),
                "read_count_total": int(io_data.read_count),
                "write_count_total": int(io_data.write_count),
            })
        except Exception:
            pass
        try:
            result["thread_count"] = int(proc.num_threads())
        except Exception:
            pass
        try:
            result["status"] = proc.status()
        except Exception:
            pass
        return result

    def _nvidia_smi_snapshot(self):
        """Редкий GPU-снимок. Запускается не чаще раза в 15 секунд, чтобы диагностика не влияла на захват."""
        try:
            path = self._cached_nvidia_smi_path
            if path is None:
                path = shutil.which("nvidia-smi") or ""
                self._cached_nvidia_smi_path = path
            if not path:
                return None
            query = (
                "index,name,driver_version,pstate,utilization.gpu,utilization.memory,"
                "utilization.encoder,memory.used,memory.total,temperature.gpu,"
                "clocks.current.graphics,clocks.current.memory,power.draw,power.limit"
            )
            result = subprocess.run(
                [path, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.5,
                creationflags=self.creation_flags(),
                cwd=self.subprocess_cwd(),
            )
            if result.returncode != 0:
                return {"available": False, "error": (result.stderr or "").strip()[:1000]}
            fields = [
                "index", "name", "driver_version", "pstate", "gpu_util_percent",
                "memory_controller_util_percent", "encoder_util_percent", "memory_used_mb",
                "memory_total_mb", "temperature_c", "graphics_clock_mhz", "memory_clock_mhz",
                "power_draw_w", "power_limit_w",
            ]
            gpus = []
            for line in (result.stdout or "").splitlines():
                values = [part.strip() for part in line.split(",")]
                item = {fields[i]: values[i] if i < len(values) else None for i in range(len(fields))}
                for key in (
                    "index", "gpu_util_percent", "memory_controller_util_percent",
                    "encoder_util_percent", "memory_used_mb", "memory_total_mb",
                    "temperature_c", "graphics_clock_mhz", "memory_clock_mhz",
                    "power_draw_w", "power_limit_w",
                ):
                    value = item.get(key)
                    try:
                        item[key] = float(value) if "." in str(value) else int(value)
                    except Exception:
                        pass
                gpus.append(item)
            return {"available": True, "gpus": gpus}
        except Exception as exc:
            return {"available": False, "error": repr(exc)}

    def collect_recording_performance_sample(self):
        now_perf = time.perf_counter()
        sample = {
            "event": "system_performance_sample",
            "wall_time": datetime.now().isoformat(timespec="milliseconds"),
            "perf_counter": round(now_perf, 6),
            "elapsed_from_record_button_seconds": round(
                now_perf - float(self.recording_start_requested_perf or now_perf), 6
            ),
            "recording_session_id": self.recording_session_id,
            "is_recording": bool(self.is_recording),
            "is_paused": bool(self.is_paused),
            "is_finalizing": bool(self.is_finalizing),
            "python_thread_count": threading.active_count(),
            "latest_ffmpeg_progress": dict(getattr(self, "recording_progress_latest", {}) or {}),
        }
        if PSUTIL_AVAILABLE:
            try:
                sample["system_cpu_percent"] = psutil.cpu_percent(None)
            except Exception:
                sample["system_cpu_percent"] = None
            try:
                vm = psutil.virtual_memory()
                sample["memory"] = {
                    "percent": vm.percent,
                    "available_bytes": int(vm.available),
                    "used_bytes": int(vm.used),
                    "total_bytes": int(vm.total),
                }
                try:
                    swap = psutil.swap_memory()
                    sample["swap"] = {
                        "percent": float(swap.percent),
                        "used_bytes": int(swap.used),
                        "free_bytes": int(swap.free),
                        "total_bytes": int(swap.total),
                        "sin_bytes_total": int(getattr(swap, "sin", 0) or 0),
                        "sout_bytes_total": int(getattr(swap, "sout", 0) or 0),
                    }
                except Exception:
                    pass
            except Exception:
                pass
            try:
                disk = psutil.disk_io_counters()
                if disk:
                    sample["disk_io_total"] = {
                        "read_bytes": int(disk.read_bytes),
                        "write_bytes": int(disk.write_bytes),
                        "read_count": int(disk.read_count),
                        "write_count": int(disk.write_count),
                        "read_time_ms": int(disk.read_time),
                        "write_time_ms": int(disk.write_time),
                    }
            except Exception:
                pass
            sample["python_process"] = self._process_metrics(os.getpid())
            sample["ffmpeg_process"] = self._process_metrics(self.recording_ffmpeg_pid)
        else:
            sample["psutil_available"] = False

        try:
            output_root = Path(self.recording_output_folder_snapshot or os.getcwd())
            usage = shutil.disk_usage(output_root)
            sample["output_disk"] = {
                "path": str(output_root),
                "free_bytes": int(usage.free),
                "used_bytes": int(usage.used),
                "total_bytes": int(usage.total),
            }
        except Exception:
            pass

        if now_perf - float(self._last_gpu_sample_perf or 0.0) >= 15.0:
            self._last_gpu_sample_perf = now_perf
            sample["nvidia_gpu"] = self._nvidia_smi_snapshot()
        return sample

    def start_recording_performance_sampler(self):
        if not self.should_write_problem_logs():
            return
        thread = getattr(self, "recording_performance_thread", None)
        if thread is not None and thread.is_alive():
            return
        self.recording_performance_stop_event.clear()

        def worker():
            detailed_until = 0.0
            try:
                while not self.recording_performance_stop_event.is_set():
                    started = time.perf_counter()
                    sample = self.collect_recording_performance_sample()
                    with self.recording_performance_lock:
                        self.recording_performance_samples.append(sample)
                        if len(self.recording_performance_samples) > 20000:
                            del self.recording_performance_samples[:3000]
                    self._append_specialized_jsonl(self.session_performance_path, sample)
                    # Первые 10 секунд и периоды высокой нагрузки — раз в секунду.
                    # В нормальном состоянии достаточно одного sample раз в 3 сек.
                    try:
                        elapsed = float(sample.get("elapsed_from_record_button_seconds") or 0.0)
                        cpu = float(sample.get("system_cpu_percent") or 0.0)
                        memory = float(((sample.get("memory") or {}).get("percent")) or 0.0)
                        speed = (sample.get("latest_ffmpeg_progress") or {}).get("speed")
                        anomalous = bool(
                            cpu >= 90.0
                            or memory >= 90.0
                            or (speed is not None and float(speed) < 0.95)
                        )
                        if anomalous:
                            detailed_until = max(detailed_until, started + 10.0)
                        interval = 1.0 if elapsed <= 10.0 or started <= detailed_until else 3.0
                    except Exception:
                        interval = 2.0
                    wait_time = max(0.1, interval - (time.perf_counter() - started))
                    self.recording_performance_stop_event.wait(wait_time)
            except Exception as exc:
                self.log_exception("recording_performance_sampler", exc)

        self.recording_performance_thread = threading.Thread(
            target=worker,
            name="recording_performance_sampler",
            daemon=True,
        )
        self.recording_performance_thread.start()

    def stop_recording_performance_sampler(self):
        try:
            self.recording_performance_stop_event.set()
        except Exception:
            pass
        thread = getattr(self, "recording_performance_thread", None)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            try:
                thread.join(timeout=2.0)
            except Exception:
                pass
        self.recording_performance_thread = None
        for thread in list(getattr(self, "recording_progress_threads", []) or []):
            try:
                if thread.is_alive() and thread is not threading.current_thread():
                    thread.join(timeout=0.3)
            except Exception:
                pass

    @staticmethod
    def _numeric_values(items, key):
        values = []
        for item in items or []:
            try:
                value = item.get(key)
                if value is not None:
                    values.append(float(value))
            except Exception:
                pass
        return values

    def summarize_ffmpeg_progress(self):
        with self.recording_progress_lock:
            samples = list(self.recording_progress_samples)
        if not samples:
            return {"sample_count": 0, "status": "no_progress_samples"}

        fps_values = self._numeric_values(samples, "fps")
        speed_values = self._numeric_values(samples, "speed")
        steady_speed_values = []
        target_fps = float(self.recording_effective_fps or 0) or None
        stalls = []
        low_speed_samples = []
        low_fps_samples = []
        cadence_counter_changes = []
        per_process = {}
        previous = None

        for sample in samples:
            pid_key = str(sample.get("ffmpeg_pid"))
            proc = per_process.setdefault(pid_key, {
                "ffmpeg_pid": sample.get("ffmpeg_pid"),
                "segment_path": sample.get("segment_path"),
                "capture_backend": sample.get("capture_backend"),
                "sample_count": 0,
                "first_sample": sample,
                "last_sample": sample,
                "max_dup_frames": 0,
                "max_drop_frames": 0,
            })
            proc["sample_count"] += 1
            proc["last_sample"] = sample
            try:
                proc["max_dup_frames"] = max(proc["max_dup_frames"], int(sample.get("dup_frames") or 0))
                proc["max_drop_frames"] = max(proc["max_drop_frames"], int(sample.get("drop_frames") or 0))
            except Exception:
                pass

            try:
                speed = sample.get("speed")
                elapsed_for_speed = float(sample.get("elapsed_from_ffmpeg_launch_seconds") or 0.0)
                if speed is not None and elapsed_for_speed >= 3.0:
                    steady_speed_values.append(float(speed))
                if speed is not None and float(speed) < 0.95:
                    low_speed_samples.append({
                        "wall_time": sample.get("wall_time"),
                        "video_time_seconds": sample.get("out_time_seconds"),
                        "ffmpeg_pid": sample.get("ffmpeg_pid"),
                        "frame": sample.get("frame"),
                        "fps": sample.get("fps"),
                        "speed": speed,
                    })
            except Exception:
                pass
            try:
                fps = sample.get("fps")
                elapsed = float(sample.get("elapsed_from_ffmpeg_launch_seconds") or 0)
                if target_fps and elapsed > 2.0 and fps is not None and float(fps) < target_fps * 0.90:
                    low_fps_samples.append({
                        "wall_time": sample.get("wall_time"),
                        "video_time_seconds": sample.get("out_time_seconds"),
                        "ffmpeg_pid": sample.get("ffmpeg_pid"),
                        "frame": sample.get("frame"),
                        "fps": fps,
                        "target_fps": target_fps,
                        "speed": sample.get("speed"),
                    })
            except Exception:
                pass

            same_process_as_previous = (
                previous is not None
                and previous.get("ffmpeg_pid") == sample.get("ffmpeg_pid")
                and previous.get("segment_path") == sample.get("segment_path")
            )
            if same_process_as_previous:
                try:
                    wall_delta = float(sample["perf_counter"]) - float(previous["perf_counter"])
                    out_now = sample.get("out_time_seconds")
                    out_prev = previous.get("out_time_seconds")
                    frame_now = sample.get("frame")
                    frame_prev = previous.get("frame")
                    out_delta = None if out_now is None or out_prev is None else float(out_now) - float(out_prev)
                    frame_delta = None if frame_now is None or frame_prev is None else int(frame_now) - int(frame_prev)
                    if wall_delta >= 0.35 and (
                        (out_delta is not None and out_delta < wall_delta * 0.45)
                        or (frame_delta is not None and frame_delta <= 1)
                    ):
                        stalls.append({
                            "wall_time": sample.get("wall_time"),
                            "video_time_seconds": out_now,
                            "ffmpeg_pid": sample.get("ffmpeg_pid"),
                            "wall_delta_seconds": round(wall_delta, 4),
                            "out_time_delta_seconds": round(out_delta, 4) if out_delta is not None else None,
                            "frame_delta": frame_delta,
                            "fps": sample.get("fps"),
                            "speed": sample.get("speed"),
                            "dup_frames": sample.get("dup_frames"),
                            "drop_frames": sample.get("drop_frames"),
                        })
                    dup_delta = int(sample.get("dup_frames") or 0) - int(previous.get("dup_frames") or 0)
                    drop_delta = int(sample.get("drop_frames") or 0) - int(previous.get("drop_frames") or 0)
                    if dup_delta > 0 or drop_delta > 0:
                        cadence_counter_changes.append({
                            "wall_time": sample.get("wall_time"),
                            "video_time_seconds": sample.get("out_time_seconds"),
                            "ffmpeg_pid": sample.get("ffmpeg_pid"),
                            "dup_delta": dup_delta,
                            "drop_delta": drop_delta,
                            "dup_total": sample.get("dup_frames"),
                            "drop_total": sample.get("drop_frames"),
                            "fps": sample.get("fps"),
                            "speed": sample.get("speed"),
                        })
                except Exception:
                    pass
            previous = sample

        process_summaries = list(per_process.values())
        total_dup = sum(int(item.get("max_dup_frames") or 0) for item in process_summaries)
        total_drop = sum(int(item.get("max_drop_frames") or 0) for item in process_summaries)
        last = samples[-1]
        frame_values = self._numeric_values(samples, "frame")
        out_times = self._numeric_values(samples, "out_time_seconds")
        return {
            "sample_count": len(samples),
            "process_count": len(process_summaries),
            "per_process": process_summaries,
            "first_sample": samples[0],
            "last_sample": last,
            "fps_min": min(fps_values) if fps_values else None,
            "fps_max": max(fps_values) if fps_values else None,
            "fps_mean": statistics.fmean(fps_values) if fps_values else None,
            "fps_median": statistics.median(fps_values) if fps_values else None,
            "speed_min": min(speed_values) if speed_values else None,
            "speed_max": max(speed_values) if speed_values else None,
            "speed_mean": statistics.fmean(speed_values) if speed_values else None,
            "speed_median": statistics.median(speed_values) if speed_values else None,
            "steady_speed_min_after_3s": min(steady_speed_values) if steady_speed_values else None,
            "steady_speed_mean_after_3s": statistics.fmean(steady_speed_values) if steady_speed_values else None,
            "steady_speed_median_after_3s": statistics.median(steady_speed_values) if steady_speed_values else None,
            "last_frame_value_seen": int(frame_values[-1]) if frame_values else None,
            "last_out_time_value_seen_seconds": out_times[-1] if out_times else None,
            "total_dup_frames_across_segments": total_dup,
            "total_drop_frames_across_segments": total_drop,
            "cadence_counter_change_events_count": len(cadence_counter_changes),
            "cadence_counter_change_events_first_200": cadence_counter_changes[:200],
            "possible_progress_stalls_count": len(stalls),
            "possible_progress_stalls_first_200": stalls[:200],
            "speed_below_0_95_count": len(low_speed_samples),
            "speed_below_0_95_first_200": low_speed_samples[:200],
            "fps_below_90_percent_of_target_count": len(low_fps_samples),
            "fps_below_90_percent_of_target_first_200": low_fps_samples[:200],
            "interpretation": (
                "Progress FFmpeg показывает способность обрабатывать поток в реальном времени. "
                "Он не доказывает, что источник ddagrab отдавал новое изображение на каждом кадре."
            ),
        }

    def summarize_capture_clock_alignment(self):
        """Сравнивает медиатаймлайн FFmpeg с монотонными часами по каждому сегменту.

        Это ключевая диагностика для длинных записей. Она исключает задержку запуска
        и завершения: сравниваются только первый и последний пригодные progress-сэмплы
        внутри одного FFmpeg-процесса. Результат не объявляет потерю кадров сам по
        себе, а показывает устойчивый дрейф, который надо сопоставлять с визуальным
        анализом и счётчиками dup/drop.
        """
        with self.recording_progress_lock:
            samples = list(self.recording_progress_samples)
        grouped = {}
        for sample in samples:
            try:
                if sample.get("frame") is None or sample.get("out_time_seconds") is None:
                    continue
                if sample.get("perf_counter") is None:
                    continue
                key = (str(sample.get("ffmpeg_pid")), str(sample.get("segment_path")))
                grouped.setdefault(key, []).append(sample)
            except Exception:
                pass

        segments = []
        total_wall_span = 0.0
        total_media_span = 0.0
        total_frame_span = 0
        for (_pid, _path), items in grouped.items():
            try:
                items.sort(key=lambda item: float(item.get("perf_counter") or 0.0))
                valid = [item for item in items if float(item.get("out_time_seconds") or 0.0) >= 0.0]
                if len(valid) < 2:
                    continue
                # Первый progress-сэмпл иногда содержит почти нулевой out_time и
                # сильнее зависит от старта. По возможности начинаем с >=0.5 сек.
                first = next(
                    (item for item in valid if float(item.get("out_time_seconds") or 0.0) >= 0.5),
                    valid[0],
                )
                last = valid[-1]
                wall_span = float(last.get("perf_counter")) - float(first.get("perf_counter"))
                media_span = float(last.get("out_time_seconds")) - float(first.get("out_time_seconds"))
                frame_span = int(last.get("frame") or 0) - int(first.get("frame") or 0)
                if wall_span <= 0.25 or media_span < 0:
                    continue
                media_to_wall_ratio = media_span / wall_span
                delivery_fps_wall = frame_span / wall_span if frame_span > 0 else None
                encoded_fps_media = frame_span / media_span if frame_span > 0 and media_span > 0 else None
                drift_percent = (1.0 - media_to_wall_ratio) * 100.0
                if wall_span < 5.0:
                    classification = "short_segment_insufficient_for_drift_conclusion"
                elif media_to_wall_ratio < 0.985:
                    classification = "media_timeline_lags_wall_clock"
                elif media_to_wall_ratio > 1.015:
                    classification = "media_timeline_leads_wall_clock"
                else:
                    classification = "media_and_wall_clocks_aligned"
                item = {
                    "ffmpeg_pid": first.get("ffmpeg_pid"),
                    "segment_path": first.get("segment_path"),
                    "capture_backend": first.get("capture_backend"),
                    "first_sample": first,
                    "last_sample": last,
                    "wall_span_seconds": round(wall_span, 6),
                    "media_span_seconds": round(media_span, 6),
                    "frame_span": frame_span,
                    "media_to_wall_ratio": round(media_to_wall_ratio, 6),
                    "timeline_lag_vs_wall_percent": round(drift_percent, 4),
                    "media_minus_wall_seconds": round(media_span - wall_span, 6),
                    "frame_delivery_fps_per_wall_second": round(delivery_fps_wall, 6) if delivery_fps_wall is not None else None,
                    "encoded_fps_per_media_second": round(encoded_fps_media, 6) if encoded_fps_media is not None else None,
                    "classification": classification,
                }
                segments.append(item)
                total_wall_span += wall_span
                total_media_span += media_span
                total_frame_span += max(0, frame_span)
            except Exception:
                continue

        aggregate_ratio = total_media_span / total_wall_span if total_wall_span > 0 else None
        aggregate = {
            "segment_count": len(segments),
            "total_wall_span_seconds": round(total_wall_span, 6) if segments else None,
            "total_media_span_seconds": round(total_media_span, 6) if segments else None,
            "total_frame_span": total_frame_span if segments else None,
            "media_to_wall_ratio": round(aggregate_ratio, 6) if aggregate_ratio is not None else None,
            "timeline_lag_vs_wall_percent": round((1.0 - aggregate_ratio) * 100.0, 4) if aggregate_ratio is not None else None,
            "frame_delivery_fps_per_wall_second": round(total_frame_span / total_wall_span, 6)
            if total_wall_span > 0 and total_frame_span > 0 else None,
            "encoded_fps_per_media_second": round(total_frame_span / total_media_span, 6)
            if total_media_span > 0 and total_frame_span > 0 else None,
        }
        if aggregate_ratio is None:
            status = "insufficient_progress_samples"
        elif aggregate_ratio < 0.985:
            status = "persistent_timeline_lag_detected"
        elif aggregate_ratio > 1.015:
            status = "persistent_timeline_lead_detected"
        else:
            status = "clocks_aligned"
        return {
            "status": status,
            "method": "first_to_last_ffmpeg_progress_samples_per_process",
            "aggregate": aggregate,
            "segments": segments,
            "media_counter_total_committed_seconds": round(float(self.recorded_seconds or 0.0), 6),
            "wall_counter_total_committed_seconds": round(float(self.recorded_wall_seconds or 0.0), 6),
            "interpretation": (
                "encoded_fps_per_media_second показывает FPS готового таймлайна; "
                "frame_delivery_fps_per_wall_second показывает число выходных кадров FFmpeg за секунду "
                "реального времени. В сборке ddagrab-wallclock-poll-v8 media_to_wall_ratio должен быть близок "
                "к 1.0: устойчивое отличие более 1.5% считается регрессией временной шкалы."
            ),
            "not_proof_by_itself": (
                "Дрейф часов не доказывает визуальные рывки. Проверять вместе с содержимым кадров, "
                "dup/drop, progress stalls и нагрузкой системы."
            ),
        }

    def summarize_performance_samples(self):
        with self.recording_performance_lock:
            samples = list(self.recording_performance_samples)
        if not samples:
            return {"sample_count": 0, "status": "no_performance_samples"}

        def nested_value(sample, path):
            value = sample
            for key in path:
                if not isinstance(value, dict):
                    return None
                value = value.get(key)
            return value

        def nested_values(path):
            result = []
            for sample in samples:
                try:
                    value = nested_value(sample, path)
                    if value is not None:
                        result.append(float(value))
                except Exception:
                    pass
            return result

        def stats(values):
            if not values:
                return None
            return {
                "min": min(values),
                "max": max(values),
                "mean": statistics.fmean(values),
                "median": statistics.median(values),
            }

        def peak_sample(path):
            best = None
            best_value = None
            for sample in samples:
                try:
                    value = nested_value(sample, path)
                    if value is None:
                        continue
                    value = float(value)
                    if best_value is None or value > best_value:
                        best_value = value
                        best = sample
                except Exception:
                    pass
            return {"value": best_value, "sample": best} if best is not None else None

        cpu = nested_values(["system_cpu_percent"])
        ram = nested_values(["memory", "percent"])
        swap_percent = nested_values(["swap", "percent"])
        swap_used = nested_values(["swap", "used_bytes"])
        py_cpu = nested_values(["python_process", "cpu_percent"])
        ff_cpu = nested_values(["ffmpeg_process", "cpu_percent"])
        ff_rss = nested_values(["ffmpeg_process", "rss_bytes"])
        free_disk = nested_values(["output_disk", "free_bytes"])
        gpu_util = []
        encoder_util = []
        gpu_mem = []
        peak_gpu_sample = None
        peak_gpu_value = None
        peak_encoder_sample = None
        peak_encoder_value = None
        for sample in samples:
            try:
                gpu = sample.get("nvidia_gpu") or {}
                for item in gpu.get("gpus") or []:
                    if item.get("gpu_util_percent") is not None:
                        value = float(item["gpu_util_percent"])
                        gpu_util.append(value)
                        if peak_gpu_value is None or value > peak_gpu_value:
                            peak_gpu_value = value
                            peak_gpu_sample = {"sample": sample, "gpu": item}
                    if item.get("encoder_util_percent") is not None:
                        value = float(item["encoder_util_percent"])
                        encoder_util.append(value)
                        if peak_encoder_value is None or value > peak_encoder_value:
                            peak_encoder_value = value
                            peak_encoder_sample = {"sample": sample, "gpu": item}
                    if item.get("memory_used_mb") is not None:
                        gpu_mem.append(float(item["memory_used_mb"]))
            except Exception:
                pass

        disk_write_rates = []
        ffmpeg_write_rates = []
        previous = None
        for sample in samples:
            if previous is not None:
                try:
                    dt = float(sample.get("perf_counter")) - float(previous.get("perf_counter"))
                except Exception:
                    dt = 0.0
                if dt > 0:
                    for path, target in (
                        (["disk_io_total", "write_bytes"], disk_write_rates),
                        (["ffmpeg_process", "write_bytes_total"], ffmpeg_write_rates),
                    ):
                        try:
                            current = nested_value(sample, path)
                            old = nested_value(previous, path)
                            if current is not None and old is not None:
                                rate = max(0.0, (float(current) - float(old)) / dt)
                                target.append({
                                    "bytes_per_second": rate,
                                    "wall_time": sample.get("wall_time"),
                                    "video_time_seconds": (sample.get("latest_ffmpeg_progress") or {}).get("out_time_seconds"),
                                    "sample": sample,
                                })
                        except Exception:
                            pass
            previous = sample

        peak_disk_rate = max(disk_write_rates, key=lambda x: x["bytes_per_second"]) if disk_write_rates else None
        peak_ffmpeg_rate = max(ffmpeg_write_rates, key=lambda x: x["bytes_per_second"]) if ffmpeg_write_rates else None
        swap_in_delta = None
        swap_out_delta = None
        try:
            first_sin = nested_value(samples[0], ["swap", "sin_bytes_total"])
            last_sin = nested_value(samples[-1], ["swap", "sin_bytes_total"])
            first_sout = nested_value(samples[0], ["swap", "sout_bytes_total"])
            last_sout = nested_value(samples[-1], ["swap", "sout_bytes_total"])
            if first_sin is not None and last_sin is not None:
                swap_in_delta = max(0, int(last_sin) - int(first_sin))
            if first_sout is not None and last_sout is not None:
                swap_out_delta = max(0, int(last_sout) - int(first_sout))
        except Exception:
            pass
        return {
            "sample_count": len(samples),
            "sampling_interval_strategy": "1s during first 10s/anomalies, 3s during steady recording",
            "system_cpu_percent": stats(cpu),
            "memory_percent": stats(ram),
            "swap_percent": stats(swap_percent),
            "swap_used_bytes": stats(swap_used),
            "swap_in_bytes_during_recording": swap_in_delta,
            "swap_out_bytes_during_recording": swap_out_delta,
            "python_cpu_percent": stats(py_cpu),
            "ffmpeg_cpu_percent": stats(ff_cpu),
            "ffmpeg_rss_bytes": stats(ff_rss),
            "nvidia_gpu_util_percent": stats(gpu_util),
            "nvidia_encoder_util_percent": stats(encoder_util),
            "nvidia_memory_used_mb": stats(gpu_mem),
            "minimum_output_disk_free_bytes": min(free_disk) if free_disk else None,
            "peak_system_cpu_sample": peak_sample(["system_cpu_percent"]),
            "peak_memory_sample": peak_sample(["memory", "percent"]),
            "peak_swap_sample": peak_sample(["swap", "percent"]),
            "peak_ffmpeg_cpu_sample": peak_sample(["ffmpeg_process", "cpu_percent"]),
            "peak_nvidia_gpu_sample": peak_gpu_sample,
            "peak_nvidia_encoder_sample": peak_encoder_sample,
            "peak_system_disk_write_rate": peak_disk_rate,
            "peak_ffmpeg_write_rate": peak_ffmpeg_rate,
            "first_sample": samples[0],
            "last_sample": samples[-1],
        }

    def analyze_visual_frame_cadence(
        self,
        path,
        max_frames=30000,
        cancel_event=None,
        frame_content_path=None,
        auto_stutter_path=None,
        effective_fps=None,
        timing_summary=None,
        update_shared_state=True,
    ):
        """Ищет визуальные замирания по разнице соседних декодированных кадров.

        Контейнер может иметь идеальные PTS, а изображение — повторяться. Поэтому
        после сохранения FFmpeg декодирует видео в очень маленькие grayscale-кадры
        160x90, а Python считает среднюю абсолютную разницу пикселей. Анализ запускается
        только после сохранения файла, в фоне, и отменяется при старте новой записи.
        """
        result = {
            "status": "not_run",
            "path": str(path),
            "method": "decoded 160x90 grayscale frame-to-frame mean absolute difference",
            "max_frames": int(max_frames),
            "thresholds": {
                "exact_duplicate_mean_abs_diff_luma": 0.02,
                "near_duplicate_mean_abs_diff_luma": 0.75,
                "long_run_min_duration_seconds": 0.25,
            },
            "important_limitation": (
                "Почти одинаковые кадры нормальны на статичном экране, при паузе пользователя и при "
                "контенте с FPS ниже частоты записи. Даже серия между участками движения считается "
                "только статичным интервалом для ручной проверки, а не доказанным рывком. Технической "
                "аномалией она становится лишь при совпадении с dup/drop, progress stall, ошибкой PTS "
                "или подтверждённой перегрузкой системы."
            ),
        }
        process = None
        try:
            path = Path(path)
            if not path.exists():
                result.update({"status": "file_missing"})
                return result

            width, height = 160, 90
            frame_size = width * height
            cmd = [
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel", "error",
                "-i", str(path),
                "-map", "0:v:0",
                "-vf", f"scale={width}:{height}:flags=fast_bilinear,format=gray",
                "-frames:v", str(int(max_frames)),
                "-fps_mode", "passthrough",
                "-f", "rawvideo",
                "pipe:1",
            ]
            process = self.start_managed_process(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=self.creation_flags(),
            )

            target_fps = float(effective_fps or self.recording_effective_fps or 0) or None
            if not target_fps:
                try:
                    target_fps = float(
                        (((timing_summary or self.last_video_timing_summary or {}).get("reported_avg_frame_rate"))) or 0
                    ) or None
                except Exception:
                    target_fps = None
            fps_for_time = target_fps or 60.0
            exact_threshold = 0.02
            near_threshold = 0.75
            long_run_min_frames = max(6, int(round(fps_for_time * 0.25)))

            frame_count = 0
            previous = None
            difference_values = []
            exact_transition_count = 0
            near_transition_count = 0
            runs = []
            current_run_start = None
            current_run_diffs = []
            timeline = []

            while frame_count < int(max_frames):
                if cancel_event is not None and cancel_event.is_set():
                    result.update({
                        "status": "cancelled",
                        "cancel_reason": "new_recording_or_application_exit",
                        "analyzed_frame_count": frame_count,
                    })
                    break
                raw = process.stdout.read(frame_size)
                if not raw:
                    break
                if len(raw) < frame_size:
                    # Неполный хвост rawvideo не является кадром.
                    break
                if previous is not None:
                    if NUMPY_AVAILABLE and np is not None:
                        prev_arr = np.frombuffer(previous, dtype=np.uint8).astype(np.int16)
                        cur_arr = np.frombuffer(raw, dtype=np.uint8).astype(np.int16)
                        diff_arr = np.abs(cur_arr - prev_arr)
                        mean_abs_diff = float(diff_arr.mean())
                        max_abs_diff = int(diff_arr.max())
                        changed_pixel_percent = float((diff_arr >= 3).mean() * 100.0)
                    else:
                        total = 0
                        max_abs_diff = 0
                        changed = 0
                        for left, right in zip(previous, raw):
                            delta = abs(left - right)
                            total += delta
                            if delta > max_abs_diff:
                                max_abs_diff = delta
                            if delta >= 3:
                                changed += 1
                        mean_abs_diff = total / float(frame_size)
                        changed_pixel_percent = changed / float(frame_size) * 100.0

                    difference_values.append(mean_abs_diff)
                    is_exact = mean_abs_diff <= exact_threshold
                    is_near = mean_abs_diff <= near_threshold
                    if is_exact:
                        exact_transition_count += 1
                    if is_near:
                        near_transition_count += 1
                        if current_run_start is None:
                            # Серия включает предыдущий кадр и текущий.
                            current_run_start = frame_count - 1
                            current_run_diffs = []
                        current_run_diffs.append(mean_abs_diff)
                    elif current_run_start is not None:
                        run_end = frame_count - 1
                        run_frames = run_end - current_run_start + 1
                        runs.append({
                            "start_frame_index": current_run_start,
                            "end_frame_index": run_end,
                            "run_frame_count": run_frames,
                            "near_duplicate_transitions": run_frames - 1,
                            "start_seconds_estimated": round(current_run_start / fps_for_time, 6),
                            "end_seconds_estimated": round((run_end + 1) / fps_for_time, 6),
                            "duration_seconds_estimated": round(run_frames / fps_for_time, 6),
                            "mean_abs_diff_luma_mean": round(statistics.fmean(current_run_diffs), 6)
                            if current_run_diffs else None,
                            "mean_abs_diff_luma_max": round(max(current_run_diffs), 6)
                            if current_run_diffs else None,
                            "exact_duplicate_like_transitions": sum(
                                1 for value in current_run_diffs if value <= exact_threshold
                            ),
                            "exact_duplicate_like_ratio": round(
                                sum(1 for value in current_run_diffs if value <= exact_threshold)
                                / max(1, len(current_run_diffs)),
                                6,
                            ),
                        })
                        current_run_start = None
                        current_run_diffs = []

                    # Сохраняем компактный временной ряд. Раньше в лог попадал почти каждый
                    # похожий кадр, поэтому файлы разрастались на сотни килобайт и дублировали
                    # списки серий. Теперь близкие кадры семплируются примерно 5 раз/сек.
                    near_sample_step = max(1, int(round(fps_for_time / 5.0)))
                    if (
                        frame_count <= 120
                        or frame_count % max(1, int(fps_for_time)) == 0
                        or (is_near and frame_count % near_sample_step == 0)
                    ):
                        if len(timeline) < 800:
                            timeline.append({
                                "frame_index": frame_count,
                                "time_seconds_estimated": round(frame_count / fps_for_time, 6),
                                "mean_abs_diff_luma": round(mean_abs_diff, 6),
                                "max_abs_diff_luma": max_abs_diff,
                                "changed_pixel_percent_ge_3": round(changed_pixel_percent, 6),
                                "exact_duplicate_like": is_exact,
                                "near_duplicate_like": is_near,
                            })
                previous = raw
                frame_count += 1

            if current_run_start is not None and frame_count > current_run_start:
                run_end = frame_count - 1
                run_frames = run_end - current_run_start + 1
                runs.append({
                    "start_frame_index": current_run_start,
                    "end_frame_index": run_end,
                    "run_frame_count": run_frames,
                    "near_duplicate_transitions": run_frames - 1,
                    "start_seconds_estimated": round(current_run_start / fps_for_time, 6),
                    "end_seconds_estimated": round((run_end + 1) / fps_for_time, 6),
                    "duration_seconds_estimated": round(run_frames / fps_for_time, 6),
                    "mean_abs_diff_luma_mean": round(statistics.fmean(current_run_diffs), 6)
                    if current_run_diffs else None,
                    "mean_abs_diff_luma_max": round(max(current_run_diffs), 6)
                    if current_run_diffs else None,
                    "exact_duplicate_like_transitions": sum(
                        1 for value in current_run_diffs if value <= exact_threshold
                    ),
                    "exact_duplicate_like_ratio": round(
                        sum(1 for value in current_run_diffs if value <= exact_threshold)
                        / max(1, len(current_run_diffs)),
                        6,
                    ),
                })

            if result.get("status") == "cancelled":
                return result

            stderr_bytes = b""
            try:
                stderr_bytes = process.stderr.read() if process.stderr else b""
            except Exception:
                pass
            try:
                returncode = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.terminate_process_tree(process, timeout=2.0, name="visual_cadence_analysis_ffmpeg")
                returncode = process.poll()
            self.unregister_child_process(process)
            process = None
            try:
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            except Exception:
                stderr_text = str(stderr_bytes)
            self.append_ffmpeg_problem_log(
                "visual frame cadence analysis",
                command=cmd,
                stderr=stderr_text,
                extra={
                    "returncode": returncode,
                    "frame_count": frame_count,
                    "frame_size": f"{width}x{height}",
                    "near_threshold": near_threshold,
                },
            )
            if returncode not in (0, None):
                result.update({
                    "status": "ffmpeg_error",
                    "returncode": returncode,
                    "stderr": stderr_text[-4000:],
                })
                return result

            long_runs = [run for run in runs if run["run_frame_count"] >= long_run_min_frames]

            def percentile(values, q):
                if not values:
                    return None
                ordered = sorted(values)
                position = (len(ordered) - 1) * q
                lower = int(position)
                upper = min(len(ordered) - 1, lower + 1)
                fraction = position - lower
                return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

            # Ручная кнопка метки убрана: рывок виден только при просмотре готового файла.
            # Поэтому ищем автоматически наиболее полезный тип аномалии: почти неизменный
            # участок, который с обеих сторон окружён заметным движением. Такая схема сильно
            # уменьшает ложные срабатывания на обычном статичном рабочем столе.
            p75_diff = percentile(difference_values, 0.75) or 0.0
            p90_diff = percentile(difference_values, 0.90) or 0.0
            motion_threshold = max(1.0, min(8.0, max(p75_diff * 0.45, p90_diff * 0.12)))
            motion_window_frames = max(6, int(round(fps_for_time * 0.35)))
            freeze_min_frames = max(8, int(round(fps_for_time * 0.25)))
            automatic_candidates = []

            for run in runs:
                run_frames = int(run.get("run_frame_count") or 0)
                exact_ratio = float(run.get("exact_duplicate_like_ratio") or 0.0)
                if run_frames < freeze_min_frames or exact_ratio < 0.95:
                    continue
                start_frame = int(run.get("start_frame_index") or 0)
                end_frame = int(run.get("end_frame_index") or start_frame)
                # difference_values[i] — переход кадр i -> i+1.
                before = difference_values[max(0, start_frame - motion_window_frames):start_frame]
                after = difference_values[end_frame:min(len(difference_values), end_frame + motion_window_frames)]
                if len(before) < 3 or len(after) < 3:
                    continue
                before_p75 = percentile(before, 0.75) or 0.0
                after_p75 = percentile(after, 0.75) or 0.0
                before_motion_fraction = sum(1 for value in before if value >= motion_threshold) / len(before)
                after_motion_fraction = sum(1 for value in after if value >= motion_threshold) / len(after)
                if not (
                    before_p75 >= motion_threshold
                    and after_p75 >= motion_threshold
                    and before_motion_fraction >= 0.35
                    and after_motion_fraction >= 0.35
                ):
                    continue

                duration = float(run.get("duration_seconds_estimated") or 0.0)
                if duration >= 1.00:
                    severity = "high_review_priority"
                elif duration >= 0.50:
                    severity = "medium_review_priority"
                else:
                    severity = "low_review_priority"
                candidate = dict(run)
                candidate.update({
                    "candidate_type": "static_interval_between_motion_needs_review",
                    "severity": severity,
                    "center_seconds_estimated": round(
                        (float(run.get("start_seconds_estimated") or 0.0)
                         + float(run.get("end_seconds_estimated") or 0.0)) / 2.0,
                        6,
                    ),
                    "motion_threshold_mean_abs_diff_luma": round(motion_threshold, 6),
                    "surrounding_window_frames": motion_window_frames,
                    "before_motion_p75": round(before_p75, 6),
                    "after_motion_p75": round(after_p75, 6),
                    "before_motion_fraction": round(before_motion_fraction, 6),
                    "after_motion_fraction": round(after_motion_fraction, 6),
                    "candidate_score": round(
                        duration
                        * min(before_p75, after_p75)
                        * min(before_motion_fraction, after_motion_fraction),
                        6,
                    ),
                    "interpretation": (
                        "Автоматически найден статичный интервал между участками движения. Это может "
                        "быть обычная остановка прокрутки или статичная сцена. Не считать техническим "
                        "рывком без совпадения с progress/PTS/dup/drop/нагрузкой либо без просмотра."
                    ),
                })
                automatic_candidates.append(candidate)

            automatic_candidates.sort(
                key=lambda item: (
                    float(item.get("candidate_score") or 0.0),
                    float(item.get("duration_seconds_estimated") or 0.0),
                ),
                reverse=True,
            )

            # Старый детектор искал только длинную непрерывную "заморозку".
            # Он пропускал cadence-рывки вида: новый кадр -> повтор -> новый кадр.
            # Анализируем движущиеся окна по одной секунде и считаем частоту
            # реальных визуальных обновлений внутри них.
            cadence_window_frames = max(12, int(round(fps_for_time)))
            cadence_step = max(6, cadence_window_frames // 2)
            cadence_motion_threshold = max(
                0.10,
                min(4.0, max(
                    (percentile(difference_values, 0.90) or 0.0) * 0.08,
                    (percentile(difference_values, 0.99) or 0.0) * 0.01,
                )),
            )
            cadence_windows = []
            for start_index in range(0, max(0, len(difference_values) - cadence_window_frames + 1), cadence_step):
                window_values = difference_values[start_index:start_index + cadence_window_frames]
                if not window_values:
                    continue
                update_indexes = [
                    index for index, value in enumerate(window_values)
                    if value >= cadence_motion_threshold
                ]
                motion_fraction = len(update_indexes) / len(window_values)
                # Сохраняем только окна, где действительно было заметное движение.
                if motion_fraction < 0.08:
                    continue
                update_gaps = [
                    right - left
                    for left, right in zip(update_indexes, update_indexes[1:])
                ]
                exact_ratio = (
                    sum(1 for value in window_values if value <= exact_threshold)
                    / len(window_values)
                )
                near_ratio = (
                    sum(1 for value in window_values if value <= near_threshold)
                    / len(window_values)
                )
                duration_seconds = len(window_values) / fps_for_time
                cadence_windows.append({
                    "start_frame_index": start_index,
                    "end_frame_index": start_index + len(window_values),
                    "start_seconds_estimated": round(start_index / fps_for_time, 6),
                    "end_seconds_estimated": round(
                        (start_index + len(window_values)) / fps_for_time, 6
                    ),
                    "motion_threshold_mean_abs_diff_luma": round(cadence_motion_threshold, 6),
                    "motion_transition_fraction": round(motion_fraction, 6),
                    "visual_update_count": len(update_indexes),
                    "visual_update_fps_estimated": round(
                        len(update_indexes) / max(0.001, duration_seconds), 6
                    ),
                    "exact_duplicate_like_ratio": round(exact_ratio, 6),
                    "near_duplicate_like_ratio": round(near_ratio, 6),
                    "median_gap_between_visual_updates_frames": (
                        round(statistics.median(update_gaps), 6) if update_gaps else None
                    ),
                    "max_gap_between_visual_updates_frames": max(update_gaps) if update_gaps else None,
                })

            cadence_windows.sort(
                key=lambda item: (
                    float(item.get("motion_transition_fraction") or 0.0),
                    float(item.get("visual_update_fps_estimated") or 0.0),
                ),
                reverse=True,
            )
            moving_windows = len(cadence_windows)
            low_cadence_windows = [
                item for item in cadence_windows
                if float(item.get("motion_transition_fraction") or 0.0) >= 0.50
                and float(item.get("visual_update_fps_estimated") or 0.0) < fps_for_time * 0.55
            ]

            result.update({
                "status": "ok",
                "analysis_resolution": f"{width}x{height} grayscale",
                "fps_used_for_estimated_timestamps": fps_for_time,
                "analyzed_frame_count": frame_count,
                "analyzed_transition_count": max(0, frame_count - 1),
                "analysis_was_truncated": frame_count >= int(max_frames),
                "exact_duplicate_like_transition_count": exact_transition_count,
                "exact_duplicate_like_percent": round(
                    exact_transition_count / max(1, frame_count - 1) * 100.0, 4
                ),
                "near_duplicate_like_transition_count": near_transition_count,
                "near_duplicate_like_percent": round(
                    near_transition_count / max(1, frame_count - 1) * 100.0, 4
                ),
                "near_duplicate_run_count": len(runs),
                "long_run_min_frames": long_run_min_frames,
                "long_run_min_duration_seconds": round(long_run_min_frames / fps_for_time, 6),
                "long_near_duplicate_run_count": len(long_runs),
                "automatic_motion_qualified_analysis": {
                    "status": (
                        "candidates_found" if automatic_candidates
                        else "no_motion_qualified_freezes_detected"
                    ),
                    "motion_threshold_mean_abs_diff_luma": round(motion_threshold, 6),
                    "motion_window_frames": motion_window_frames,
                    "minimum_freeze_frames": freeze_min_frames,
                    "minimum_freeze_duration_seconds": round(freeze_min_frames / fps_for_time, 6),
                    "candidate_count": len(automatic_candidates),
                    "candidate_rule": (
                        "exact-like ratio >= 0.95, длительность >= 0.25 сек, "
                        "до и после серии присутствует заметное движение; результат информационный"
                    ),
                },
                "suspected_freeze_candidates": automatic_candidates[:30],
                "moving_content_cadence_analysis": {
                    "status": (
                        "low_visual_update_cadence_detected"
                        if low_cadence_windows else
                        "no_low_cadence_moving_windows_detected"
                    ),
                    "analysis_window_seconds": round(cadence_window_frames / fps_for_time, 6),
                    "window_step_seconds": round(cadence_step / fps_for_time, 6),
                    "motion_threshold_mean_abs_diff_luma": round(cadence_motion_threshold, 6),
                    "moving_window_count": moving_windows,
                    "low_cadence_window_count": len(low_cadence_windows),
                    "rule": (
                        "окно содержит заметное движение минимум в 50% переходов, "
                        "но оценочная частота визуальных обновлений ниже 55% целевого FPS"
                    ),
                    "highest_motion_windows": cadence_windows[:20],
                    "lowest_cadence_moving_windows": sorted(
                        low_cadence_windows,
                        key=lambda item: float(item.get("visual_update_fps_estimated") or 0.0),
                    )[:20],
                    "important_limitation": (
                        "Низкая частота визуальных обновлений может принадлежать исходному видео, "
                        "анимации или приложению. Окна с кратким движением и в основном статичной сценой "
                        "не повышают общий статус. Для непрерывного скролла/перетаскивания это сильный "
                        "признак повторов содержимого, даже когда PTS контейнера идеальны."
                    ),
                },
                "frame_difference_statistics": {
                    "min": round(min(difference_values), 6) if difference_values else None,
                    "max": round(max(difference_values), 6) if difference_values else None,
                    "mean": round(statistics.fmean(difference_values), 6) if difference_values else None,
                    "median": round(statistics.median(difference_values), 6) if difference_values else None,
                    "p01": round(percentile(difference_values, 0.01), 6) if difference_values else None,
                    "p05": round(percentile(difference_values, 0.05), 6) if difference_values else None,
                    "p95": round(percentile(difference_values, 0.95), 6) if difference_values else None,
                    "p99": round(percentile(difference_values, 0.99), 6) if difference_values else None,
                },
                "longest_near_duplicate_runs": sorted(
                    runs,
                    key=lambda item: item["run_frame_count"],
                    reverse=True,
                )[:50],
                "all_long_near_duplicate_runs": long_runs[:120],
                "difference_timeline_samples": timeline,
            })
            return result
        except Exception as exc:
            result.update({"status": "exception", "error": repr(exc)})
            self.log_exception("analyze_visual_frame_cadence", exc)
            return result
        finally:
            if process is not None:
                try:
                    self.terminate_process_tree(process, timeout=1.0, name="visual_cadence_analysis_cleanup")
                except Exception:
                    pass
            try:
                if update_shared_state:
                    self.last_frame_content_analysis = result
                target_frame_path = frame_content_path or self.session_frame_content_path
                target_auto_path = auto_stutter_path or self.session_auto_stutter_path
                if target_frame_path:
                    # Здесь намеренно пишем result напрямую: списки уже ограничены,
                    # а общий _safe_log_value обрезал бы их до 120 элементов.
                    Path(target_frame_path).write_text(
                        json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                if target_auto_path:
                    automatic = (result or {}).get("automatic_motion_qualified_analysis") or {}
                    candidates = (result or {}).get("suspected_freeze_candidates") or []
                    Path(target_auto_path).write_text(
                        json.dumps({
                            "status": (result or {}).get("status"),
                            "purpose": (
                                "Статичные интервалы для ручной проверки в уже сохранённом видео. "
                                "Они не считаются техническими рывками без дополнительных подтверждений."
                            ),
                            "analysis": automatic,
                            "candidates": candidates,
                            "important_limitation": (result or {}).get("important_limitation"),
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            except Exception:
                pass

    def _automatic_evidence_around_candidates(self, frame_content_analysis):
        """Сопоставляет автоматические кандидаты с FFmpeg и нагрузкой системы."""
        candidates = list((frame_content_analysis or {}).get("suspected_freeze_candidates") or [])[:30]
        with self.recording_progress_lock:
            progress = list(self.recording_progress_samples)
        with self.recording_performance_lock:
            performance = list(self.recording_performance_samples)
        evidence = []
        for candidate in candidates:
            try:
                target = float(candidate.get("center_seconds_estimated"))
            except Exception:
                continue
            progress_near = []
            performance_near = []
            for sample in progress:
                try:
                    video_time = float(sample.get("out_time_seconds"))
                    delta = video_time - target
                    if abs(delta) <= 1.0:
                        item = dict(sample)
                        item["seconds_from_candidate"] = round(delta, 6)
                        progress_near.append(item)
                except Exception:
                    pass
            for sample in performance:
                try:
                    latest = sample.get("latest_ffmpeg_progress") or {}
                    video_time = float(latest.get("out_time_seconds"))
                    delta = video_time - target
                    if abs(delta) <= 1.0:
                        item = dict(sample)
                        item["seconds_from_candidate"] = round(delta, 6)
                        performance_near.append(item)
                except Exception:
                    pass
            evidence.append({
                "candidate": candidate,
                "ffmpeg_progress_within_1_second": progress_near,
                "system_performance_within_1_second": performance_near,
            })
        return evidence

    @staticmethod
    def _summary_stat_max(summary, key):
        try:
            value = ((summary or {}).get(key) or {}).get("max")
            return float(value) if value is not None else None
        except Exception:
            return None

    def build_automatic_smoothness_verdict(
        self,
        timing_summary,
        frame_content_analysis,
        progress_summary,
        performance_summary,
        clock_alignment=None,
    ):
        """Короткий машинный вывод с разделением фактов и наблюдений."""
        positives = []
        observations = []
        warnings = []
        problems = []

        health = (timing_summary or {}).get("timing_health") or {}
        health_status = health.get("status")
        if health_status == "error":
            problems.append(
                "Проверка тайминга контейнера обнаружила реальную ошибку PTS/DTS, FPS или большие разрывы."
            )
        elif health_status == "warning":
            warnings.append(
                "Контейнер читается, но FFmpeg сообщил диагностические предупреждения; проверить timing_health."
            )
        else:
            positives.append("PTS/DTS, средний FPS и интервалы кадров контейнера в норме.")

        dup_count = int((progress_summary or {}).get("total_dup_frames_across_segments") or 0)
        drop_count = int((progress_summary or {}).get("total_drop_frames_across_segments") or 0)
        if dup_count == 0 and drop_count == 0:
            positives.append("FFmpeg не сообщил выходных dup/drop кадров.")
        else:
            warnings.append(f"FFmpeg сообщил dup={dup_count}, drop={drop_count}; проверить моменты изменений счётчиков.")

        stall_count = int((progress_summary or {}).get("possible_progress_stalls_count") or 0)
        if stall_count > 0:
            warnings.append(f"FFmpeg progress содержит {stall_count} возможных остановок медиатаймлайна.")
        else:
            positives.append("FFmpeg progress не показал остановок медиатаймлайна.")

        steady_min = (progress_summary or {}).get("steady_speed_min_after_3s")
        steady_median = (progress_summary or {}).get("steady_speed_median_after_3s")
        try:
            steady_min = float(steady_min) if steady_min is not None else None
        except Exception:
            steady_min = None
        try:
            steady_median = float(steady_median) if steady_median is not None else None
        except Exception:
            steady_median = None
        if steady_median is not None:
            if steady_median < 0.90:
                problems.append(f"Устойчивая медианная скорость FFmpeg {steady_median:.3f}x заметно ниже реального времени.")
            elif steady_median < 0.97:
                warnings.append(f"Устойчивая медианная скорость FFmpeg {steady_median:.3f}x ниже желательной.")
            elif steady_median < 0.99:
                observations.append(
                    f"Устойчивая медианная скорость FFmpeg {steady_median:.3f}x немного ниже 1.0x; "
                    "при отсутствии stalls и dup/drop это ещё не доказательство рывков."
                )
            else:
                positives.append(f"FFmpeg удерживал реальное время; устойчивая медиана {steady_median:.3f}x.")
        if steady_min is not None and steady_min < 0.90 and stall_count == 0:
            observations.append(
                f"Был краткий минимум speed={steady_min:.3f}x, но progress не зафиксировал остановку таймлайна."
            )

        clock_alignment = clock_alignment or {}
        alignment_status = clock_alignment.get("status")
        alignment_aggregate = clock_alignment.get("aggregate") or {}
        drift_percent = alignment_aggregate.get("timeline_lag_vs_wall_percent")
        try:
            drift_value = float(drift_percent) if drift_percent is not None else None
        except Exception:
            drift_value = None
        if alignment_status == "clocks_aligned":
            positives.append("Медиатаймлайн FFmpeg согласован с монотонными часами ПК.")
        elif alignment_status in {"persistent_timeline_lag_detected", "persistent_timeline_lead_detected"}:
            message = (
                f"Найден устойчивый дрейф медиатаймлайна относительно часов: {drift_value:.3f}%"
                if drift_value is not None else
                "Найден устойчивый дрейф медиатаймлайна относительно часов."
            )
            if drift_value is not None and abs(drift_value) >= 5.0:
                problems.append(
                    message + ". Временная шкала заметно меняет реальную длительность; проверить секундомер и аудиосинхронизацию."
                )
            elif drift_value is not None and abs(drift_value) >= 1.5:
                warnings.append(
                    message + ". Это уже значимое сжатие/растяжение времени, даже если PTS контейнера формально ровные."
                )
            else:
                observations.append(
                    message + ". Небольшое отличие ещё может быть погрешностью старта/остановки сегмента."
                )

        cadence_analysis = (
            (frame_content_analysis or {}).get("moving_content_cadence_analysis") or {}
        )
        low_cadence_count = int(cadence_analysis.get("low_cadence_window_count") or 0)
        if low_cadence_count > 0:
            warnings.append(
                f"Анализ содержимого нашёл {low_cadence_count} движущихся окон с низкой "
                "частотой визуальных обновлений. Это возможные повторы изображения, "
                "которые обычные PTS/dup/drop не показывают."
            )

        exact_duplicate_percent = (frame_content_analysis or {}).get(
            "exact_duplicate_like_percent"
        )
        try:
            exact_duplicate_percent = float(exact_duplicate_percent)
        except Exception:
            exact_duplicate_percent = None
        if exact_duplicate_percent is not None and exact_duplicate_percent >= 85:
            observations.append(
                f"Доля почти одинаковых соседних кадров очень высокая: "
                f"{exact_duplicate_percent:.1f}%. На статичном экране это нормально, "
                "но при тесте с непрерывным движением подтверждает проблему cadence."
            )

        candidates = list((frame_content_analysis or {}).get("suspected_freeze_candidates") or [])
        if candidates:
            observations.append(
                f"В содержимом кадров найдено {len(candidates)} статичных интервал(ов) между движением. "
                "Они не считаются ошибкой без технического совпадения или просмотра видео."
            )
        else:
            positives.append("Анализ содержимого не нашёл статичных интервалов, окружённых движением.")

        memory_max = self._summary_stat_max(performance_summary, "memory_percent")
        swap_max = self._summary_stat_max(performance_summary, "swap_percent")
        swap_in_delta = int((performance_summary or {}).get("swap_in_bytes_during_recording") or 0)
        swap_out_delta = int((performance_summary or {}).get("swap_out_bytes_during_recording") or 0)
        cpu_max = self._summary_stat_max(performance_summary, "system_cpu_percent")
        gpu_max = self._summary_stat_max(performance_summary, "nvidia_gpu_util_percent")
        encoder_max = self._summary_stat_max(performance_summary, "nvidia_encoder_util_percent")
        if memory_max is not None:
            if memory_max >= 95:
                problems.append(f"Оперативная память была занята до {memory_max:.1f}% — возможен активный своп.")
            elif memory_max >= 85:
                warnings.append(f"Оперативная память была занята до {memory_max:.1f}%; желательно освободить RAM.")
            elif memory_max >= 80:
                observations.append(f"Оперативная память была занята до {memory_max:.1f}%, но критического дефицита не видно.")
        if swap_in_delta > 0 or swap_out_delta > 0:
            warnings.append(
                "Во время записи была активность файла подкачки: "
                f"read={swap_in_delta} bytes, write={swap_out_delta} bytes. При совпадении по времени это может давать рывки."
            )
        elif swap_max is not None and swap_max > 0:
            observations.append(
                f"Файл подкачки занят максимум на {swap_max:.1f}%, но движения данных подкачки во время записи не зафиксировано."
            )
        if cpu_max is not None and cpu_max >= 95:
            warnings.append(f"Пиковая загрузка CPU достигала {cpu_max:.1f}%.")
        if gpu_max is not None and gpu_max >= 95:
            warnings.append(f"Пиковая загрузка GPU достигала {gpu_max:.1f}%.")
        if encoder_max is not None and encoder_max >= 95:
            warnings.append(f"Пиковая загрузка NVENC достигала {encoder_max:.1f}%.")

        if problems:
            status = "problem_detected"
        elif warnings:
            status = "healthy_with_warnings"
        elif observations:
            status = "healthy_with_observations"
        else:
            status = "healthy"
        return {
            "status": status,
            "positives": positives,
            "observations_not_proven_problems": observations,
            "warnings": warnings,
            "problems": problems,
            "recording_pipeline_changed_by_this_diagnostics_update": False,
            "recording_pipeline_change": (
                "Нет. В этой версии изменены только диагностика, фоновые служебные процессы "
                "и пост-анализ; стабильный ddagrab/NVENC pipeline не менялся."
            ),
            "interpretation": (
                "Автоматический вывод разделяет подтверждённые технические ошибки, предупреждения "
                "и обычные наблюдения. Статичный контент сам по себе не понижает статус записи."
            ),
        }

    @staticmethod
    def _compact_progress_summary(summary):
        summary = summary or {}
        keys = (
            "sample_count", "process_count", "fps_min", "fps_max", "fps_mean", "fps_median",
            "speed_min", "speed_max", "speed_mean", "speed_median",
            "steady_speed_min_after_3s", "steady_speed_mean_after_3s", "steady_speed_median_after_3s",
            "last_frame_value_seen", "last_out_time_value_seen_seconds",
            "total_dup_frames_across_segments", "total_drop_frames_across_segments",
            "cadence_counter_change_events_count", "possible_progress_stalls_count",
            "speed_below_0_95_count", "fps_below_90_percent_of_target_count",
        )
        return {key: summary.get(key) for key in keys if key in summary}

    @staticmethod
    def _compact_performance_summary(summary):
        summary = summary or {}
        keys = (
            "sample_count", "sampling_interval_strategy", "sampling_interval_target_seconds", "system_cpu_percent",
            "memory_percent", "swap_percent", "swap_used_bytes",
            "swap_in_bytes_during_recording", "swap_out_bytes_during_recording",
            "python_cpu_percent", "ffmpeg_cpu_percent", "ffmpeg_rss_bytes",
            "nvidia_gpu_util_percent", "nvidia_encoder_util_percent", "nvidia_memory_used_mb",
            "minimum_output_disk_free_bytes",
        )
        return {key: summary.get(key) for key in keys if key in summary}

    @staticmethod
    def _compact_timing_summary(summary):
        summary = summary or {}
        result = {
            "timing_health": summary.get("timing_health"),
        }
        preferred = (
            "label", "path", "duration_seconds", "reported_duration_seconds",
            "reported_avg_frame_rate", "reported_r_frame_rate", "frame_count",
            "packet_count", "packet_gap_count", "max_packet_gap_seconds",
            "effective_fps", "expected_media_seconds", "expected_wall_seconds",
            "requested_wall_seconds",
        )
        for key in preferred:
            if key in summary:
                result[key] = summary.get(key)
        # Если названия в конкретной версии отличаются, оставляем полезные
        # скаляры по смысловым словам, но не копируем большие списки packets/frames.
        for key, value in summary.items():
            if key in result or isinstance(value, (dict, list, tuple)):
                continue
            low = str(key).lower()
            if any(token in low for token in ("duration", "fps", "frame", "packet", "gap", "drift")):
                result[key] = value
        return result

    @staticmethod
    def _read_jsonl_samples(path, max_lines=12000):
        samples = []
        try:
            path = Path(path)
            if not path.exists():
                return samples
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-int(max_lines):]:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    samples.append(item)
        except Exception:
            pass
        return samples

    def _automatic_evidence_from_log_files(self, frame_content_analysis, progress_path, performance_path):
        """Ищет контекст кандидатов по сохранённым адаптивным JSONL, не по mutable self-state."""
        candidates = list((frame_content_analysis or {}).get("suspected_freeze_candidates") or [])[:20]
        progress = self._read_jsonl_samples(progress_path)
        performance = self._read_jsonl_samples(performance_path)
        evidence = []
        for candidate in candidates:
            try:
                target = float(candidate.get("center_seconds_estimated"))
            except Exception:
                continue
            progress_near = []
            performance_near = []
            for sample in progress:
                try:
                    video_time = float(sample.get("out_time_seconds"))
                    delta = video_time - target
                    if abs(delta) <= 1.5:
                        item = {
                            key: sample.get(key)
                            for key in ("wall_time", "out_time_seconds", "frame", "fps", "speed", "dup_frames", "drop_frames")
                        }
                        item["seconds_from_candidate"] = round(delta, 6)
                        progress_near.append(item)
                except Exception:
                    pass
            for sample in performance:
                try:
                    latest = sample.get("latest_ffmpeg_progress") or {}
                    video_time = float(latest.get("out_time_seconds"))
                    delta = video_time - target
                    if abs(delta) <= 1.5:
                        item = {
                            "wall_time": sample.get("wall_time"),
                            "seconds_from_candidate": round(delta, 6),
                            "system_cpu_percent": sample.get("system_cpu_percent"),
                            "memory_percent": (sample.get("memory") or {}).get("percent"),
                            "swap_percent": (sample.get("swap") or {}).get("percent"),
                            "ffmpeg_cpu_percent": (sample.get("ffmpeg_process") or {}).get("cpu_percent"),
                            "latest_ffmpeg_progress": {
                                key: latest.get(key)
                                for key in ("out_time_seconds", "fps", "speed", "dup_frames", "drop_frames")
                            },
                        }
                        performance_near.append(item)
                except Exception:
                    pass
            evidence.append({
                "candidate": candidate,
                "ffmpeg_progress_near": progress_near[:8],
                "system_performance_near": performance_near[:6],
            })
        return evidence

    def build_post_save_diagnostics_context(self, timing_summary, outcome="saved", error_text=None):
        """Замораживает данные завершённой сессии до возможного старта следующей."""
        clock_alignment = self.summarize_capture_clock_alignment()
        context = {
            "recording_session_id": self.recording_session_id,
            "output_path": str(self.output_path) if self.output_path else None,
            "summary_path": str(self.session_summary_path) if self.session_summary_path else None,
            "events_path": str(self.session_events_path) if self.session_events_path else None,
            "ai_report_path": str(self.session_ai_smoothness_path) if self.session_ai_smoothness_path else None,
            "progress_path": str(self.session_ffmpeg_progress_path) if self.session_ffmpeg_progress_path else None,
            "performance_path": str(self.session_performance_path) if self.session_performance_path else None,
            "frame_content_path": str(self.session_frame_content_path) if self.session_frame_content_path else None,
            "auto_stutter_path": str(self.session_auto_stutter_path) if self.session_auto_stutter_path else None,
            "clock_alignment_path": str(self.session_clock_alignment_path) if self.session_clock_alignment_path else None,
            "timing_detail_path": str(getattr(self, "session_timing_detail_path", None) or ""),
            "source_manifest_path": str(getattr(self, "session_source_manifest_path", None) or ""),
            "source_snapshot_path": str(self.session_source_snapshot_path) if self.session_source_snapshot_path else None,
            "timing_summary": timing_summary or {},
            "progress_summary": self.summarize_ffmpeg_progress(),
            "performance_summary": self.summarize_performance_samples(),
            "clock_alignment": clock_alignment,
            "outcome": outcome,
            "error_text": error_text,
            "capture_backend": self.recording_capture_backend,
            "requested_fps": self.recording_requested_fps,
            "effective_fps": self.recording_effective_fps,
            "monitor_refresh_hz": self.recording_refresh_hz,
            "ddagrab_poll_fps": self.recording_ddagrab_poll_fps,
            "capture_region": self.capture_region,
            "settings": dict(self.recording_settings_snapshot or {}),
            "recorded_media_seconds": self.recorded_seconds,
            "recorded_wall_seconds": self.recorded_wall_seconds,
            "created_at": datetime.now().isoformat(timespec="milliseconds"),
            "app_build": APP_BUILD,
            "diagnostic_schema": DIAGNOSTIC_SCHEMA,
        }
        try:
            if context["clock_alignment_path"]:
                Path(context["clock_alignment_path"]).write_text(
                    json.dumps(clock_alignment, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception:
            pass
        return context

    def write_pending_post_diagnostics_report(self, context):
        """Маленький отчёт сразу после сохранения, пока тяжёлый анализ ещё идёт в фоне."""
        try:
            path = context.get("ai_report_path")
            if not path:
                return
            report = {
                "schema": DIAGNOSTIC_SCHEMA,
                "app_build": APP_BUILD,
                "status": "saved_post_visual_analysis_pending",
                "generated_at": datetime.now().isoformat(timespec="milliseconds"),
                "session": {
                    "recording_session_id": context.get("recording_session_id"),
                    "output_path": context.get("output_path"),
                    "capture_backend": context.get("capture_backend"),
                    "requested_fps": context.get("requested_fps"),
                    "effective_fps": context.get("effective_fps"),
                    "monitor_refresh_hz": context.get("monitor_refresh_hz"),
                },
                "timing": self._compact_timing_summary(context.get("timing_summary")),
                "clock_alignment": {
                    "status": (context.get("clock_alignment") or {}).get("status"),
                    "aggregate": (context.get("clock_alignment") or {}).get("aggregate"),
                },
                "ffmpeg_progress": self._compact_progress_summary(context.get("progress_summary")),
                "performance": self._compact_performance_summary(context.get("performance_summary")),
                "post_visual_analysis": {
                    "status": "pending_background",
                    "note": "Видео уже сохранено и проверено; визуальный анализ выполняется отдельно и не задерживает UI.",
                },
                "raw_log_files": {
                    "progress": context.get("progress_path"),
                    "performance": context.get("performance_path"),
                    "frame_content": context.get("frame_content_path"),
                    "candidates": context.get("auto_stutter_path"),
                    "clock_alignment": context.get("clock_alignment_path"),
                    "timing_detail": context.get("timing_detail_path"),
                    "source_manifest": context.get("source_manifest_path"),
                },
            }
            Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _append_post_context_event(self, context, event, data=None, level="INFO"):
        try:
            path = context.get("events_path")
            if not path:
                return
            payload = {
                "time": datetime.now().isoformat(timespec="milliseconds"),
                "level": str(level).upper(),
                "event": event,
                "thread": threading.current_thread().name,
                "recording_session_id": context.get("recording_session_id"),
            }
            if data is not None:
                payload["data"] = self._safe_log_value(data, max_text=2000)
            self.append_limited_text_file(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def _write_completed_post_diagnostics(self, context, frame_content_analysis):
        progress_summary = context.get("progress_summary") or {}
        performance_summary = context.get("performance_summary") or {}
        timing_summary = context.get("timing_summary") or {}
        clock_alignment = context.get("clock_alignment") or {}
        automatic_verdict = self.build_automatic_smoothness_verdict(
            timing_summary,
            frame_content_analysis,
            progress_summary,
            performance_summary,
            clock_alignment=clock_alignment,
        )
        evidence = self._automatic_evidence_from_log_files(
            frame_content_analysis,
            context.get("progress_path"),
            context.get("performance_path"),
        )

        cadence = (frame_content_analysis or {}).get("moving_content_cadence_analysis") or {}
        frame_brief = {
            "status": (frame_content_analysis or {}).get("status"),
            "analyzed_frame_count": (frame_content_analysis or {}).get("analyzed_frame_count"),
            "analysis_was_truncated": (frame_content_analysis or {}).get("analysis_was_truncated"),
            "exact_duplicate_like_percent": (frame_content_analysis or {}).get("exact_duplicate_like_percent"),
            "near_duplicate_like_percent": (frame_content_analysis or {}).get("near_duplicate_like_percent"),
            "candidate_count": len(list((frame_content_analysis or {}).get("suspected_freeze_candidates") or [])),
            "moving_content_cadence": {
                "status": cadence.get("status"),
                "moving_window_count": cadence.get("moving_window_count"),
                "low_cadence_window_count": cadence.get("low_cadence_window_count"),
                "rule": cadence.get("rule"),
                "lowest_cadence_moving_windows": list(cadence.get("lowest_cadence_moving_windows") or [])[:10],
            },
        }
        report = {
            "schema": DIAGNOSTIC_SCHEMA,
            "app_build": APP_BUILD,
            "status": "post_visual_analysis_completed",
            "generated_at": datetime.now().isoformat(timespec="milliseconds"),
            "session": {
                "recording_session_id": context.get("recording_session_id"),
                "output_path": context.get("output_path"),
                "capture_backend": context.get("capture_backend"),
                "requested_fps": context.get("requested_fps"),
                "effective_fps": context.get("effective_fps"),
                "monitor_refresh_hz": context.get("monitor_refresh_hz"),
                "recorded_media_seconds": context.get("recorded_media_seconds"),
                "recorded_wall_seconds": context.get("recorded_wall_seconds"),
            },
            "automatic_smoothness_verdict": automatic_verdict,
            "timing": self._compact_timing_summary(timing_summary),
            "clock_alignment": {
                "status": clock_alignment.get("status"),
                "aggregate": clock_alignment.get("aggregate"),
            },
            "ffmpeg_progress": self._compact_progress_summary(progress_summary),
            "performance": self._compact_performance_summary(performance_summary),
            "visual_frame_content": frame_brief,
            "candidate_evidence_nearby": evidence[:12],
            "raw_log_files": {
                "progress": context.get("progress_path"),
                "performance": context.get("performance_path"),
                "frame_content": context.get("frame_content_path"),
                "candidates": context.get("auto_stutter_path"),
                "clock_alignment": context.get("clock_alignment_path"),
                "timing_detail": context.get("timing_detail_path"),
                "source_manifest": context.get("source_manifest_path"),
            },
            "ai_rules": [
                "Не считать статичную сцену техническим рывком без движения и корреляции.",
                "low_visual_update_cadence понижает статус только при движении минимум в 50% переходов окна.",
                "Сопоставлять кандидаты с 07/08 и техническими счетчиками до вывода о причине.",
            ],
        }
        try:
            if context.get("ai_report_path"):
                Path(context["ai_report_path"]).write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception:
            pass
        try:
            if context.get("auto_stutter_path"):
                Path(context["auto_stutter_path"]).write_text(
                    json.dumps({
                        "status": (frame_content_analysis or {}).get("status"),
                        "automatic_verdict": automatic_verdict,
                        "analysis": (frame_content_analysis or {}).get("automatic_motion_qualified_analysis"),
                        "moving_content_cadence_summary": frame_brief["moving_content_cadence"],
                        "candidates": list((frame_content_analysis or {}).get("suspected_freeze_candidates") or [])[:50],
                        "evidence_near_candidates": evidence[:20],
                        "important_limitation": (frame_content_analysis or {}).get("important_limitation"),
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception:
            pass
        try:
            summary_path = context.get("summary_path")
            if summary_path:
                compact_summary = {
                    "recording_session_id": context.get("recording_session_id"),
                    "app_build": APP_BUILD,
                    "outcome": context.get("outcome"),
                    "output_path": context.get("output_path"),
                    "requested_fps": context.get("requested_fps"),
                    "target_fps": context.get("effective_fps"),
                    "automatic_status": automatic_verdict.get("status"),
                    "timing_status": ((timing_summary.get("timing_health") or {}).get("status")),
                    "clock_alignment_status": clock_alignment.get("status"),
                    "ffmpeg_dup": progress_summary.get("total_dup_frames_across_segments"),
                    "ffmpeg_drop": progress_summary.get("total_drop_frames_across_segments"),
                    "progress_stalls": progress_summary.get("possible_progress_stalls_count"),
                    "steady_speed_median": progress_summary.get("steady_speed_median_after_3s"),
                    "visual_candidate_count": frame_brief.get("candidate_count"),
                    "continuous_motion_low_cadence_window_count": frame_brief["moving_content_cadence"].get("low_cadence_window_count"),
                    "source_manifest": context.get("source_manifest_path"),
                }
                text = (
                    "AI-ДИАГНОСТИКА SCREEN RECORDER PRO — КОРОТКАЯ КАРТА СЕССИИ\n\n"
                    "Порядок чтения: 04 ошибки → 01 timeline → 05 окружение; для видео 06/13 → 07/08 → 09/10.\n"
                    + json.dumps(compact_summary, ensure_ascii=False, indent=2)
                    + "\n\nДля ChatGPT: отделяй факты от гипотез; не считай статичный контент рывком; "
                      "предлагай минимальную правку и способ проверки.\n"
                )
                Path(summary_path).write_text(text, encoding="utf-8")
        except Exception:
            pass
        return report

    def cancel_post_save_diagnostics(self, reason="cancelled"):
        event = getattr(self, "post_diagnostics_cancel_event", None)
        if event is not None:
            try:
                event.set()
            except Exception:
                pass
        self.post_diagnostics_running = False

    def start_post_save_diagnostics(self, context):
        """Запускает тяжёлый анализ уже после показа пользователю сохранённого файла."""
        if not context or not context.get("output_path") or not context.get("frame_content_path"):
            return
        self.cancel_post_save_diagnostics(reason="replace_previous")
        cancel_event = threading.Event()
        self.post_diagnostics_cancel_event = cancel_event
        self.post_diagnostics_running = True
        self.write_pending_post_diagnostics_report(context)
        self._append_post_context_event(context, "post_save_diagnostics_started", {
            "output_path": context.get("output_path"),
            "runs_in_background": True,
        })

        def worker():
            try:
                frame_analysis = self.analyze_visual_frame_cadence(
                    context.get("output_path"),
                    cancel_event=cancel_event,
                    frame_content_path=context.get("frame_content_path"),
                    auto_stutter_path=context.get("auto_stutter_path"),
                    effective_fps=context.get("effective_fps"),
                    timing_summary=context.get("timing_summary"),
                    update_shared_state=False,
                )
                if cancel_event.is_set() or (frame_analysis or {}).get("status") == "cancelled":
                    self._append_post_context_event(context, "post_save_diagnostics_cancelled", {
                        "reason": "new_recording_or_application_exit",
                    })
                    return
                self._write_completed_post_diagnostics(context, frame_analysis)
                self._append_post_context_event(context, "post_save_diagnostics_completed", {
                    "visual_status": (frame_analysis or {}).get("status"),
                    "candidate_count": len(list((frame_analysis or {}).get("suspected_freeze_candidates") or [])),
                })
            except Exception as exc:
                self._append_post_context_event(context, "post_save_diagnostics_failed", {
                    "error": repr(exc),
                    "traceback": traceback.format_exc(),
                }, level="WARN")
            finally:
                if self.post_diagnostics_cancel_event is cancel_event:
                    self.post_diagnostics_running = False

        thread = threading.Thread(
            target=worker,
            name=f"post_save_diagnostics_{context.get('recording_session_id')}",
            daemon=True,
        )
        self.post_diagnostics_thread = thread
        thread.start()

    def write_ai_smoothness_report(

        self,
        timing_summary=None,
        frame_content_analysis=None,
        outcome=None,
        error_text=None,
    ):
        """Пишет единый JSON, рассчитанный на прямой разбор ChatGPT 5.6 SOL."""
        try:
            if not self.session_ai_smoothness_path:
                return None
            progress_summary = self.summarize_ffmpeg_progress()
            performance_summary = self.summarize_performance_samples()
            timing_summary = timing_summary or self.last_video_timing_summary
            frame_content_analysis = frame_content_analysis or self.last_frame_content_analysis
            first_frame_delay = None
            try:
                if self.recording_first_frame_perf is not None and self.recording_start_requested_perf is not None:
                    first_frame_delay = float(self.recording_first_frame_perf) - float(self.recording_start_requested_perf)
            except Exception:
                pass
            clock_alignment = self.summarize_capture_clock_alignment()
            automatic_evidence = self._automatic_evidence_around_candidates(frame_content_analysis)
            automatic_verdict = self.build_automatic_smoothness_verdict(
                timing_summary,
                frame_content_analysis,
                progress_summary,
                performance_summary,
                clock_alignment=clock_alignment,
            )
            report = {
                "schema": DIAGNOSTIC_SCHEMA,
                "schema_purpose": (
                    "Автоматически сопоставить тайминг, содержимое готовых кадров, "
                    "FFmpeg progress и нагрузку системы без ручной кнопки метки."
                ),
                "ai_target": "ChatGPT 5.6 Thinking / SOL",
                "app_build": APP_BUILD,
                "video_timing_strategy": {
                    "ddagrab": "poll at up to 2x output FPS + arrival-wallclock setpts + single fps filter + fps_mode=passthrough",
                    "gdigrab": "single fps filter using source PTS + settb + setpts=PTS-STARTPTS + fps_mode=passthrough",
                    "goal": "preserve real wall-clock duration while producing one monotonic CFR timeline",
                    "forbidden_regression": "setpts=N*ticks without a preceding source-clock fps normalizer",
                },
                "generated_at": datetime.now().isoformat(timespec="milliseconds"),
                "outcome": outcome,
                "error_text": error_text,
                "analysis_rules_for_ai": [
                    "Не считать ровные PTS доказательством плавного изображения.",
                    "Отделять стартовую задержку от потери кадров в середине записи.",
                    "Не считать статичный интервал техническим рывком без совпадения с progress/PTS/dup/drop/нагрузкой или без просмотра файла.",
                    "Сопоставлять все аномалии по video_time/perf_counter/wall_time.",
                    "Считать устойчивый абсолютный дрейф media_to_wall более 1.5% проблемой временной шкалы, даже при ровных PTS.",
                    "Для каждой причины указывать доказательства, контраргументы и уверенность в процентах.",
                ],
                "session": {
                    "recording_session_id": self.recording_session_id,
                    "capture_backend": self.recording_capture_backend,
                    "requested_fps": self.recording_requested_fps,
                    "effective_fps": self.recording_effective_fps,
                    "monitor_refresh_hz": self.recording_refresh_hz,
                    "ddagrab_poll_fps": self.recording_ddagrab_poll_fps,
                    "capture_region": self.capture_region,
                    "ffmpeg_pid": self.recording_ffmpeg_pid,
                    "ffmpeg_command": self.recording_ffmpeg_command,
                    "record_button_to_first_frame_seconds": round(first_frame_delay, 6)
                    if first_frame_delay is not None else None,
                    "active_media_seconds_counter": self.recorded_seconds,
                    "active_capture_wall_seconds_counter": self.recorded_wall_seconds,
                    "output_path": str(self.output_path) if self.output_path else None,
                    "settings": self.recording_settings_snapshot or {},
                    "runtime": self.collect_basic_runtime_info(),
                },
                "timing_and_container_analysis": timing_summary,
                "visual_frame_content_analysis": frame_content_analysis,
                "ffmpeg_progress_summary": progress_summary,
                "system_performance_summary": performance_summary,
                "ffmpeg_progress_clock_alignment": clock_alignment,
                "timing_strategy_validation": {
                    "expected_media_to_wall_ratio": 1.0,
                    "acceptable_absolute_drift_percent": 1.5,
                    "status": clock_alignment.get("status"),
                    "actual_media_to_wall_ratio": (clock_alignment.get("aggregate") or {}).get("media_to_wall_ratio"),
                    "actual_timeline_lag_vs_wall_percent": (clock_alignment.get("aggregate") or {}).get("timeline_lag_vs_wall_percent"),
                    "passes_source_clock_preservation_check": clock_alignment.get("status") == "clocks_aligned",
                },
                "automatic_smoothness_verdict": automatic_verdict,
                "automatic_stutter_candidate_evidence": automatic_evidence,
                "raw_log_files": {
                    "ffmpeg_progress_jsonl": str(self.session_ffmpeg_progress_path),
                    "system_performance_jsonl": str(self.session_performance_path),
                    "automatic_stutter_candidates_json": str(self.session_auto_stutter_path),
                    "frame_content_json": str(self.session_frame_content_path),
                    "source_manifest": str(getattr(self, "session_source_manifest_path", None)),
                    "source_code_snapshot_on_error": str(self.session_source_snapshot_path),
                    "ready_ai_prompt": str(self.session_ai_prompt_path),
                    "clock_alignment_json": str(self.session_clock_alignment_path),
                    "ffmpeg_command_and_stderr": str(self.session_ffmpeg_path),
                    "recording_text_log": str(self.current_log_path),
                },
                "known_limits": [
                    "Даже motion-qualified кандидат не является абсолютным доказательством: приложение или видео могли законно остановить движение.",
                    "nvidia-smi is sampled only every 15 seconds to avoid adding recording load.",
                    "If psutil is unavailable, CPU/process/disk metrics are less detailed.",
                    "Player/display judder can exist even when the encoded file cadence is correct; compare in multiple players.",
                ],
            }
            self.last_ai_smoothness_report = report
            self.session_ai_smoothness_path.write_text(
                json.dumps(self._safe_log_value(report, max_text=50000), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if self.session_clock_alignment_path:
                self.session_clock_alignment_path.write_text(
                    json.dumps(clock_alignment, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            if self.session_auto_stutter_path:
                self.session_auto_stutter_path.write_text(
                    json.dumps({
                        "status": "ok",
                        "automatic_verdict": automatic_verdict,
                        "analysis": (frame_content_analysis or {}).get(
                            "automatic_motion_qualified_analysis"
                        ),
                        "moving_content_cadence_analysis": (
                            frame_content_analysis or {}
                        ).get("moving_content_cadence_analysis"),
                        "candidates": (frame_content_analysis or {}).get(
                            "suspected_freeze_candidates"
                        ) or [],
                        "evidence_near_candidates": automatic_evidence,
                        "important_limitation": (frame_content_analysis or {}).get(
                            "important_limitation"
                        ),
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            self.problem_log_event("ai_smoothness_report_written", {
                "path": self.session_ai_smoothness_path,
                "outcome": outcome,
                "automatic_candidate_count": len(
                    list((frame_content_analysis or {}).get("suspected_freeze_candidates") or [])
                ),
                "progress_sample_count": progress_summary.get("sample_count"),
                "performance_sample_count": performance_summary.get("sample_count"),
                "clock_alignment_status": clock_alignment.get("status"),
            })
            return report
        except Exception as exc:
            self.log_exception("write_ai_smoothness_report", exc)
            return None
