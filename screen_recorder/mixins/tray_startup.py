from ..shared import *


class TrayStartupMixin:
    def get_startup_command(self):
        """Возвращает переносимую команду Run для .py и собранного .exe."""
        if getattr(sys, "frozen", False):
            executable = Path(sys.executable).resolve()
            if not executable.is_file():
                raise FileNotFoundError(f"Не найден файл программы для автозапуска: {executable}")
            return f'"{executable}" --tray'

        # sys.argv[0] может быть относительным и при запуске из реестра
        # разрешиться относительно другой рабочей папки. __file__ всегда
        # указывает именно на эту копию программы.
        script_path = get_program_entry_path()
        exe = Path(sys.executable).resolve()
        if exe.name.lower() == "python.exe":
            pythonw = exe.with_name("pythonw.exe")
            if pythonw.is_file():
                exe = pythonw
        if not exe.is_file():
            raise FileNotFoundError(f"Не найден Python для автозапуска: {exe}")
        if not script_path.is_file():
            raise FileNotFoundError(f"Не найден файл программы для автозапуска: {script_path}")
        return f'"{exe}" "{script_path}" --tray'

    def sync_startup_tray_setting(self, show_errors=True, source="settings"):
        """Синхронизирует настройку с HKCU Run и проверяет результат записи."""
        if os.name != "nt":
            return False
        enabled = bool(self.startup_tray_var.get())
        expected_command = None
        previous_command = None
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            access = winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, access) as key:
                try:
                    previous_command = winreg.QueryValueEx(key, APP_NAME)[0]
                except FileNotFoundError:
                    previous_command = None

                if enabled:
                    expected_command = self.get_startup_command()
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, expected_command)
                    actual_command, actual_type = winreg.QueryValueEx(key, APP_NAME)
                    if actual_type != winreg.REG_SZ or str(actual_command) != expected_command:
                        raise RuntimeError(
                            "Windows не подтвердила записанную команду автозапуска"
                        )
                    self.status_var.set("Автозапуск включён: при входе в Windows появятся трей и плавающая кнопка.")
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
                    try:
                        winreg.QueryValueEx(key, APP_NAME)
                    except FileNotFoundError:
                        pass
                    else:
                        raise RuntimeError("Windows не удалила запись автозапуска")
                    self.status_var.set("Автозапуск программы выключен.")

            self.diagnostic_log("windows_startup_synchronized", {
                "source": source,
                "enabled": enabled,
                "previous_command": previous_command,
                "expected_command": expected_command,
                "verified": True,
            })
            return True
        except Exception as exc:
            self.diagnostic_log("windows_startup_sync_failed", {
                "source": source,
                "enabled": enabled,
                "previous_command": previous_command,
                "expected_command": expected_command,
                "error": repr(exc),
            }, level="ERROR")
            self.status_var.set(f"Не удалось изменить автозапуск: {exc}")
            if show_errors:
                messagebox.showerror("Ошибка автозапуска", f"Не удалось изменить автозапуск:\n{exc}")
            return False

    def create_tray_image(self):
        """Создаёт понятную иконку для системного трея.

        Иконка зависит только от Pillow и не использует захват экрана.
        Поэтому трей может работать независимо от модулей записи.
        """
        if not PIL_AVAILABLE:
            return None
        image = Image.new("RGBA", (64, 64), (30, 30, 30, 255))
        draw = ImageDraw.Draw(image)
        # Красная кнопка записи
        draw.ellipse((13, 13, 51, 51), fill=(230, 60, 60, 255), outline=(255, 255, 255, 255), width=3)
        # Белый квадрат внутри — как стоп/rec-индикатор
        draw.rectangle((27, 27, 37, 37), fill=(255, 255, 255, 255))
        # Маленькая синяя полоска, чтобы иконка отличалась от других красных значков
        draw.rectangle((6, 54, 58, 60), fill=(80, 170, 255, 255))
        return image

    def tray_available(self):
        return bool(TRAY_AVAILABLE and PIL_AVAILABLE)

    def run_tray_icon_safely(self):
        """Запускает pystray и сохраняет ошибку, если иконка не создалась."""
        try:
            def setup(icon):
                try:
                    icon.visible = True
                except Exception:
                    pass
                self.tray_ready_event.set()
                self.diagnostic_log("tray_icon_ready", {
                    "started_from_windows_startup": self.started_from_windows_startup,
                })
                try:
                    icon.notify("Screen Recorder Pro работает в трее", "Управление записью — через плавающую панель. ПКМ по иконке → Закрыть программу")
                except Exception:
                    pass

            self.tray_icon.run(setup=setup)
        except Exception as exc:
            self.tray_error = exc
            self.tray_icon = None
            self.tray_ready_event.set()
            self.log_exception("run_tray_icon_safely", exc)

    def ensure_tray_icon(self):
        """Гарантирует создание иконки в трее перед скрытием окна."""
        if self.tray_icon is not None:
            thread = getattr(self, "tray_thread", None)
            if self.tray_error is None and thread is not None and thread.is_alive():
                return True
            self.tray_icon = None

        if not self.tray_available():
            missing = []
            if not TRAY_AVAILABLE:
                missing.append("pystray")
            if not PIL_AVAILABLE:
                missing.append("pillow")
            msg = "Для настоящего трея нужно установить: " + " ".join(missing or ["pystray", "pillow"])
            self.status_var.set(msg)
            if not self.tray_unavailable_warned:
                self.tray_unavailable_warned = True
                try:
                    messagebox.showwarning(
                        "Трей недоступен",
                        msg + "\n\nВыполни в командной строке:\n"
                              "pip install pystray pillow\n\n"
                              "Пока зависимости не установлены, крестик не сможет убрать программу именно в трей."
                    )
                except Exception:
                    pass
            self.diagnostic_log("tray_icon_dependencies_missing", {
                "missing": missing,
                "started_from_windows_startup": self.started_from_windows_startup,
            }, level="ERROR")
            return False

        self.tray_error = None
        self.tray_ready_event.clear()

        menu = pystray.Menu(
            pystray.MenuItem("Показать плавающую панель", lambda _icon, _item: self.root.after(0, self.show_from_tray), default=True),
            pystray.MenuItem("Настройки", lambda _icon, _item: self.root.after(0, self.open_settings_window)),
            pystray.MenuItem("Начать запись", lambda _icon, _item: self.root.after(0, self.start_recording)),
            pystray.MenuItem("Остановить запись", lambda _icon, _item: self.root.after(0, self.stop_recording)),
            pystray.MenuItem("Закрыть программу", lambda _icon, _item: self.root.after(0, self.exit_app)),
        )
        self.tray_icon = pystray.Icon(APP_NAME, self.create_tray_image(), "Screen Recorder Pro", menu)
        self.tray_thread = threading.Thread(target=self.run_tray_icon_safely, daemon=True)
        self.tray_thread.start()

        # Даём pystray короткий момент реально зарегистрировать иконку в Windows,
        # а уже потом скрываем окно. Раньше окно могло исчезать, а иконка ещё не
        # успевала появиться или ошибка терялась в фоновом потоке.
        started = time.time()
        while time.time() - started < 1.2:
            if self.tray_ready_event.is_set():
                break
            try:
                self.root.update_idletasks()
            except Exception:
                pass
            time.sleep(0.03)

        if not self.tray_ready_event.is_set():
            timeout_error = RuntimeError("Windows не подтвердила создание иконки в трее за 1.2 секунды")
            self.tray_error = timeout_error
            icon = self.tray_icon
            self.tray_icon = None
            try:
                if icon is not None:
                    icon.stop()
            except Exception:
                pass
            self.log_exception("ensure_tray_icon.timeout", timeout_error)
            self.status_var.set(str(timeout_error))
            return False

        if self.tray_error is not None:
            error_text = str(self.tray_error)
            self.tray_icon = None
            self.status_var.set(f"Не удалось создать иконку в трее: {error_text}")
            try:
                messagebox.showerror(
                    "Ошибка трея",
                    "Не удалось создать иконку в системном трее.\n\n"
                    f"Ошибка: {error_text}\n\n"
                    "Проверь установку:\n"
                    "pip install pystray pillow"
                )
            except Exception:
                pass
            return False

        return True

    def minimize_to_tray(self):
        """Сворачивает программу в системный трей, не закрывая запись и фоновые службы."""
        ok = self.ensure_tray_icon()
        if not ok:
            # Не закрываем программу. Оставляем обычным свёрнутым окном, чтобы
            # пользователь не подумал, что она исчезла без иконки.
            try:
                self.root.iconify()
            except Exception:
                pass
            return False

        self.root.withdraw()
        try:
            self.show_annotation_overlay(open_toolbar=True)
        except Exception:
            pass
        self.status_var.set("Программа работает в трее. Управление записью — через плавающую панель.")
        return True

    def show_from_tray(self):
        # В новой версии пункт «Показать» не раскрывает большое окно.
        # Он возвращает только плавающую панель записи.
        try:
            self.root.withdraw()
        except Exception:
            pass
        try:
            self.show_annotation_overlay(open_toolbar=True)
        except Exception as exc:
            self.status_var.set(f"Не удалось показать плавающую панель: {exc}")
