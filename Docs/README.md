# Документация проекта Screen Recorder Pro

Эта папка помогает Codex и разработчику быстро понять проект и вносить изменения без регрессий.

## Рекомендуемый порядок чтения

1. [`../AGENTS.md`](../AGENTS.md) — обязательные правила работы.
2. [`01_PROJECT_OVERVIEW.md`](01_PROJECT_OVERVIEW.md) — назначение и текущая стабильная база.
3. [`02_CODE_MAP.md`](02_CODE_MAP.md) — где находится каждая подсистема.
4. [`03_RECORDING_PIPELINE.md`](03_RECORDING_PIPELINE.md) — путь кадра и критические инварианты плавности.
5. [`04_AUDIO_PIPELINE.md`](04_AUDIO_PIPELINE.md) — микрофон, системный звук и выравнивание.
6. [`05_THREADING_AND_PROCESSES.md`](05_THREADING_AND_PROCESSES.md) — Tkinter, потоки и FFmpeg-процессы.
7. [`06_DIAGNOSTICS_AND_LOGS.md`](06_DIAGNOSTICS_AND_LOGS.md) — структура логов 00–13 и правила интерпретации.
8. [`07_DEVELOPMENT_AND_TESTING.md`](07_DEVELOPMENT_AND_TESTING.md) — безопасный workflow и проверки.
9. [`08_TROUBLESHOOTING.md`](08_TROUBLESHOOTING.md) — диагностика типовых проблем.
10. [`09_RELEASE_CHECKLIST.md`](09_RELEASE_CHECKLIST.md) — проверка перед выдачей новой версии.

## Быстрый выбор документа по задаче

| Задача | Сначала открыть |
|---|---|
| Рывки, FPS, ускорение видео | `03_RECORDING_PIPELINE.md`, `06_DIAGNOSTICS_AND_LOGS.md` |
| Нет системного звука | `04_AUDIO_PIPELINE.md`, `08_TROUBLESHOOTING.md` |
| Ошибка при старте записи | `02_CODE_MAP.md`, `08_TROUBLESHOOTING.md` |
| Зависание GUI или оставшийся ffmpeg.exe | `05_THREADING_AND_PROCESSES.md` |
| Настройки, пути, перенос проекта | `01_PROJECT_OVERVIEW.md`, `02_CODE_MAP.md` |
| Рефакторинг модулей | `02_CODE_MAP.md`, `07_DEVELOPMENT_AND_TESTING.md` |
| Подготовка новой версии | `09_RELEASE_CHECKLIST.md` |
