# Поддержка и диагностика

Если Screen Recorder Pro работает нестабильно, создайте GitHub Issue через форму **Сообщить об ошибке** и приложите минимальный диагностический контекст, достаточный для воспроизведения.

## Что указать

- commit/версию проекта;
- Windows и Python;
- GPU и драйвер, если используется NVENC/ddagrab;
- версию FFmpeg/FFprobe;
- выбранные monitor/area, FPS, encoder и аудиоустройства;
- точные шаги воспроизведения;
- ожидаемое и фактическое поведение;
- релевантный фрагмент problem/smoothness/audio diagnostics.

## Самопроверка

```powershell
python -m compileall -q .
python verify_project.py
python verify_capture_recovery.py
python verify_save_safety.py
python verify_recording_publication.py
```

Часть проверок использует FFmpeg/FFprobe. Полный capture, NVENC, Desktop Duplication, audio-device и hotkey сценарий всё равно требует runtime-проверки на Windows с реальным оборудованием.

## Конфиденциальность

Диагностика может содержать пути, имена аудиоустройств, команды FFmpeg и сведения о локальном окружении. Перед публикацией проверьте содержимое и удалите личные данные. Для security-вопросов используйте рекомендации из [`SECURITY.md`](SECURITY.md).
