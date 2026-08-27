from ..shared import *


class UiMixin:
    def setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#1e1e1e")
        style.configure("TLabel", background="#1e1e1e", foreground="white", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=8)
        style.configure("TEntry", padding=5)
        style.configure("TCombobox", padding=5)
        style.configure("TLabelframe", background="#1e1e1e", foreground="white")
        style.configure("TLabelframe.Label", background="#1e1e1e", foreground="white", font=("Segoe UI", 10, "bold"))
        style.configure("Red.TLabel", background="#1e1e1e", foreground="#ff4d4d", font=("Segoe UI", 13, "bold"))
        style.configure("Green.TLabel", background="#1e1e1e", foreground="#6ee36e", font=("Segoe UI", 13, "bold"))
        style.configure("Yellow.TLabel", background="#1e1e1e", foreground="#ffd166", font=("Segoe UI", 13, "bold"))
        style.configure("Audio.Horizontal.TProgressbar", troughcolor="#333333", background="#6ee36e", thickness=12)

    def create_ui(self):
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=0)
        file_menu.add_command(label="Настройки", command=self.open_settings_window)
        file_menu.add_separator()
        file_menu.add_command(label="Свернуть в трей", command=self.minimize_to_tray)
        # Главное закрытие программы теперь делается из иконки в трее:
        # правой кнопкой мыши -> «Закрыть программу». Крестик окна только сворачивает.
        file_menu.add_command(label="Закрыть программу", command=self.exit_app)
        menu.add_cascade(label="Файл", menu=file_menu)
        self.root.config(menu=menu)

        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=12, pady=12)

        left = ttk.Frame(main, width=345)
        left.pack(side="left", fill="y", padx=(0, 12))
        left.pack_propagate(False)

        right = ttk.Frame(main)
        right.pack(side="right", fill="both", expand=True)

        self.create_left_panel(left)
        self.create_right_panel(right)

    def create_left_panel(self, parent):
        quick_box = ttk.LabelFrame(parent, text="Быстрый доступ")
        quick_box.pack(fill="x", pady=(0, 10))

        ttk.Button(
            quick_box,
            text="⚙ Настройки",
            command=self.open_settings_window,
        ).pack(fill="x", padx=10, pady=(10, 8))

        ttk.Label(
            quick_box,
            text="Все параметры записи находятся в отдельном окне настроек. Настроил один раз — закрыл окно. Если папка программы доступна для записи, настройки сохраняются рядом с программой; если нет — в локальной папке пользователя.",
            wraplength=310,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        info_box = ttk.LabelFrame(parent, text="Текущие параметры")
        info_box.pack(fill="x", pady=(0, 10))

        ttk.Label(info_box, text="Папка сохранения:").pack(anchor="w", padx=10, pady=(10, 2))
        ttk.Label(info_box, textvariable=self.output_folder, wraplength=310, foreground="#cfcfcf").pack(anchor="w", padx=10, pady=(0, 8))

        ttk.Label(info_box, text="Имя видео:").pack(anchor="w", padx=10, pady=(0, 2))
        ttk.Label(info_box, text="Запись экрана [дата и время сохранения]", foreground="#cfcfcf").pack(anchor="w", padx=10, pady=(0, 8))

        quality_row = ttk.Frame(info_box)
        quality_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(quality_row, text="Формат:").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Label(quality_row, textvariable=self.format_var, foreground="#cfcfcf").grid(row=0, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(quality_row, text="FPS:").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Label(quality_row, textvariable=self.fps_var, foreground="#cfcfcf").grid(row=1, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(quality_row, text="Подстройка FPS:").grid(row=2, column=0, sticky="w", pady=2)
        self.auto_adjust_fps_text = tk.StringVar()
        self.update_auto_adjust_fps_text()
        ttk.Label(quality_row, textvariable=self.auto_adjust_fps_text, foreground="#cfcfcf").grid(row=2, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(quality_row, text="Видео:").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Label(quality_row, textvariable=self.video_bitrate_var, foreground="#cfcfcf").grid(row=3, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(quality_row, text="Захват:").grid(row=4, column=0, sticky="w", pady=2)
        ttk.Label(quality_row, textvariable=self.capture_method_var, foreground="#cfcfcf", wraplength=220).grid(row=4, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(quality_row, text="Кодер:").grid(row=5, column=0, sticky="w", pady=2)
        ttk.Label(quality_row, textvariable=self.encoder_var, foreground="#cfcfcf", wraplength=220).grid(row=5, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(quality_row, text="Аудио:").grid(row=6, column=0, sticky="w", pady=2)
        ttk.Label(quality_row, textvariable=self.audio_bitrate_var, foreground="#cfcfcf").grid(row=6, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(quality_row, text="Курсор:").grid(row=7, column=0, sticky="w", pady=2)
        self.cursor_state_text = tk.StringVar()
        self.update_cursor_state_text()
        ttk.Label(quality_row, textvariable=self.cursor_state_text, foreground="#cfcfcf", wraplength=220).grid(row=7, column=1, sticky="w", padx=(8, 0), pady=2)

        draw_box = ttk.LabelFrame(parent, text="Плавающая панель")
        draw_box.pack(fill="x")
        ttk.Checkbutton(
            draw_box,
            text="Плавающая панель включена всегда",
            variable=self.draw_enabled_var,
            state="disabled",
        ).pack(anchor="w", padx=10, pady=(10, 4))
        ttk.Label(
            draw_box,
            text="Панель висит поверх рабочего стола, раскрывается при наведении на индикатор ● и умеет запускать/останавливать запись, ставить паузу и рисовать поверх экрана.",
            wraplength=310,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

    def open_settings_window(self):
        if getattr(self, "is_recording", False) or getattr(self, "is_finalizing", False):
            self.status_var.set("Настройки заблокированы до окончания записи и сохранения файла.")
            return
        if self.settings_window is not None:
            try:
                if self.settings_window.winfo_exists():
                    self.settings_window.deiconify()
                    self.settings_window.lift()
                    self.settings_window.focus_force()
                    self.refresh_audio_devices(silent=True)
                    self.start_audio_meters()
                    self.schedule_audio_device_refresh(90000)
                    return
            except Exception:
                self.settings_window = None

        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title("Настройки записи")
        window.geometry("620x640")
        window.minsize(560, 540)
        window.configure(bg="#1e1e1e")
        # В минимальном режиме root скрыт. Если сделать settings transient к
        # скрытому root, окно настроек может оказаться за другими окнами или не
        # попасть на панель задач. Привязываем transient только когда root видим.
        try:
            if str(self.root.state()) != "withdrawn":
                window.transient(self.root)
        except Exception:
            pass
        try:
            window.lift()
            window.focus_force()
            window.attributes("-topmost", True)
            window.after(250, lambda: window.attributes("-topmost", False))
            window.after(80, lambda: self.maximize_settings_window(window))
        except Exception:
            pass

        def on_close():
            if getattr(self, "screenshot_hotkey_capture_active", False):
                self.cancel_screenshot_hotkey_capture()
            self.save_settings()
            self.mic_combo = None
            self.system_combo = None
            self.webcam_combo = None
            self.hotkey_combo = None
            self.screenshot_hotkey_combo = None
            self.settings_window = None
            self.cancel_audio_device_refresh()
            self.stop_audio_meters(join_timeout=0.35)
            try:
                window.destroy()
            except Exception:
                pass

        window.protocol("WM_DELETE_WINDOW", on_close)

        root_frame = ttk.Frame(window)
        root_frame.pack(fill="both", expand=True, padx=12, pady=12)

        header = ttk.Frame(root_frame)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="Настройки", font=("Segoe UI", 15, "bold")).pack(side="left")
        ttk.Button(header, text="Закрыть", command=on_close).pack(side="right")

        notebook = ttk.Notebook(root_frame)
        notebook.pack(fill="both", expand=True)

        save_tab_outer = ttk.Frame(notebook)
        video_tab_outer = ttk.Frame(notebook)
        audio_tab_outer = ttk.Frame(notebook)
        screenshot_tab_outer = ttk.Frame(notebook)
        extra_tab_outer = ttk.Frame(notebook)
        logs_tab_outer = ttk.Frame(notebook)

        notebook.add(save_tab_outer, text="Сохранение")
        notebook.add(video_tab_outer, text="Видео")
        notebook.add(audio_tab_outer, text="Звук")
        notebook.add(screenshot_tab_outer, text="Скриншот")
        notebook.add(extra_tab_outer, text="Дополнительно")
        notebook.add(logs_tab_outer, text="Логи проблем")

        save_tab = self.create_scrollable_settings_tab(save_tab_outer)
        video_tab = self.create_scrollable_settings_tab(video_tab_outer)
        audio_tab = self.create_scrollable_settings_tab(audio_tab_outer)
        screenshot_tab = self.create_scrollable_settings_tab(screenshot_tab_outer)
        extra_tab = self.create_scrollable_settings_tab(extra_tab_outer)
        logs_tab = self.create_scrollable_settings_tab(logs_tab_outer)

        # -------- Сохранение --------
        save_box = ttk.LabelFrame(save_tab, text="Файл")
        save_box.pack(fill="x", padx=10, pady=10)
        ttk.Label(save_box, text="Папка сохранения:").pack(anchor="w", padx=10, pady=(10, 2))
        folder_row = ttk.Frame(save_box)
        folder_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Entry(folder_row, textvariable=self.output_folder).pack(side="left", fill="x", expand=True)
        ttk.Button(folder_row, text="...", width=4, command=self.choose_folder).pack(side="right", padx=(6, 0))

        ttk.Label(save_box, text="Имя видео создаётся автоматически:").pack(anchor="w", padx=10, pady=(0, 2))
        ttk.Label(save_box, text="Запись экрана [дата и время сохранения]", foreground="#cfcfcf").pack(anchor="w", padx=10, pady=(0, 10))

        ttk.Label(save_box, text="Формат видео:").pack(anchor="w", padx=10, pady=(0, 2))
        ttk.Combobox(save_box, textvariable=self.format_var, values=VIDEO_FORMATS, state="readonly").pack(fill="x", padx=10, pady=(0, 10))

        settings_file_box = ttk.LabelFrame(save_tab, text="Файл настроек")
        settings_file_box.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(settings_file_box, text="settings.json сохраняется в первой доступной для записи папке:", foreground="#cfcfcf").pack(anchor="w", padx=10, pady=(10, 2))
        ttk.Label(settings_file_box, text=str(SETTINGS_PATH), wraplength=540, foreground="#cfcfcf").pack(anchor="w", padx=10, pady=(0, 6))
        ttk.Label(settings_file_box, text="Логи проблем сохраняются здесь; каждая запись получает отдельную папку:", foreground="#cfcfcf").pack(anchor="w", padx=10, pady=(0, 2))
        ttk.Label(settings_file_box, text=str(LOGS_DIR), wraplength=540, foreground="#cfcfcf").pack(anchor="w", padx=10, pady=(0, 10))

        # -------- Видео --------
        video_box = ttk.LabelFrame(video_tab, text="Качество видео")
        video_box.pack(fill="x", padx=10, pady=10)

        grid = ttk.Frame(video_box)
        grid.pack(fill="x", padx=10, pady=10)
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="FPS:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(grid, textvariable=self.fps_var, values=["30", "60", "72", "75", "90", "120", "144", "165", "240"], state="readonly").grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Checkbutton(
            grid,
            text="Подстраивать FPS под герцовку монитора",
            variable=self.auto_adjust_fps_var,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 6))

        ttk.Label(grid, text="Битрейт видео, Мбит/с:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(grid, textvariable=self.video_bitrate_var).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(grid, text="1–100").grid(row=2, column=2, sticky="w", padx=(8, 0), pady=4)

        ttk.Label(grid, text="Способ захвата:").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(grid, textvariable=self.capture_method_var, values=CAPTURE_METHODS, state="readonly").grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(grid, text="Кодирование:").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(grid, textvariable=self.encoder_var, values=ENCODER_METHODS, state="readonly").grid(row=4, column=1, columnspan=2, sticky="ew", pady=4)

        monitor_count = detect_monitor_count()
        if monitor_count > 1:
            ttk.Label(grid, text="Монитор (ddagrab):").grid(row=10, column=0, sticky="w", pady=4)
            ttk.Combobox(
                grid,
                textvariable=self.monitor_index_var,
                values=[str(i + 1) for i in range(monitor_count)],
                state="readonly",
                width=6,
            ).grid(row=10, column=1, sticky="w", pady=4)

        ttk.Label(
            video_box,
            text="Запись области экрана — кнопкой «⬚ Область» на плавающей панели: выделяешь область и запись стартует сразу. Обычная «⏺ Запись» пишет весь экран.",
            wraplength=540,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 8))

        ttk.Checkbutton(grid, text="Показывать курсор при записи", variable=self.cursor_visible_var).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 2))
        ttk.Label(grid, text="Размер курсора в видео:").grid(row=6, column=0, sticky="w", pady=4)
        ttk.Combobox(
            grid,
            textvariable=self.cursor_size_percent_var,
            values=[str(value) for value in RECORDING_CURSOR_SIZE_PERCENT_OPTIONS],
            state="readonly",
            width=8,
        ).grid(row=6, column=1, sticky="w", pady=4)
        ttk.Label(grid, text="%").grid(row=6, column=2, sticky="w", padx=(8, 0), pady=4)
        ttk.Label(
            grid,
            text="100% — системный курсор. Другой размер записывается как стандартная стрелка.",
            foreground="#cfcfcf",
            wraplength=520,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ttk.Checkbutton(grid, text="Подсветка курсора", variable=self.cursor_highlight_var).grid(row=8, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(grid, text="Размер подсветки:").grid(row=9, column=0, sticky="w", pady=4)
        ttk.Scale(grid, from_=20, to=200, variable=self.cursor_highlight_size_var, orient="horizontal").grid(row=9, column=1, sticky="ew", pady=4)
        ttk.Label(grid, textvariable=self.cursor_highlight_size_var, width=4).grid(row=9, column=2, sticky="w", padx=(8, 0), pady=4)

        ttk.Label(
            video_box,
            text="Рекомендуемый режим: захват «Авто — ddagrab, потом GDI», кодирование NVIDIA NVENC, запись на SSD. В режиме ddagrab + NVENC кадры передаются напрямую из D3D11 в видеокодер без скачивания на CPU. Итоговый файл получает ровную частоту кадров (CFR). Если включена галочка подстройки, FPS автоматически приводится к частоте монитора. Если ddagrab недоступен, программа сама перейдёт на запасной GDI.",
            wraplength=540,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        webcam_box = ttk.LabelFrame(video_tab, text="Вебкамера")
        webcam_box.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(webcam_box, text="Камера для кнопки «📷 Вебкамера»:").pack(anchor="w", padx=10, pady=(10, 2))
        webcam_row = ttk.Frame(webcam_box)
        webcam_row.pack(fill="x", padx=10, pady=(0, 8))
        self.webcam_combo = ttk.Combobox(
            webcam_row,
            textvariable=self.webcam_device_var,
            values=self.webcam_devices,
            state="readonly",
        )
        self.webcam_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(
            webcam_row,
            text="Обновить",
            command=lambda: self.refresh_webcam_devices(silent=False),
        ).pack(side="right", padx=(8, 0))
        ttk.Label(
            webcam_box,
            text="Окно вебкамеры открывается поверх других окон и попадает в запись экрана. Его можно двигать и растягивать за края.",
            wraplength=540,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        # -------- Звук --------
        audio_box = ttk.LabelFrame(audio_tab, text="Устройства и громкость")
        audio_box.pack(fill="x", padx=10, pady=10)

        ttk.Label(audio_box, text="Микрофон:").pack(anchor="w", padx=10, pady=(10, 2))
        self.mic_combo = ttk.Combobox(audio_box, textvariable=self.mic_device_var, values=self.mic_audio_devices, state="readonly")
        self.mic_combo.pack(fill="x", padx=10, pady=(0, 6))

        mic_vol_row = ttk.Frame(audio_box)
        mic_vol_row.pack(fill="x", padx=10)
        ttk.Label(mic_vol_row, text="Громкость микрофона:").pack(side="left")
        ttk.Label(mic_vol_row, textvariable=self.mic_volume_var).pack(side="right")
        ttk.Scale(audio_box, from_=0, to=200, variable=self.mic_volume_var, orient="horizontal").pack(fill="x", padx=10, pady=(0, 4))

        mic_meter_row = ttk.Frame(audio_box)
        mic_meter_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(mic_meter_row, text="Индикатор:", width=12).pack(side="left")
        ttk.Progressbar(mic_meter_row, variable=self.mic_level_var, maximum=100, style="Audio.Horizontal.TProgressbar").pack(side="left", fill="x", expand=True)
        ttk.Label(mic_meter_row, textvariable=self.mic_level_text, width=5).pack(side="right")

        ttk.Label(audio_box, text="Звук компьютера:").pack(anchor="w", padx=10, pady=(0, 2))
        self.system_combo = ttk.Combobox(audio_box, textvariable=self.system_device_var, values=self.system_audio_devices, state="readonly")
        self.system_combo.pack(fill="x", padx=10, pady=(0, 6))

        sys_vol_row = ttk.Frame(audio_box)
        sys_vol_row.pack(fill="x", padx=10)
        ttk.Label(sys_vol_row, text="Громкость звука компьютера:").pack(side="left")
        ttk.Label(sys_vol_row, textvariable=self.system_volume_var).pack(side="right")
        ttk.Scale(audio_box, from_=0, to=200, variable=self.system_volume_var, orient="horizontal").pack(fill="x", padx=10, pady=(0, 4))

        sys_meter_row = ttk.Frame(audio_box)
        sys_meter_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(sys_meter_row, text="Индикатор:", width=12).pack(side="left")
        ttk.Progressbar(sys_meter_row, variable=self.system_level_var, maximum=100, style="Audio.Horizontal.TProgressbar").pack(side="left", fill="x", expand=True)
        ttk.Label(sys_meter_row, textvariable=self.system_level_text, width=5).pack(side="right")

        ttk.Label(audio_box, text="Битрейт аудио:").pack(anchor="w", padx=10, pady=(0, 2))
        ttk.Combobox(audio_box, textvariable=self.audio_bitrate_var, values=["128k", "192k", "256k", "320k"], state="readonly").pack(fill="x", padx=10, pady=(0, 10))

        ttk.Button(audio_tab, text="Обновить список аудиоустройств", command=lambda: self.refresh_audio_devices(silent=False)).pack(anchor="w", padx=10, pady=(0, 8))
        ttk.Label(
            audio_tab,
            text="Для звука компьютера НЕ выбирай микрофон, Line In или Focusrite Analogue 1+2 — это входы, они не пишут Telegram/YouTube/игры. Выбирай «Звук компьютера (по умолчанию Windows)» или конкретный «WASAPI loopback: Динамики/Наушники». Если FFmpeg без WASAPI, программа попробует записать системный звук напрямую через Windows CoreAudio loopback.",
            wraplength=540,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        # -------- Скриншот --------
        screenshot_box = ttk.LabelFrame(screenshot_tab, text="Снимок выбранной области")
        screenshot_box.pack(fill="x", padx=10, pady=10)
        ttk.Label(screenshot_box, text="Клавиша или сочетание для снимка:").pack(anchor="w", padx=10, pady=(10, 2))
        self.screenshot_hotkey_display_var.set(self.screenshot_hotkey_var.get())
        self.screenshot_hotkey_combo = ttk.Entry(
            screenshot_box,
            textvariable=self.screenshot_hotkey_display_var,
            state="readonly",
            cursor="hand2",
        )
        self.screenshot_hotkey_combo.pack(fill="x", padx=10, pady=(0, 8))
        self.screenshot_hotkey_combo.bind("<Button-1>", self.on_screenshot_hotkey_field_clicked)
        self.screenshot_hotkey_combo.bind("<Return>", self.on_screenshot_hotkey_field_clicked)
        self.screenshot_hotkey_combo.bind("<space>", self.on_screenshot_hotkey_field_clicked)
        ttk.Label(
            screenshot_box,
            text="Нажми прямо на поле выше, затем нужную клавишу или сочетание. Поддерживается Print Screen. Esc отменяет выбор. Одна буква тоже допустима, но будет срабатывать при обычном наборе текста.",
            wraplength=540,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        screenshot_actions = ttk.Frame(screenshot_box)
        screenshot_actions.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(
            screenshot_actions,
            text="Выбрать область и скопировать",
            command=self.take_screenshot,
        ).pack(side="left")
        ttk.Label(
            screenshot_box,
            textvariable=self.screenshot_status_var,
            wraplength=540,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))
        ttk.Label(
            screenshot_tab,
            text="После нажатия клавиши выдели область мышью. Изображение копируется в буфер обмена Windows и не сохраняется в файл. Вставить его можно сочетанием Ctrl+V.",
            wraplength=540,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        # -------- Дополнительно --------
        extra_box = ttk.LabelFrame(extra_tab, text="Поведение программы")
        extra_box.pack(fill="x", padx=10, pady=10)
        ttk.Label(extra_box, text="Размер плавающей кнопки:").pack(anchor="w", padx=10, pady=(10, 2))
        floating_size_row = ttk.Frame(extra_box)
        floating_size_row.pack(fill="x", padx=10, pady=(0, 8))
        tk.Scale(
            floating_size_row,
            from_=24,
            to=72,
            resolution=1,
            variable=self.floating_panel_size_var,
            orient="horizontal",
            showvalue=False,
            bg="#1e1e1e",
            fg="white",
            troughcolor="#333333",
            highlightthickness=0,
        ).pack(side="left", fill="x", expand=True)
        ttk.Label(floating_size_row, textvariable=self.floating_panel_size_var, width=4).pack(side="right", padx=(8, 0))

        ttk.Label(extra_box, text="Клавиша / сочетание для старта/остановки записи:").pack(anchor="w", padx=10, pady=(0, 2))
        self.hotkey_combo = ttk.Combobox(
            extra_box,
            textvariable=self.hotkey_var,
            values=["f8", "f9", "f10", "ctrl+shift+r", "ctrl+alt+r", "ctrl+alt+f9"],
        )
        self.hotkey_combo.pack(fill="x", padx=10, pady=(0, 8))

        ttk.Checkbutton(
            extra_box,
            text="Запускать программу в трее и показывать плавающую кнопку при старте Windows",
            variable=self.startup_tray_var,
        ).pack(anchor="w", padx=10, pady=(0, 10))

        ttk.Label(extra_box, text="Авто-остановка через, минут (0 — выключено):").pack(anchor="w", padx=10, pady=(0, 2))
        ttk.Entry(extra_box, textvariable=self.auto_stop_minutes_var, width=8).pack(anchor="w", padx=10, pady=(0, 8))
        ttk.Checkbutton(extra_box, text="Обратный отсчёт 3-2-1 перед записью", variable=self.countdown_enabled_var).pack(anchor="w", padx=10, pady=(0, 4))
        ttk.Checkbutton(extra_box, text="Показывать нажатые клавиши в записи", variable=self.show_keys_overlay_var).pack(anchor="w", padx=10, pady=(0, 4))
        ttk.Checkbutton(extra_box, text="После «Стоп» открывать папку и выделять записанное видео", variable=self.open_folder_after_stop_var).pack(anchor="w", padx=10, pady=(0, 10))

        ttk.Label(
            extra_tab,
            text="Настройки применяются автоматически и сохраняются в доступной для записи папке settings.",
            wraplength=540,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        # -------- Логи проблем --------
        logs_main_box = ttk.LabelFrame(logs_tab, text="Запись логов для исправления багов")
        logs_main_box.pack(fill="x", padx=10, pady=10)
        ttk.Checkbutton(
            logs_main_box,
            text="Писать логи проблем",
            variable=self.problem_logs_enabled_var,
        ).pack(anchor="w", padx=10, pady=(10, 6))
        ttk.Label(
            logs_main_box,
            text=(
                "Когда включено, каждая запись создаёт отдельную папку с датой и временем: "
                "резюме для нейросети, события JSONL, FFmpeg-команды, подробный лог, ошибки и снимок настроек. "
                "Когда выключено, подробные папки сессий не создаются."
            ),
            wraplength=540,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        logs_retention_box = ttk.LabelFrame(logs_tab, text="Автоудаление и размер")
        logs_retention_box.pack(fill="x", padx=10, pady=(0, 10))
        logs_grid = ttk.Frame(logs_retention_box)
        logs_grid.pack(fill="x", padx=10, pady=10)
        logs_grid.columnconfigure(1, weight=1)

        ttk.Label(logs_grid, text="Хранить обычные логи, дней:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(logs_grid, textvariable=self.problem_logs_retention_days_var, width=10).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(logs_grid, text="0 — не удалять").grid(row=0, column=2, sticky="w", padx=(8, 0), pady=4)

        ttk.Label(logs_grid, text="Хранить логи с ошибками, дней:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(logs_grid, textvariable=self.problem_logs_error_retention_days_var, width=10).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(logs_grid, text="рекомендуется дольше").grid(row=1, column=2, sticky="w", padx=(8, 0), pady=4)

        ttk.Label(logs_grid, text="Максимум одного файла лога, МБ:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(logs_grid, textvariable=self.problem_logs_max_file_mb_var, width=10).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(logs_grid, text="0.2–20 МБ").grid(row=2, column=2, sticky="w", padx=(8, 0), pady=4)

        ttk.Checkbutton(
            logs_retention_box,
            text="Автоочистка старых логов при запуске программы",
            variable=self.problem_logs_cleanup_on_start_var,
        ).pack(anchor="w", padx=10, pady=(0, 4))
        ttk.Checkbutton(
            logs_retention_box,
            text="Сохранять логи успешных записей",
            variable=self.problem_logs_keep_successful_var,
        ).pack(anchor="w", padx=10, pady=(0, 10))
        ttk.Label(
            logs_retention_box,
            text=(
                "Если снять галочку с успешных записей, после удачного сохранения видео папка логов этой записи "
                "будет удаляться автоматически. При ошибке лог всегда сохраняется, чтобы можно было исправить программу."
            ),
            wraplength=540,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        logs_actions_box = ttk.LabelFrame(logs_tab, text="Папка логов и ручное управление")
        logs_actions_box.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(logs_actions_box, text="Папка логов проблем:", foreground="#cfcfcf").pack(anchor="w", padx=10, pady=(10, 2))
        ttk.Label(logs_actions_box, text=str(LOGS_DIR), wraplength=540, foreground="#cfcfcf").pack(anchor="w", padx=10, pady=(0, 8))
        ttk.Label(logs_actions_box, textvariable=self.problem_logs_status_var, wraplength=540, foreground="#cfcfcf").pack(anchor="w", padx=10, pady=(0, 8))

        logs_buttons = ttk.Frame(logs_actions_box)
        logs_buttons.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(logs_buttons, text="Открыть папку логов", command=self.open_problem_logs_folder).pack(side="left", padx=(0, 8))
        ttk.Button(logs_buttons, text="Очистить старые сейчас", command=self.cleanup_problem_logs_manual).pack(side="left", padx=(0, 8))
        ttk.Button(logs_buttons, text="Удалить все логи", command=self.delete_all_problem_logs_prompt).pack(side="left")

        ttk.Label(
            logs_tab,
            text=(
                "Оптимальный режим для отправки нейросети: логи включены, обычные логи 30 дней, "
                "логи с ошибками 90 дней, лимит 2 МБ на файл. Так папка остаётся лёгкой, но содержит всё важное."
            ),
            wraplength=540,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        self.update_problem_logs_status_text()

        self.refresh_audio_devices(silent=True)
        self.refresh_webcam_devices(silent=True)
        self.start_audio_meters()
        self.schedule_audio_device_refresh(90000)
        window.lift()
        window.focus_force()

    def maximize_settings_window(self, window):
        try:
            if window is None or not window.winfo_exists():
                return
            try:
                window.state("zoomed")
                return
            except Exception:
                pass
            sw = max(1, int(window.winfo_screenwidth()))
            sh = max(1, int(window.winfo_screenheight()))
            window.geometry(f"{sw}x{sh}+0+0")
        except Exception:
            pass

    def create_scrollable_settings_tab(self, parent):
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, bg="#1e1e1e", highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def update_scrollregion(_event=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
            except Exception:
                pass

        def update_content_width(event):
            try:
                canvas.itemconfigure(window_id, width=event.width)
            except Exception:
                pass

        def on_mousewheel(event):
            try:
                delta = int(-1 * (event.delta / 120))
                canvas.yview_scroll(delta, "units")
            except Exception:
                pass

        content.bind("<Configure>", update_scrollregion)
        canvas.bind("<Configure>", update_content_width)
        for widget in (canvas, content):
            widget.bind("<MouseWheel>", on_mousewheel, add="+")
            widget.bind("<Button-4>", lambda _event: canvas.yview_scroll(-1, "units"), add="+")
            widget.bind("<Button-5>", lambda _event: canvas.yview_scroll(1, "units"), add="+")

        def install_mousewheel_bindings():
            def walk(widget):
                try:
                    widget.bind("<MouseWheel>", on_mousewheel, add="+")
                    widget.bind("<Button-4>", lambda _event: canvas.yview_scroll(-1, "units"), add="+")
                    widget.bind("<Button-5>", lambda _event: canvas.yview_scroll(1, "units"), add="+")
                    children = widget.winfo_children()
                except Exception:
                    children = []
                for child in children:
                    walk(child)
            walk(content)
            update_scrollregion()

        self.root.after(120, install_mousewheel_bindings)
        return content

    def create_right_panel(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(0, 8))

        ttk.Label(top, text="Управление записью", font=("Segoe UI", 14, "bold")).pack(side="left")
        self.rec_label = ttk.Label(top, textvariable=self.rec_indicator_var, style="Green.TLabel")
        self.rec_label.pack(side="right", padx=(12, 0))
        ttk.Label(top, textvariable=self.timer_var, font=("Segoe UI", 14, "bold")).pack(side="right")

        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 12))
        self.start_button = ttk.Button(controls, text="● Начать запись", command=self.start_recording)
        self.start_button.pack(side="left", padx=(0, 8))
        self.pause_button = ttk.Button(controls, text="⏸ Пауза", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(controls, text="■ Остановить и сохранить", command=self.stop_recording, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))

        info = ttk.LabelFrame(parent, text="Режим записи")
        info.pack(fill="x", pady=(0, 10))
        ttk.Label(
            info,
            text="Предпросмотр видео убран полностью. Теперь программа не делает второй захват экрана и не тратит ресурсы на отрисовку превью во время записи.",
            wraplength=560,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(10, 6))
        ttk.Label(
            info,
            text="Для самой плавной записи выбери FPS, равный или кратный частоте монитора, либо включи галочку автоматической подстройки FPS. Битрейт видео вводится в настройках числом от 1 до 100 Мбит/с.",
            wraplength=560,
            foreground="#cfcfcf",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        bottom = ttk.Frame(parent)
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Label(bottom, text="Статус:").pack(side="left")
        ttk.Label(bottom, textvariable=self.status_var, wraplength=600).pack(side="left", padx=(6, 0))

        quick_actions = ttk.Frame(parent)
        quick_actions.pack(fill="x", pady=(10, 0))
        self.open_output_folder_button = ttk.Button(
            quick_actions,
            text="Открыть папку с видео",
            command=self.open_last_output_folder,
            state="disabled",
        )
        self.open_output_folder_button.pack(side="left", padx=(0, 8))
        self.open_log_button = ttk.Button(
            quick_actions,
            text="Открыть лог",
            command=self.open_last_log,
            state="disabled",
        )
        self.open_log_button.pack(side="left")
        self.trim_button = ttk.Button(
            quick_actions,
            text="Обрезать концы",
            command=self.trim_last_output_dialog,
            state="disabled",
        )
        self.trim_button.pack(side="left", padx=(8, 0))
        self.make_gif_button = ttk.Button(
            quick_actions,
            text="Сделать GIF",
            command=self.make_gif_from_last_output,
            state="disabled",
        )
        self.make_gif_button.pack(side="left", padx=(8, 0))

    def set_settings_window_enabled(self, enabled):
        """Блокирует параметры записи на время записи/сохранения.

        Менять FPS, формат, устройства звука или кодер во время записи нельзя:
        сегменты могут получиться с разными параметрами, а concat -c copy потом
        собирает повреждённый файл.
        """
        state = "normal" if enabled else "disabled"
        combo_state = "readonly" if enabled else "disabled"

        def walk(widget):
            try:
                children = widget.winfo_children()
            except Exception:
                children = []
            for child in children:
                try:
                    cls = child.winfo_class()
                    text = ""
                    try:
                        text = str(child.cget("text"))
                    except Exception:
                        pass
                    if cls in ("TEntry", "Entry"):
                        child.configure(state=state)
                    elif cls in ("TCombobox",):
                        child.configure(state=combo_state)
                    elif cls in ("TScale", "Scale", "TCheckbutton", "Checkbutton"):
                        child.configure(state=state)
                    elif cls in ("TButton", "Button") and text not in ("Закрыть",):
                        child.configure(state=state)
                except Exception:
                    pass
                walk(child)

        try:
            if self.settings_window is not None and self.settings_window.winfo_exists():
                walk(self.settings_window)
        except Exception:
            pass
