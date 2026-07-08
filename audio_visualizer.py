"""
Signal — a native desktop audio visualizer for Windows.

Captures system/output audio directly via WASAPI loopback (no virtual
cable needed) or from any physical microphone, and renders a real-time
spectrum/waveform visualization with pygame.

Requirements (Windows only):
    pip install pyaudiowpatch numpy pygame

Run:
    python audio_visualizer.py
"""

import sys
import os
import queue
import threading
import platform
import tempfile
import zipfile
import urllib.request
import urllib.error
import logging
import datetime
import traceback
import json
import time
import collections
import colorsys
import webbrowser

import numpy as np
import pygame

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    print("This app requires 'pyaudiowpatch', which only works on Windows.")
    print("Install it with:  pip install pyaudiowpatch")
    sys.exit(1)


def _set_windows_dpi_awareness():
    """Mark this process as per-monitor DPI aware. Without this, Windows
    silently bitmap-scales the entire window on high-DPI or non-100%-scaled
    displays, which shows up as blurry text/UI and geometry that doesn't
    line up correctly between windowed and fullscreen modes - since the app
    is working with a virtualized resolution rather than real device
    pixels. This must run before ANY window (Tkinter or pygame) is created,
    so it's called at import time, at the top of the file."""
    try:
        import ctypes
        # Windows 10 1703+: per-monitor v2 - best, handles moving the
        # window between monitors with different scaling correctly
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()  # older Windows fallback
    except Exception:
        pass  # non-Windows or very old Windows - nothing we can do, not fatal


_set_windows_dpi_awareness()

try:
    import OpenGL.GL as gl
    OPENGL_AVAILABLE = True
except ImportError:
    OPENGL_AVAILABLE = False

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    TKINTER_AVAILABLE = True
except ImportError:    TKINTER_AVAILABLE = False

VB_CABLE_URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"

APP_VERSION = "1.1.0"
UPDATE_REPO = "CarlFox98/signal-audio-visualizer"
UPDATE_CHECK_TIMEOUT = 4  # seconds - must not noticeably delay startup

CHUNK = 512
FORMAT = pyaudio.paInt16
WINDOW_W, WINDOW_H = 960, 560

# Fixed attack rate for the spectrum envelope (see compute_spectrum) - how
# much of a new, LOUDER value gets blended in per frame. Low = fast attack,
# so bars snap up on a hit almost instantly. The user-facing "smoothing"
# slider only controls the release (decay) rate, since a slow attack is
# what makes a visualizer feel laggy, not a slow release.
SPECTRUM_ATTACK = 0.2

# How fast a peak-hold marker falls back down once the value it was
# tracking drops (bars/radial/grid caps) - multiplied in per frame, so
# closer to 1.0 = slower fall. Tuned to look like a classic VU meter's
# mechanical peak-hold rather than an instant snap-back.
PEAK_FALL_RATE = 0.985

BG = (8, 9, 10)
PANEL = (21, 23, 26)
LINE = (38, 42, 46)
AMBER = (232, 161, 59)
AMBER_DIM = (107, 82, 40)
TEAL = (59, 168, 152)
TEXT = (231, 228, 220)
TEXT_DIM = (122, 125, 130)

BG_HEX = "#0c0d0e"
PANEL_HEX = "#15171a"
LINE_HEX = "#262a2e"
AMBER_HEX = "#e8a13b"
TEAL_HEX = "#3ba898"
TEXT_HEX = "#e7e4dc"
TEXT_DIM_HEX = "#7a7d82"

THEMES = [
    {"name": "amber / teal", "primary": (232, 161, 59), "secondary": (59, 168, 152)},
    {"name": "cyan / magenta", "primary": (56, 199, 255), "secondary": (255, 66, 161)},
    {"name": "acid green", "primary": (140, 255, 90), "secondary": (30, 140, 70)},
    {"name": "violet / gold", "primary": (168, 120, 255), "secondary": (240, 200, 90)},
    {"name": "monochrome", "primary": (235, 235, 235), "secondary": (120, 120, 120)},
]

def _rgb_hex(rgb):
    """(r, g, b) int tuple -> '#rrggbb', for handing THEMES colors to
    Tkinter widgets that need a hex string (e.g. the theme swatches)."""
    return "#{:02x}{:02x}{:02x}".format(*rgb)


# Fixed palettes for modes whose visual identity IS a specific color scheme
# (deliberately not tied to the user-selected theme above)
FUTURISTIC_CYAN = (70, 220, 255)
FUTURISTIC_WHITE = (225, 240, 255)
NEON_CYAN = (0, 225, 255)
NEON_ORANGE = (255, 140, 20)

if getattr(sys, "frozen", False):
    # running as a PyInstaller-built .exe - use the folder the .exe lives
    # in, not the temporary extraction folder __file__ would point to
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "visualizer_config.json")
LOG_MAX_AGE_DAYS = 30


def load_config():
    """Load the last-used device name, mode, gain, smoothing, and other
    tunables. Returns sensible defaults if the file doesn't exist or is
    malformed."""
    defaults = {
        "device_name": None, "mode": "bars", "gain": 1.4, "smoothing": 0.7, "theme_idx": 0,
        "attack": SPECTRUM_ATTACK, "beat_sensitivity": 1.4, "force_software": False,
        "window_w": None, "window_h": None,
    }
    if not os.path.exists(CONFIG_PATH):
        return defaults
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        defaults.update({k: data[k] for k in defaults if k in data})
    except (OSError, json.JSONDecodeError, TypeError):
        logging.warning("Could not read saved config, using defaults", exc_info=True)
    return defaults


def save_config(device_name, mode, gain, smoothing, theme_idx=0, attack=SPECTRUM_ATTACK,
                 beat_sensitivity=1.4, force_software=False, window_w=None, window_h=None):
    data = {
        "device_name": device_name,
        "mode": mode,
        "gain": gain,
        "smoothing": smoothing,
        "theme_idx": theme_idx,
        "attack": attack,
        "beat_sensitivity": beat_sensitivity,
        "force_software": force_software,
        "window_w": window_w,
        "window_h": window_h,
    }
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        logging.warning("Could not save config (non-fatal)", exc_info=True)


def prune_old_logs():
    """Delete log files older than LOG_MAX_AGE_DAYS so the logs folder
    doesn't grow forever. Failures here are non-fatal and just get printed."""
    if not os.path.isdir(LOG_DIR):
        return
    cutoff = datetime.datetime.now() - datetime.timedelta(days=LOG_MAX_AGE_DAYS)
    for name in os.listdir(LOG_DIR):
        if not name.startswith("session_") or not name.endswith(".log"):
            continue
        path = os.path.join(LOG_DIR, name)
        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
            if mtime < cutoff:
                os.remove(path)
        except OSError:
            pass


def show_error_dialog(title, message):
    """Show a Tk error dialog if possible, falling back to a console pause
    if Tkinter isn't available or a display can't be reached."""
    if TKINTER_AVAILABLE:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(title, message)
            root.destroy()
            return
        except Exception:
            pass
    print("\n" + "=" * 60)
    print(title)
    print(message)
    print("=" * 60)
    input("\nPress Enter to close...")


def setup_logging():
    """Set up a per-run log file plus console output, and capture any
    uncaught exception (from the main thread) into that log file."""
    os.makedirs(LOG_DIR, exist_ok=True)
    prune_old_logs()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"session_{timestamp}.log")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    console_handler.setLevel(logging.WARNING)
    root_logger.addHandler(console_handler)

    logging.info("=" * 60)
    logging.info(f"Signal Audio Visualizer starting (v{APP_VERSION})")
    logging.info(f"Log file: {log_path}")
    logging.info(f"Python: {sys.version.split()[0]}  ({platform.architecture()[0]})")
    logging.info(f"OS: {platform.platform()}")
    try:
        logging.info(f"numpy: {np.__version__}")
    except Exception:
        logging.info("numpy: version unknown")
    try:
        logging.info(f"pygame: {pygame.version.ver}")
    except Exception:
        logging.info("pygame: version unknown")
    try:
        logging.info(f"pyaudiowpatch: {pyaudio.__version__}")
    except Exception:
        logging.info("pyaudiowpatch: version unknown")

    return log_path


def log_uncaught_exception(exc_type, exc_value, exc_traceback):
    """Global hook so any exception that escapes the main thread gets
    written to the log file with a full traceback before the program exits."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.critical("UNHANDLED EXCEPTION - the program is about to crash:")
    logging.critical("".join(traceback.format_exception(exc_type, exc_value, exc_traceback)))
    show_error_dialog(
        "Signal Audio Visualizer - unexpected error",
        "The program hit an unexpected error and needs to close.\n\n"
        f"Full details were written to:\n{LOG_DIR}\n\n"
        "Please share the latest session_*.log file from that folder if you need help.",
    )


def log_thread_exception(args):
    """Hook for exceptions raised in background threads (Python 3.8+),
    which the default excepthook above does not catch."""
    logging.critical(f"UNHANDLED EXCEPTION in thread '{args.thread.name}':")
    logging.critical(
        "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    )


sys.excepthook = log_uncaught_exception
threading.excepthook = log_thread_exception


def download_and_install_vb_cable():
    """Download the official VB-CABLE virtual audio driver, extract it, and
    launch its installer with administrator rights. Returns True if the
    installer was successfully launched (not necessarily finished)."""

    logging.info("User requested VB-CABLE install")
    print("\nDownloading VB-CABLE (official VB-Audio virtual audio driver)...")
    print(f"Source: {VB_CABLE_URL}")

    tmp_dir = tempfile.mkdtemp(prefix="vbcable_")
    zip_path = os.path.join(tmp_dir, "VBCABLE_Driver_Pack45.zip")

    try:
        urllib.request.urlretrieve(VB_CABLE_URL, zip_path)
    except (urllib.error.URLError, OSError) as e:
        logging.error(f"VB-CABLE download failed: {e}")
        print(f"Download failed: {e}")
        print(f"You can download it manually from: {VB_CABLE_URL}")
        return False

    print("Extracting installer...")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_dir)
    except zipfile.BadZipFile as e:
        logging.error(f"VB-CABLE archive extraction failed: {e}")
        print(f"Failed to extract the downloaded archive: {e}")
        return False

    is_64bit = platform.architecture()[0] == "64bit"
    candidate_names = (
        ["VBCABLE_Setup_x64.exe", "VBCABLE_Setup.exe"]
        if is_64bit
        else ["VBCABLE_Setup.exe", "VBCABLE_Setup_x64.exe"]
    )

    setup_exe = None
    for root, _, files in os.walk(tmp_dir):
        lower_files = {f.lower(): f for f in files}
        for name in candidate_names:
            if name.lower() in lower_files:
                setup_exe = os.path.join(root, lower_files[name.lower()])
                break
        if setup_exe:
            break

    if setup_exe is None:
        logging.error(f"VB-CABLE installer exe not found under {tmp_dir}")
        print("Could not find the installer executable inside the downloaded package.")
        print(f"Open this folder manually and run the setup file: {tmp_dir}")
        return False

    print(f"\nLaunching installer: {os.path.basename(setup_exe)}")
    print("Windows will show a User Account Control prompt - approve it to continue.")
    print("In the installer window, click 'Install'.\n")
    logging.info(f"Launching VB-CABLE installer: {setup_exe}")

    try:
        import ctypes
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", setup_exe, None, os.path.dirname(setup_exe), 1
        )
        if int(result) <= 32:
            logging.error(f"ShellExecuteW returned error code {result}")
            print("Windows blocked or cancelled the elevation request.")
            print(f"Run it manually as administrator from: {setup_exe}")
            return False
    except Exception as e:
        logging.exception("Failed to launch VB-CABLE installer")
        print(f"Could not launch the installer automatically: {e}")
        print(f"Run it manually as administrator from: {setup_exe}")
        return False

    print("IMPORTANT: once the installer finishes, reboot your PC - the virtual")
    print("audio driver needs a restart to register correctly.")
    print("After rebooting, re-run this program: 'CABLE Input' / 'CABLE Output'")
    print("will show up as devices you can route an app to and capture from.\n")
    logging.info("VB-CABLE installer launched successfully")
    return True


def _parse_version(v):
    """Parse a version string like 'v1.2.0' or '1.2.0' into a tuple of ints
    for comparison. Returns None if it doesn't look like a plain dotted
    version number."""
    v = v.strip().lstrip("vV")
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return None


def check_for_update():
    """Best-effort check of the GitHub Releases API for a newer published
    version than APP_VERSION. Returns {"version", "url"} if a newer release
    is available, otherwise None - including on any failure (no internet,
    no releases published yet, rate limiting, unparseable tag, etc). This
    must never raise or block startup; failures are logged and swallowed."""
    url = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"signal-audio-visualizer/{APP_VERSION}",
    })
    try:
        with urllib.request.urlopen(req, timeout=UPDATE_CHECK_TIMEOUT) as resp:
            data = json.load(resp)
        latest = _parse_version(data.get("tag_name", ""))
        current = _parse_version(APP_VERSION)
        if latest is not None and current is not None and latest > current:
            return {
                "version": data.get("tag_name"),
                "url": data.get("html_url", f"https://github.com/{UPDATE_REPO}/releases/latest"),
            }
        return None
    except urllib.error.HTTPError as e:
        if e.code != 404:  # 404 just means no release has been published yet
            logging.warning(f"Update check failed: HTTP {e.code}")
        return None
    except (urllib.error.URLError, OSError, ValueError) as e:
        logging.warning(f"Update check failed (non-fatal): {e}")
        return None


def list_capture_devices(p):
    """Return a combined list of loopback (system audio) and input (mic) devices."""
    devices = []

    try:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_output_index = wasapi_info["defaultOutputDevice"]
        default_output_name = p.get_device_info_by_index(default_output_index)["name"]
    except Exception:
        default_output_name = None

    for dev in p.get_loopback_device_info_generator():
        is_default = (
            default_output_name is not None
            and dev["name"].startswith(default_output_name)
        )
        kind = (
            "system audio (loopback) — your main output, captures everything"
            if is_default
            else "alternate device (loopback) — route a single app here to isolate it"
        )
        devices.append({
            "index": dev["index"],
            "name": dev["name"],
            "channels": dev["maxInputChannels"],
            "rate": int(dev["defaultSampleRate"]),
            "kind": kind,
        })

    try:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        api_index = wasapi_info["index"]
    except Exception:
        api_index = None

    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info["maxInputChannels"] <= 0:
            continue
        if info.get("isLoopbackDevice"):
            continue
        if api_index is not None and info["hostApi"] != api_index:
            continue
        devices.append({
            "index": info["index"],
            "name": info["name"],
            "channels": info["maxInputChannels"],
            "rate": int(info["defaultSampleRate"]),
            "kind": "microphone / line-in",
        })

    return devices


def choose_device(p):
    while True:
        devices = list_capture_devices(p)
        if not devices:
            logging.error("No capture devices found by list_capture_devices()")
            print("No capture devices found.")
            sys.exit(1)

        logging.info(f"Found {len(devices)} capture device(s):")
        for d in devices:
            logging.info(f"  {d['name']}  ({d['kind']}, {d['channels']}ch @ {d['rate']}Hz)")

        print("\nAvailable audio sources:\n")
        for n, d in enumerate(devices):
            print(f"  [{n}] {d['name']}  —  {d['kind']}")
        print()
        print("  [i] install a virtual audio cable now (lets you isolate a single app)")
        print()
        print("Tip — to visualize ONLY one app (e.g. Spotify) instead of everything:")
        print("  1. Install the virtual cable (option 'i' above, or vb-audio.com/Cable)")
        print("  2. Windows Settings > System > Sound > Volume mixer")
        print("     -> set that one app's output device to the virtual cable")
        print("  3. Pick the virtual cable's loopback entry from the list above")
        print("  (everything else stays on your normal speakers as usual)\n")

        raw = input(f"Select a source (0-{len(devices)-1}, or 'i' to install): ").strip()

        if raw.lower() in ("i", "install"):
            download_and_install_vb_cable()
            continue

        if raw.isdigit() and 0 <= int(raw) < len(devices):
            chosen = devices[int(raw)]
            logging.info(f"User selected device: {chosen['name']} ({chosen['kind']})")
            return chosen
        print("Not a valid choice, try again.")


def run_gui_launcher(p):
    """Show a Tkinter window for picking the audio source and initial
    settings before the visualizer opens. Returns a dict with keys
    'device', 'mode', 'gain', 'smoothing', or None if the user closed
    the window without starting."""

    result = {}
    saved = load_config()

    root = tk.Tk()
    root.title("Signal Audio Visualizer")
    root.geometry(f"{saved.get('window_w') or 760}x{saved.get('window_h') or 600}")
    root.minsize(680, 560)
    root.configure(bg=BG_HEX)
    root.resizable(True, True)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TFrame", background=BG_HEX)
    style.configure("TLabel", background=BG_HEX, foreground=TEXT_HEX, font=("Consolas", 10))
    style.configure("Header.TLabel", background=BG_HEX, foreground=AMBER_HEX,
                     font=("Consolas", 14, "bold"))
    style.configure("Dim.TLabel", background=BG_HEX, foreground=TEXT_DIM_HEX, font=("Consolas", 9))
    style.configure("TButton", background=PANEL_HEX, foreground=TEXT_HEX,
                     font=("Consolas", 10), borderwidth=1)
    style.map("TButton", background=[("active", LINE_HEX)])
    style.configure("Accent.TButton", background=TEAL_HEX, foreground=BG_HEX,
                     font=("Consolas", 10, "bold"))
    style.map("Accent.TButton", background=[("active", "#4fc3b0")])
    style.configure("TRadiobutton", background=BG_HEX, foreground=TEXT_HEX, font=("Consolas", 10))
    style.map("TRadiobutton", background=[("active", BG_HEX)])
    style.configure("TCheckbutton", background=BG_HEX, foreground=TEXT_HEX, font=("Consolas", 10))
    style.map("TCheckbutton", background=[("active", BG_HEX)])
    style.configure("Horizontal.TScale", background=BG_HEX)

    def divider(parent):
        f = tk.Frame(parent, bg=LINE_HEX, height=1)
        f.pack(fill="x", pady=(10, 10))
        return f

    outer = ttk.Frame(root, padding=16)
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(0, weight=1, minsize=300)
    outer.columnconfigure(1, weight=1, minsize=300)
    outer.rowconfigure(1, weight=1)

    # --- header row (spans both columns) ---------------------------------
    header = ttk.Frame(outer)
    header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))

    ttk.Label(header, text="SIGNAL_VIS", style="Header.TLabel").pack(anchor="w")
    ttk.Label(header, text="choose an audio source and initial settings",
              style="Dim.TLabel").pack(anchor="w", pady=(0, 4))

    # Update check runs on a background thread so a slow/absent network
    # connection can't delay showing this window. update_state is only
    # written by the background thread and read by poll_update() below,
    # which runs on the main thread via root.after() - no lock needed
    # since it's a single flag flip plus a single dict write.
    update_var = tk.StringVar(value="")
    update_state = {"done": False, "result": None}

    def bg_check_update():
        update_state["result"] = check_for_update()
        update_state["done"] = True

    threading.Thread(target=bg_check_update, daemon=True).start()

    def poll_update():
        if update_state["done"]:
            info = update_state["result"]
            if info:
                update_var.set(f"update available: {info['version']}  (click to open)")
            return
        root.after(300, poll_update)

    root.after(300, poll_update)

    update_label = ttk.Label(header, textvariable=update_var, style="Dim.TLabel", cursor="hand2")
    update_label.pack(anchor="w")
    update_label.bind(
        "<Button-1>",
        lambda e: webbrowser.open(update_state["result"]["url"]) if update_state["result"] else None,
    )

    # --- left column: audio source picker ---------------------------------
    left_col = ttk.Frame(outer)
    left_col.grid(row=1, column=0, sticky="nsew", padx=(0, 16))
    left_col.columnconfigure(0, weight=1)
    left_col.rowconfigure(1, weight=1)

    ttk.Label(left_col, text="Audio sources", style="TLabel").grid(row=0, column=0, sticky="w")

    list_frame = ttk.Frame(left_col)
    list_frame.grid(row=1, column=0, sticky="nsew", pady=(4, 4))
    list_frame.columnconfigure(0, weight=1)
    list_frame.rowconfigure(0, weight=1)

    scrollbar = tk.Scrollbar(list_frame)
    scrollbar.grid(row=0, column=1, sticky="ns")

    listbox = tk.Listbox(
        list_frame, height=8, bg=PANEL_HEX, fg=TEXT_HEX,
        selectbackground=AMBER_HEX, selectforeground=BG_HEX,
        font=("Consolas", 10), borderwidth=0, highlightthickness=1,
        highlightbackground=LINE_HEX, yscrollcommand=scrollbar.set,
    )
    listbox.grid(row=0, column=0, sticky="nsew")
    scrollbar.config(command=listbox.yview)

    devices_holder = {"devices": []}

    def refresh_devices(select_saved=False):
        try:
            devices = list_capture_devices(p)
        except Exception:
            logging.exception("Failed to enumerate devices for the GUI launcher")
            devices = []
        devices_holder["devices"] = devices
        listbox.delete(0, tk.END)
        for d in devices:
            listbox.insert(tk.END, f"{d['name']}  —  {d['kind']}")
        status_var.set(f"found {len(devices)} device(s)")

        if select_saved and saved.get("device_name"):
            for idx, d in enumerate(devices):
                if d["name"] == saved["device_name"]:
                    listbox.selection_set(idx)
                    listbox.see(idx)
                    break

    status_var = tk.StringVar(value="")
    ttk.Label(left_col, textvariable=status_var, style="Dim.TLabel").grid(
        row=2, column=0, sticky="w", pady=(4, 8)
    )

    btn_row = ttk.Frame(left_col)
    btn_row.grid(row=3, column=0, sticky="w", pady=(0, 12))

    def do_refresh():
        refresh_devices()

    def do_install():
        proceed = messagebox.askyesno(
            "Install virtual audio cable",
            "This downloads the official VB-CABLE driver from vb-audio.com and "
            "launches its installer with administrator rights.\n\n"
            "You'll see a Windows permission prompt and the installer's own "
            "window - nothing is installed silently.\n\nContinue?",
        )
        if not proceed:
            return
        install_btn.config(state="disabled", text="installing...")
        root.update_idletasks()
        ok = False
        try:
            ok = download_and_install_vb_cable()
        except Exception:
            logging.exception("Unexpected error during VB-CABLE install from GUI")
        install_btn.config(state="normal", text="install virtual audio cable")
        if ok:
            messagebox.showinfo(
                "Installer launched",
                "Finish the installer window, then REBOOT your PC - the driver "
                "needs a restart to register.\n\n"
                "After rebooting, come back and click Refresh to see "
                "'CABLE Input' / 'CABLE Output' in the list.",
            )
        else:
            messagebox.showerror(
                "Install failed",
                f"Something went wrong. Check the log file for details:\n{LOG_DIR}",
            )
        refresh_devices()

    ttk.Button(btn_row, text="refresh", command=do_refresh).pack(side="left")
    install_btn = ttk.Button(btn_row, text="install virtual audio cable", command=do_install)
    install_btn.pack(side="left", padx=(8, 0))

    tip = (
        "Tip — to visualize ONLY one app (e.g. Spotify): install the virtual cable "
        "above, then in Windows Settings > System > Sound > Volume mixer set that "
        "app's output to the virtual cable, then pick its loopback entry here."
    )
    tip_label = ttk.Label(left_col, text=tip, style="Dim.TLabel", wraplength=320, justify="left")
    tip_label.grid(row=4, column=0, sticky="w")

    # --- right column: mode / theme / tuning settings ---------------------
    right_col = ttk.Frame(outer)
    right_col.grid(row=1, column=1, sticky="nsew")

    ttk.Label(right_col, text="Starting visual mode", style="TLabel").pack(anchor="w")
    mode_var = tk.StringVar(value=saved.get("mode", "bars"))
    mode_grid = ttk.Frame(right_col)
    mode_grid.pack(fill="x", pady=(4, 4), anchor="w")
    modes = [
        ("bars", "bars"), ("wave", "waveform"), ("radial", "radial"), ("particles", "particles"),
        ("rainbow", "rainbow"), ("futuristic", "futuristic"), ("grid", "neon grid"),
    ]
    for i, (value, label) in enumerate(modes):
        r, c = divmod(i, 3)
        ttk.Radiobutton(mode_grid, text=label, value=value, variable=mode_var).grid(
            row=r, column=c, sticky="w", padx=(0, 14), pady=(0, 4)
        )

    divider(right_col)

    theme_row = ttk.Frame(right_col)
    theme_row.pack(fill="x", pady=(0, 4), anchor="w")
    ttk.Label(theme_row, text="color theme", style="Dim.TLabel").pack(side="left", padx=(0, 8))
    theme_names = [t["name"] for t in THEMES]
    theme_var = tk.StringVar(value=theme_names[min(saved.get("theme_idx", 0), len(theme_names) - 1)])
    theme_dropdown = ttk.Combobox(
        theme_row, textvariable=theme_var, values=theme_names, state="readonly", width=16
    )
    theme_dropdown.pack(side="left")

    # small live preview of the selected theme's two colors, so you don't
    # have to start the visualizer just to see what a theme looks like
    swatch_primary = tk.Canvas(theme_row, width=16, height=16, highlightthickness=1,
                                highlightbackground=LINE_HEX, bg=BG_HEX)
    swatch_primary.pack(side="left", padx=(10, 3))
    swatch_secondary = tk.Canvas(theme_row, width=16, height=16, highlightthickness=1,
                                  highlightbackground=LINE_HEX, bg=BG_HEX)
    swatch_secondary.pack(side="left")

    def update_swatches(*_args):
        idx = theme_names.index(theme_var.get()) if theme_var.get() in theme_names else 0
        swatch_primary.configure(bg=_rgb_hex(THEMES[idx]["primary"]))
        swatch_secondary.configure(bg=_rgb_hex(THEMES[idx]["secondary"]))

    theme_var.trace_add("write", update_swatches)
    update_swatches()

    divider(right_col)

    ttk.Label(right_col, text="Tuning", style="TLabel").pack(anchor="w", pady=(0, 4))

    def slider_row(parent, label_text, var, frm, to):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(2, 2))
        ttk.Label(row, text=label_text, style="Dim.TLabel", width=13, anchor="w").pack(side="left")
        ttk.Scale(row, from_=frm, to=to, variable=var).pack(side="left", fill="x", expand=True, padx=(8, 0))

    gain_var = tk.DoubleVar(value=saved.get("gain", 1.4))
    slider_row(right_col, "gain", gain_var, 0.1, 4.0)

    smoothing_var = tk.DoubleVar(value=saved.get("smoothing", 0.7))
    slider_row(right_col, "smoothing", smoothing_var, 0.0, 0.97)

    attack_var = tk.DoubleVar(value=saved.get("attack", SPECTRUM_ATTACK))
    slider_row(right_col, "attack", attack_var, 0.02, 0.9)

    beat_sensitivity_var = tk.DoubleVar(value=saved.get("beat_sensitivity", 1.4))
    slider_row(right_col, "beat sensitivity", beat_sensitivity_var, 1.05, 2.5)

    divider(right_col)

    force_software_var = tk.BooleanVar(value=saved.get("force_software", False))
    ttk.Checkbutton(
        right_col, text="force software rendering (troubleshooting)",
        variable=force_software_var,
    ).pack(anchor="w")

    # --- start button (spans both columns) ---------------------------------
    def do_start():
        sel = listbox.curselection()
        if not sel:
            messagebox.showwarning("No device selected", "Pick an audio source from the list first.")
            return
        device = devices_holder["devices"][sel[0]]
        theme_idx = theme_names.index(theme_var.get()) if theme_var.get() in theme_names else 0
        result["device"] = device
        result["mode"] = mode_var.get()
        result["gain"] = gain_var.get()
        result["smoothing"] = smoothing_var.get()
        result["theme_idx"] = theme_idx
        result["attack"] = attack_var.get()
        result["beat_sensitivity"] = beat_sensitivity_var.get()
        result["force_software"] = force_software_var.get()
        result["window_w"] = root.winfo_width()
        result["window_h"] = root.winfo_height()
        save_config(
            device["name"], result["mode"], result["gain"], result["smoothing"], theme_idx,
            attack=result["attack"], beat_sensitivity=result["beat_sensitivity"],
            force_software=result["force_software"],
            window_w=result["window_w"], window_h=result["window_h"],
        )
        root.destroy()

    start_btn = ttk.Button(outer, text="start visualizer", command=do_start, style="Accent.TButton")
    start_btn.grid(row=2, column=0, columnspan=2, sticky="ew", ipady=6, pady=(16, 0))

    refresh_devices(select_saved=True)
    root.mainloop()

    return result if "device" in result else None


class AudioCapture:
    def __init__(self, p, device):
        self.p = p
        self.device = device
        self.q = queue.Queue(maxsize=8)
        self.channels = max(1, device["channels"])
        self.rate = device["rate"]
        self._stop = False
        logging.info(
            f"Opening audio stream: {device['name']} "
            f"({self.channels}ch @ {self.rate}Hz, device index {device['index']})"
        )
        try:
            self.stream = p.open(
                format=FORMAT,
                channels=self.channels,
                rate=self.rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=CHUNK,
                stream_callback=self._callback,
            )
        except Exception:
            logging.exception(f"Failed to open audio stream for device: {device['name']}")
            raise

    def _callback(self, in_data, frame_count, time_info, status):
        try:
            if status:
                logging.warning(f"Audio callback status flag set: {status}")
            self.q.put_nowait(in_data)
        except queue.Full:
            pass
        except Exception:
            logging.exception("Unexpected error inside the audio capture callback")
        return (None, pyaudio.paContinue)

    def start(self):
        try:
            self.stream.start_stream()
            logging.info("Audio stream started")
        except Exception:
            logging.exception("Failed to start audio stream")
            raise

    def stop(self):
        self._stop = True
        try:
            self.stream.stop_stream()
            self.stream.close()
            logging.info("Audio stream stopped and closed")
        except Exception:
            logging.exception("Error while stopping/closing the audio stream (non-fatal)")

    def latest_samples(self):
        """Drain the queue and return the most recent chunk as float32 mono samples."""
        data = None
        while True:
            try:
                data = self.q.get_nowait()
            except queue.Empty:
                break
        if data is None:
            return None
        arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if self.channels > 1:
            arr = arr.reshape(-1, self.channels).mean(axis=1)
        return arr


class Visualizer:
    CONTROL_BAR_H = 44

    def __init__(self, capture, source_label, force_software=False):
        pygame.init()
        pygame.display.set_caption("SIGNAL_VIS — " + source_label)

        # pygame.display.Info() only reliably reports the TRUE desktop
        # resolution before any window/mode has been set - afterward, on
        # some platform/driver combinations, it can reflect the current
        # window's size instead. Capture it once, right here, and reuse
        # this value for fullscreen sizing from now on.
        _pre_info = pygame.display.Info()
        self.desktop_size = (_pre_info.current_w, _pre_info.current_h)

        # force_software is a startup-time escape hatch (set in the
        # launcher) for machines where GPU rendering is technically
        # available but misbehaves - distinct from the in-app 'g' toggle,
        # which flips self.use_gl at runtime instead.
        self.gl_requested = OPENGL_AVAILABLE and not force_software
        self.use_gl = False
        self.screen = self._create_display(WINDOW_W, WINDOW_H, fullscreen=False,
                                            want_gl=self.gl_requested)

        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("consolas", 14)
        self.font_small = pygame.font.SysFont("consolas", 11)

        self.capture = capture
        self.source_label = source_label
        self.mode = "bars"
        self.gain = 1.4
        self.smoothing = 0.7
        self.attack = SPECTRUM_ATTACK
        self.beat_sensitivity = 1.4
        self.smoothed_spectrum = None
        self.particles = []
        self.running = True

        # peak-hold markers for the bars/radial/grid modes - lazily sized
        # on first use, same pattern as smoothed_spectrum above
        self.bar_peaks = None
        self.radial_peaks = None
        self.grid_peaks = None

        self.theme_idx = 0
        self.primary = THEMES[0]["primary"]
        self.secondary = THEMES[0]["secondary"]

        # beat / onset detection - maxlen chosen to cover roughly the same
        # ~1s rolling window as before CHUNK was halved (43 chunks @ 1024
        # samples was ~1s at 44.1kHz; doubled here to match at 512 samples)
        self.rms_history = collections.deque(maxlen=86)
        self.last_beat_time = 0.0
        self.beat_flash = 0.0

        # animation phases for the rainbow / futuristic / neon grid modes
        self.rainbow_phase = 0.0
        self.futuristic_rotation = 0.0
        self.grid_scroll = 0.0

        self.control_rects = []  # populated each frame by build_control_bar
        self.control_bar_h = self.CONTROL_BAR_H  # recomputed each frame - see _layout_control_bar
        self._control_bar_rows = []

        self.fullscreen = False
        self.windowed_size = (WINDOW_W, WINDOW_H)

        self.last_samples = np.zeros(CHUNK, dtype=np.float32)

    def _create_display(self, w, h, fullscreen, want_gl):
        """Create (or recreate) the display surface. Tries a GPU-accelerated
        OpenGL context first if requested; on ANY failure (missing driver
        support, PyOpenGL not installed, headless/remote desktop, etc.) it
        falls back to the plain software renderer that's been working all
        along. Sets self.use_gl accordingly.

        For fullscreen, this uses self.desktop_size (captured once, before
        any window existed) rather than an arbitrary or re-queried size.
        Passing the real desktop resolution with the FULLSCREEN flag lets
        SDL2 use borderless "fullscreen desktop" behavior rather than an
        exclusive fullscreen video-mode switch. Exclusive fullscreen (or
        requesting a resolution that doesn't match the desktop) can
        silently grant a different size than requested - causing blur,
        scaling bugs, and mouse coordinates that desync from the window's
        actual size (clicks missing their targets). Note: relying on
        SDL's (0, 0) "auto-detect desktop size" convention was tried first
        but proved inconsistent between software and GPU rendering modes
        on at least one tested driver combination, which is why the size
        is captured explicitly up front instead."""
        if fullscreen:
            size = self.desktop_size
            base_flags = pygame.FULLSCREEN
        else:
            size = (w, h)
            base_flags = pygame.RESIZABLE

        if want_gl:
            try:
                screen = pygame.display.set_mode(size, base_flags | pygame.OPENGL | pygame.DOUBLEBUF)
                version = gl.glGetString(gl.GL_VERSION)
                version_str = version.decode(errors="replace") if version else "unknown"
                logging.info(f"GPU rendering active (OpenGL {version_str})")
                self.use_gl = True
                return screen
            except Exception:
                logging.exception(
                    "OpenGL display init failed - falling back to software rendering"
                )

        self.use_gl = False
        return pygame.display.set_mode(size, base_flags)

    def set_theme(self, idx):
        self.theme_idx = idx % len(THEMES)
        self.primary = THEMES[self.theme_idx]["primary"]
        self.secondary = THEMES[self.theme_idx]["secondary"]

    def cycle_theme(self):
        self.set_theme(self.theme_idx + 1)

    def toggle_fullscreen(self):
        try:
            # want_gl=self.use_gl preserves whichever renderer is currently
            # active across the toggle. Using self.gl_requested here (just
            # "is GL possible on this machine") instead would silently flip
            # a manually-forced-software renderer (via 'g') back to GPU
            # rendering on every fullscreen toggle.
            if not self.fullscreen:
                self.windowed_size = self.screen.get_size()
                self.screen = self._create_display(0, 0, fullscreen=True, want_gl=self.use_gl)
                self.fullscreen = True
            else:
                self.screen = self._create_display(
                    *self.windowed_size, fullscreen=False, want_gl=self.use_gl
                )
                self.fullscreen = False
            logging.info(f"Toggled fullscreen: {self.fullscreen} (GPU rendering: {self.use_gl})")
        except Exception:
            logging.exception("Failed to toggle fullscreen (non-fatal)")

    def toggle_renderer(self):
        """Manually switch between GPU (OpenGL) and software rendering,
        mainly useful for troubleshooting on a machine where GPU rendering
        misbehaves but doesn't outright fail (e.g. remote desktop, some
        virtual GPU drivers)."""
        want_gl = not self.use_gl
        if want_gl and not self.gl_requested:
            logging.info("GPU rendering unavailable on this system (PyOpenGL not installed)")
            return
        w, h = self.screen.get_size()
        try:
            self.screen = self._create_display(w, h, self.fullscreen, want_gl=want_gl)
            logging.info(f"Manually switched renderer - GPU rendering: {self.use_gl}")
        except Exception:
            logging.exception("Failed to switch renderer (non-fatal)")

    def compute_spectrum(self, samples):
        windowed = samples * np.hanning(len(samples))
        spectrum = np.abs(np.fft.rfft(windowed))
        spectrum = spectrum / (len(samples) / 2)
        spectrum = np.clip(spectrum * self.gain, 0, 1)
        if self.smoothed_spectrum is None or len(self.smoothed_spectrum) != len(spectrum):
            self.smoothed_spectrum = spectrum
        else:
            # Fast attack / slow release: rising bins snap toward the new
            # (louder) value quickly, falling bins ease down at the
            # user-controlled smoothing rate. A single symmetric
            # coefficient here makes onsets feel laggy since it damps the
            # attack just as much as the decay.
            rising = spectrum > self.smoothed_spectrum
            a = np.where(rising, self.attack, self.smoothing)
            self.smoothed_spectrum = a * self.smoothed_spectrum + (1 - a) * spectrum
        return self.smoothed_spectrum

    def _update_peak(self, peaks, values):
        """Lazy-init peak-hold array shared by the bars/radial/grid peak
        markers: each element decays by PEAK_FALL_RATE per frame, snapping
        back up wherever the current value exceeds the decayed peak."""
        values = np.asarray(values, dtype=np.float32)
        if peaks is None or len(peaks) != len(values):
            return values.copy()
        return np.maximum(peaks * PEAK_FALL_RATE, values)

    def _ui_scale(self, w, h):
        """Multiplier for line widths and small marker sizes (peak caps,
        dots, HUD accents) in the bars/radial/futuristic/grid modes, so
        they stay visible and proportionate whether the window is tiny or
        4K, instead of being a fixed pixel count that vanishes at high
        resolution or overwhelms a small window. 560 matches the app's
        default window height; clamped so nothing gets sub-pixel or absurd
        at extreme sizes."""
        return max(0.4, min(3.0, min(w, h) / 560.0))

    def detect_beat(self, samples):
        """Simple onset detector: flags a beat when the current frame's RMS
        energy spikes well above its own recent rolling average. Cheap
        (no extra FFT) and good enough to drive visual accents."""
        if samples is None or len(samples) == 0:
            return False

        rms = float(np.sqrt(np.mean(np.square(samples))))
        beat = False

        if len(self.rms_history) >= 10:
            avg = sum(self.rms_history) / len(self.rms_history)
            now = time.time()
            if rms > avg * self.beat_sensitivity and rms > 0.02 and (now - self.last_beat_time) > 0.12:
                beat = True
                self.last_beat_time = now

        self.rms_history.append(rms)
        return beat

    def draw_bars(self, spectrum, w, h, beat_flash):
        bar_count = 96
        # Deliberately only the bottom quarter of the spectrum, not the
        # full Nyquist range: music's energy is overwhelmingly
        # concentrated down here, so spreading bars across the full range
        # instead just fills half the display with near-silent high-
        # frequency bins and makes the whole thing look/feel less
        # responsive. Tried the full range, reverted after it visibly
        # deadened the visualization in testing.
        usable = spectrum[: len(spectrum) // 2]
        step = max(1, len(usable) // bar_count)
        bar_w = w / bar_count * 0.72
        gap = w / bar_count * 0.28
        boost = 1.0 + 0.18 * beat_flash

        idxs = np.minimum(np.arange(bar_count) * step, len(usable) - 1)
        values = np.clip(usable[idxs] * boost, 0.0, 1.0)
        self.bar_peaks = self._update_peak(self.bar_peaks, values)

        cy = h / 2
        radius = max(1, min(4, int(bar_w // 2)))
        cap_w = max(1, round(2 * self._ui_scale(w, h)))
        for i in range(bar_count):
            v = float(values[i])
            bar_h = v * (h * 0.92)
            half_h = bar_h / 2
            x = i * (bar_w + gap)
            t = i / bar_count
            color = tuple(
                int(self.primary[c] * (1 - t) + self.secondary[c] * t) for c in range(3)
            )
            # Center-mirrored instead of bottom-anchored: grows both up and
            # down from the vertical middle, which reads as more "alive"
            # than a flat baseline and gives peak caps room on both ends.
            pygame.draw.rect(
                self.screen, color,
                (float(x), float(cy - half_h), float(bar_w), float(bar_h)),
                border_radius=radius,
            )

            peak_half = float(self.bar_peaks[i]) * (h * 0.92) / 2
            if peak_half > half_h + 1:
                cap_color = tuple(min(255, c + 70) for c in color)
                pygame.draw.line(self.screen, cap_color, (x, cy - peak_half), (x + bar_w, cy - peak_half), cap_w)
                pygame.draw.line(self.screen, cap_color, (x, cy + peak_half), (x + bar_w, cy + peak_half), cap_w)

    def draw_wave(self, samples, w, h, beat_flash):
        n = len(samples)
        if n < 2 or w < 2:
            pygame.draw.line(self.screen, LINE, (0, h / 2), (w, h / 2), 1)
            return

        # resample the buffer to exactly match the pixel width so the
        # waveform always spans the full window, whether it's 640px or
        # 3840px wide, regardless of how many samples came in this frame
        target_points = min(max(w, 2), 2000)
        src_x = np.linspace(0, n - 1, target_points)
        resampled = np.interp(src_x, np.arange(n), samples)

        points = []
        for idx in range(target_points):
            x = (idx / (target_points - 1)) * (w - 1) if target_points > 1 else 0.0
            v = float(resampled[idx]) * self.gain
            y = h / 2 + v * (h / 2 * 0.9)
            points.append((float(x), float(y)))

        line_width = 2 + int(round(2 * beat_flash))
        if len(points) > 1:
            pygame.draw.lines(self.screen, self.primary, False, points, line_width)
        pygame.draw.line(self.screen, LINE, (0, h / 2), (w, h / 2), 1)

    def draw_radial(self, spectrum, w, h, beat_flash):
        cx, cy = w / 2, h / 2
        usable = spectrum[: len(spectrum) // 2]
        bass = float(usable[:16].mean()) if len(usable) >= 16 else float(usable.mean())
        scale = self._ui_scale(w, h)

        base_r = min(w, h) * 0.18 * (1.0 + 0.12 * beat_flash)
        core_r = base_r * (0.45 + 0.35 * bass)

        # soft glow behind a solid core disc, both reactive to bass energy -
        # a "sunburst" center instead of just an empty ring
        glow = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.circle(glow, (*self.primary, 50), (int(cx), int(cy)), int(core_r * 1.8))
        pygame.draw.circle(glow, (*self.primary, 90), (int(cx), int(cy)), int(core_r * 1.25))
        self.screen.blit(glow, (0, 0))
        pygame.draw.circle(self.screen, self.primary, (int(cx), int(cy)), max(2, int(core_r)))

        ring_color = tuple(
            min(255, int(LINE[c] + (255 - LINE[c]) * beat_flash * 0.5)) for c in range(3)
        )
        ring_w = max(1, round(1 * scale))
        pygame.draw.circle(self.screen, ring_color, (int(cx), int(cy)), int(base_r), ring_w)

        # a soft expanding ring that appears briefly on each detected beat
        if beat_flash > 0.05:
            pulse_r = base_r + beat_flash * (min(w, h) * 0.12)
            pulse_alpha = max(0, min(255, int(beat_flash * 160)))
            s = pygame.Surface((w, h), pygame.SRCALPHA)
            pygame.draw.circle(s, (*self.primary, pulse_alpha), (int(cx), int(cy)), int(pulse_r), max(1, round(2 * scale)))
            self.screen.blit(s, (0, 0))

        bar_count = 120
        step = max(1, len(usable) // bar_count)
        idxs = np.minimum(np.arange(bar_count) * step, len(usable) - 1)
        values = np.clip(usable[idxs], 0.0, 1.0)
        self.radial_peaks = self._update_peak(self.radial_peaks, values)
        max_len = min(w, h) * 0.32
        spoke_w = max(1, round(3 * scale))
        peak_dot_r = max(1, round(2 * scale))

        for i in range(bar_count):
            v = float(values[i])
            length = v * max_len
            angle = (i / bar_count) * 2 * np.pi - np.pi / 2
            x1 = float(cx + np.cos(angle) * base_r)
            y1 = float(cy + np.sin(angle) * base_r)
            x2 = float(cx + np.cos(angle) * (base_r + length))
            y2 = float(cy + np.sin(angle) * (base_r + length))
            color = self.primary if i / bar_count < 0.5 else self.secondary
            pygame.draw.line(self.screen, color, (x1, y1), (x2, y2), spoke_w)

            peak_len = float(self.radial_peaks[i]) * max_len
            if peak_len > length + 2:
                px = float(cx + np.cos(angle) * (base_r + peak_len))
                py = float(cy + np.sin(angle) * (base_r + peak_len))
                cap_color = tuple(min(255, c + 70) for c in color)
                pygame.draw.circle(self.screen, cap_color, (int(px), int(py)), peak_dot_r)

        # shorter inner spokes pointing back toward the core, sampling a
        # different (upper-mid) slice of the usable range for a mandala-like
        # second layer that moves somewhat independently of the outer ring
        inner_count = 60
        inner_offset = len(usable) // 2
        inner_span = len(usable) - inner_offset
        if inner_span > 0:
            inner_step = max(1, inner_span // inner_count)
            for i in range(inner_count):
                idx = min(inner_offset + i * inner_step, len(usable) - 1)
                v = float(usable[idx])
                length = v * (base_r - core_r) * 0.9
                angle = (i / inner_count) * 2 * np.pi - np.pi / 2 + (np.pi / inner_count)
                x1 = float(cx + np.cos(angle) * base_r)
                y1 = float(cy + np.sin(angle) * base_r)
                x2 = float(cx + np.cos(angle) * (base_r - length))
                y2 = float(cy + np.sin(angle) * (base_r - length))
                color = self.secondary if i / inner_count < 0.5 else self.primary
                pygame.draw.line(self.screen, color, (x1, y1), (x2, y2), max(1, round(2 * scale)))

    def draw_particles(self, spectrum, w, h, beat_flash, beat):
        overlay = pygame.Surface((w, h))
        overlay.set_alpha(60)
        overlay.fill(BG)
        self.screen.blit(overlay, (0, 0))

        avg = float(np.mean(spectrum[:32])) if len(spectrum) else 0.0

        def spawn_particle(energetic=False):
            speed_boost = 3.0 if energetic else 0.0
            self.particles.append({
                "x": np.random.uniform(0, w),
                "y": h,
                "vy": -(1 + avg * 6 + speed_boost + np.random.uniform(0, 2)),
                "vx": np.random.uniform(-0.6, 0.6),
                "r": (1.5 + avg * 4) * (1.4 if energetic else 1.0),
                "life": 1.0,
                "color": self.primary if np.random.random() > 0.5 else self.secondary,
            })

        if np.random.random() < 0.3 + avg * 0.6:
            spawn_particle()

        # on a detected beat, spawn a small burst all at once for a visible "hit"
        if beat:
            for _ in range(10):
                spawn_particle(energetic=True)

        alive = []
        for p in self.particles:
            p["x"] += p["vx"]
            p["y"] += p["vy"]
            p["life"] -= 0.012
            if p["life"] > 0 and p["y"] > -10:
                alpha = max(0, min(255, int(p["life"] * 255)))
                s = pygame.Surface((int(p["r"] * 2 + 2), int(p["r"] * 2 + 2)), pygame.SRCALPHA)
                pygame.draw.circle(s, (*p["color"], alpha), (int(p["r"] + 1), int(p["r"] + 1)), int(p["r"]))
                self.screen.blit(s, (p["x"] - p["r"], p["y"] - p["r"]))
                alive.append(p)
        self.particles = alive

    def draw_rainbow(self, spectrum, w, h, beat_flash):
        """Full-spectrum bars where hue cycles continuously across both
        position and time, instead of a fixed two-color gradient."""
        bar_count = 96
        usable = spectrum[: len(spectrum) // 2]
        step = max(1, len(usable) // bar_count)
        bar_w = w / bar_count * 0.72
        gap = w / bar_count * 0.28
        boost = 1.0 + 0.18 * beat_flash

        self.rainbow_phase = (self.rainbow_phase + 0.003) % 1.0

        for i in range(bar_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            bar_h = min(v * boost, 1.0) * (h * 0.92)
            x = i * (bar_w + gap)
            hue = (i / bar_count + self.rainbow_phase) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 0.85, 1.0)
            color = (int(r * 255), int(g * 255), int(b * 255))
            y0 = h - bar_h
            pygame.draw.rect(self.screen, color, (float(x), float(y0), float(bar_w), float(bar_h)))

    def draw_futuristic(self, spectrum, w, h, beat_flash):
        """Sci-fi HUD-style readout: a rotating outer ring, a counter-
        rotating dashed ring, a radar sweep, a segmented spectrum dial, a
        pulsing core, and beat-reactive HUD corner brackets."""
        cx, cy = w / 2, h / 2
        self.futuristic_rotation = (self.futuristic_rotation + 0.006) % (2 * np.pi)
        scale = self._ui_scale(w, h)

        usable = spectrum[: len(spectrum) // 2]
        seg_count = 72
        step = max(1, len(usable) // seg_count)
        outer_r = min(w, h) * 0.32
        inner_r = min(w, h) * 0.20

        # slowly rotating outer ring
        ring_segs = 90
        pts = []
        for i in range(ring_segs + 1):
            a = i / ring_segs * 2 * np.pi + self.futuristic_rotation
            pts.append((cx + np.cos(a) * outer_r * 1.05, cy + np.sin(a) * outer_r * 1.05))
        pygame.draw.lines(self.screen, FUTURISTIC_CYAN, True, pts, max(1, round(1 * scale)))

        # a second, faster, counter-rotating ring further out with an
        # actual dash pattern (skips every other segment), for parallax
        dash_r = outer_r * 1.18
        dash_segs = 60
        dash_rotation = -self.futuristic_rotation * 1.8
        dash_w = max(1, round(1 * scale))
        for i in range(0, dash_segs, 2):
            a1 = i / dash_segs * 2 * np.pi + dash_rotation
            a2 = (i + 1) / dash_segs * 2 * np.pi + dash_rotation
            x1 = cx + np.cos(a1) * dash_r
            y1 = cy + np.sin(a1) * dash_r
            x2 = cx + np.cos(a2) * dash_r
            y2 = cy + np.sin(a2) * dash_r
            pygame.draw.line(self.screen, FUTURISTIC_CYAN, (x1, y1), (x2, y2), dash_w)

        # radar-style sweep with a fading trail, rotating independently of
        # the two rings above
        sweep_angle = self.futuristic_rotation * 2.5
        trail_count = 10
        trail_w = max(1, round(2 * scale))
        trail_surf = pygame.Surface((w, h), pygame.SRCALPHA)
        for i in range(trail_count):
            a = sweep_angle - i * 0.05
            alpha = max(0, int(160 * (1 - i / trail_count)))
            x2 = cx + np.cos(a) * outer_r * 1.05
            y2 = cy + np.sin(a) * outer_r * 1.05
            pygame.draw.line(trail_surf, (*FUTURISTIC_CYAN, alpha), (cx, cy), (x2, y2), trail_w)
        self.screen.blit(trail_surf, (0, 0))

        # segmented spectrum dial - brightness/width now scale with the bin
        # value instead of a flat color, so louder segments visibly pop
        for i in range(seg_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            length = min(v, 1.0) * (outer_r - inner_r)
            angle = (i / seg_count) * 2 * np.pi - np.pi / 2 + self.futuristic_rotation * 0.3
            x1 = cx + np.cos(angle) * inner_r
            y1 = cy + np.sin(angle) * inner_r
            x2 = cx + np.cos(angle) * (inner_r + length)
            y2 = cy + np.sin(angle) * (inner_r + length)
            color = tuple(
                int(FUTURISTIC_CYAN[c] * (1 - v) + FUTURISTIC_WHITE[c] * v) for c in range(3)
            )
            pygame.draw.line(self.screen, color, (x1, y1), (x2, y2), max(1, round((2 + v) * scale)))

        # pulsing core
        core_r = max(2, inner_r * 0.5 * (1.0 + 0.3 * beat_flash))
        pygame.draw.circle(self.screen, FUTURISTIC_WHITE, (int(cx), int(cy)), int(core_r), max(1, round(2 * scale)))

        # HUD corner brackets - extend briefly on a detected beat. Sized as
        # a fraction of min(w, h) (not fixed pixels) so they stay correctly
        # anchored to the corners at any window size/resolution instead of
        # becoming a speck at 4K or overlapping the dial in a tiny window.
        ref = min(w, h)
        bracket = ref * (24 + 14 * beat_flash) / 560.0
        margin = ref * 16 / 560.0
        bracket_w = max(1, round(2 * scale))
        for bx, by, dx, dy in [
            (margin, margin, 1, 1), (w - margin, margin, -1, 1),
            (margin, h - margin, 1, -1), (w - margin, h - margin, -1, -1),
        ]:
            pygame.draw.line(self.screen, FUTURISTIC_CYAN, (bx, by), (bx + dx * bracket, by), bracket_w)
            pygame.draw.line(self.screen, FUTURISTIC_CYAN, (bx, by), (bx, by + dy * bracket), bracket_w)

    def draw_grid(self, spectrum, w, h, beat_flash):
        """Neon perspective grid floor with a glowing, beat-reactive
        synthwave sun, continuously scrolling floor lines, and spectrum-
        reactive light bars with peak-hold caps - an original take on the
        glowing circuit-grid aesthetic, not any specific copyrighted
        artwork."""
        horizon_y = h * 0.42
        vanish_x = w / 2
        scale = self._ui_scale(w, h)

        usable = spectrum[: len(spectrum) // 2]
        bass = float(usable[:16].mean()) if len(usable) >= 16 else float(usable.mean())
        self.grid_scroll = (self.grid_scroll + 0.006 + 0.03 * bass) % 1.0

        glow_color = NEON_ORANGE if beat_flash > 0.4 else NEON_CYAN

        # glowing sun sitting on the horizon, pulsing with bass/beat -
        # drawn first, then masked below the horizon (solid BG rect) so it
        # can't bleed through the gaps in the floor grid drawn afterward
        sun_r = min(w, h) * 0.09 * (1.0 + 0.15 * bass + 0.25 * beat_flash)
        glow = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.circle(glow, (*glow_color, 60), (int(vanish_x), int(horizon_y)), int(sun_r * 1.8))
        pygame.draw.circle(glow, (*glow_color, 110), (int(vanish_x), int(horizon_y)), int(sun_r * 1.3))
        self.screen.blit(glow, (0, 0))
        pygame.draw.circle(self.screen, glow_color, (int(vanish_x), int(horizon_y)), int(sun_r))
        pygame.draw.rect(self.screen, BG, (0, horizon_y, w, h - horizon_y))

        pygame.draw.line(self.screen, glow_color, (0, horizon_y), (w, horizon_y), max(1, round(2 * scale)))

        # converging vertical lines toward the vanishing point
        num_v = 16
        thin_w = max(1, round(1 * scale))
        for i in range(num_v + 1):
            bx = i / num_v * w
            pygame.draw.line(self.screen, NEON_CYAN, (bx, h), (vanish_x, horizon_y), thin_w)

        # horizontal lines with perspective spacing, continuously scrolling
        # toward the viewer at a rate driven by bass energy instead of
        # sitting static
        num_h = 10
        for j in range(1, num_h + 1):
            t = (j / num_h + self.grid_scroll) % 1.0
            if t <= 0.001:
                continue
            y = horizon_y + (h - horizon_y) * (t ** 2)
            fade = 0.3 + 0.7 * t
            color = tuple(int(c * fade) for c in NEON_CYAN)
            pygame.draw.line(self.screen, color, (0, y), (w, y), thin_w)

        # spectrum-reactive light bars rising from the grid, with peak-hold caps
        bar_count = 40
        step = max(1, len(usable) // bar_count)
        idxs = np.minimum(np.arange(bar_count) * step, len(usable) - 1)
        values = np.clip(usable[idxs], 0.0, 1.0)
        self.grid_peaks = self._update_peak(self.grid_peaks, values)
        bar_w = max(1, round(2 * scale))
        cap_half_w = max(1, round(3 * scale))
        for i in range(bar_count):
            v = float(values[i])
            bx = (i + 0.5) / bar_count * w
            bar_h = v * (horizon_y * 0.9)
            color = NEON_ORANGE if v > 0.6 else NEON_CYAN
            pygame.draw.line(self.screen, color, (bx, horizon_y), (bx, horizon_y - bar_h), bar_w)

            peak_h = float(self.grid_peaks[i]) * (horizon_y * 0.9)
            if peak_h > bar_h + 2:
                cap_color = tuple(min(255, c + 60) for c in color)
                pygame.draw.line(
                    self.screen, cap_color,
                    (bx - cap_half_w, horizon_y - peak_h), (bx + cap_half_w, horizon_y - peak_h), bar_w,
                )

    # ------------------------------------------------------------------
    # GPU (OpenGL) rendering path. Mirrors each software draw_* method but
    # issues real GPU draw calls instead of CPU-rasterized pygame.draw
    # calls. Text/UI (HUD + control bar) still uses pygame's software font
    # rendering onto small offscreen surfaces, which are then uploaded as
    # GL textures and composited on top - this avoids having to reimplement
    # font rendering in raw OpenGL.
    # ------------------------------------------------------------------

    def gl_clear(self, w, h):
        gl.glViewport(0, 0, w, h)
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()
        gl.glOrtho(0, w, h, 0, -1, 1)  # top-left origin, matching pygame's coordinate system
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glLoadIdentity()
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glClearColor(BG[0] / 255, BG[1] / 255, BG[2] / 255, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)

    def gl_draw_bars(self, spectrum, w, h, beat_flash):
        bar_count = 96
        usable = spectrum[: len(spectrum) // 2]
        step = max(1, len(usable) // bar_count)
        bar_w = w / bar_count * 0.72
        gap = w / bar_count * 0.28
        boost = 1.0 + 0.18 * beat_flash

        idxs = np.minimum(np.arange(bar_count) * step, len(usable) - 1)
        values = np.clip(usable[idxs] * boost, 0.0, 1.0)
        self.bar_peaks = self._update_peak(self.bar_peaks, values)
        cy = h / 2

        gl.glBegin(gl.GL_QUADS)
        for i in range(bar_count):
            v = float(values[i])
            bar_h = v * (h * 0.92)
            half_h = bar_h / 2
            x = i * (bar_w + gap)
            t = i / bar_count
            color = tuple(
                (self.primary[c] * (1 - t) + self.secondary[c] * t) / 255 for c in range(3)
            )
            y0, y1 = cy - half_h, cy + half_h
            gl.glColor3f(*color)
            gl.glVertex2f(x, y1)
            gl.glVertex2f(x + bar_w, y1)
            gl.glVertex2f(x + bar_w, y0)
            gl.glVertex2f(x, y0)
        gl.glEnd()

        gl.glLineWidth(max(1.0, 2.0 * self._ui_scale(w, h)))
        gl.glBegin(gl.GL_LINES)
        for i in range(bar_count):
            v = float(values[i])
            half_h = v * (h * 0.92) / 2
            peak_half = float(self.bar_peaks[i]) * (h * 0.92) / 2
            if peak_half <= half_h + 1:
                continue
            x = i * (bar_w + gap)
            t = i / bar_count
            color = tuple(
                min(1.0, (self.primary[c] * (1 - t) + self.secondary[c] * t) / 255 + 0.27)
                for c in range(3)
            )
            gl.glColor3f(*color)
            gl.glVertex2f(x, cy - peak_half); gl.glVertex2f(x + bar_w, cy - peak_half)
            gl.glVertex2f(x, cy + peak_half); gl.glVertex2f(x + bar_w, cy + peak_half)
        gl.glEnd()
        gl.glLineWidth(1.0)

    def _gl_line(self, x1, y1, x2, y2, color, width=1.0):
        gl.glLineWidth(float(width))
        gl.glColor3ub(*color)
        gl.glBegin(gl.GL_LINES)
        gl.glVertex2f(x1, y1)
        gl.glVertex2f(x2, y2)
        gl.glEnd()

    def gl_draw_wave(self, samples, w, h, beat_flash):
        n = len(samples)
        if n < 2 or w < 2:
            self._gl_line(0, h / 2, w, h / 2, LINE, 1)
            return

        target_points = min(max(w, 2), 2000)
        src_x = np.linspace(0, n - 1, target_points)
        resampled = np.interp(src_x, np.arange(n), samples)

        line_width = 2 + int(round(2 * beat_flash))
        gl.glLineWidth(float(line_width))
        gl.glColor3ub(*self.primary)
        gl.glBegin(gl.GL_LINE_STRIP)
        for idx in range(target_points):
            x = (idx / (target_points - 1)) * (w - 1) if target_points > 1 else 0.0
            v = float(resampled[idx]) * self.gain
            y = h / 2 + v * (h / 2 * 0.9)
            gl.glVertex2f(x, y)
        gl.glEnd()

        self._gl_line(0, h / 2, w, h / 2, LINE, 1)

    def _gl_filled_circle(self, cx, cy, r, color_rgba, segs=32):
        gl.glColor4f(*color_rgba)
        gl.glBegin(gl.GL_TRIANGLE_FAN)
        gl.glVertex2f(cx, cy)
        for i in range(segs + 1):
            a = i / segs * 2 * np.pi
            gl.glVertex2f(cx + np.cos(a) * r, cy + np.sin(a) * r)
        gl.glEnd()

    def gl_draw_radial(self, spectrum, w, h, beat_flash):
        cx, cy = w / 2, h / 2
        usable = spectrum[: len(spectrum) // 2]
        bass = float(usable[:16].mean()) if len(usable) >= 16 else float(usable.mean())
        scale = self._ui_scale(w, h)

        base_r = min(w, h) * 0.18 * (1.0 + 0.12 * beat_flash)
        core_r = base_r * (0.45 + 0.35 * bass)
        bar_count = 120
        step = max(1, len(usable) // bar_count)
        segs = 64

        primary_f = tuple(c / 255 for c in self.primary)
        self._gl_filled_circle(cx, cy, core_r * 1.8, (*primary_f, 0.20))
        self._gl_filled_circle(cx, cy, core_r * 1.25, (*primary_f, 0.35))
        self._gl_filled_circle(cx, cy, max(2, core_r), (*primary_f, 1.0))

        ring_color = tuple(
            min(255, int(LINE[c] + (255 - LINE[c]) * beat_flash * 0.5)) / 255 for c in range(3)
        )
        gl.glLineWidth(max(1.0, 1.0 * scale))
        gl.glColor3f(*ring_color)
        gl.glBegin(gl.GL_LINE_LOOP)
        for i in range(segs):
            a = i / segs * 2 * np.pi
            gl.glVertex2f(cx + np.cos(a) * base_r, cy + np.sin(a) * base_r)
        gl.glEnd()

        if beat_flash > 0.05:
            pulse_r = base_r + beat_flash * (min(w, h) * 0.12)
            alpha = max(0.0, min(1.0, beat_flash * 160 / 255))
            gl.glColor4f(self.primary[0] / 255, self.primary[1] / 255, self.primary[2] / 255, alpha)
            gl.glLineWidth(max(1.0, 2.0 * scale))
            gl.glBegin(gl.GL_LINE_LOOP)
            for i in range(segs):
                a = i / segs * 2 * np.pi
                gl.glVertex2f(cx + np.cos(a) * pulse_r, cy + np.sin(a) * pulse_r)
            gl.glEnd()

        idxs = np.minimum(np.arange(bar_count) * step, len(usable) - 1)
        values = np.clip(usable[idxs], 0.0, 1.0)
        self.radial_peaks = self._update_peak(self.radial_peaks, values)
        max_len = min(w, h) * 0.32

        gl.glLineWidth(max(1.0, 3.0 * scale))
        gl.glBegin(gl.GL_LINES)
        for i in range(bar_count):
            v = float(values[i])
            length = v * max_len
            angle = (i / bar_count) * 2 * np.pi - np.pi / 2
            x1 = cx + np.cos(angle) * base_r
            y1 = cy + np.sin(angle) * base_r
            x2 = cx + np.cos(angle) * (base_r + length)
            y2 = cy + np.sin(angle) * (base_r + length)
            color = self.primary if i / bar_count < 0.5 else self.secondary
            gl.glColor3ub(*color)
            gl.glVertex2f(x1, y1)
            gl.glVertex2f(x2, y2)
        gl.glEnd()

        peak_dot_r = max(1, 2 * scale)
        for i in range(bar_count):
            peak_len = float(self.radial_peaks[i]) * max_len
            v = float(values[i])
            if peak_len <= v * max_len + 2:
                continue
            angle = (i / bar_count) * 2 * np.pi - np.pi / 2
            px = cx + np.cos(angle) * (base_r + peak_len)
            py = cy + np.sin(angle) * (base_r + peak_len)
            color = self.primary if i / bar_count < 0.5 else self.secondary
            cap = tuple(min(1.0, c / 255 + 0.27) for c in color)
            self._gl_filled_circle(px, py, peak_dot_r, (*cap, 1.0), segs=8)

        # shorter inner spokes pointing back toward the core, sampling a
        # different (upper-mid) slice of the usable range - mirrors the
        # software path's mandala-style second layer
        inner_count = 60
        inner_offset = len(usable) // 2
        inner_span = len(usable) - inner_offset
        if inner_span > 0:
            inner_step = max(1, inner_span // inner_count)
            gl.glLineWidth(max(1.0, 2.0 * scale))
            gl.glBegin(gl.GL_LINES)
            for i in range(inner_count):
                idx = min(inner_offset + i * inner_step, len(usable) - 1)
                v = float(usable[idx])
                length = v * (base_r - core_r) * 0.9
                angle = (i / inner_count) * 2 * np.pi - np.pi / 2 + (np.pi / inner_count)
                x1 = cx + np.cos(angle) * base_r
                y1 = cy + np.sin(angle) * base_r
                x2 = cx + np.cos(angle) * (base_r - length)
                y2 = cy + np.sin(angle) * (base_r - length)
                color = self.secondary if i / inner_count < 0.5 else self.primary
                gl.glColor3ub(*color)
                gl.glVertex2f(x1, y1)
                gl.glVertex2f(x2, y2)
            gl.glEnd()
            gl.glLineWidth(1.0)

    def gl_draw_particles(self, spectrum, w, h, beat_flash, beat):
        avg = float(np.mean(spectrum[:32])) if len(spectrum) else 0.0

        def spawn_particle(energetic=False):
            speed_boost = 3.0 if energetic else 0.0
            self.particles.append({
                "x": np.random.uniform(0, w),
                "y": h,
                "vy": -(1 + avg * 6 + speed_boost + np.random.uniform(0, 2)),
                "vx": np.random.uniform(-0.6, 0.6),
                "r": (1.5 + avg * 4) * (1.4 if energetic else 1.0),
                "life": 1.0,
                "color": self.primary if np.random.random() > 0.5 else self.secondary,
            })

        if np.random.random() < 0.3 + avg * 0.6:
            spawn_particle()
        if beat:
            for _ in range(10):
                spawn_particle(energetic=True)

        segs = 12
        alive = []
        for p in self.particles:
            p["x"] += p["vx"]
            p["y"] += p["vy"]
            p["life"] -= 0.012
            if p["life"] > 0 and p["y"] > -10:
                alpha = max(0.0, min(1.0, p["life"]))
                gl.glColor4f(p["color"][0] / 255, p["color"][1] / 255, p["color"][2] / 255, alpha)
                gl.glBegin(gl.GL_TRIANGLE_FAN)
                gl.glVertex2f(p["x"], p["y"])
                for i in range(segs + 1):
                    a = i / segs * 2 * np.pi
                    gl.glVertex2f(p["x"] + np.cos(a) * p["r"], p["y"] + np.sin(a) * p["r"])
                gl.glEnd()
                alive.append(p)
        self.particles = alive

    def gl_draw_rainbow(self, spectrum, w, h, beat_flash):
        bar_count = 96
        usable = spectrum[: len(spectrum) // 2]
        step = max(1, len(usable) // bar_count)
        bar_w = w / bar_count * 0.72
        gap = w / bar_count * 0.28
        boost = 1.0 + 0.18 * beat_flash

        self.rainbow_phase = (self.rainbow_phase + 0.003) % 1.0

        gl.glBegin(gl.GL_QUADS)
        for i in range(bar_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            bar_h = min(v * boost, 1.0) * (h * 0.92)
            x = i * (bar_w + gap)
            hue = (i / bar_count + self.rainbow_phase) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 0.85, 1.0)
            y0 = h - bar_h
            gl.glColor3f(r, g, b)
            gl.glVertex2f(x, h)
            gl.glVertex2f(x + bar_w, h)
            gl.glVertex2f(x + bar_w, y0)
            gl.glVertex2f(x, y0)
        gl.glEnd()

    def gl_draw_futuristic(self, spectrum, w, h, beat_flash):
        cx, cy = w / 2, h / 2
        self.futuristic_rotation = (self.futuristic_rotation + 0.006) % (2 * np.pi)
        scale = self._ui_scale(w, h)

        usable = spectrum[: len(spectrum) // 2]
        seg_count = 72
        step = max(1, len(usable) // seg_count)
        outer_r = min(w, h) * 0.32
        inner_r = min(w, h) * 0.20
        cyan = tuple(c / 255 for c in FUTURISTIC_CYAN)
        white = tuple(c / 255 for c in FUTURISTIC_WHITE)

        gl.glColor3f(*cyan)
        gl.glLineWidth(max(1.0, 1.0 * scale))
        ring_segs = 90
        gl.glBegin(gl.GL_LINE_LOOP)
        for i in range(ring_segs):
            a = i / ring_segs * 2 * np.pi + self.futuristic_rotation
            gl.glVertex2f(cx + np.cos(a) * outer_r * 1.05, cy + np.sin(a) * outer_r * 1.05)
        gl.glEnd()

        # second, faster, counter-rotating dashed ring further out
        dash_r = outer_r * 1.18
        dash_segs = 60
        dash_rotation = -self.futuristic_rotation * 1.8
        gl.glLineWidth(max(1.0, 1.0 * scale))
        gl.glBegin(gl.GL_LINES)
        for i in range(0, dash_segs, 2):
            a1 = i / dash_segs * 2 * np.pi + dash_rotation
            a2 = (i + 1) / dash_segs * 2 * np.pi + dash_rotation
            gl.glVertex2f(cx + np.cos(a1) * dash_r, cy + np.sin(a1) * dash_r)
            gl.glVertex2f(cx + np.cos(a2) * dash_r, cy + np.sin(a2) * dash_r)
        gl.glEnd()

        # radar-style sweep with a fading trail
        sweep_angle = self.futuristic_rotation * 2.5
        trail_count = 10
        gl.glLineWidth(max(1.0, 2.0 * scale))
        gl.glBegin(gl.GL_LINES)
        for i in range(trail_count):
            a = sweep_angle - i * 0.05
            alpha = max(0.0, (1 - i / trail_count)) * (160 / 255)
            gl.glColor4f(cyan[0], cyan[1], cyan[2], alpha)
            gl.glVertex2f(cx, cy)
            gl.glVertex2f(cx + np.cos(a) * outer_r * 1.05, cy + np.sin(a) * outer_r * 1.05)
        gl.glEnd()

        # segmented dial - each segment gets its own Begin/End so its line
        # width can scale with the bin value (glLineWidth can't be changed
        # mid-batch), mirroring the software path's per-segment thickness
        for i in range(seg_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            length = min(v, 1.0) * (outer_r - inner_r)
            angle = (i / seg_count) * 2 * np.pi - np.pi / 2 + self.futuristic_rotation * 0.3
            x1 = cx + np.cos(angle) * inner_r
            y1 = cy + np.sin(angle) * inner_r
            x2 = cx + np.cos(angle) * (inner_r + length)
            y2 = cy + np.sin(angle) * (inner_r + length)
            gl.glLineWidth(max(1.0, (2 + v) * scale))
            gl.glColor3f(*(cyan[c] * (1 - v) + white[c] * v for c in range(3)))
            gl.glBegin(gl.GL_LINES)
            gl.glVertex2f(x1, y1)
            gl.glVertex2f(x2, y2)
            gl.glEnd()

        core_r = max(2, inner_r * 0.5 * (1.0 + 0.3 * beat_flash))
        gl.glColor3f(*white)
        gl.glLineWidth(max(1.0, 2.0 * scale))
        gl.glBegin(gl.GL_LINE_LOOP)
        for i in range(32):
            a = i / 32 * 2 * np.pi
            gl.glVertex2f(cx + np.cos(a) * core_r, cy + np.sin(a) * core_r)
        gl.glEnd()

        # HUD corner brackets sized as a fraction of min(w, h), not fixed
        # pixels - see the software path's comment for why
        ref = min(w, h)
        bracket = ref * (24 + 14 * beat_flash) / 560.0
        margin = ref * 16 / 560.0
        gl.glColor3f(*cyan)
        gl.glLineWidth(max(1.0, 2.0 * scale))
        gl.glBegin(gl.GL_LINES)
        for bx, by, dx, dy in [
            (margin, margin, 1, 1), (w - margin, margin, -1, 1),
            (margin, h - margin, 1, -1), (w - margin, h - margin, -1, -1),
        ]:
            gl.glVertex2f(bx, by); gl.glVertex2f(bx + dx * bracket, by)
            gl.glVertex2f(bx, by); gl.glVertex2f(bx, by + dy * bracket)
        gl.glEnd()

    def gl_draw_grid(self, spectrum, w, h, beat_flash):
        horizon_y = h * 0.42
        vanish_x = w / 2
        cyan = tuple(c / 255 for c in NEON_CYAN)
        orange = tuple(c / 255 for c in NEON_ORANGE)
        scale = self._ui_scale(w, h)

        usable = spectrum[: len(spectrum) // 2]
        bass = float(usable[:16].mean()) if len(usable) >= 16 else float(usable.mean())
        self.grid_scroll = (self.grid_scroll + 0.006 + 0.03 * bass) % 1.0
        glow_color = orange if beat_flash > 0.4 else cyan

        # glowing sun on the horizon, pulsing with bass/beat, masked below
        # the horizon so it can't show through the floor grid drawn after
        sun_r = min(w, h) * 0.09 * (1.0 + 0.15 * bass + 0.25 * beat_flash)
        self._gl_filled_circle(vanish_x, horizon_y, sun_r * 1.8, (*glow_color, 0.24))
        self._gl_filled_circle(vanish_x, horizon_y, sun_r * 1.3, (*glow_color, 0.43))
        self._gl_filled_circle(vanish_x, horizon_y, sun_r, (*glow_color, 1.0))
        bg_f = tuple(c / 255 for c in BG)
        gl.glColor3f(*bg_f)
        gl.glBegin(gl.GL_QUADS)
        gl.glVertex2f(0, horizon_y)
        gl.glVertex2f(w, horizon_y)
        gl.glVertex2f(w, h)
        gl.glVertex2f(0, h)
        gl.glEnd()

        gl.glColor3f(*glow_color)
        gl.glLineWidth(max(1.0, 2.0 * scale))
        gl.glBegin(gl.GL_LINES)
        gl.glVertex2f(0, horizon_y); gl.glVertex2f(w, horizon_y)
        gl.glEnd()

        gl.glLineWidth(max(1.0, 1.0 * scale))
        gl.glColor3f(*cyan)
        gl.glBegin(gl.GL_LINES)
        num_v = 16
        for i in range(num_v + 1):
            bx = i / num_v * w
            gl.glVertex2f(bx, h); gl.glVertex2f(vanish_x, horizon_y)
        gl.glEnd()

        gl.glBegin(gl.GL_LINES)
        num_h = 10
        for j in range(1, num_h + 1):
            t = (j / num_h + self.grid_scroll) % 1.0
            if t <= 0.001:
                continue
            y = horizon_y + (h - horizon_y) * (t ** 2)
            fade = 0.3 + 0.7 * t
            gl.glColor3f(cyan[0] * fade, cyan[1] * fade, cyan[2] * fade)
            gl.glVertex2f(0, y); gl.glVertex2f(w, y)
        gl.glEnd()

        bar_count = 40
        step = max(1, len(usable) // bar_count)
        idxs = np.minimum(np.arange(bar_count) * step, len(usable) - 1)
        values = np.clip(usable[idxs], 0.0, 1.0)
        self.grid_peaks = self._update_peak(self.grid_peaks, values)
        cap_half_w = max(1, round(3 * scale))

        gl.glLineWidth(max(1.0, 2.0 * scale))
        gl.glBegin(gl.GL_LINES)
        for i in range(bar_count):
            v = float(values[i])
            bx = (i + 0.5) / bar_count * w
            bar_h = v * (horizon_y * 0.9)
            gl.glColor3f(*(orange if v > 0.6 else cyan))
            gl.glVertex2f(bx, horizon_y); gl.glVertex2f(bx, horizon_y - bar_h)
        gl.glEnd()

        gl.glBegin(gl.GL_LINES)
        for i in range(bar_count):
            v = float(values[i])
            bar_h = v * (horizon_y * 0.9)
            peak_h = float(self.grid_peaks[i]) * (horizon_y * 0.9)
            if peak_h <= bar_h + 2:
                continue
            bx = (i + 0.5) / bar_count * w
            base_color = orange if v > 0.6 else cyan
            cap_color = tuple(min(1.0, c + 0.24) for c in base_color)
            gl.glColor3f(*cap_color)
            gl.glVertex2f(bx - cap_half_w, horizon_y - peak_h); gl.glVertex2f(bx + cap_half_w, horizon_y - peak_h)
        gl.glEnd()
        gl.glLineWidth(1.0)

    def _gl_draw_texture_from_surface(self, surface, w, surf_h, dest_y):
        """Upload a pygame Surface as a GL texture and draw it as a single
        alpha-blended quad at the given y offset. Used to composite the
        HUD strip and control bar (still rendered via pygame's font/blit
        system) on top of the GPU-drawn visualization."""
        tex_data = pygame.image.tostring(surface, "RGBA", False)
        tex_id = gl.glGenTextures(1)
        try:
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexImage2D(
                gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, w, surf_h, 0,
                gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, tex_data,
            )
            gl.glEnable(gl.GL_TEXTURE_2D)
            gl.glColor4f(1, 1, 1, 1)
            y0, y1 = dest_y, dest_y + surf_h
            gl.glBegin(gl.GL_QUADS)
            gl.glTexCoord2f(0, 0); gl.glVertex2f(0, y0)
            gl.glTexCoord2f(1, 0); gl.glVertex2f(w, y0)
            gl.glTexCoord2f(1, 1); gl.glVertex2f(w, y1)
            gl.glTexCoord2f(0, 1); gl.glVertex2f(0, y1)
            gl.glEnd()
            gl.glDisable(gl.GL_TEXTURE_2D)
        finally:
            gl.glDeleteTextures([int(tex_id)])

    def _render_ui_overlay_gl(self, w, h, fps):
        """Render the HUD strip and control bar to small offscreen surfaces
        (reusing the existing pygame-based draw_hud/draw_control_bar
        methods unchanged) and composite them on top of the GL scene."""
        real_screen = self.screen
        try:
            top_h = 24
            top_surface = pygame.Surface((w, top_h), pygame.SRCALPHA)
            self.screen = top_surface
            self.draw_hud(w, top_h, fps)
            self._gl_draw_texture_from_surface(top_surface, w, top_h, dest_y=0)

            bottom_surface = pygame.Surface((w, self.control_bar_h), pygame.SRCALPHA)
            self.screen = bottom_surface
            self.draw_control_bar(w, self.control_bar_h)
            # draw_control_bar builds self.control_rects in the small
            # offscreen surface's LOCAL coordinates - translate them back
            # to real window coordinates so mouse clicks hit-test correctly
            offset_y = h - self.control_bar_h
            self.control_rects = [
                (rect.move(0, offset_y), kind, key) for (rect, kind, key) in self.control_rects
            ]
            self._gl_draw_texture_from_surface(bottom_surface, w, self.control_bar_h, dest_y=offset_y)
        finally:
            self.screen = real_screen

    def _render_software_frame(self, w, h, chart_h, beat):
        self.screen.fill(BG)
        if self.mode == "wave":
            self.draw_wave(self.last_samples, w, chart_h, self.beat_flash)
        else:
            spectrum = self.compute_spectrum(self.last_samples)
            if self.mode == "bars":
                self.draw_bars(spectrum, w, chart_h, self.beat_flash)
            elif self.mode == "radial":
                self.draw_radial(spectrum, w, chart_h, self.beat_flash)
            elif self.mode == "particles":
                self.draw_particles(spectrum, w, chart_h, self.beat_flash, beat)
            elif self.mode == "rainbow":
                self.draw_rainbow(spectrum, w, chart_h, self.beat_flash)
            elif self.mode == "futuristic":
                self.draw_futuristic(spectrum, w, chart_h, self.beat_flash)
            elif self.mode == "grid":
                self.draw_grid(spectrum, w, chart_h, self.beat_flash)
        fps = self.clock.get_fps()
        self.draw_hud(w, chart_h, fps)
        self.draw_control_bar(w, h)

    def _render_gl_frame(self, w, h, chart_h, beat):
        self.gl_clear(w, h)
        if self.mode == "wave":
            self.gl_draw_wave(self.last_samples, w, chart_h, self.beat_flash)
        else:
            spectrum = self.compute_spectrum(self.last_samples)
            if self.mode == "bars":
                self.gl_draw_bars(spectrum, w, chart_h, self.beat_flash)
            elif self.mode == "radial":
                self.gl_draw_radial(spectrum, w, chart_h, self.beat_flash)
            elif self.mode == "particles":
                self.gl_draw_particles(spectrum, w, chart_h, self.beat_flash, beat)
            elif self.mode == "rainbow":
                self.gl_draw_rainbow(spectrum, w, chart_h, self.beat_flash)
            elif self.mode == "futuristic":
                self.gl_draw_futuristic(spectrum, w, chart_h, self.beat_flash)
            elif self.mode == "grid":
                self.gl_draw_grid(spectrum, w, chart_h, self.beat_flash)
        fps = self.clock.get_fps()
        self._render_ui_overlay_gl(w, h, fps)

    def draw_hud(self, w, h, fps):
        margin = 10
        gap = 14
        renderer = "GPU" if self.use_gl else "CPU"

        # try the full source+renderer label first; if it alone doesn't
        # even fit a narrow window, drop the renderer suffix
        left_text = f"source: {self.source_label}   renderer: {renderer}"
        left_s = self.font_small.render(left_text, True, TEXT_DIM)
        if left_s.get_width() > w - margin * 2:
            left_text = f"source: {self.source_label}"
            left_s = self.font_small.render(left_text, True, TEXT_DIM)

        right_text = f"fps: {int(fps)}"
        right_s = self.font_small.render(right_text, True, TEXT_DIM)

        # only draw the fps counter if it fits without overlapping the left label
        show_right = (margin + left_s.get_width() + gap + right_s.get_width() + margin) <= w

        self.screen.blit(left_s, (margin, 6))
        if show_right:
            self.screen.blit(right_s, (w - right_s.get_width() - margin, 6))

        # the keyboard-shortcut hint is the least essential element (the
        # control bar already exposes all of this via clickable buttons),
        # so it's the first thing dropped when space is tight
        hint = "keyboard: 1-7 modes   t theme   f fullscreen   g renderer   up/down gain   left/right smoothing   esc quit"
        hint_s = self.font_small.render(hint, True, TEXT_DIM)
        hint_x = (w - hint_s.get_width()) // 2
        left_edge = margin + left_s.get_width() + gap
        right_edge = (w - right_s.get_width() - margin - gap) if show_right else (w - margin)
        if hint_x >= left_edge and (hint_x + hint_s.get_width()) <= right_edge:
            self.screen.blit(hint_s, (hint_x, 6))

    def _layout_control_bar(self, w):
        """Two-pass control bar layout: build the ordered list of buttons/
        labels, measure each one's width via font_small.size() (no drawing),
        then pack them left-to-right, wrapping to a new row whenever the
        next item would overflow the available width. Called once per
        frame from run(), before the chart height is known - draw_control_
        bar() and _render_ui_overlay_gl() then just draw/size against the
        result cached on self._control_bar_rows / self.control_bar_h rather
        than recomputing it.

        Returns (rows, total_height). Each item in a row is a dict with
        resolved x/y (relative to the bar's own top-left) plus enough info
        for draw_control_bar to render it and, for buttons, register a
        click hit-box."""
        margin = 10
        btn_h = self.CONTROL_BAR_H - 16
        row_gap = 4

        items = []

        def add_button(label, kind, key, active=False, border_color=None,
                        gap_before=6, group_start=False):
            items.append({
                "is_button": True, "label": label, "kind": kind, "key": key,
                "active": active, "border_color": border_color,
                "gap_before": gap_before, "group_start": group_start,
            })

        def add_label(text, gap_before=14, group_start=True):
            items.append({
                "is_button": False, "label": text,
                "gap_before": gap_before, "group_start": group_start,
            })

        for key, label in [("bars", "Bars"), ("wave", "Wave"),
                            ("radial", "Radial"), ("particles", "Particles"),
                            ("rainbow", "Rainbow"), ("futuristic", "Futuristic"),
                            ("grid", "Neon Grid")]:
            add_button(label, "mode", key, active=(self.mode == key))

        add_label(f"gain {self.gain:.1f}")
        add_button("-", "gain", "gain-", gap_before=8)
        add_button("+", "gain", "gain+")

        add_label(f"smooth {self.smoothing:.2f}")
        add_button("-", "smooth", "smooth-", gap_before=8)
        add_button("+", "smooth", "smooth+")

        add_button(f"theme: {THEMES[self.theme_idx]['name']}", "theme", "cycle",
                   border_color=self.primary, gap_before=14, group_start=True)

        add_button("windowed" if self.fullscreen else "fullscreen", "fullscreen", "toggle",
                   gap_before=14, group_start=True)

        for item in items:
            text_w, text_h = self.font_small.size(item["label"])
            item["text_h"] = text_h
            item["w"] = text_w + (18 if item["is_button"] else 0)

        rows = [[]]
        cursor_x = margin
        for item in items:
            first_in_row = len(rows[-1]) == 0
            gap = 0 if first_in_row else item["gap_before"]
            if not first_in_row and cursor_x + gap + item["w"] > w - margin:
                rows.append([])
                first_in_row = True
                gap = 0
            item["x"] = (margin if first_in_row else cursor_x) + gap
            item["draw_separator"] = item["group_start"] and not first_in_row
            cursor_x = item["x"] + item["w"]
            rows[-1].append(item)

        for r, row in enumerate(rows):
            row_y = 8 + r * (btn_h + row_gap)
            for item in row:
                item["y"] = row_y

        total_h = 16 + len(rows) * btn_h + (len(rows) - 1) * row_gap
        return rows, total_h

    def draw_control_bar(self, w, h):
        """Draws the clickable bottom control bar from the layout computed
        this frame by _layout_control_bar() (see run()), and rebuilds the
        hit-test rects used by handle_control_click()."""
        bar_y = h - self.control_bar_h
        pygame.draw.rect(self.screen, PANEL, (0, bar_y, w, self.control_bar_h))
        pygame.draw.line(self.screen, LINE, (0, bar_y), (w, bar_y), 1)

        btn_h = self.CONTROL_BAR_H - 16
        self.control_rects = []
        for row in self._control_bar_rows:
            for item in row:
                x, y = item["x"], bar_y + item["y"]
                if item["draw_separator"]:
                    sep_x = x - item["gap_before"] / 2
                    pygame.draw.line(self.screen, LINE, (sep_x, y), (sep_x, y + btn_h), 1)
                if item["is_button"]:
                    rect = pygame.Rect(x, y, item["w"], btn_h)
                    active = item["active"]
                    text_s = self.font_small.render(item["label"], True, BG if active else TEXT)
                    bg_color = self.primary if active else PANEL
                    pygame.draw.rect(self.screen, bg_color, rect, border_radius=4)
                    pygame.draw.rect(self.screen, item["border_color"] or LINE, rect, 1, border_radius=4)
                    self.screen.blit(
                        text_s,
                        (rect.x + (rect.w - text_s.get_width()) // 2,
                         rect.y + (rect.h - text_s.get_height()) // 2),
                    )
                    self.control_rects.append((rect, item["kind"], item["key"]))
                else:
                    text_s = self.font_small.render(item["label"], True, TEXT_DIM)
                    self.screen.blit(text_s, (x, y + (btn_h - text_s.get_height()) // 2))

    def handle_control_click(self, pos):
        for rect, kind, key in self.control_rects:
            if rect.collidepoint(pos):
                if kind == "mode":
                    self.mode = key
                elif kind == "gain":
                    self.gain = (
                        max(0.1, self.gain - 0.1) if key == "gain-"
                        else min(4.0, self.gain + 0.1)
                    )
                elif kind == "smooth":
                    self.smoothing = (
                        max(0.0, self.smoothing - 0.02) if key == "smooth-"
                        else min(0.97, self.smoothing + 0.02)
                    )
                elif kind == "theme":
                    self.cycle_theme()
                elif kind == "fullscreen":
                    self.toggle_fullscreen()
                return True
        return False

    def handle_keys(self, event):
        if event.key == pygame.K_ESCAPE:
            if self.fullscreen:
                self.toggle_fullscreen()
            else:
                self.running = False
        elif event.key == pygame.K_1:
            self.mode = "bars"
        elif event.key == pygame.K_2:
            self.mode = "wave"
        elif event.key == pygame.K_3:
            self.mode = "radial"
        elif event.key == pygame.K_4:
            self.mode = "particles"
        elif event.key == pygame.K_5:
            self.mode = "rainbow"
        elif event.key == pygame.K_6:
            self.mode = "futuristic"
        elif event.key == pygame.K_7:
            self.mode = "grid"
        elif event.key == pygame.K_t:
            self.cycle_theme()
        elif event.key in (pygame.K_F11, pygame.K_f):
            self.toggle_fullscreen()
        elif event.key == pygame.K_g:
            self.toggle_renderer()
        elif event.key == pygame.K_UP:
            self.gain = min(4.0, self.gain + 0.1)
        elif event.key == pygame.K_DOWN:
            self.gain = max(0.1, self.gain - 0.1)
        elif event.key == pygame.K_RIGHT:
            self.smoothing = min(0.97, self.smoothing + 0.02)
        elif event.key == pygame.K_LEFT:
            self.smoothing = max(0.0, self.smoothing - 0.02)

    def run(self):
        self.capture.start()
        consecutive_errors = 0
        max_consecutive_errors = 10
        try:
            while self.running:
                try:
                    for event in pygame.event.get():
                        if event.type == pygame.QUIT:
                            self.running = False
                        elif event.type == pygame.KEYDOWN:
                            self.handle_keys(event)
                        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                            self.handle_control_click(event.pos)
                        elif event.type == pygame.VIDEORESIZE:
                            global WINDOW_W, WINDOW_H
                            WINDOW_W, WINDOW_H = event.w, event.h
                            # Actual resizing is handled by the size-sync
                            # check below, not here - see its comment for why.

                    # Sync self.screen to the OS window's actual current size
                    # rather than trusting VIDEORESIZE's payload. Clicking
                    # native maximize/restore/minimize buttons (as opposed to
                    # dragging an edge) doesn't reliably deliver a correctly-
                    # timed VIDEORESIZE on every SDL/driver combination, which
                    # left self.screen (and therefore the rendered content
                    # AND the control bar's click hit-boxes, which are built
                    # against self.screen's size) out of sync with the real
                    # window - the visible symptom being a GUI that doesn't
                    # rescale, or buttons that don't respond where they look
                    # like they should. Polling get_window_size() every frame
                    # is cheap and catches every case uniformly. Also always
                    # passes want_gl=self.use_gl (the renderer currently
                    # active), not self.gl_requested (just "is GL possible on
                    # this machine") - using the latter here previously meant
                    # a manually-forced-software renderer (via 'g') could get
                    # silently flipped back to GPU rendering by an unrelated
                    # resize, which is the same class of bug as fullscreen
                    # toggling doing the same thing (see toggle_fullscreen).
                    if not self.fullscreen:
                        actual_size = pygame.display.get_window_size()
                        if (
                            actual_size[0] > 0 and actual_size[1] > 0
                            and actual_size != self.screen.get_size()
                        ):
                            try:
                                self.screen = self._create_display(
                                    actual_size[0], actual_size[1],
                                    fullscreen=False, want_gl=self.use_gl,
                                )
                            except Exception:
                                logging.exception("Failed to sync display surface to window size")

                    samples = self.capture.latest_samples()
                    if samples is not None and len(samples) > 0:
                        self.last_samples = samples

                    beat = self.detect_beat(self.last_samples)
                    self.beat_flash = 1.0 if beat else max(0.0, self.beat_flash - 0.06)

                    w, h = self.screen.get_size()
                    self._control_bar_rows, self.control_bar_h = self._layout_control_bar(w)
                    chart_h = max(50, h - self.control_bar_h)

                    if self.use_gl:
                        try:
                            self._render_gl_frame(w, h, chart_h, beat)
                        except Exception:
                            logging.exception(
                                "GPU rendering failed - switching to software rendering "
                                "for the rest of this session"
                            )
                            self.use_gl = False
                            try:
                                self.screen = self._create_display(
                                    w, h, self.fullscreen, want_gl=False
                                )
                                w, h = self.screen.get_size()
                                self._control_bar_rows, self.control_bar_h = self._layout_control_bar(w)
                                chart_h = max(50, h - self.control_bar_h)
                            except Exception:
                                logging.exception(
                                    "Failed to recreate software display after GPU fallback"
                                )
                            self._render_software_frame(w, h, chart_h, beat)
                    else:
                        self._render_software_frame(w, h, chart_h, beat)

                    pygame.display.flip()
                    self.clock.tick(60)

                    consecutive_errors = 0

                except pygame.error:
                    # the display/window itself is gone (e.g. closed abruptly) -
                    # nothing more we can safely draw, so stop cleanly
                    logging.exception("Pygame display error - stopping the visualizer")
                    self.running = False

                except Exception:
                    consecutive_errors += 1
                    logging.exception(
                        f"Error while rendering a frame ({consecutive_errors}/"
                        f"{max_consecutive_errors} consecutive) - skipping this frame"
                    )
                    if consecutive_errors >= max_consecutive_errors:
                        logging.critical(
                            "Too many consecutive frame errors, stopping the visualizer"
                        )
                        self.running = False
        finally:
            self.capture.stop()
            pygame.quit()
            logging.info("Visualizer window closed, cleanup complete")


def main():
    log_path = setup_logging()

    p = pyaudio.PyAudio()
    try:
        if TKINTER_AVAILABLE:
            logging.info("Using GUI launcher")
            selection = run_gui_launcher(p)
            if selection is None:
                logging.info("User closed the launcher window without starting")
                return
            device = selection["device"]
            initial_mode = selection["mode"]
            initial_gain = selection["gain"]
            initial_smoothing = selection["smoothing"]
            initial_theme = selection.get("theme_idx", 0)
            initial_attack = selection.get("attack", SPECTRUM_ATTACK)
            initial_beat_sensitivity = selection.get("beat_sensitivity", 1.4)
            force_software = selection.get("force_software", False)
        else:
            logging.warning("Tkinter not available - falling back to console picker")
            print("Tkinter isn't available on this system, falling back to console mode.\n")
            update = check_for_update()
            if update:
                print(f"A newer version is available: {update['version']}")
                print(f"  {update['url']}\n")
            device = choose_device(p)
            initial_mode, initial_gain, initial_smoothing, initial_theme = "bars", 1.4, 0.7, 0
            initial_attack, initial_beat_sensitivity, force_software = SPECTRUM_ATTACK, 1.4, False

        print(f"\nUsing: {device['name']}  ({device['kind']})")
        print("Opening visualizer window... (press ESC in the window to quit)\n")
        logging.info(f"Launching visualizer window with device: {device['name']}")

        capture = AudioCapture(p, device)
        viz = Visualizer(capture, device["name"], force_software=force_software)
        viz.mode = initial_mode
        viz.gain = initial_gain
        viz.smoothing = initial_smoothing
        viz.attack = initial_attack
        viz.beat_sensitivity = initial_beat_sensitivity
        viz.set_theme(initial_theme)
        viz.run()
        logging.info("Program exited normally")

    except Exception:
        logging.exception("Fatal error - the program is stopping")
        show_error_dialog(
            "Signal Audio Visualizer - fatal error",
            "Something went wrong and the program has to stop.\n\n"
            f"Full details were saved to:\n{log_path}\n\n"
            "Please share that file if you need help fixing this.",
        )
        sys.exit(1)

    finally:
        try:
            p.terminate()
        except Exception:
            logging.exception("Error while terminating PyAudio (non-fatal)")


if __name__ == "__main__":
    main()
