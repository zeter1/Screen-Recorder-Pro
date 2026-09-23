import atexit
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path

from screen_recorder.app import ScreenRecorderProWin11
from screen_recorder.shared import (
    APP_BUILD,
    SingleInstanceGuard,
    get_source_snapshot_root,
    resolve_ffmpeg_path,
    resolve_ffprobe_path,
)


def run_packaging_smoke() -> int:
    """Headless smoke-check used by Windows CI against the actual packaged EXE."""
    def report(message, *, error=False):
        stream = sys.stderr if error else sys.stdout
        if stream is not None:
            print(message, file=stream)

    tools = [
        ("ffmpeg", resolve_ffmpeg_path()),
        ("ffprobe", resolve_ffprobe_path()),
    ]
    for name, resolved in tools:
        if not resolved:
            report(f"PACKAGING_SMOKE_FAIL: {name} not resolved", error=True)
            return 20
        candidate = Path(str(resolved))
        executable = str(candidate) if candidate.is_file() else shutil.which(str(resolved))
        if not executable:
            report(f"PACKAGING_SMOKE_FAIL: {name} missing: {resolved}", error=True)
            return 21
        try:
            completed = subprocess.run(
                [executable, "-version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except Exception as exc:
            report(f"PACKAGING_SMOKE_FAIL: {name}: {exc!r}", error=True)
            return 22
        if completed.returncode != 0:
            report(
                f"PACKAGING_SMOKE_FAIL: {name} exit={completed.returncode}\n"
                f"{(completed.stdout or '')[-2000:]}",
                error=True,
            )
            return 23

    source_root = get_source_snapshot_root()
    try:
        embedded_sources = list(Path(source_root).rglob("*.py"))
    except Exception:
        embedded_sources = []
    if not embedded_sources:
        report(
            f"PACKAGING_SMOKE_FAIL: diagnostic source snapshot is empty: {source_root}",
            error=True,
        )
        return 24

    report(
        f"PACKAGING_SMOKE_OK build={APP_BUILD} embedded_sources={len(embedded_sources)}"
    )
    return 0


def main() -> int:
    if "--packaging-smoke" in sys.argv:
        return run_packaging_smoke()

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
