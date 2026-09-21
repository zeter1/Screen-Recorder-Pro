**Язык / Language:** **Русский** · [English](README_EN.md)

# Screen Recorder Pro

**Screen Recorder Pro** — продвинутая программа для записи экрана и создания скриншотов в Windows. Проект написан на Python и использует FFmpeg, Desktop Duplication, NVENC, захват системного звука, глобальные горячие клавиши и отдельную диагностическую систему.

Приложение рассчитано на стабильную длительную запись: умеет контролировать дочерние процессы FFmpeg, восстанавливаться после части ошибок, работать с несколькими источниками аудио и сохранять полезную диагностику, если что-то пошло не так.

## Что демонстрирует проект

- построение Windows screen-capture pipeline поверх FFmpeg и Desktop Duplication (`ddagrab`);
- работу с GPU-кодированием NVIDIA NVENC и CPU fallback;
- захват и синхронизацию нескольких источников аудио;
- WinAPI-интеграцию: глобальные hotkeys, Print Screen, tray и автозапуск;
- управление жизненным циклом FFmpeg и дочерних процессов при длительной записи;
- модульную архитектуру крупного Tkinter-приложения через специализированные mixin-модули;
- отдельную диагностику таймингов, плавности, аудиоустройств, команд FFmpeg и завершения процессов;
- автоматические структурные и регрессионные проверки через отдельные `verify_*.py`.

## Основные возможности

- запись экрана через FFmpeg Desktop Duplication (`ddagrab`) с резервным переходом на GDI;
- аппаратное кодирование NVIDIA NVENC и программное CPU-кодирование;
- захват микрофона и системного звука Windows;
- резервный CoreAudio loopback для системного звука;
- глобальные горячие клавиши и нативная обработка Print Screen;
- скриншот выделенной области по замороженному изображению рабочего стола;
- инструменты рисования и стрелок поверх скриншота;
- пауза и продолжение записи с последующей сборкой сегментов;
- предпросмотр веб-камеры;
- слой аннотаций;
- системный трей и автозапуск Windows;
- безопасное управление FFmpeg и очистка временных процессов;
- структурированные диагностические логи.

## Готовая Windows-сборка

Для обычного использования **не нужно отдельно устанавливать Python, FFmpeg или Python-зависимости**.

1. Откройте [GitHub Releases](https://github.com/zeter1/Screen-Recorder-Pro/releases).
2. Скачайте `Screen-Recorder-Pro.exe` из последней Windows-сборки.
3. Запустите EXE.

GitHub Actions собирает один `Screen-Recorder-Pro.exe` через PyInstaller. Внутрь сборки включены `ffmpeg.exe` и `ffprobe.exe`, а перед публикацией CI запускает регрессионные проверки и headless smoke-test именно собранного EXE. Рядом с бинарником публикуется `Screen-Recorder-Pro.sha256.txt` для проверки целостности.

> Пока EXE не подписан коммерческим code-signing сертификатом, Windows SmartScreen может показать предупреждение для нового/редко скачиваемого файла. Это отдельно от автоматической проверки сборки в CI.

## Установка из исходников

### 1. Установите Python

Используйте актуальную x64-версию Python для Windows.

### 2. Установите FFmpeg

`ffmpeg.exe` и `ffprobe.exe` должны быть доступны через `PATH`.

### 3. Скачайте проект

```bash
git clone https://github.com/zeter1/Screen-Recorder-Pro.git
cd Screen-Recorder-Pro
```

### 4. Установите зависимости

```powershell
python -m pip install -r requirements.txt
```

## Запуск из исходников

```powershell
python "Screen Recorder Pro.py"
```

или:

```powershell
python main.py
```

## Как пользоваться

1. Запустите программу.
2. Выберите монитор или область, FPS, формат, кодирование и папку сохранения.
3. Настройте микрофон и системный звук.
4. При наличии NVIDIA GPU можно выбрать NVENC.
5. Начните запись кнопкой или глобальной горячей клавишей.
6. Во время записи используйте паузу, аннотации и другие инструменты.
7. Остановите запись — программа завершит FFmpeg, обработает сегменты и опубликует итоговый файл.
8. Для скриншота используйте Print Screen или назначенную горячую клавишу, затем выберите область и при необходимости добавьте рисунок или стрелку.

## Конвейер записи

```text
Desktop Duplication / ddagrab
→ кадры D3D11 в памяти GPU
→ временные метки по реальному времени
→ нормализация CFR
→ NVIDIA NVENC / CPU fallback
→ MP4 / MKV / AVI / MOV
```

## Архитектура

```text
main.py
Screen Recorder Pro.py
verify_project.py
verify_capture_recovery.py
verify_save_safety.py
verify_recording_publication.py
Docs/
screen_recorder/
├── app.py
├── shared.py
├── components/
│   ├── annotation_overlay.py
│   ├── audio_loopback.py
│   └── webcam_preview.py
└── mixins/
    ├── audio_devices.py
    ├── capture_commands.py
    ├── finalize.py
    ├── instant_buffer.py
    ├── problem_logs.py
    ├── processes.py
    ├── recording_control.py
    ├── recording_session.py
    ├── screenshots_hotkeys.py
    ├── segment_audio.py
    ├── settings.py
    ├── smoothness_diagnostics.py
    ├── timing.py
    ├── tray_startup.py
    └── ui.py
```

Класс приложения собирается из mixin-модулей, которые разделяют интерфейс, запись, аудио, FFmpeg-команды, скриншоты, диагностику и финализацию.

## Проверка проекта

```powershell
python -m compileall -q .
python verify_project.py
python verify_capture_recovery.py
python verify_save_safety.py
python verify_recording_publication.py
```

GitHub Actions выполняет эти проверки на Windows runner и проверяет наличие FFmpeg/FFprobe.

## Надёжность и диагностика

- контролируемое завершение дочерних процессов;
- тайм-ауты и проверка return code;
- recovery для части прерванных capture/finalization сценариев;
- защита уже созданного результата от небезопасной перезаписи;
- ограничение размера логов;
- очистка временных файлов;
- отдельная диагностика таймингов, плавности и аудиоустройств.

## Ограничения и уровень проверки

- проект предназначен для Windows;
- Desktop Duplication, NVENC, реальные мониторы, аудиоустройства, hotkeys и webcam требуют runtime-проверки на реальном Windows-компьютере;
- CI проверяет кодовые инварианты и FFmpeg-интеграцию, но не может полностью эмулировать конкретное оборудование пользователя;
- доступность `ddagrab`/NVENC зависит от FFmpeg, GPU и драйвера.

## Разработка с помощью ИИ

`AGENTS.md` и папка `Docs/` содержат архитектурные правила и процедуры проверки для ChatGPT/Codex. AI-generated изменения рассматриваются как предложения и проходят те же проверки, что и обычные изменения кода.

## Поддержка

- [Поддержка и диагностические данные](SUPPORT.md)
- [Security / privacy](SECURITY.md)
- Bug Report: `.github/ISSUE_TEMPLATE/bug_report.yml`

## Лицензия

Открытая лицензия в настоящий момент не предоставлена. Репозиторий опубликован для портфолио и ознакомления с исходным кодом.
