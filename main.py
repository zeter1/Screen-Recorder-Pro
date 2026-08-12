import atexit
import sys
import tkinter as tk

from screen_recorder.app import ScreenRecorderProWin11
from screen_recorder.shared import SingleInstanceGuard


def main() -> int:
    single_instance_guard = SingleInstanceGuard()
    if not single_instance_guard.acquire():
        SingleInstanceGuard.notify_already_running()
        return 0
    atexit.register(single_instance_guard.release)

    root = tk.Tk()
    # Главное окно не появляется при запуске: управление идёт из трея
    # и через плавающую панель.
    root.withdraw()
    app = ScreenRecorderProWin11(root)
    try:
        root.mainloop()
    finally:
        try:
            app.diagnostic_log("mainloop_finally", {
                "running": getattr(app, "running", None),
                "is_recording": getattr(app, "is_recording", None),
                "is_finalizing": getattr(app, "is_finalizing", None),
            })
        except Exception:
            pass
        try:
            app.force_shutdown_child_processes()
        except Exception:
            pass
        single_instance_guard.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
