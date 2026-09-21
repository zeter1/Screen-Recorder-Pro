**Язык / Language:** [Русский](README.md) · **English**

# Screen Recorder Pro

**Screen Recorder Pro** is an advanced Windows application for screen recording and screenshots. It is written in Python and uses FFmpeg, Desktop Duplication, NVENC, system-audio capture, global hotkeys, and a dedicated diagnostics layer.

The application is designed for stable long-running capture: it manages FFmpeg child processes, recovers from selected failure modes, handles multiple audio sources, and preserves useful diagnostics when something goes wrong.

## What this project demonstrates

- a Windows screen-capture pipeline built on FFmpeg and Desktop Duplication (`ddagrab`);
- NVIDIA NVENC hardware encoding with CPU fallback;
- capture and synchronization of multiple audio sources;
- WinAPI integration for global hotkeys, Print Screen, tray, and autostart;
- FFmpeg/subprocess lifecycle management for long recording sessions;
- modular architecture for a large Tkinter application through focused mixins;
- diagnostics for timing, smoothness, audio devices, FFmpeg commands, and process shutdown;
- automated structural and regression checks through dedicated `verify_*.py` scripts.

## Main features

- screen capture through FFmpeg Desktop Duplication (`ddagrab`) with GDI fallback;
- NVIDIA NVENC and software CPU encoding;
- microphone and Windows system-audio capture;
- fallback CoreAudio loopback for system audio;
- global hotkeys and native Print Screen handling;
- frozen-desktop region screenshots;
- drawing and arrow tools for screenshots;
- pause/resume with segment assembly;
- webcam preview;
- annotation overlay;
- system tray and Windows autostart;
- safe FFmpeg process management and temporary-process cleanup;
- structured diagnostic logs.

## Ready-to-run Windows build

For normal use, you **do not need to install Python, FFmpeg, or the Python dependencies separately**.

1. Open [GitHub Releases](https://github.com/zeter1/Screen-Recorder-Pro/releases).
2. Download `Screen-Recorder-Pro.exe` from the latest Windows build.
3. Run the EXE.

GitHub Actions builds a single `Screen-Recorder-Pro.exe` with PyInstaller. The package includes `ffmpeg.exe` and `ffprobe.exe`; before publication, CI runs the regression checks and a headless smoke test against the packaged EXE itself. A `Screen-Recorder-Pro.sha256.txt` integrity file is published next to the binary.

> The EXE is not currently signed with a commercial code-signing certificate, so Windows SmartScreen may warn about a new or rarely downloaded binary. That is separate from the automated CI verification.

## Installation from source

### 1. Install Python

Use a current x64 Python release for Windows.

### 2. Install FFmpeg

`ffmpeg.exe` and `ffprobe.exe` must be available through `PATH`.

### 3. Download the project

```bash
git clone https://github.com/zeter1/Screen-Recorder-Pro.git
cd Screen-Recorder-Pro
```

### 4. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

## Running from source

```powershell
python "Screen Recorder Pro.py"
```

or:

```powershell
python main.py
```

## Usage

1. Start the application.
2. Choose the monitor or region, FPS, format, encoder, and output directory.
3. Configure microphone and system audio.
4. Select NVENC when a compatible NVIDIA GPU is available.
5. Start recording with the UI or a global hotkey.
6. Use pause, annotations, and other tools while recording.
7. Stop recording; the application finalizes FFmpeg, processes segments, and publishes the output file.
8. For screenshots, use Print Screen or the configured hotkey, select a region, and optionally add drawings or arrows.

## Recording pipeline

```text
Desktop Duplication / ddagrab
→ D3D11 frames in GPU memory
→ real-time timestamps
→ CFR normalization
→ NVIDIA NVENC / CPU fallback
→ MP4 / MKV / AVI / MOV
```

## Architecture

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

The main application class is composed from focused mixins covering UI, recording, audio, FFmpeg command generation, screenshots, diagnostics, and finalization.

## Verification

```powershell
python -m compileall -q .
python verify_project.py
python verify_capture_recovery.py
python verify_save_safety.py
python verify_recording_publication.py
```

GitHub Actions runs these checks on a Windows runner and verifies that FFmpeg/FFprobe are available.

## Reliability and diagnostics

- controlled child-process termination;
- timeouts and return-code checks;
- recovery for selected interrupted capture/finalization scenarios;
- protection of already-created output from unsafe overwrite;
- bounded log sizes;
- temporary-file cleanup;
- dedicated timing, smoothness, and audio-device diagnostics.

## Limitations and verification level

- the project targets Windows;
- Desktop Duplication, NVENC, real monitors, audio devices, hotkeys, and webcam paths require runtime validation on real Windows hardware;
- CI verifies code invariants and FFmpeg integration but cannot fully emulate every user's hardware;
- `ddagrab`/NVENC availability depends on FFmpeg, GPU, and driver support.

## AI-assisted development

`AGENTS.md` and `Docs/` contain architectural rules and verification procedures for ChatGPT/Codex workflows. AI-generated changes are treated as proposals and validated with the same checks as normal code changes.

## Support

- [Support and diagnostic data](SUPPORT.md)
- [Security / privacy](SECURITY.md)
- Bug Report: `.github/ISSUE_TEMPLATE/bug_report.yml`

## License

No open-source license is currently granted. The repository is published for portfolio review and source-code inspection.
