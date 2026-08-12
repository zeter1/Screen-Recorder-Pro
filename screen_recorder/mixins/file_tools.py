from ..shared import *


class FileToolsMixin:
    def reveal_in_file_manager(self, path):
        """Открывает папку с видео и выделяет конкретный файл.

        Старый способ через subprocess.Popen(["explorer.exe", "/select,..."])
        на некоторых Windows 11 открывал «Документы», особенно когда путь был
        на другом диске, с пробелами или кириллицей. Поэтому для Windows сначала
        используется штатный Shell API SHOpenFolderAndSelectItems. Это именно
        API проводника для открытия папки и выбора файла.
        """
        try:
            if not path:
                messagebox.showinfo("Видео не найдено", "Сначала запиши и сохрани видео.")
                return False

            target = Path(path).expanduser()
            try:
                target = target.resolve(strict=False)
            except Exception:
                target = Path(path).expanduser()

            if os.name == "nt":
                if target.is_file():
                    return self.select_file_in_windows_explorer(target)
                if target.is_dir():
                    try:
                        os.startfile(str(target))
                        return True
                    except Exception:
                        return self.open_folder_with_explorer_fallback(target)
                messagebox.showinfo("Видео не найдено", f"Файл уже не найден:\n{target}")
                return False

            if sys.platform == "darwin":
                if target.is_file():
                    subprocess.Popen(["open", "-R", str(target)])
                    return True
                if target.is_dir():
                    subprocess.Popen(["open", str(target)])
                    return True
                messagebox.showinfo("Видео не найдено", f"Файл уже не найден:\n{target}")
                return False

            if target.is_file():
                subprocess.Popen(["xdg-open", str(target.parent)])
                return True
            if target.is_dir():
                subprocess.Popen(["xdg-open", str(target)])
                return True
            messagebox.showinfo("Видео не найдено", f"Файл уже не найден:\n{target}")
            return False
        except Exception as exc:
            messagebox.showerror("Не удалось открыть", str(exc))
            return False

    def select_file_in_windows_explorer(self, target):
        """Надёжно выделяет файл в Проводнике Windows.

        Возвращает True, если удалось хотя бы открыть нужную папку. Сначала
        пробуем SHOpenFolderAndSelectItems — это самый правильный путь. Если
        Shell API по какой-то причине недоступен, делаем fallback через
        ShellExecuteW, а затем просто открываем родительскую папку, чтобы Windows
        не уводил пользователя в «Документы».
        """
        target = Path(target)
        if not target.is_file():
            messagebox.showinfo("Видео не найдено", f"Файл уже не найден:\n{target}")
            return False

        # 1) Основной способ: Windows Shell API, без командной строки Explorer.
        try:
            shell32 = ctypes.windll.shell32
            ole32 = ctypes.windll.ole32
            co_initialized = False
            try:
                hr_init = ole32.CoInitialize(None)
                co_initialized = hr_init in (0, 1)
            except Exception:
                co_initialized = False

            pidl = None
            try:
                shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
                shell32.ILCreateFromPathW.restype = ctypes.c_void_p
                shell32.SHOpenFolderAndSelectItems.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_uint,
                    ctypes.c_void_p,
                    ctypes.c_uint,
                ]
                shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
                try:
                    shell32.ILFree.argtypes = [ctypes.c_void_p]
                    shell32.ILFree.restype = None
                except Exception:
                    pass

                pidl = shell32.ILCreateFromPathW(str(target))
                if pidl:
                    hr = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
                    if hr == 0:
                        return True
            finally:
                try:
                    if pidl:
                        shell32.ILFree(pidl)
                except Exception:
                    pass
                if co_initialized:
                    try:
                        ole32.CoUninitialize()
                    except Exception:
                        pass
        except Exception:
            pass

        # 2) Запасной способ: ShellExecuteW передаёт параметры Explorer корректнее,
        # чем subprocess с массивом аргументов.
        try:
            params = f'/select,"{str(target)}"'
            result = ctypes.windll.shell32.ShellExecuteW(None, "open", "explorer.exe", params, None, 1)
            if int(result) > 32:
                return True
        except Exception:
            pass

        # 3) Последний безопасный fallback: открываем именно родительскую папку
        # видео. Это лучше, чем попасть в «Документы».
        return self.open_folder_with_explorer_fallback(target.parent)

    def open_folder_with_explorer_fallback(self, folder):
        """Открывает конкретную папку в проводнике и не даёт провалиться в Документы."""
        try:
            folder = Path(folder)
            if not folder.exists() or not folder.is_dir():
                messagebox.showinfo("Папка не найдена", f"Папка уже не найдена:\n{folder}")
                return False
            if os.name == "nt":
                try:
                    os.startfile(str(folder))
                    return True
                except Exception:
                    result = ctypes.windll.shell32.ShellExecuteW(None, "open", str(folder), None, None, 1)
                    return int(result) > 32
            subprocess.Popen(["xdg-open", str(folder)])
            return True
        except Exception as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc))
            return False

    def open_file_default_app(self, path):
        try:
            target = Path(path)
            if os.name == "nt":
                os.startfile(str(target))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
            return True
        except Exception as exc:
            messagebox.showerror("Не удалось открыть", str(exc))
            return False

    def open_last_output_folder(self):
        """Открывает папку с последним сохранённым видео и выделяет сам файл."""
        path = self.last_output_path
        if not path or not Path(path).exists() or not Path(path).is_file():
            messagebox.showinfo(
                "Видео не найдено",
                "Последнее сохранённое видео пока не найдено.\n\n"
                "Сначала останови запись и дождись сообщения «Файл сохранён»."
            )
            self.last_output_path = None
            self.update_after_recording_buttons()
            return
        self.reveal_in_file_manager(path)

    def open_last_log(self):
        if not self.should_write_problem_logs():
            messagebox.showinfo("Логи проблем", "Логи проблем сейчас выключены в настройках.")
            return
        path = self.last_debug_log_path or self.current_log_path
        if path and Path(path).exists():
            self.open_file_default_app(path)
        else:
            messagebox.showinfo("Лог", "Лог последней записи пока не найден.")

    def _last_output_or_warn(self):
        path = getattr(self, "last_output_path", None)
        if not path or not Path(path).is_file():
            messagebox.showinfo("Видео не найдено", "Сначала останови запись и дождись сообщения «Файл сохранён».")
            return None
        return Path(path)

    def get_ffprobe_path(self):
        """Находит ffprobe в PATH или рядом с используемым ffmpeg/EXE."""
        candidates = []
        try:
            ffmpeg_path = Path(str(self.ffmpeg_path))
            probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
            candidates.append(ffmpeg_path.with_name(probe_name))
        except Exception:
            pass
        probe_in_path = shutil.which("ffprobe")
        if probe_in_path:
            candidates.append(Path(probe_in_path))
        probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        candidates.extend([
            APP_DIR / probe_name,
            APP_DIR / "ffmpeg" / "bin" / probe_name,
        ])
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return str(candidate)
            except Exception:
                continue
        return None

    def get_media_duration(self, path):
        ffprobe = self.get_ffprobe_path()
        if not ffprobe:
            return None
        try:
            result = self.run_managed_process(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="ignore", timeout=20, creationflags=self.creation_flags(),
            )
            return float((result.stdout or "").strip())
        except Exception:
            return None

    def _run_export_in_thread(self, command, out_path, busy_text, done_text):
        """Гоняет ffmpeg-экспорт в фоне, не морозя GUI; сообщает результат."""
        self.status_var.set(busy_text)

        def worker():
            ok = False
            err = ""
            try:
                self.run_managed_process(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    encoding="utf-8", errors="ignore", timeout=1800, creationflags=self.creation_flags(),
                )
                ok = Path(out_path).exists() and Path(out_path).stat().st_size > 0
            except Exception as exc:
                err = str(exc)
            def finish():
                if ok:
                    self.status_var.set(done_text)
                    self.reveal_in_file_manager(out_path)
                else:
                    self.status_var.set("Не удалось выполнить экспорт.")
                    messagebox.showerror("Ошибка экспорта", err or "FFmpeg не создал файл.")
            self.root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def make_gif_from_last_output(self):
        src = self._last_output_or_warn()
        if not src:
            return
        out_path = src.with_name(src.stem + ".gif")
        fps = 15
        # Двухпроходный палитровый GIF — заметно качественнее наивного.
        vf = (f"fps={fps},scale=640:-1:flags=lanczos,split[s0][s1];"
              f"[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3")
        command = [self.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning",
                   "-i", str(src), "-vf", vf, "-loop", "0", str(out_path)]
        self._run_export_in_thread(command, out_path, "Делаю GIF...", f"GIF готов: {out_path}")

    def trim_last_output_dialog(self):
        from tkinter import simpledialog
        src = self._last_output_or_warn()
        if not src:
            return
        duration = self.get_media_duration(src)
        info = f"Длительность: {duration:.1f} сек.\n" if duration else ""
        try:
            head = simpledialog.askfloat("Обрезать концы", info + "Сколько секунд убрать с начала?",
                                         initialvalue=0, minvalue=0, parent=self.root)
            if head is None:
                return
            tail = simpledialog.askfloat("Обрезать концы", "Сколько секунд убрать с конца?",
                                         initialvalue=0, minvalue=0, parent=self.root)
            if tail is None:
                return
        except Exception:
            return
        if duration:
            new_len = duration - head - tail
            if new_len <= 0:
                messagebox.showwarning("Обрезка", "После обрезки ничего не останется. Уменьши значения.")
                return
        ext = src.suffix.lower().lstrip(".")
        out_path = src.with_name(src.stem + " (обрезано)" + src.suffix)
        # -ss перед -i = быстрый seek; -c copy режет по ключевым кадрам (быстро,
        # без перекодирования). Возможен сдвиг на доли секунды до ближайшего keyframe.
        command = [self.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning",
                   "-ss", str(head), "-i", str(src)]
        if duration:
            command += ["-t", str(max(0.1, duration - head - tail))]
        command += ["-c", "copy"]
        if ext in ("mp4", "mov"):
            command += [
                "-movflags", "+faststart",
                "-video_track_timescale", str(self.MP4_VIDEO_TRACK_TIMESCALE),
            ]
            if self.should_use_hevc():
                command += ["-tag:v", "hvc1"]
        command += [str(out_path)]
        self._run_export_in_thread(command, out_path, "Обрезаю видео...", f"Готово: {out_path}")

    def update_after_recording_buttons(self):
        try:
            output_exists = bool(self.last_output_path and Path(self.last_output_path).is_file())
            if self.open_output_folder_button is not None:
                self.open_output_folder_button.configure(state="normal" if output_exists else "disabled")
            for btn in (self.make_gif_button, self.trim_button):
                if btn is not None:
                    btn.configure(state="normal" if output_exists else "disabled")
        except Exception:
            pass
        try:
            log_exists = bool(self.last_debug_log_path and Path(self.last_debug_log_path).exists())
            if self.open_log_button is not None:
                self.open_log_button.configure(state="normal" if log_exists else "disabled")
        except Exception:
            pass
        try:
            if self.annotation_overlay is not None:
                self.annotation_overlay.update_record_controls()
        except Exception:
            pass
