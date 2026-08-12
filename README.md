# Screen Recorder Pro

**Windows screen recorder built with Python, Tkinter and FFmpeg.**

Screen Recorder Pro is a modular Windows desktop application for screen recording, screenshots, microphone/system-audio capture, webcam preview and detailed recording diagnostics. The current source build is `2026-08-12-native-printscreen-hotkey-v13`.

The project intentionally prioritizes recording stability, useful diagnostics and safe cleanup of background processes over aggressive refactoring.

## Highlights

- screen capture through FFmpeg Desktop Duplication (`ddagrab`) with GDI fallback;
- NVIDIA NVENC and CPU encoding options;
- microphone and Windows system-audio capture;
- CoreAudio loopback fallback when FFmpeg WASAPI is unavailable;
- global hotkeys, including native Windows `RegisterHotKey` handling for Print Screen;
- region screenshots based on a frozen desktop snapshot;
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

`verify_project.py` checks, among other things:

- that all Python files compile;
- mixin structure and required methods;
- system-audio device selection regressions;
- screenshot-region normalization;
- native Print Screen hotkey implementation;
- safe ownership checks for automatic log cleanup.

The supplied source archive was checked before publication: `verify_project.py` completed successfully and `compileall` found no Python syntax errors.

> Hardware-dependent recording behavior — real `ddagrab`, NVENC, Windows clipboard, low-level hotkeys and physical audio devices — must still be verified on Windows hardware.

See `VALIDATION.txt` for the latest project-specific validation notes.

## Diagnostics and reliability

The application contains dedicated diagnostics for FFmpeg commands, recording events, timing, smoothness, errors and source-version fingerprints. Runtime logs, settings, temporary recordings and generated media are excluded from Git.

Long-running and external-process code is designed around:

- managed child processes;
- explicit shutdown cleanup;
- timeouts and return-code checks;
- bounded diagnostic files;
- temporary-file cleanup;
- preserving useful recording output when recovery is safe.

## AI-assisted development

`AGENTS.md` documents project invariants and validation rules used during AI-assisted development with Codex/ChatGPT. AI-generated changes are treated as proposals and are validated with repository checks and manual Windows testing where hardware behavior is involved.

## Current validation snapshot

`VALIDATION.txt` documents the v13/v12/v11 regression work around native Print Screen registration, screenshot selection, bounded diagnostics and preservation of the stable recording pipeline.

## License

No open-source license is currently granted. The repository is public for portfolio and source-review purposes.
