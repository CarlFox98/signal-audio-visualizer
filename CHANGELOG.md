# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Baseline documented at the point version control was introduced. Prior
history was not tracked in Git; this entry reflects the app's state as of
this repository's first commit.

### Added

- WASAPI loopback capture (system audio, per-app via VB-CABLE) and
  microphone/line-in capture via `pyaudiowpatch`.
- Seven visual modes: bars, waveform, radial, particles, rainbow bars,
  futuristic HUD, neon grid.
- GPU-accelerated (OpenGL) rendering path for every mode, with automatic
  fallback to software rendering on failure, and a manual toggle (`g`).
- Five color themes, adjustable gain/smoothing, and simple RMS-based beat
  detection driving visual accents.
- Tkinter launcher window for picking an audio source and initial settings,
  with a console fallback when Tkinter isn't available.
- Persistent settings (`visualizer_config.json`) and per-session log files
  (`logs/session_*.log`, auto-pruned after 30 days) with a friendly error
  dialog on crash.
- `setup_and_run.bat` (venv + dependency setup) and `build_exe.bat`
  (PyInstaller standalone `.exe` build).

### Known notes

- `requirements.txt` pins `numpy==2.5.1`, `pygame-ce==2.5.7`,
  `pyaudiowpatch==0.2.12.8` as a tested combination — a past `pygame-ce`
  update introduced a real rendering bug, so test thoroughly before
  upgrading any of these.
