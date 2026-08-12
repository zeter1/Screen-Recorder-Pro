# 03. Конвейер записи видео

## Цель стабильного конвейера

Получить визуально плавное видео с реальной длительностью, монотонными PTS/DTS и без лишнего копирования кадров GPU → CPU → GPU.

## Путь запуска

Упрощённый поток:

```text
AnnotationOverlay / горячая клавиша / UI
→ RecordingSessionMixin.start_recording()
→ проверка FFmpeg и настроек
→ создание temp/session/logs
→ выбор backend и кодера
→ start_new_segment()
→ build_ffmpeg_command()
→ managed FFmpeg process
→ progress reader + performance sampler + watchdog
```

## Выбор backend

`FfmpegSupportMixin.choose_capture_backend()` выбирает:

1. `ddagrab` для Desktop Duplication;
2. `gdigrab` как fallback;
3. DXcam не используется в стабильной версии.

При режиме «Авто — ddagrab, потом GDI» ошибка старта `ddagrab` должна приводить к контролируемому fallback, а не к зависанию GUI.

## Частота опроса и выходной FPS

Это разные величины:

- `poll_fps` — сколько раз в секунду Desktop Duplication опрашивается;
- `output_fps` — FPS готового файла.

Правило `get_ddagrab_poll_fps()`:

```text
если refresh_hz >= output_fps × 2
и refresh_hz кратна output_fps,
то poll_fps = min(refresh_hz, output_fps × 2)
иначе poll_fps = output_fps
```

Пример:

```text
монитор: 144 Гц
выход:   72 FPS
опрос:   144 FPS
```

Это уменьшает повтор старого изображения при локальной задержке одного опроса.

## Источник ddagrab

Строится в `build_ddagrab_source_expression()`:

```text
ddagrab=
  framerate=<poll_fps>:
  draw_mouse=<0|1>:
  output_idx=<monitor>:
  dup_frames=1
```

Для выбранной области добавляются `video_size`, `offset_x`, `offset_y`. Crop выполняется в Desktop Duplication, чтобы не скачивать кадр на CPU.

## Временная шкала и CFR

Стабильная схема для ddagrab:

```text
settb=expr=1/1000000,
setpts=RTCTIME-RTCSTART,
fps=<output_fps>:round=near,
settb=expr=1/39600,
setpts=PTS-STARTPTS
```

Логика:

1. time base переводится в микросекунды;
2. каждому входному кадру назначается время фактического поступления по системным часам;
3. один `fps`-фильтр формирует ровный CFR;
4. time base переводится в timescale контейнера;
5. начало обнуляется.

Выход:

```text
-fps_mode passthrough
```

## Почему нельзя `setpts=N*ticks`

Схема по номеру кадра гарантирует ровные метки, но предполагает, что источник реально выдаёт целевой FPS.

Если фактически поступает около 69,6 кадров/с, а каждому кадру назначается 1/72 секунды, видео становится короче реального времени и воспроизводится быстрее. На длинной записи drift накапливается.

Поэтому запрещено возвращать:

```text
setpts=N*550
```

как единственный источник временной шкалы для 72 FPS.

## NVENC

Основные параметры стабильного режима:

```text
h264_nvenc / hevc_nvenc
preset=fast
rc=vbr
rc-lookahead=0
spatial_aq=1
temporal_aq=0
bf=0
```

Цель — минимизировать очередь и задержку, а не максимизировать эффективность сжатия.

Для `ddagrab + NVENC` фильтр не должен содержать `hwdownload`.

CPU fallback может требовать `hwdownload` и конвертацию pixel format; запрет относится именно к аппаратному пути ddagrab → NVENC.

## Пауза и сегменты

Пауза завершает текущий сегмент. Возобновление создаёт новый сегмент. При остановке:

1. завершается текущий FFmpeg;
2. фиксируется длительность сегмента;
3. останавливается CoreAudio loopback;
4. проверяется целостность сегментов;
5. подмешивается и выравнивается аудио;
6. сегменты объединяются;
7. проверяется итоговый файл;
8. рассчитываются timing и smoothness reports;
9. временные файлы удаляются только после успеха.

Нельзя удалять temp-сегменты до подтверждённой финальной сборки.

## Показатели нормальной записи

Ожидается:

```text
Non-monotonic DTS = 0
backward timestamps = 0
non-positive intervals = 0
severe gaps = 0
dup/drop FFmpeg = 0 или объяснимое значение
timeline drift < 1–1.5%
FFmpeg progress без длительных остановок
A/V end gap обычно < 0.08–0.12 сек
```

Один нестандартный интервал первого пакета может быть особенностью старта и не равен рывку в середине записи.

## Что проверять после изменения конвейера

Автоматически:

- фильтр содержит wall-clock rebase;
- `fps=` встречается один раз;
- нет `setpts=N*`;
- NVENC-путь не содержит `hwdownload`;
- `fps_mode=passthrough` сохранён;
- 144/72 даёт poll 144;
- команда корректно строится для GDI и CPU fallback.

На Windows:

- 30–60 секунд непрерывной прокрутки;
- запись на 2–5 минут с видимым секундомером для drift;
- пауза/resume;
- запись области;
- полный экран;
- просмотр в нескольких плеерах при спорном результате.
