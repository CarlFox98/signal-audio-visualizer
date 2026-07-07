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

CHUNK = 1024
FORMAT = pyaudio.paInt16
WINDOW_W, WINDOW_H = 960, 560

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
    """Load the last-used device name, mode, gain, and smoothing. Returns
    sensible defaults if the file doesn't exist or is malformed."""
    defaults = {"device_name": None, "mode": "bars", "gain": 1.4, "smoothing": 0.7, "theme_idx": 0}
    if not os.path.exists(CONFIG_PATH):
        return defaults
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        defaults.update({k: data[k] for k in defaults if k in data})
    except (OSError, json.JSONDecodeError, TypeError):
        logging.warning("Could not read saved config, using defaults", exc_info=True)
    return defaults


def save_config(device_name, mode, gain, smoothing, theme_idx=0):
    data = {
        "device_name": device_name,
        "mode": mode,
        "gain": gain,
        "smoothing": smoothing,
        "theme_idx": theme_idx,
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
    logging.info("Signal Audio Visualizer starting")
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
    root.geometry("620x590")
    root.configure(bg=BG_HEX)
    root.resizable(False, False)

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
    style.configure("Horizontal.TScale", background=BG_HEX)

    outer = ttk.Frame(root, padding=16)
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text="SIGNAL_VIS", style="Header.TLabel").pack(anchor="w")
    ttk.Label(outer, text="choose an audio source and initial settings",
              style="Dim.TLabel").pack(anchor="w", pady=(0, 12))

    ttk.Label(outer, text="Audio sources", style="TLabel").pack(anchor="w")
    list_frame = ttk.Frame(outer)
    list_frame.pack(fill="x", pady=(4, 4))

    scrollbar = tk.Scrollbar(list_frame)
    scrollbar.pack(side="right", fill="y")

    listbox = tk.Listbox(
        list_frame, height=8, bg=PANEL_HEX, fg=TEXT_HEX,
        selectbackground=AMBER_HEX, selectforeground=BG_HEX,
        font=("Consolas", 10), borderwidth=0, highlightthickness=1,
        highlightbackground=LINE_HEX, yscrollcommand=scrollbar.set,
    )
    listbox.pack(side="left", fill="x", expand=True)
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
    ttk.Label(outer, textvariable=status_var, style="Dim.TLabel").pack(anchor="w", pady=(0, 8))

    btn_row = ttk.Frame(outer)
    btn_row.pack(fill="x", pady=(0, 12))

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
    tip_label = ttk.Label(outer, text=tip, style="Dim.TLabel", wraplength=580, justify="left")
    tip_label.pack(anchor="w", pady=(0, 16))

    ttk.Label(outer, text="Starting visual mode", style="TLabel").pack(anchor="w")
    mode_var = tk.StringVar(value=saved.get("mode", "bars"))
    mode_row = ttk.Frame(outer)
    mode_row.pack(fill="x", pady=(4, 4))
    mode_row2 = ttk.Frame(outer)
    mode_row2.pack(fill="x", pady=(0, 12))
    for value, label in [("bars", "bars"), ("wave", "waveform"),
                          ("radial", "radial"), ("particles", "particles")]:
        ttk.Radiobutton(mode_row, text=label, value=value, variable=mode_var).pack(
            side="left", padx=(0, 16)
        )
    for value, label in [("rainbow", "rainbow"), ("futuristic", "futuristic"),
                          ("grid", "neon grid")]:
        ttk.Radiobutton(mode_row2, text=label, value=value, variable=mode_var).pack(
            side="left", padx=(0, 16)
        )

    theme_row = ttk.Frame(outer)
    theme_row.pack(fill="x", pady=(0, 12))
    ttk.Label(theme_row, text="color theme", style="Dim.TLabel").pack(side="left", padx=(0, 8))
    theme_names = [t["name"] for t in THEMES]
    theme_var = tk.StringVar(value=theme_names[min(saved.get("theme_idx", 0), len(theme_names) - 1)])
    theme_dropdown = ttk.Combobox(
        theme_row, textvariable=theme_var, values=theme_names, state="readonly", width=18
    )
    theme_dropdown.pack(side="left")

    slider_row = ttk.Frame(outer)
    slider_row.pack(fill="x", pady=(0, 16))

    gain_var = tk.DoubleVar(value=saved.get("gain", 1.4))
    ttk.Label(slider_row, text="gain", style="Dim.TLabel").grid(row=0, column=0, sticky="w")
    gain_scale = ttk.Scale(slider_row, from_=0.1, to=4.0, variable=gain_var, length=220)
    gain_scale.grid(row=0, column=1, padx=(8, 0))

    smoothing_var = tk.DoubleVar(value=saved.get("smoothing", 0.7))
    ttk.Label(slider_row, text="smoothing", style="Dim.TLabel").grid(
        row=1, column=0, sticky="w", pady=(8, 0)
    )
    smoothing_scale = ttk.Scale(slider_row, from_=0.0, to=0.97, variable=smoothing_var, length=220)
    smoothing_scale.grid(row=1, column=1, padx=(8, 0), pady=(8, 0))

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
        save_config(device["name"], result["mode"], result["gain"], result["smoothing"], theme_idx)
        root.destroy()

    start_btn = ttk.Button(outer, text="start visualizer", command=do_start, style="Accent.TButton")
    start_btn.pack(fill="x", ipady=6)

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

    def __init__(self, capture, source_label):
        pygame.init()
        pygame.display.set_caption("SIGNAL_VIS — " + source_label)

        # pygame.display.Info() only reliably reports the TRUE desktop
        # resolution before any window/mode has been set - afterward, on
        # some platform/driver combinations, it can reflect the current
        # window's size instead. Capture it once, right here, and reuse
        # this value for fullscreen sizing from now on.
        _pre_info = pygame.display.Info()
        self.desktop_size = (_pre_info.current_w, _pre_info.current_h)

        self.gl_requested = OPENGL_AVAILABLE
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
        self.smoothed_spectrum = None
        self.particles = []
        self.running = True

        self.theme_idx = 0
        self.primary = THEMES[0]["primary"]
        self.secondary = THEMES[0]["secondary"]

        # beat / onset detection
        self.rms_history = collections.deque(maxlen=43)
        self.last_beat_time = 0.0
        self.beat_flash = 0.0

        # animation phases for the rainbow / futuristic / neon grid modes
        self.rainbow_phase = 0.0
        self.futuristic_rotation = 0.0

        self.control_rects = []  # populated each frame by build_control_bar

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
            if not self.fullscreen:
                self.windowed_size = self.screen.get_size()
                self.screen = self._create_display(0, 0, fullscreen=True, want_gl=self.gl_requested)
                self.fullscreen = True
            else:
                self.screen = self._create_display(
                    *self.windowed_size, fullscreen=False, want_gl=self.gl_requested
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
            a = self.smoothing
            self.smoothed_spectrum = a * self.smoothed_spectrum + (1 - a) * spectrum
        return self.smoothed_spectrum

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
            if rms > avg * 1.4 and rms > 0.02 and (now - self.last_beat_time) > 0.12:
                beat = True
                self.last_beat_time = now

        self.rms_history.append(rms)
        return beat

    def draw_bars(self, spectrum, w, h, beat_flash):
        bar_count = 96
        usable = spectrum[: len(spectrum) // 2]
        step = max(1, len(usable) // bar_count)
        bar_w = w / bar_count * 0.72
        gap = w / bar_count * 0.28
        boost = 1.0 + 0.18 * beat_flash
        for i in range(bar_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            bar_h = min(v * boost, 1.0) * (h * 0.92)
            x = i * (bar_w + gap)
            t = i / bar_count
            color = tuple(
                int(self.primary[c] * (1 - t) + self.secondary[c] * t) for c in range(3)
            )
            pygame.draw.rect(self.screen, color, (float(x), float(h - bar_h), float(bar_w), float(bar_h)))

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
        base_r = min(w, h) * 0.18 * (1.0 + 0.12 * beat_flash)
        bar_count = 120
        usable = spectrum[: len(spectrum) // 2]
        step = max(1, len(usable) // bar_count)

        ring_color = tuple(
            min(255, int(LINE[c] + (255 - LINE[c]) * beat_flash * 0.5)) for c in range(3)
        )
        pygame.draw.circle(self.screen, ring_color, (int(cx), int(cy)), int(base_r), 1)

        # a soft expanding ring that appears briefly on each detected beat
        if beat_flash > 0.05:
            pulse_r = base_r + beat_flash * (min(w, h) * 0.12)
            pulse_alpha = max(0, min(255, int(beat_flash * 160)))
            s = pygame.Surface((w, h), pygame.SRCALPHA)
            pygame.draw.circle(s, (*self.primary, pulse_alpha), (int(cx), int(cy)), int(pulse_r), 2)
            self.screen.blit(s, (0, 0))

        for i in range(bar_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            length = min(v, 1.0) * (min(w, h) * 0.32)
            angle = (i / bar_count) * 2 * np.pi - np.pi / 2
            x1 = float(cx + np.cos(angle) * base_r)
            y1 = float(cy + np.sin(angle) * base_r)
            x2 = float(cx + np.cos(angle) * (base_r + length))
            y2 = float(cy + np.sin(angle) * (base_r + length))
            color = self.primary if i / bar_count < 0.5 else self.secondary
            pygame.draw.line(self.screen, color, (x1, y1), (x2, y2), 3)

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
        """Sci-fi HUD-style readout: a rotating dashed outer ring, a
        segmented spectrum dial, a pulsing core, and HUD corner brackets."""
        cx, cy = w / 2, h / 2
        self.futuristic_rotation = (self.futuristic_rotation + 0.006) % (2 * np.pi)

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
        pygame.draw.lines(self.screen, FUTURISTIC_CYAN, True, pts, 1)

        # segmented spectrum dial
        for i in range(seg_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            length = min(v, 1.0) * (outer_r - inner_r)
            angle = (i / seg_count) * 2 * np.pi - np.pi / 2 + self.futuristic_rotation * 0.3
            x1 = cx + np.cos(angle) * inner_r
            y1 = cy + np.sin(angle) * inner_r
            x2 = cx + np.cos(angle) * (inner_r + length)
            y2 = cy + np.sin(angle) * (inner_r + length)
            pygame.draw.line(self.screen, FUTURISTIC_CYAN, (x1, y1), (x2, y2), 2)

        # pulsing core
        core_r = max(2, inner_r * 0.5 * (1.0 + 0.3 * beat_flash))
        pygame.draw.circle(self.screen, FUTURISTIC_WHITE, (int(cx), int(cy)), int(core_r), 2)

        # HUD corner brackets
        bracket, margin = 24, 16
        for bx, by, dx, dy in [
            (margin, margin, 1, 1), (w - margin, margin, -1, 1),
            (margin, h - margin, 1, -1), (w - margin, h - margin, -1, -1),
        ]:
            pygame.draw.line(self.screen, FUTURISTIC_CYAN, (bx, by), (bx + dx * bracket, by), 2)
            pygame.draw.line(self.screen, FUTURISTIC_CYAN, (bx, by), (bx, by + dy * bracket), 2)

    def draw_grid(self, spectrum, w, h, beat_flash):
        """Neon perspective grid floor with glowing horizon and
        spectrum-reactive light bars - an original take on the glowing
        circuit-grid aesthetic, not any specific copyrighted artwork."""
        horizon_y = h * 0.42
        vanish_x = w / 2

        glow_color = NEON_ORANGE if beat_flash > 0.4 else NEON_CYAN
        pygame.draw.line(self.screen, glow_color, (0, horizon_y), (w, horizon_y), 2)

        # converging vertical lines toward the vanishing point
        num_v = 16
        for i in range(num_v + 1):
            bx = i / num_v * w
            pygame.draw.line(self.screen, NEON_CYAN, (bx, h), (vanish_x, horizon_y), 1)

        # horizontal lines with perspective spacing, brighter toward viewer
        num_h = 10
        for j in range(1, num_h + 1):
            t = j / num_h
            y = horizon_y + (h - horizon_y) * (t ** 2)
            fade = 0.3 + 0.7 * t
            color = tuple(int(c * fade) for c in NEON_CYAN)
            pygame.draw.line(self.screen, color, (0, y), (w, y), 1)

        # spectrum-reactive light bars rising from the grid
        usable = spectrum[: len(spectrum) // 2]
        bar_count = 40
        step = max(1, len(usable) // bar_count)
        for i in range(bar_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            bx = (i + 0.5) / bar_count * w
            bar_h = min(v, 1.0) * (horizon_y * 0.9)
            color = NEON_ORANGE if v > 0.6 else NEON_CYAN
            pygame.draw.line(self.screen, color, (bx, horizon_y), (bx, horizon_y - bar_h), 2)

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

        gl.glBegin(gl.GL_QUADS)
        for i in range(bar_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            bar_h = min(v * boost, 1.0) * (h * 0.92)
            x = i * (bar_w + gap)
            t = i / bar_count
            color = tuple(
                (self.primary[c] * (1 - t) + self.secondary[c] * t) / 255 for c in range(3)
            )
            y0 = h - bar_h
            gl.glColor3f(*color)
            gl.glVertex2f(x, h)
            gl.glVertex2f(x + bar_w, h)
            gl.glVertex2f(x + bar_w, y0)
            gl.glVertex2f(x, y0)
        gl.glEnd()

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

    def gl_draw_radial(self, spectrum, w, h, beat_flash):
        cx, cy = w / 2, h / 2
        base_r = min(w, h) * 0.18 * (1.0 + 0.12 * beat_flash)
        bar_count = 120
        usable = spectrum[: len(spectrum) // 2]
        step = max(1, len(usable) // bar_count)
        segs = 64

        ring_color = tuple(
            min(255, int(LINE[c] + (255 - LINE[c]) * beat_flash * 0.5)) / 255 for c in range(3)
        )
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
            gl.glBegin(gl.GL_LINE_LOOP)
            for i in range(segs):
                a = i / segs * 2 * np.pi
                gl.glVertex2f(cx + np.cos(a) * pulse_r, cy + np.sin(a) * pulse_r)
            gl.glEnd()

        gl.glBegin(gl.GL_LINES)
        for i in range(bar_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            length = min(v, 1.0) * (min(w, h) * 0.32)
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

        usable = spectrum[: len(spectrum) // 2]
        seg_count = 72
        step = max(1, len(usable) // seg_count)
        outer_r = min(w, h) * 0.32
        inner_r = min(w, h) * 0.20
        cyan = tuple(c / 255 for c in FUTURISTIC_CYAN)
        white = tuple(c / 255 for c in FUTURISTIC_WHITE)

        gl.glColor3f(*cyan)
        ring_segs = 90
        gl.glBegin(gl.GL_LINE_LOOP)
        for i in range(ring_segs):
            a = i / ring_segs * 2 * np.pi + self.futuristic_rotation
            gl.glVertex2f(cx + np.cos(a) * outer_r * 1.05, cy + np.sin(a) * outer_r * 1.05)
        gl.glEnd()

        gl.glBegin(gl.GL_LINES)
        for i in range(seg_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            length = min(v, 1.0) * (outer_r - inner_r)
            angle = (i / seg_count) * 2 * np.pi - np.pi / 2 + self.futuristic_rotation * 0.3
            x1 = cx + np.cos(angle) * inner_r
            y1 = cy + np.sin(angle) * inner_r
            x2 = cx + np.cos(angle) * (inner_r + length)
            y2 = cy + np.sin(angle) * (inner_r + length)
            gl.glVertex2f(x1, y1)
            gl.glVertex2f(x2, y2)
        gl.glEnd()

        core_r = max(2, inner_r * 0.5 * (1.0 + 0.3 * beat_flash))
        gl.glColor3f(*white)
        gl.glBegin(gl.GL_LINE_LOOP)
        for i in range(32):
            a = i / 32 * 2 * np.pi
            gl.glVertex2f(cx + np.cos(a) * core_r, cy + np.sin(a) * core_r)
        gl.glEnd()

        bracket, margin = 24, 16
        gl.glColor3f(*cyan)
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

        gl.glColor3f(*(orange if beat_flash > 0.4 else cyan))
        gl.glLineWidth(2.0)
        gl.glBegin(gl.GL_LINES)
        gl.glVertex2f(0, horizon_y); gl.glVertex2f(w, horizon_y)
        gl.glEnd()

        gl.glLineWidth(1.0)
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
            t = j / num_h
            y = horizon_y + (h - horizon_y) * (t ** 2)
            fade = 0.3 + 0.7 * t
            gl.glColor3f(cyan[0] * fade, cyan[1] * fade, cyan[2] * fade)
            gl.glVertex2f(0, y); gl.glVertex2f(w, y)
        gl.glEnd()

        usable = spectrum[: len(spectrum) // 2]
        bar_count = 40
        step = max(1, len(usable) // bar_count)
        gl.glLineWidth(2.0)
        gl.glBegin(gl.GL_LINES)
        for i in range(bar_count):
            idx = min(i * step, len(usable) - 1)
            v = float(usable[idx])
            bx = (i + 0.5) / bar_count * w
            bar_h = min(v, 1.0) * (horizon_y * 0.9)
            gl.glColor3f(*(orange if v > 0.6 else cyan))
            gl.glVertex2f(bx, horizon_y); gl.glVertex2f(bx, horizon_y - bar_h)
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

            bottom_surface = pygame.Surface((w, self.CONTROL_BAR_H), pygame.SRCALPHA)
            self.screen = bottom_surface
            self.draw_control_bar(w, self.CONTROL_BAR_H)
            # draw_control_bar builds self.control_rects in the small
            # offscreen surface's LOCAL coordinates - translate them back
            # to real window coordinates so mouse clicks hit-test correctly
            offset_y = h - self.CONTROL_BAR_H
            self.control_rects = [
                (rect.move(0, offset_y), kind, key) for (rect, kind, key) in self.control_rects
            ]
            self._gl_draw_texture_from_surface(bottom_surface, w, self.CONTROL_BAR_H, dest_y=offset_y)
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

    def draw_control_bar(self, w, h):
        """Draws the clickable bottom control bar and rebuilds the hit-test
        rects used by handle_control_click(). Called once per frame."""
        bar_y = h - self.CONTROL_BAR_H
        pygame.draw.rect(self.screen, PANEL, (0, bar_y, w, self.CONTROL_BAR_H))
        pygame.draw.line(self.screen, LINE, (0, bar_y), (w, bar_y), 1)

        self.control_rects = []
        btn_h = self.CONTROL_BAR_H - 16
        pad_y = bar_y + 8
        x = 10

        def draw_button(label, active=False, border_color=None):
            nonlocal x
            text_s = self.font_small.render(label, True, BG if active else TEXT)
            btn_w = text_s.get_width() + 18
            rect = pygame.Rect(x, pad_y, btn_w, btn_h)
            bg_color = self.primary if active else PANEL
            pygame.draw.rect(self.screen, bg_color, rect, border_radius=4)
            pygame.draw.rect(self.screen, border_color or LINE, rect, 1, border_radius=4)
            self.screen.blit(
                text_s,
                (rect.x + (rect.w - text_s.get_width()) // 2,
                 rect.y + (rect.h - text_s.get_height()) // 2),
            )
            x += btn_w + 6
            return rect

        for key, label in [("bars", "Bars"), ("wave", "Wave"),
                            ("radial", "Radial"), ("particles", "Particles"),
                            ("rainbow", "Rainbow"), ("futuristic", "Futuristic"),
                            ("grid", "Neon Grid")]:
            rect = draw_button(label, active=(self.mode == key))
            self.control_rects.append((rect, "mode", key))

        x += 14
        gain_label = self.font_small.render(f"gain {self.gain:.1f}", True, TEXT_DIM)
        self.screen.blit(gain_label, (x, pad_y + (btn_h - gain_label.get_height()) // 2))
        x += gain_label.get_width() + 8
        for key, label in [("gain-", "-"), ("gain+", "+")]:
            rect = draw_button(label)
            self.control_rects.append((rect, "gain", key))

        x += 14
        smooth_label = self.font_small.render(f"smooth {self.smoothing:.2f}", True, TEXT_DIM)
        self.screen.blit(smooth_label, (x, pad_y + (btn_h - smooth_label.get_height()) // 2))
        x += smooth_label.get_width() + 8
        for key, label in [("smooth-", "-"), ("smooth+", "+")]:
            rect = draw_button(label)
            self.control_rects.append((rect, "smooth", key))

        x += 14
        theme_rect = draw_button(f"theme: {THEMES[self.theme_idx]['name']}",
                                  border_color=self.primary)
        self.control_rects.append((theme_rect, "theme", "cycle"))

        x += 14
        fs_label = "windowed" if self.fullscreen else "fullscreen"
        fs_rect = draw_button(fs_label)
        self.control_rects.append((fs_rect, "fullscreen", "toggle"))

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
                            if not self.fullscreen:
                                try:
                                    self.screen = self._create_display(
                                        event.w, event.h, fullscreen=False,
                                        want_gl=self.gl_requested,
                                    )
                                except Exception:
                                    logging.exception("Failed to resize display surface")

                    samples = self.capture.latest_samples()
                    if samples is not None and len(samples) > 0:
                        self.last_samples = samples

                    beat = self.detect_beat(self.last_samples)
                    self.beat_flash = 1.0 if beat else max(0.0, self.beat_flash - 0.06)

                    w, h = self.screen.get_size()
                    chart_h = max(50, h - self.CONTROL_BAR_H)

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
                                chart_h = max(50, h - self.CONTROL_BAR_H)
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
        else:
            logging.warning("Tkinter not available - falling back to console picker")
            print("Tkinter isn't available on this system, falling back to console mode.\n")
            device = choose_device(p)
            initial_mode, initial_gain, initial_smoothing, initial_theme = "bars", 1.4, 0.7, 0

        print(f"\nUsing: {device['name']}  ({device['kind']})")
        print("Opening visualizer window... (press ESC in the window to quit)\n")
        logging.info(f"Launching visualizer window with device: {device['name']}")

        capture = AudioCapture(p, device)
        viz = Visualizer(capture, device["name"])
        viz.mode = initial_mode
        viz.gain = initial_gain
        viz.smoothing = initial_smoothing
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
