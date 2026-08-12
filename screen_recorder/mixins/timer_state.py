from ..shared import *


class TimerStateMixin:
    def set_rec_state(self, state):
        if state == "recording":
            self.rec_indicator_var.set("● REC")
            self.rec_label.configure(style="Red.TLabel")
        elif state == "paused":
            self.rec_indicator_var.set("⏸ PAUSE")
            self.rec_label.configure(style="Yellow.TLabel")
        elif state == "saving":
            self.rec_indicator_var.set("● SAVE")
            self.rec_label.configure(style="Yellow.TLabel")
        else:
            self.rec_indicator_var.set("● READY")
            self.rec_label.configure(style="Green.TLabel")

    def update_timer(self):
        # Таймер показывает длительность медиатаймлайна, которая попадёт в файл.
        # FFmpeg progress обновляется каждые ~0.5 сек; до первой строки используем
        # wall-clock fallback, чтобы таймер не стоял на нуле при старте.
        seconds = float(self.recorded_seconds or 0.0)
        if self.is_recording and not self.is_paused and self.segment_started_at is not None:
            try:
                current_media = float(self.current_segment_media_seconds or 0.0)
            except Exception:
                current_media = 0.0
            if current_media > 0.0:
                seconds += current_media
            else:
                seconds += max(0.0, time.perf_counter() - self.segment_started_at)

        total = int(seconds)
        hours = total // 3600
        minutes = (total % 3600) // 60
        secs = total % 60
        self.timer_var.set(f"{hours:02d}:{minutes:02d}:{secs:02d}")

        if self.running:
            self.root.after(300, self.update_timer)
