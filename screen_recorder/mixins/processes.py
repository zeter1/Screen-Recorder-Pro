from ..shared import *


class ProcessMixin:
    def is_gui_thread(self):
        """True, если код выполняется в основном Tkinter-потоке.

        В GUI-потоке нельзя делать долгие FFmpeg/DXcam/TaskKill-проверки:
        Windows помечает окно как «Не отвечает». Поэтому тяжёлые проверки
        либо берут уже прогретый кэш, либо выполняются в фоне.
        """
        try:
            gui_ident = getattr(self, "gui_thread_ident", None)
            return gui_ident is not None and threading.get_ident() == gui_ident
        except Exception:
            return False

    def cancel_recording_watchdog(self):
        job = getattr(self, "recording_watchdog_job", None)
        self.recording_watchdog_job = None
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass

    def schedule_recording_watchdog(self, delay_ms=500):
        """Следит, что FFmpeg не завершился сам, пока интерфейс показывает REC."""
        self.cancel_recording_watchdog()
        if (
            self.running
            and self.is_recording
            and not self.is_paused
            and not self.is_finalizing
            and not getattr(self, "is_pause_transitioning", False)
        ):
            self.recording_watchdog_job = self.root.after(
                max(100, int(delay_ms)),
                self._recording_watchdog_tick,
            )

    def _recording_watchdog_tick(self):
        self.recording_watchdog_job = None
        if (
            not self.running
            or not self.is_recording
            or self.is_paused
            or self.is_finalizing
            or getattr(self, "is_pause_transitioning", False)
        ):
            return

        with self.process_lock:
            process = self.process

        return_code = None
        try:
            if process is not None:
                return_code = process.poll()
        except Exception as exc:
            self.log_exception("recording_watchdog.poll", exc)
            self.schedule_recording_watchdog()
            return

        if process is not None and return_code is None:
            self.schedule_recording_watchdog()
            return

        if process is None:
            reason = "Процесс записи FFmpeg неожиданно отсутствует."
        else:
            reason = f"FFmpeg неожиданно завершил запись с кодом {return_code}."
        self.recording_failure_reason = reason
        self.diagnostic_log(
            "recording_process_died_unexpectedly",
            {
                "recording_session_id": self.recording_session_id,
                "pid": getattr(process, "pid", None),
                "return_code": return_code,
                "segments": [str(path) for path in self.segments],
                "reason": reason,
            },
            level="ERROR",
        )
        self.append_problem_error("recording_process_died_unexpectedly", reason)
        try:
            self.status_var.set(reason + " Сохраняю доступную часть записи...")
        except Exception:
            pass
        # Обычный путь остановки проверит сегмент и сохранит всё, что FFmpeg
        # успел корректно записать. Пользователь не останется с ложным REC.
        self.stop_recording()

    @staticmethod
    def creation_flags():
        if os.name == "nt":
            return subprocess.CREATE_NO_WINDOW
        return 0

    @staticmethod
    def recording_creation_flags():
        if os.name == "nt":
            return subprocess.CREATE_NO_WINDOW | getattr(subprocess, "HIGH_PRIORITY_CLASS", 0)
        return 0

    def subprocess_cwd(self):
        """Безопасная рабочая папка для FFmpeg.

        Если FFmpeg запускается без cwd, он наследует текущую папку Python.
        Из-за этого даже фоновые индикаторы звука могут держать папку проекта
        или папку сохранения заблокированной в File Locksmith. Все дочерние
        процессы теперь стартуют из служебной папки данных.
        """
        for folder in (DATA_DIR, Path(tempfile.gettempdir())):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                return str(folder)
            except Exception:
                continue
        return None

    def register_child_process(self, process):
        if process is None:
            return process
        try:
            with self.child_processes_lock:
                self.child_processes.add(process)
        except Exception:
            pass
        return process

    def unregister_child_process(self, process):
        if process is None:
            return
        meta = None
        try:
            with self.child_processes_lock:
                self.child_processes.discard(process)
                meta = self._process_meta.pop(id(process), None)
        except Exception:
            pass
        if meta:
            try:
                returncode = process.poll()
                expected_returncodes = meta.get("expected_returncodes", (0,))
                expected = returncode is None or returncode in expected_returncodes
                self.diagnostic_log("subprocess_popen_finish", {
                    "process_log_id": meta.get("process_log_id"),
                    "pid": getattr(process, "pid", None),
                    "returncode": returncode,
                    "expected_returncode": expected,
                    "expected_returncodes": expected_returncodes,
                    "elapsed_sec": round(time.perf_counter() - float(meta.get("started_perf", time.perf_counter())), 3),
                    "command": meta.get("command"),
                    "name": meta.get("name"),
                }, level="INFO" if expected else "WARN")
            except Exception:
                pass

    def start_managed_process(self, command, **kwargs):
        """Запускает subprocess.Popen под контролем приложения.

        Важно: cwd задаётся всегда, а процесс регистрируется для последующего
        taskkill/kill при выходе. Это главный фикс зависших ffmpeg.exe.
        """
        expected_returncodes = kwargs.pop("expected_returncodes", (0,))
        try:
            expected_returncodes = tuple(int(code) for code in expected_returncodes)
        except Exception:
            expected_returncodes = (0,)
        kwargs.setdefault("cwd", self.subprocess_cwd())
        self._process_seq += 1
        process_log_id = self._process_seq
        started_perf = time.perf_counter()
        command_text = self.command_to_log_text(command)
        self.diagnostic_log("subprocess_popen_start", {
            "process_log_id": process_log_id,
            "command": command_text,
            "cwd": kwargs.get("cwd"),
            "stdin": self.stream_to_log_text(kwargs.get("stdin")),
            "stdout": self.stream_to_log_text(kwargs.get("stdout")),
            "stderr": self.stream_to_log_text(kwargs.get("stderr")),
            "text": kwargs.get("text"),
            "encoding": kwargs.get("encoding"),
            "expected_returncodes": expected_returncodes,
            "creationflags": kwargs.get("creationflags"),
        })
        if self.is_ffmpeg_related_command(command):
            self.append_ffmpeg_problem_log("Popen start", command=command, extra={
                "process_log_id": process_log_id,
                "cwd": kwargs.get("cwd"),
                "stdin": self.stream_to_log_text(kwargs.get("stdin")),
                "stdout": self.stream_to_log_text(kwargs.get("stdout")),
                "stderr": self.stream_to_log_text(kwargs.get("stderr")),
                "expected_returncodes": expected_returncodes,
            })
        try:
            process = subprocess.Popen(command, **kwargs)
        except Exception as exc:
            error_details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self.diagnostic_log("subprocess_popen_failed", {
                "process_log_id": process_log_id,
                "command": command_text,
                "cwd": kwargs.get("cwd"),
                "error": error_details,
            }, level="ERROR")
            if self.is_ffmpeg_related_command(command):
                self.append_ffmpeg_problem_log("Popen failed", command=command, stderr=error_details, extra={"process_log_id": process_log_id})
            self.append_problem_error("subprocess_popen_failed", error_details)
            raise
        try:
            with self.child_processes_lock:
                self._process_meta[id(process)] = {
                    "process_log_id": process_log_id,
                    "started_perf": started_perf,
                    "command": command_text,
                    "expected_returncodes": expected_returncodes,
                    "name": Path(str(command[0])).name if isinstance(command, (list, tuple)) and command else str(command)[:80],
                }
        except Exception:
            pass
        self.diagnostic_log("subprocess_popen_started", {
            "process_log_id": process_log_id,
            "pid": getattr(process, "pid", None),
            "elapsed_start_sec": round(time.perf_counter() - started_perf, 3),
        })
        self.register_child_process(process)
        return process

    def run_managed_process(self, command, **kwargs):
        expected_returncodes = kwargs.pop("expected_returncodes", (0,))
        try:
            expected_returncodes = tuple(int(code) for code in expected_returncodes)
        except Exception:
            expected_returncodes = (0,)
        kwargs.setdefault("cwd", self.subprocess_cwd())
        self._process_seq += 1
        process_log_id = self._process_seq
        started_perf = time.perf_counter()
        command_text = self.command_to_log_text(command)
        expected_device_probe = self.is_expected_ffmpeg_device_probe_command(command)
        self.diagnostic_log("subprocess_run_start", {
            "process_log_id": process_log_id,
            "command": command_text,
            "cwd": kwargs.get("cwd"),
            "timeout": kwargs.get("timeout"),
            "expected_returncodes": expected_returncodes,
            "capture_output": kwargs.get("capture_output"),
            "stdout": self.stream_to_log_text(kwargs.get("stdout")),
            "stderr": self.stream_to_log_text(kwargs.get("stderr")),
            "text": kwargs.get("text"),
            "encoding": kwargs.get("encoding"),
            "creationflags": kwargs.get("creationflags"),
            "expected_device_probe": expected_device_probe,
        })
        try:
            result = subprocess.run(command, **kwargs)
            expected = result.returncode in expected_returncodes
            stdout_for_log = getattr(result, "stdout", None)
            stderr_for_log = getattr(result, "stderr", None)
            if expected_device_probe:
                stderr_for_log = self.normalize_expected_ffmpeg_probe_stderr(stderr_for_log)
            self.diagnostic_log("subprocess_run_finish", {
                "process_log_id": process_log_id,
                "returncode": result.returncode,
                "expected_returncode": expected,
                "expected_device_probe": expected_device_probe,
                "expected_device_probe_note": self.expected_ffmpeg_probe_note(command) if expected_device_probe else None,
                "elapsed_sec": round(time.perf_counter() - started_perf, 3),
                "command": command_text,
                "stdout": "<omitted: expected device probe>" if expected_device_probe and expected else self._safe_log_value(stdout_for_log, max_text=3000),
                "stderr": "<omitted: expected device probe; device list is in audio_devices_refreshed>" if expected_device_probe and expected else self._safe_log_value(stderr_for_log, max_text=3000),
            }, level="INFO" if expected else "WARN")
            if self.is_ffmpeg_related_command(command):
                extra = {
                    "process_log_id": process_log_id,
                    "returncode": result.returncode,
                    "expected_returncode": expected,
                    "expected_device_probe": expected_device_probe,
                    "elapsed_sec": round(time.perf_counter() - started_perf, 3),
                }
                if expected_device_probe:
                    extra["note_for_ai"] = self.expected_ffmpeg_probe_note(command)
                self.append_ffmpeg_problem_log("subprocess.run finish", command=command,
                    stdout=stdout_for_log, stderr=stderr_for_log, extra=extra)
            return result
        except subprocess.TimeoutExpired as exc:
            self.diagnostic_log("subprocess_run_timeout", {
                "process_log_id": process_log_id,
                "elapsed_sec": round(time.perf_counter() - started_perf, 3),
                "timeout": kwargs.get("timeout"),
                "command": command_text,
                "stdout": self._safe_log_value(getattr(exc, "stdout", None), max_text=8000),
                "stderr": self._safe_log_value(getattr(exc, "stderr", None), max_text=8000),
            }, level="ERROR")
            if self.is_ffmpeg_related_command(command):
                self.append_ffmpeg_problem_log("subprocess.run timeout", command=command,
                    stdout=getattr(exc, "stdout", None), stderr=getattr(exc, "stderr", None),
                    extra={"process_log_id": process_log_id, "timeout": kwargs.get("timeout"), "elapsed_sec": round(time.perf_counter() - started_perf, 3)})
            self.append_problem_error("subprocess_run_timeout", f"command={command_text}\nstdout={getattr(exc, 'stdout', None)}\nstderr={getattr(exc, 'stderr', None)}")
            raise
        except Exception as exc:
            error_details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self.diagnostic_log("subprocess_run_failed", {
                "process_log_id": process_log_id,
                "elapsed_sec": round(time.perf_counter() - started_perf, 3),
                "command": command_text,
                "error": error_details,
            }, level="ERROR")
            if self.is_ffmpeg_related_command(command):
                self.append_ffmpeg_problem_log("subprocess.run failed", command=command, stderr=error_details, extra={"process_log_id": process_log_id})
            self.append_problem_error("subprocess_run_failed", error_details)
            raise

    def terminate_process_tree(self, process, timeout=2.0, name="process"):
        """Корректно, а затем принудительно завершает процесс и его дерево."""
        if process is None:
            return
        try:
            if process.poll() is not None:
                self.unregister_child_process(process)
                return
        except Exception:
            pass

        try:
            if getattr(process, "stdin", None):
                try:
                    process.stdin.close()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            process.terminate()
            process.wait(timeout=timeout)
            self.unregister_child_process(process)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=max(0.5, min(2.0, float(timeout or 0.8))),
                    creationflags=self.creation_flags(),
                    cwd=self.subprocess_cwd(),
                )
            except Exception:
                pass

        try:
            if process.poll() is None:
                process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=1.5)
        except Exception:
            pass
        self.unregister_child_process(process)

    def force_shutdown_child_processes(self):
        """Финальная зачистка всех дочерних FFmpeg/ffprobe перед выходом."""
        try:
            with self.child_processes_lock:
                processes = list(self.child_processes)
        except Exception:
            processes = []
        for process in processes:
            self.terminate_process_tree(process, timeout=0.8, name="shutdown_child")
        try:
            with self.child_processes_lock:
                self.child_processes.clear()
        except Exception:
            pass

    def cleanup_stale_ffmpeg_processes_from_previous_runs(self):
        """Убирает старые ffmpeg.exe, оставленные предыдущими версиями программы."""
        if os.name != "nt":
            return
        self.diagnostic_log("cleanup_stale_ffmpeg_start")
        try:
            ps_script = (
                "Get-CimInstance Win32_Process -Filter \"name = 'ffmpeg.exe'\" | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=6,
                creationflags=self.creation_flags(),
                cwd=self.subprocess_cwd(),
            )
            raw = (result.stdout or "").strip()
            if not raw:
                return
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            # Только уникальные маркеры нашей программы, чтобы не убить чужой
            # ffmpeg. Голую подстроку "recording_temp" убрали — она слишком общая.
            markers = [
                "screenrecorderprowin11",            # -metadata encoder=... нашей записи
                "lavfi.astats.overall.rms_level",    # наши аудио-индикаторы
                "astats=metadata=1:reset=0.25",
                str(TEMP_RECORDINGS_DIR).lower().replace("\\", "\\\\"),
                str(TEMP_RECORDINGS_DIR).lower(),
            ]
            for item in data:
                try:
                    pid = int(item.get("ProcessId"))
                    cmd = str(item.get("CommandLine") or "").lower()
                    if not cmd:
                        continue
                    if any(marker and marker in cmd for marker in markers):
                        self.diagnostic_log("cleanup_stale_ffmpeg_kill", {
                            "pid": pid,
                            "command": item.get("CommandLine"),
                        }, level="WARN")
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=4,
                            creationflags=self.creation_flags(),
                            cwd=self.subprocess_cwd(),
                        )
                except Exception:
                    pass
            self.diagnostic_log("cleanup_stale_ffmpeg_finish", {"checked_processes": len(data)})
        except Exception as exc:
            self.diagnostic_log("cleanup_stale_ffmpeg_failed", {
                "error": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            }, level="WARN")
