# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-07-07

### Added

- Ground-up visual overhaul of four modes, all with peak-hold markers (a
  bright marker at the recent max that falls back slowly, like a VU
  meter) for a snappier feel:
  - **Bars**: center-mirrored (grows up/down from the middle instead of a
    bottom baseline) with rounded ends.
  - **Radial**: a glowing bass-reactive core disc and a second inner ring
    of spokes sampling different frequency content, for a fuller
    "sunburst" layout.
  - **Futuristic**: a counter-rotating dashed outer ring, a radar-sweep
    wedge with a fading trail, value-scaled dial segment brightness, and
    beat-reactive HUD corner brackets.
  - **Neon grid**: continuously scrolling floor lines (speed tied to bass
    energy) and a glowing synthwave sun on the horizon that pulses with
    the beat.
- New tunables, exposed in the launcher and persisted like the existing
  gain/smoothing settings: **attack** (the spectrum envelope's rise
  speed), **beat sensitivity** (onset-detection threshold), and a **force
  software rendering** checkbox for GPU troubleshooting.
- Launcher window is now resizable and remembers its size between runs,
  with a responsive two-column layout, a live color-swatch preview next
  to the theme dropdown, and a cleaner mode-picker grid.
- In-app control bar now measures itself and wraps into multiple rows
  when the window is too narrow for one, instead of silently running off
  the edge of the window.

### Changed

- Spectrum-based visual modes (bars, radial, rainbow, futuristic, neon grid)
  now use a fast-attack/slow-release envelope instead of one symmetric
  smoothing coefficient, so they snap up on a hit instead of lagging behind
  it. The smoothing slider now controls only the release (decay) rate.
- Reduced the audio chunk size from 1024 to 512 samples, roughly halving
  input latency (~23ms → ~12ms at 44.1kHz), at the cost of coarser
  frequency resolution per FFT bin. The beat-detector's rolling RMS window
  was widened to match, so it still averages over about the same ~1 second.
- Line widths, peak-hold markers, and HUD elements in the bars/radial/
  futuristic/grid modes now scale with the window's size/resolution
  instead of using fixed pixel values, so they stay visible at a tiny
  window and don't turn into thick slabs at 4K.
- Toggling fullscreen or resizing the window now preserves whichever
  renderer (software/GPU) is currently active, instead of potentially
  resetting it.

### Fixed

- Maximizing, minimizing, or restoring the window no longer leaves the
  rendered content and the control bar's clickable regions out of sync
  with the window's actual size - resizing is now verified against the
  real OS window size every frame instead of relying solely on a resize
  event that isn't always delivered reliably (e.g. clicking the native
  maximize button, as opposed to dragging an edge).
- The in-app control bar was silently overflowing past the visible edge
  of the window at the app's own default size, making some buttons
  unreachable - fixed by the new row-wrapping layout above.

### Considered and reverted

- Tried extending the spectrum-based modes' bar mapping to the full
  computed FFT range instead of just the bottom quarter, to surface
  high-frequency content (hi-hats, cymbals) that was previously excluded.
  Reverted after live testing: most music's energy is concentrated in that
  bottom quarter, so the extra range mostly added near-silent bins,
  diluting the whole visualization and making it feel *less* responsive
  overall. Revisit with a log/perceptual frequency-to-bar mapping instead
  of a linear one if the high end needs to be visible without this
  tradeoff.

## [1.0.0] - 2026-07-07

Baseline documented at the point version control was introduced. Prior
history was not tracked in Git; this entry reflects the app's state as of
this repository's first commit.

### Added

- Startup update check: queries the GitHub Releases API for
  `CarlFox98/signal-audio-visualizer` and, if a newer version has been
  published, shows a clickable notice in the launcher (or prints a link in
  console-fallback mode). Runs on a background thread, times out after 4
  seconds, and silently does nothing on any failure (offline, no releases
  published yet, rate limiting) — never blocks or interrupts startup.
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
