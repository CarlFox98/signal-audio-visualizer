# Signal — Audio Visualizer

A native Windows desktop audio visualizer. Captures system/output audio
directly via WASAPI loopback (no virtual cable needed) or from any physical
microphone, and renders a real-time spectrum/waveform visualization.

## Features

- **7 visual modes** — bars, waveform, radial, particles, rainbow bars,
  a futuristic HUD dial, and a neon perspective grid.
- **GPU-accelerated rendering** via OpenGL, with automatic fallback to
  software rendering if a GPU/driver isn't available (toggle manually with
  `g`).
- **5 color themes**, adjustable gain and smoothing, and simple beat/onset
  detection that drives visual accents.
- **System audio capture with no setup** — reads directly from your
  default output device via WASAPI loopback. Optionally install the
  [VB-CABLE](https://vb-audio.com/Cable/) virtual audio driver (from inside
  the app) to isolate a single application's audio instead of everything.
- Persists your last-used device, mode, gain, smoothing, and theme between
  runs.
- Per-session log files and a friendly error dialog if something goes wrong,
  so problems are easy to diagnose and report.

## Requirements

- Windows 10/11 (uses WASAPI loopback capture, which is Windows-only).
- Python 3.11–3.13 (if running from source).

## Quick start

### Option A — run from source

```
setup_and_run.bat
```

This creates a local virtual environment (`venv/`), installs the pinned
dependencies from [requirements.txt](requirements.txt), and launches the app.
Run it again any time to just launch the app (it reuses the existing venv).

Or do it manually:

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python audio_visualizer.py
```

### Option B — build a standalone .exe

```
build_exe.bat
```

Produces `dist/SignalVisualizer.exe` — a single file that runs on any
Windows PC with no Python installation required. Requires `setup_and_run.bat`
to have been run at least once first (it needs the venv's dependencies to
bundle).

## Usage

On launch, a small picker window lets you choose an audio source (your
system output, a specific loopback device, or a microphone) and initial
mode/theme/gain/smoothing before the visualizer window opens.

### Isolating a single app's audio

By default you capture *everything* playing on your PC. To visualize only
one app (e.g. a music player):

1. Click **install virtual audio cable** in the picker window (or run the
   VB-CABLE installer manually from [vb-audio.com](https://vb-audio.com/Cable/)).
2. Reboot — the virtual driver needs a restart to register.
3. In Windows Settings → System → Sound → Volume mixer, set that one app's
   output device to the virtual cable.
4. Pick the virtual cable's loopback entry from the source list.

Everything else keeps playing on your normal speakers as usual.

### Keyboard shortcuts

| Key(s)              | Action                          |
|----------------------|---------------------------------|
| `1`–`7`              | Switch visual mode              |
| `t`                  | Cycle color theme                |
| `f` / `F11`          | Toggle fullscreen                |
| `g`                  | Toggle GPU/software renderer     |
| `↑` / `↓`            | Gain up / down                   |
| `←` / `→`            | Smoothing down / up              |
| `Esc`                | Exit fullscreen, or quit          |

All of the above are also available as clickable buttons in the on-screen
control bar.

## Configuration and logs

- Settings (device, mode, gain, smoothing, theme) are saved to
  `visualizer_config.json` next to the app and reloaded on the next run.
- Each run writes a timestamped log to `logs/session_*.log`. Logs older than
  30 days are pruned automatically. If the app crashes, share the latest
  log file when reporting an issue.

## Checking for updates

On startup, the app does a quick, non-blocking check of this repo's
[GitHub Releases](https://github.com/CarlFox98/signal-audio-visualizer/releases)
for a newer published version than the one you're running (`APP_VERSION` in
`audio_visualizer.py`). If one exists, the launcher window shows a clickable
notice (or, in console-fallback mode, prints a link). It never downloads or
installs anything automatically, and any failure (offline, no releases
published yet) is silently ignored.

To publish a version the check will detect, tag a commit and create a
GitHub release with a matching `vX.Y.Z` tag, e.g.:

```
git tag v1.0.0
git push origin v1.0.0
gh release create v1.0.0 --title "v1.0.0" --generate-notes
```

## Dependencies

Pinned in [requirements.txt](requirements.txt) as a tested, known-working
combination:

- [`numpy`](https://numpy.org/) — FFT and signal processing.
- [`pygame-ce`](https://pyga.me/) — windowing, input, and rendering.
- [`pyaudiowpatch`](https://github.com/s0d3s/PyAudioWPatch) — WASAPI loopback
  audio capture (a PyAudio fork with loopback support).
- [`PyOpenGL`](http://pyopengl.sourceforge.net/) (optional) — enables the
  GPU rendering path; the app works fine without it.

See [CHANGELOG.md](CHANGELOG.md) before upgrading any of these, especially
`pygame-ce`.

## License

[MIT](LICENSE)
