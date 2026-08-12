# 02. Карта кода и архитектура

## Общая схема

Главный класс собирается через множественное наследование:

```python
class ScreenRecorderProWin11(
    UiMixin,
    FileToolsMixin,
    SettingsMixin,
    WebcamDevicesMixin,
    AudioDevicesMixin,
    ProcessMixin,
    ProblemLogsMixin,
    FfmpegSupportMixin,
    InstantBufferMixin,
    RecordingSessionMixin,
    SegmentAudioMixin,
    CaptureCommandsMixin,
    DxcamCaptureMixin,
    RecordingControlMixin,
    TimingMixin,
    SmoothnessDiagnosticsMixin,
    FinalizeMixin,
    OverlayControlsMixin,
    TimerStateMixin,
    ScreenshotsHotkeysMixin,
    TrayStartupMixin,
    LifecycleMixin,
):
    ...
```

`ScreenRecorderProWin11.__init__` создаёт почти всё общее состояние. Mixin-классы используют это состояние через `self`.

## Важное следствие mixin-архитектуры

Метод в одном mixin может вызывать метод из другого mixin и использовать поля, созданные в `app.py`.

Перед изменением или переносом метода обязательно:

```powershell
rg "имя_метода" .
rg "self\.имя_поля" screen_recorder
```

Не добавляй отдельный `__init__` в mixin без явного вызова из главного класса.

## Корневые файлы

| Файл | Ответственность |
|---|---|
| `Screen Recorder Pro.py` | Совместимый launcher. Не удалять. |
| `main.py` | Single instance, Tk root, mainloop, аварийное завершение процессов. |
| `verify_project.py` | Автоматическая компиляция, импорт и регрессионные проверки. |
| `requirements.txt` | Python-зависимости. |
| `.gitignore` | Исключение логов, настроек, временных файлов и сборок. |

## Пакет `screen_recorder`

### `screen_recorder/shared.py`

Содержит:

- общие импорты;
- опциональные зависимости и флаги доступности;
- `APP_NAME`, `APP_BUILD`, `DIAGNOSTIC_SCHEMA`;
- названия устройств и списки режимов;
- пути `APP_DIR`, `DATA_DIR`, `LOGS_DIR`, `TEMP_RECORDINGS_DIR`;
- `SingleInstanceGuard`;
- атомарную запись текста;
- нормализацию битрейта, FPS и размеров;
- определение герцовки и числа мониторов;
- сбор объединённого снимка модульных исходников.

Это import-hub проекта. Рефакторинг `shared.py` затрагивает почти все файлы.

### `screen_recorder/app.py`

Содержит:

- MRO главного класса;
- общие константы класса;
- всё состояние текущего процесса и записи;
- создание Tkinter-переменных;
- запуск UI, трея, аудиометров, горячих клавиш и фоновой подготовки.

Новые поля общего состояния обычно добавляются сюда.

## Компоненты

### `components/audio_loopback.py`

`WasapiLoopbackWaveRecorder` — нативная запись системного звука Windows через CoreAudio loopback, когда FFmpeg не поддерживает WASAPI.

Критические функции:

- COM/CoreAudio инициализация;
- чтение пакетов;
- преобразование в PCM16 stereo;
- добивка тишины по реальным часам;
- гарантированное закрытие WAV и COM-объектов.

### `components/webcam_preview.py`

`WebcamPreviewWindow`:

- отдельное поверхностное окно вебкамеры;
- OpenCV как основной путь;
- FFmpeg rawvideo как fallback;
- очередь последнего кадра;
- изменение размера и полноэкранный режим.

### `components/annotation_overlay.py`

`AnnotationOverlay`:

- плавающий индикатор;
- панель записи/паузы/стопа;
- выбор области;
- рисование поверх экрана;
- управление геометрией и topmost;
- запрет вызовов Tkinter из фонового потока.

## Mixin-модули

| Модуль | Основные задачи и методы |
|---|---|
| `ui.py` | Главное окно, вкладки настроек, элементы управления. |
| `file_tools.py` | Открытие файлов/папок, GIF, обрезка, ffprobe duration. |
| `settings.py` | Чтение, миграция, backup и атомарное сохранение настроек. |
| `webcam_devices.py` | Поиск и выбор DirectShow-вебкамер. |
| `audio_devices.py` | DShow/WASAPI-устройства, default endpoints, аудиометры. |
| `processes.py` | Managed subprocess, watchdog, завершение дерева процессов. |
| `problem_logs.py` | Логи запуска и сессии, файлы 00–13, traceback. |
| `ffmpeg_support.py` | Проверка FFmpeg, кодеров, фильтров, backend и temp root. |
| `instant_buffer.py` | Фоновая подготовка, выбор области и отдельная Canvas-панель пометок скриншота. |
| `recording_session.py` | `start_recording`, старт сегмента, запуск FFmpeg. |
| `segment_audio.py` | Источники аудио, CoreAudio fallback, mux и A/V alignment. |
| `capture_commands.py` | Геометрия, ddagrab, crop, FPS, фильтры, кодеры, команды. |
| `dxcam_capture.py` | Отключённый DXcam-контур и raw frame обработка. |
| `recording_control.py` | Пауза, resume, stop, validation, cleanup. |
| `timing.py` | Анализ PTS/DTS, packet cadence, FPS и drift. |
| `smoothness_diagnostics.py` | FFmpeg progress, нагрузка, GPU, визуальный cadence, AI report. |
| `finalize.py` | Восстановление orphan-сегментов, concat и финализация. |
| `overlay_controls.py` | Плавающая панель, область, webcam overlay, key overlay. |
| `timer_state.py` | Таймер записи и GUI-состояния. |
| `screenshots_hotkeys.py` | Замороженный кадр, карандаш/стрелки, обрезка, clipboard и горячие клавиши. |
| `tray_startup.py` | Трей, Run registry и автозапуск. |
| `lifecycle.py` | Безопасное закрытие приложения. |

## Куда вносить типовые изменения

| Запрос | Основной файл | Часто связанные файлы |
|---|---|---|
| Изменить FPS/плавность | `capture_commands.py` | `timing.py`, `smoothness_diagnostics.py`, `verify_project.py` |
| Ошибка старта FFmpeg | `recording_session.py` | `ffmpeg_support.py`, `processes.py`, `problem_logs.py` |
| Пауза/стоп/сохранение | `recording_control.py` | `finalize.py`, `segment_audio.py` |
| Нет системного звука | `audio_devices.py`, `segment_audio.py` | `audio_loopback.py`, `ffmpeg_support.py` |
| Ошибка в настройках | `settings.py` | `ui.py`, `shared.py` |
| Зависает интерфейс | профильный UI/mixin | `processes.py`, `app.py` |
| Остаётся ffmpeg.exe | `processes.py`, `lifecycle.py` | `main.py`, `recording_control.py` |
| Плохие/неполные логи | `problem_logs.py` | `smoothness_diagnostics.py`, `timing.py` |
| Трей/автозапуск | `tray_startup.py` | `main.py`, `shared.py` |
| Скриншот | `screenshots_hotkeys.py` | `instant_buffer.py`, `tray_startup.py` |

## Известный архитектурный долг

- `shared.py` импортируется через wildcard почти всеми mixin-файлами.
- Главный объект содержит большое общее состояние.
- Некоторые зависимости между mixin не выражены типами или интерфейсами.
- Полного набора unit-тестов пока нет; основной gate — `verify_project.py` плюс Windows smoke test.

Не исправляй весь архитектурный долг во время локальной функциональной задачи.
