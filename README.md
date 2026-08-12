# Screen Recorder Pro

**Windows screen recorder and screenshot tool built with Python, Tkinter and FFmpeg.**

Screen Recorder Pro is a modular Windows desktop application for screen recording, screenshots, microphone/system-audio capture, webcam preview and detailed recording diagnostics. The current source build is `2026-08-12-screenshot-toolbar-persistence-v16`.

The project prioritizes recording stability, useful diagnostics and safe cleanup of background processes. Recent work extends the screenshot workflow without rewriting the validated ddagrab/NVENC/CFR recording pipeline.

## Highlights

- screen capture through FFmpeg Desktop Duplication (`ddagrab`) with GDI fallback;
- NVIDIA NVENC and CPU encoding options;
- microphone and Windows system-audio capture;
- CoreAudio loopback fallback when FFmpeg WASAPI is unavailable;
- global hotkeys, including native Windows `RegisterHotKey` handling for Print Screen;
- frozen-desktop region screenshots so menus and transient UI remain visible while selecting an area;
- screenshot toolbar with **Region**, **Draw**, **Arrow**, **Undo** and **Clear** tools;
- separate persistent colors and sizes for drawing and arrow tools;
- persistent screenshot-toolbar position with automatic clamping when monitor geometry changes;
- pause/resume and multi-segment finalization;
- webcam preview and annotation overlay;
- tray operation and Windows autostart support;
- managed FFmpeg/subprocess lifecycle and shutdown cleanup;
- structured diagnostic logs designed for root-cause analysis;
- automated structural/regression verification in `verify_project.py`.

## Recording pipeline

The stable GPU path is:

```text
Desktop Duplication / ddagrab
→ D3D11 frames in GPU memory
→ wall-clock timestamps
→ one CFR normalization step
→ NVIDIA NVENC
→ MP4 / MKV / AVI / MOV
```

For the project's validated 144 Hz → 72 FPS scenario, the application polls `ddagrab` at 144 FPS and produces a 72 FPS output while preserving wall-clock timing.

## Project structure

```text
main.py
Screen Recorder Pro.py
verify_project.py
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

`ScreenRecorderProWin11` is assembled from mixins. The modules separate recording, audio, FFmpeg commands, lifecycle, UI, diagnostics, screenshots and finalization without changing the public launcher.

Detailed architecture and troubleshooting notes are available in [`Docs/README.md`](Docs/README.md).

## Requirements

- Windows 10/11
- Python
- FFmpeg and FFprobe available through `PATH`
- Python dependencies from `requirements.txt`
- NVIDIA GPU is recommended for the NVENC path; CPU encoders are also exposed by the application

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

Run:

```powershell
python "Screen Recorder Pro.py"
```

or:

```powershell
python main.py
```

## Verification

The repository includes a project verifier:

```powershell
python verify_project.py
python -m compileall .
```

`verify_project.py` checks project structure, required methods, system-audio selection regressions, screenshot-region behavior and safe log-cleanup ownership rules.

The v16 source package was checked before publication in a non-Windows environment: `verify_project.py` completed successfully and `compileall` found no Python syntax errors.

> Hardware-dependent behavior — real `ddagrab`, NVENC, Windows clipboard, native hotkeys and physical audio devices — still requires verification on Windows hardware.

## Diagnostics and reliability

The application contains dedicated diagnostics for FFmpeg commands, recording events, timing, smoothness, screenshot selection, hotkeys, errors and source-version fingerprints. Runtime logs, settings, temporary recordings and generated media are excluded from Git.

Long-running and external-process code is designed around:

- managed child processes;
- explicit shutdown cleanup;
- timeouts and return-code checks;
- bounded diagnostic files;
- temporary-file cleanup;
- preserving useful recording output when recovery is safe.

## AI-assisted development

`AGENTS.md` and the `Docs/` folder document project invariants, architecture and validation rules used during AI-assisted development with Codex/ChatGPT. AI-generated changes are treated as proposals and are validated with repository checks and manual Windows testing where hardware behavior is involved.

## License

No open-source license is currently granted. The repository is public for portfolio and source-review purposes.
