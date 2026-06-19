from PIL import Image, ImageTk, ImageDraw, ImageFont
import tkinter as tk
from tkinter import ttk
import tkinter.messagebox as messagebox
import re
import json
import os
import threading
from threading import Timer
import time
import logging
import sys
import socket
from datetime import datetime
import shlex
import argparse

# When running as a PyInstaller-frozen binary on Linux, the bootloader's
# LD_LIBRARY_PATH override can interfere with libvlc's normal discovery
# and relative plugin path resolution, causing either "no function
# libvlc_new" (lib not found) or libvlc loading with zero plugins
# (e.g. "unknown option --rtsp-tcp"). We rely on the system-installed
# VLC, so point python-vlc explicitly at the system libvlc and plugin
# directory before importing the bindings, which load libvlc at import
# time. Covers common Fedora/RHEL and Debian/Ubuntu layouts.
if getattr(sys, 'frozen', False) and sys.platform.startswith('linux'):
    _lib_candidates = (
        '/usr/lib64/libvlc.so.5',                       # Fedora/RHEL
        '/usr/lib/x86_64-linux-gnu/libvlc.so.5',        # Debian/Ubuntu amd64
        '/usr/lib/i386-linux-gnu/libvlc.so.5',          # Debian/Ubuntu i386
        '/usr/lib/aarch64-linux-gnu/libvlc.so.5',       # Debian/Ubuntu arm64
        '/usr/local/lib/libvlc.so.5',
    )
    _plugin_candidates = (
        '/usr/lib64/vlc/plugins',                       # Fedora/RHEL
        '/usr/lib/x86_64-linux-gnu/vlc/plugins',        # Debian/Ubuntu amd64
        '/usr/lib/i386-linux-gnu/vlc/plugins',          # Debian/Ubuntu i386
        '/usr/lib/aarch64-linux-gnu/vlc/plugins',       # Debian/Ubuntu arm64
        '/usr/lib/vlc/plugins',
        '/usr/local/lib/vlc/plugins',
    )
    for _candidate in _lib_candidates:
        if os.path.isfile(_candidate):
            os.environ.setdefault('PYTHON_VLC_LIB_PATH', _candidate)
            break
    for _candidate in _plugin_candidates:
        if os.path.isdir(_candidate):
            os.environ.setdefault('VLC_PLUGIN_PATH', _candidate)
            break

import vlc
import ctypes
from ctypes import Structure

def debounce(wait):
    """Decorator to debounce a function."""
    def decorator(fn):
        def debounced(*args, **kwargs):
            def call_it():
                fn(*args, **kwargs)
            if hasattr(debounced, '_timer'):
                debounced._timer.cancel()
            debounced._timer = Timer(wait, call_it)
            debounced._timer.start()
        return debounced
    return decorator

# Define libvlc_media_stats_t structure
class libvlc_media_stats_t(Structure):
    _fields_ = [
        ("i_read_bytes", ctypes.c_int),
        ("f_input_bitrate", ctypes.c_float),
        ("i_demux_read_bytes", ctypes.c_int),
        ("f_demux_bitrate", ctypes.c_float),
        ("i_demux_corrupted", ctypes.c_int),
        ("i_demux_discontinuity", ctypes.c_int),
        ("i_decoded_video", ctypes.c_int),
        ("i_decoded_audio", ctypes.c_int),
        ("i_displayed_pictures", ctypes.c_int),
        ("i_lost_pictures", ctypes.c_int),
        ("i_played_abuffers", ctypes.c_int),
        ("i_lost_abuffers", ctypes.c_int),
        ("i_sent_packets", ctypes.c_int),
        ("i_sent_bytes", ctypes.c_int),
        ("f_send_bitrate", ctypes.c_float),
    ]

class tapoStreamer:
    DEFAULT_VLC_PARAMS = [
        "--avcodec-hw=any",
        "--network-caching=3000",
        "--deinterlace=auto"
    ]

    @classmethod
    def parse_vlcparams(cls, raw, default=None):
        """Parse a VLC params string or list into a validated list of
        '--option' style flags. Falls back to default (or
        DEFAULT_VLC_PARAMS) on empty/invalid input. Centralized so
        config-load and the settings-dialog save path can't drift."""
        default = default if default is not None else cls.DEFAULT_VLC_PARAMS
        try:
            if isinstance(raw, (list, tuple)):
                params = list(raw)
            else:
                raw_str = (raw or "").strip()
                params = shlex.split(raw_str) if raw_str else []
            valid_params = [p for p in params if p.startswith('--')]
            return valid_params or default
        except Exception as e:
            logging.error(f"Failed to parse VLC parameters '{raw}': {e}", exc_info=True)
            return default

    MIN_WIDTH = 1340
    MIN_HEIGHT = 720

    def check_decoder_availability(self):
        """Best-effort check for a hardware-capable ffmpeg h264 decoder.

        On some distros (e.g. stock Fedora), ffmpeg ships only the
        software-only libopenh264 decoder, which silently prevents VLC
        hardware acceleration from ever engaging (--avcodec-hw=any has
        nothing to accelerate). This is just a diagnostic log line so
        users aren't left guessing why GPU usage never moves.
        """
        if not sys.platform.startswith("linux"):
            return
        try:
            import subprocess
            result = subprocess.run(
                ['ffmpeg', '-decoders'],
                capture_output=True, text=True, timeout=5
            )
            output = result.stdout
            h264_lines = [l for l in output.splitlines() if 'h264' in l.lower()]
            has_hw_capable = any(
                ' h264 ' in l or l.strip().split()[-1] == 'h264'
                for l in h264_lines if 'libopenh264' not in l and 'v4l2m2m' not in l
            )
            only_openh264 = h264_lines and all('libopenh264' in l or 'v4l2m2m' in l for l in h264_lines)
            if only_openh264 or not has_hw_capable:
                logging.warning(
                    "ffmpeg appears to only provide software h264 decoding "
                    "(libopenh264) - hardware acceleration will not be available. "
                    "On Fedora/RHEL, install ffmpeg from RPM Fusion for a "
                    "hardware-capable build (h264 with vaapi/cuda/qsv support)."
                )
            else:
                logging.debug("ffmpeg h264 decoder appears hardware-capable")
        except FileNotFoundError:
            logging.debug("ffmpeg not found on PATH, skipping decoder check")
        except Exception as e:
            logging.debug(f"Could not check ffmpeg decoder availability: {e}")

    def _setup_logging(self, debug_mode):
        """Configure logging based on debug mode."""
        # Clear existing handlers to avoid duplicates
        logging.getLogger().handlers = []

        if not debug_mode:
            # Disable logging by setting a null handler and high level
            null_handler = logging.NullHandler()
            logging.getLogger().addHandler(null_handler)
            logging.getLogger().setLevel(logging.CRITICAL + 1)  # Higher than any log level
            return

        # Configure logging for debug mode
        log_level = logging.DEBUG
        log_dir = os.path.dirname(self.config_file)
        log_file = os.path.join(log_dir, "tapo-streamer.log")

        # Configure root logger
        logging.basicConfig(
            filename=log_file,
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )

        # Configure zeep logger to suppress ISO8601Error
        zeep_logger = logging.getLogger('zeep')
        zeep_logger.propagate = False  # Prevent propagation to root logger

        # Additional safety for zeep.xsd.types.simple
        logging.getLogger('zeep.xsd.types.simple').setLevel(logging.WARNING)

        logging.info("Logging initialized with level DEBUG")

    def __init__(self, root):
        # Parse command-line arguments
        parser = argparse.ArgumentParser(description="Tapo Streamer Application")
        parser.add_argument('--debug', action='store_true', help="Enable debug logging")
        args = parser.parse_args()

        # --- Configuration Setup ---
        self.root = root
        self.root.title("Tapo Streamer")

        self.root.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)

        # Set up configuration directory
        if sys.platform.startswith("linux"):
            config_dir = os.path.join(os.path.expanduser("~"), ".tapo-streamer")
        else:
            config_dir = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "TapoStreamer")
        os.makedirs(config_dir, exist_ok=True)
        self.config_file = os.path.join(config_dir, "config.json")

        # Initialize debug mode and logging
        self.debug_mode = args.debug
        self.speed_cycle = [1.0, 2.0, 4.0, 8.0]
        self.load_config()  # Load config to set debug_mode if not overridden
        if not args.debug and hasattr(self, 'config_debug'):
            self.debug_mode = self.config_debug
        self._setup_logging(self.debug_mode)
        self.check_decoder_availability()

        # --- Application Setup ---
        if getattr(sys, 'frozen', False):
            self.base_path = sys._MEIPASS
            if sys.platform.startswith('win'):
                os.environ['VLC_PLUGIN_PATH'] = os.path.join(self.base_path, 'vlc', 'plugins')
            # On Linux, VLC_PLUGIN_PATH is set at module-load time above,
            # pointing at the system VLC plugin directory.
        else:
            self.base_path = os.path.dirname(os.path.abspath(__file__))

        icon_path = os.path.join(self.base_path, "cam.png")
        try:
            img = Image.open(icon_path)
            img_titlebar = img.resize((64, 64), Image.LANCZOS)
            icon = ImageTk.PhotoImage(img_titlebar)
            self.root.iconphoto(True, icon)
        except Exception as e:
            logging.error(f"Error loading icon: {e}", exc_info=True)
            logging.debug(f"Error loading icon: {e}")

        # --- Initialize Core State ---
        self.root.configure(bg="#222222")
        self.root.minsize(1340, 720)
        self.root.protocol("WM_DELETE_WINDOW", self.cleanup)
        self.running = True
        self.is_fullscreen = False
        self.fullscreen_index = None
        self.help_overlay = None

        self.vlc_instance = None
        self.stream_initializing = [False] * 4  # From prior refactor
        self.stream_init_lock = threading.Lock()  # From prior refactor
        self.stream_cleanup_events = [threading.Event() for _ in range(4)]  # Events for cleanup signaling
        # Locks that serialise archive-entry background threads against
        # concurrent toggle_archive_mode(exit) calls on the main thread.
        # Acquiring the lock in _enter_archive_mode_thread and checking it
        # in the exit path prevents two threads calling cleanup_stream()
        # simultaneously, which causes a libvlc segfault.
        self.archive_entry_locks = [threading.Lock() for _ in range(4)]
        self.media_players = [None] * 4
        self.streams = [""] * 4
        self.panels = [None] * 4
        self.labels = [None] * 4
        self.drop_timestamps = [[] for _ in range(4)]
        self.fullscreen_buttons = [None] * 4
        self.fullscreen_images = [None] * 4
        self.exit_fullscreen_button = None
        self.exit_fullscreen_image = None

        self.onvif_cams = {}
        self.ptz_moving = False
        self.ptz_busy = False
        self.ptz_buttons_disabled = False
        self.ptz_lock = threading.Lock()
        self.ptz_click_counts = [0] * 4

        self.is_archive_mode = [False] * 4 
        self.archive_mode_button = None
        self.archive_mode_image = None
        self.current_archive_path = [None] * 4
        self.playback_speeds = [1.0] * 4
        self.is_paused = [False] * 4
        self.video_ended = [False] * 4
        self.pagination_state = [{} for _ in range(4)]

        self.config_button = None
        self.ptz_buttons_disabled = False
        self.ptz_buttons = []
        self.ptz_images = []
        self.archive_buttons = [None] * 4
        self.archive_images = [None] * 4
        self.archive_canvas = [None] * 4
        self.back_buttons = [None] * 4
        self.exit_buttons = [None] * 4
        self.pause_buttons = [None] * 4
        self.speed_buttons = [None] * 4
        self.pause_images = [None] * 4
        self.speed_images = [None] * 4
        self.replay_buttons = [None] * 4
        self.replay_images = [None] * 4
        self.rewind_buttons = [None] * 4
        self.rewind_images = [None] * 4
        self.clicked_items = {index: set() for index in range(4)}

        self.panel_sizes = [(0, 0)] * 4
        self.target_dims = [(0, 0)] * 4
        self.frame_shapes = [(0, 0)] * 4
        self.last_layout_update = 0
        self.debounce_timer = None
  
        # Initialize frame count tracking
        self.last_dropped_frames = {}  # Last cumulative dropped frames per stream
        self.last_displayed_frames = {}  # Last cumulative displayed frames per stream
        for i in range(len(self.ips)):
            self.last_dropped_frames[i] = 0
            self.last_displayed_frames[i] = 0

        # Pre-render and cache all icons
        self.icon_cache = {
            "up": self.create_icon("up"),
            "down": self.create_icon("down"),
            "left": self.create_icon("left"),
            "right": self.create_icon("right"),
            "minimize": self.create_icon("minimize"),
            "config": self.create_icon("config"),
            "disk": self.create_icon("disk"),
            "fullscreen": self.create_icon("fullscreen"),
            "pause": self.create_icon("pause"),
            "play": self.create_icon("play"),
            "speed": self.create_icon("speed"),
            "replay": self.create_icon("replay"),
            "rewind": self.create_icon("rewind"),
            "exit": self.create_icon("exit"),
            "resize": self.create_icon("resize"),
            "back": self.create_icon("back"),
            "folder": self.create_icon("folder", opacity=1.0),
            "folder_clicked": self.create_icon("folder", opacity=0.6),
            "archive": self.create_icon("archive", opacity=1.0),
        }

        # Cache for per-weekday folder icons (Mon/Tue/.../Sun x
        # clicked/unclicked), generated lazily by get_day_folder_icon.
        self.day_folder_icon_cache = {}

        # --- Final Setup ---
        # Cache of loaded/resized archive thumbnails, keyed by
        # (path, width, height) -> PhotoImage, to avoid re-decoding and
        # re-resizing JPEGs from disk on every render_archive_view call
        # (page changes, navigation, fullscreen toggles, etc.)
        self.thumbnail_cache = {}
        self.thumbnail_cache_order = []  # insertion order for simple LRU eviction
        self.thumbnail_cache_max = 200

        self.init_ui()
        self.update_streams()
        self.root.after(0, lambda: threading.Thread(target=self.start_streams, daemon=True).start())

    def load_config(self):
        # Initialize default configuration
        self.username = ""
        self.password = ""
        self.archive_dir = ""
        # Cached result of the last os.path.exists(archive_dir) probe.
        # build_config_panel() reads this instead of calling
        # os.path.exists() directly, since that can block for seconds on
        # a spun-down disk or slow network mount and was previously
        # freezing the main thread on every config panel rebuild.
        # Optimistically True so the button shows before the first probe.
        self.ips = ["", "", "", ""]
        self.hq_enabled = [True] * 4
        self.audio_enabled = [True] * 4
        self.ptz_supported = [False] * 4
        self.config_debug = False
        self.vlcparams = self.DEFAULT_VLC_PARAMS
        self.ptz_resolution = 3
        self.saved_window_size = "1340x720"
        self.enable_fullscreen_buttons = False
        self.default_playback_speed = 1.0
        # New stream reliability settings
        self.enable_retries = True
        self.max_retry_attempts = 5
        self.initial_backoff_delay = 2.0
        self.enable_quality_downgrade = True
        self.drop_threshold = 8
        self.drop_window = 30.0
        self.downgrade_cooldown = 120.0
        self.enable_auto_revert_hq = False
        self.stability_period = 300.0
        self.no_frame_timeout = 15.0
        self.archive_font = "arial"

        # Load from config file if it exists
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    config = json.load(f)
                self.username = config.get("username", self.username)
                self.password = config.get("password", self.password)
                self.archive_dir = config.get("archive_dir", self.archive_dir)
                self.ips = config.get("ips", self.ips)
                self.hq_enabled = [bool(config.get("hq_enabled", self.hq_enabled)[i]) for i in range(4)]
                self.audio_enabled = config.get("audio_enabled", self.audio_enabled)
                self.ptz_supported = config.get("ptz_supported", self.ptz_supported)
                self.config_debug = config.get("debug", self.config_debug)
                self.vlcparams = self.parse_vlcparams(config.get("vlcparams", self.vlcparams), default=self.vlcparams)
                self.ptz_resolution = config.get("ptz_resolution", self.ptz_resolution)
                self.saved_window_size = config.get("saved_window_size", self.saved_window_size)
                self.enable_fullscreen_buttons = config.get("enable_fullscreen_buttons", self.enable_fullscreen_buttons)
                self.default_playback_speed = config.get("default_playback_speed", self.default_playback_speed)
                # Load new stream reliability settings
                self.enable_retries = config.get("enable_retries", self.enable_retries)
                self.max_retry_attempts = config.get("max_retry_attempts", self.max_retry_attempts)
                self.initial_backoff_delay = config.get("initial_backoff_delay", self.initial_backoff_delay)
                self.enable_quality_downgrade = config.get("enable_quality_downgrade", self.enable_quality_downgrade)
                self.drop_threshold = config.get("drop_threshold", self.drop_threshold)
                self.drop_window = config.get("drop_window", self.drop_window)
                self.downgrade_cooldown = config.get("downgrade_cooldown", self.downgrade_cooldown)
                self.enable_auto_revert_hq = config.get("enable_auto_revert_hq", self.enable_auto_revert_hq)
                self.stability_period = config.get("stability_period", self.stability_period)
                raw_no_frame = config.get("no_frame_timeout", self.no_frame_timeout)
                self.no_frame_timeout = float(raw_no_frame) if raw_no_frame > 5 else 15.0
                raw_font = config.get("archive_font", self.archive_font)
                ALLOWED_FONTS = {"arial", "helvetica", "courier", "times", "verdana", "tahoma", "trebuchet ms"}
                self.archive_font = raw_font if raw_font in ALLOWED_FONTS else "arial"

                # Validate saved_window_size
                try:
                    if self.saved_window_size != "fullscreen":
                        width, height = map(int, self.saved_window_size.split("x"))
                        if width < self.MIN_WIDTH or height < self.MIN_HEIGHT:
                            logging.warning(f"Invalid saved_window_size: {self.saved_window_size}, using default 1340x720")
                            self.saved_window_size = "1340x720"
                except (ValueError, TypeError):
                    logging.warning(f"Invalid saved_window_size: {self.saved_window_size}, using default 1340x720")
                    self.saved_window_size = "1340x720"

                # Validate ptz_resolution
                if not isinstance(self.ptz_resolution, int) or self.ptz_resolution < 1 or self.ptz_resolution > 5:
                    self.ptz_resolution = 3

                # Validate default_playback_speed
                try:
                    self.default_playback_speed = float(self.default_playback_speed)
                    if self.default_playback_speed not in self.speed_cycle:
                        logging.warning(f"Invalid default_playback_speed: {self.default_playback_speed}, using default 1.0")
                        self.default_playback_speed = 1.0
                except (ValueError, TypeError):
                    logging.warning(f"Invalid default_playback_speed: {self.default_playback_speed}, using default 1.0")
                    self.default_playback_speed = 1.0

                # Validate new settings
                if self.max_retry_attempts < 1:
                    logging.warning(f"Invalid max_retry_attempts: {self.max_retry_attempts}, using default 3")
                    self.max_retry_attempts = 5
                if self.initial_backoff_delay <= 0:
                    logging.warning(f"Invalid initial_backoff_delay: {self.initial_backoff_delay}, using default 1.0")
                    self.initial_backoff_delay = 2.0
                if self.drop_threshold < 1:
                    logging.warning(f"Invalid drop_threshold: {self.drop_threshold}, using default 10")
                    self.drop_threshold = 8
                if self.drop_window <= 0:
                    logging.warning(f"Invalid drop_window: {self.drop_window}, using default 5.0")
                    self.drop_window = 30.0
                if self.downgrade_cooldown < 10:
                    logging.warning(f"Invalid downgrade_cooldown: {self.downgrade_cooldown}, using default 30.0")
                    self.downgrade_cooldown = 120.0
                if self.stability_period < 10:
                    logging.warning(f"Invalid stability_period: {self.stability_period}, using default 30.0")
                    self.stability_period = 300.0

            except json.JSONDecodeError as e:
                logging.error(f"Failed to parse config file {self.config_file}: {e}. Using default settings.")
                self.save_config()
            except PermissionError as e:
                logging.error(f"Permission denied accessing config file {self.config_file}: {e}. Using default settings.")
            except Exception as e:
                logging.error(f"Unexpected error loading config file {self.config_file}: {e}", exc_info=True)
                self.save_config()
        else:
            logging.info(f"Config file {self.config_file} does not exist. Creating with default settings.")
            self.save_config()

    def save_config(self):
        config = {
            "username": self.username,
            "password": self.password,
            "archive_dir": self.archive_dir,
            "vlcparams": self.vlcparams,
            "ips": self.ips,
            "hq_enabled": self.hq_enabled,
            "audio_enabled": self.audio_enabled,
            "ptz_supported": self.ptz_supported,
            "debug": self.config_debug,
            "ptz_resolution": self.ptz_resolution,
            "saved_window_size": self.saved_window_size,
            "enable_fullscreen_buttons": self.enable_fullscreen_buttons,
            "default_playback_speed": self.default_playback_speed,
            "enable_retries": self.enable_retries,
            "max_retry_attempts": self.max_retry_attempts,
            "initial_backoff_delay": self.initial_backoff_delay,
            "enable_quality_downgrade": self.enable_quality_downgrade,
            "drop_threshold": self.drop_threshold,
            "drop_window": self.drop_window,
            "downgrade_cooldown": self.downgrade_cooldown,
            "enable_auto_revert_hq": self.enable_auto_revert_hq,
            "stability_period": self.stability_period,
            "no_frame_timeout": self.no_frame_timeout,
            "archive_font": self.archive_font
        }
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, "w") as f:
                json.dump(config, f, indent=4)
        except PermissionError as e:
            logging.error(f"Permission denied saving config to {self.config_file}: {e}")
            messagebox.showerror("Error", f"Failed to save configuration due to permission issues: {e}")
        except Exception as e:
            logging.error(f"Failed to save config to {self.config_file}: {e}", exc_info=True)
            messagebox.showerror("Error", f"Failed to save configuration: {e}")

    def show_config_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Configuration")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        # Create a Notebook (tabbed interface)
        notebook = ttk.Notebook(dialog)
        notebook.pack(pady=10, padx=10, fill="both", expand=True)

        # Core Tab
        core_frame = ttk.Frame(notebook)
        notebook.add(core_frame, text="General")
        core_frame.columnconfigure(1, weight=1)

        # Advanced Tab
        advanced_frame = ttk.Frame(notebook)
        notebook.add(advanced_frame, text="Advanced")
        advanced_frame.columnconfigure(1, weight=1)

        # Shared grid options
        LBL  = dict(sticky="w",  padx=(12, 6), pady=4)
        WIDE = dict(sticky="we", padx=(0,  12), pady=4)
        SPAN = dict(sticky="w",  padx=(12, 12), pady=4, columnspan=2)

        # --- General Tab ---
        row = 0

        # Username
        tk.Label(core_frame, text="Username:", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        username_entry = tk.Entry(core_frame, width=32)
        username_entry.insert(0, self.username)
        username_entry.grid(row=row, column=1, **WIDE)
        row += 1

        # Password
        tk.Label(core_frame, text="Password:", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        password_entry = tk.Entry(core_frame, width=32)
        password_entry.insert(0, self.password)
        password_entry.grid(row=row, column=1, **WIDE)
        row += 1

        # Video Path
        tk.Label(core_frame, text="Video Path:", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        archive_entry = tk.Entry(core_frame, width=32)
        archive_entry.insert(0, self.archive_dir)
        archive_entry.grid(row=row, column=1, **WIDE)
        row += 1

        # Camera IPs and settings
        # Each cam row: label + IP entry in col 0-1, then HQ/Audio/PTZ checkboxes in a sub-frame in col 1
        ip_entries = []
        hq_checkboxes = []
        audio_checkboxes = []
        ptz_checkboxes = []
        for i in range(4):
            tk.Label(core_frame, text=f"Cam {i+1} IP:", font=("Arial", 10)).grid(row=row, column=0, **LBL)

            cam_frame = ttk.Frame(core_frame)
            cam_frame.grid(row=row, column=1, sticky="we", padx=(0, 12), pady=4)

            ip_entry = tk.Entry(cam_frame, width=16)
            ip_entry.insert(0, self.ips[i])
            ip_entry.pack(side="left")
            ip_entries.append(ip_entry)

            hq_var = tk.BooleanVar(value=self.hq_enabled[i])
            ttk.Checkbutton(cam_frame, text="HQ", variable=hq_var).pack(side="left", padx=(8, 0))
            hq_checkboxes.append(hq_var)

            audio_var = tk.BooleanVar(value=self.audio_enabled[i])
            ttk.Checkbutton(cam_frame, text="Audio", variable=audio_var).pack(side="left", padx=(8, 0))
            audio_checkboxes.append(audio_var)

            ptz_var = tk.BooleanVar(value=self.ptz_supported[i])
            ttk.Checkbutton(cam_frame, text="PTZ", variable=ptz_var).pack(side="left", padx=(8, 0))
            ptz_checkboxes.append(ptz_var)

            row += 1

        # PTZ Travel Distance
        tk.Label(core_frame, text="PTZ Travel Distance:", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        ptz_resolution_var = tk.IntVar(value=self.ptz_resolution)
        ttk.Combobox(
            core_frame, textvariable=ptz_resolution_var, values=[1, 2, 3, 4, 5], state="readonly", width=6
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        # Default Playback Speed
        tk.Label(core_frame, text="Default Playback Speed:", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        playback_speed_var = tk.DoubleVar(value=self.default_playback_speed)
        ttk.Combobox(
            core_frame, textvariable=playback_speed_var, values=self.speed_cycle, state="readonly", width=6
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        # Archive Font
        tk.Label(core_frame, text="Archive Font:", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        ARCHIVE_FONT_OPTIONS = ["Arial", "Helvetica", "Courier", "Times", "Verdana", "Tahoma", "Trebuchet MS"]
        archive_font_var = tk.StringVar(value=self.archive_font.title())
        ttk.Combobox(
            core_frame, textvariable=archive_font_var, values=ARCHIVE_FONT_OPTIONS, state="readonly", width=16
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        ttk.Separator(core_frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="we", padx=12, pady=6
        )
        row += 1

        # Show Stream Buttons
        fullscreen_buttons_var = tk.BooleanVar(value=self.enable_fullscreen_buttons)
        ttk.Checkbutton(core_frame, text="Show Stream Buttons", variable=fullscreen_buttons_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        # Save Window Size
        save_window_size_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(core_frame, text="Save Window Size", variable=save_window_size_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        # --- Advanced Tab ---
        row = 0

        # Stream Reliability section header
        tk.Label(advanced_frame, text="Stream Reliability", font=("Arial", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=(10, 2)
        )
        row += 1
        ttk.Separator(advanced_frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="we", padx=12, pady=(0, 4)
        )
        row += 1

        enable_retries_var = tk.BooleanVar(value=self.enable_retries)
        ttk.Checkbutton(advanced_frame, text="Enable Automatic Retries", variable=enable_retries_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        tk.Label(advanced_frame, text="Max Retry Attempts:", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        max_retry_attempts_entry = tk.Entry(advanced_frame, width=10)
        max_retry_attempts_entry.insert(0, str(self.max_retry_attempts))
        max_retry_attempts_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Initial Backoff Delay (s):", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        initial_backoff_delay_entry = tk.Entry(advanced_frame, width=10)
        initial_backoff_delay_entry.insert(0, str(self.initial_backoff_delay))
        initial_backoff_delay_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        # Quality Downgrading section header
        tk.Label(advanced_frame, text="Quality Downgrading", font=("Arial", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=(10, 2)
        )
        row += 1
        ttk.Separator(advanced_frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="we", padx=12, pady=(0, 4)
        )
        row += 1

        enable_quality_downgrade_var = tk.BooleanVar(value=self.enable_quality_downgrade)
        ttk.Checkbutton(advanced_frame, text="Enable Quality Downgrading", variable=enable_quality_downgrade_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        enable_auto_revert_hq_var = tk.BooleanVar(value=self.enable_auto_revert_hq)
        ttk.Checkbutton(advanced_frame, text="Enable Auto-Revert to HQ", variable=enable_auto_revert_hq_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        tk.Label(advanced_frame, text="Frame Drop Threshold:", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        drop_threshold_entry = tk.Entry(advanced_frame, width=10)
        drop_threshold_entry.insert(0, str(self.drop_threshold))
        drop_threshold_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Frame Drop Window (s):", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        drop_window_entry = tk.Entry(advanced_frame, width=10)
        drop_window_entry.insert(0, str(self.drop_window))
        drop_window_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Downgrade Cooldown (s):", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        downgrade_cooldown_entry = tk.Entry(advanced_frame, width=10)
        downgrade_cooldown_entry.insert(0, str(self.downgrade_cooldown))
        downgrade_cooldown_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Stability Period (s):", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        stability_period_entry = tk.Entry(advanced_frame, width=10)
        stability_period_entry.insert(0, str(self.stability_period))
        stability_period_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="No-Frame Timeout (s):", font=("Arial", 10)).grid(row=row, column=0, **LBL)
        no_frame_timeout_entry = tk.Entry(advanced_frame, width=10)
        no_frame_timeout_entry.insert(0, str(self.no_frame_timeout))
        no_frame_timeout_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        # VLC Options section header
        tk.Label(advanced_frame, text="VLC Options", font=("Arial", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=(10, 2)
        )
        row += 1
        ttk.Separator(advanced_frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="we", padx=12, pady=(0, 4)
        )
        row += 1

        vlc_params = tk.Text(advanced_frame, width=45, height=6)
        vlc_params.insert("1.0", ' '.join(self.vlcparams or self.DEFAULT_VLC_PARAMS))
        vlc_params.grid(row=row, column=0, columnspan=2, sticky="we", padx=(12, 12), pady=4)
        row += 1

        ttk.Separator(advanced_frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="we", padx=12, pady=6
        )
        row += 1

        # Debug mode
        debug_var = tk.BooleanVar(value=self.config_debug)
        ttk.Checkbutton(advanced_frame, text="Enable Debug Logging", variable=debug_var).grid(
            row=row, column=0, **SPAN
        )

        # Save and Cancel Buttons
        button_frame = ttk.Frame(dialog)
        button_frame.pack(pady=10)

        tk.Button(
            button_frame, text="Save", width=10, font=("Arial", 10),
            command=lambda: self.save_streams(
                username_entry, password_entry, ip_entries,
                hq_checkboxes, audio_checkboxes, ptz_checkboxes,
                fullscreen_buttons_var, debug_var, archive_entry, vlc_params,
                ptz_resolution_var, save_window_size_var, dialog,
                enable_retries_var, max_retry_attempts_entry, initial_backoff_delay_entry,
                enable_quality_downgrade_var, drop_threshold_entry, drop_window_entry,
                downgrade_cooldown_entry, enable_auto_revert_hq_var, stability_period_entry,
                playback_speed_var, archive_font_var, no_frame_timeout_entry
            )
        ).pack(side="left", padx=5)

        tk.Button(
            button_frame, text="Cancel", width=10, font=("Arial", 10),
            command=dialog.destroy
        ).pack(side="left", padx=5)

        dialog.update_idletasks()

    def save_streams(self, username_entry, password_entry, ip_entries, hq_checkboxes, audio_checkboxes, ptz_checkboxes, fullscreen_buttons_var, debug_var, archive_entry, vlc_params, ptz_resolution_var, save_window_size_var, dialog, enable_retries_var, max_retry_attempts_entry, initial_backoff_delay_entry, enable_quality_downgrade_var, drop_threshold_entry, drop_window_entry, downgrade_cooldown_entry, enable_auto_revert_hq_var, stability_period_entry, playback_speed_var, archive_font_var=None, no_frame_timeout_entry=None):
        old_fullscreen_buttons = self.enable_fullscreen_buttons
        self.username = username_entry.get().strip()
        self.password = password_entry.get().strip()
        self.archive_dir = archive_entry.get().strip()
        self.ips = [e.get().strip() for e in ip_entries]
        self.hq_enabled = [v.get() for v in hq_checkboxes]
        self.audio_enabled = [v.get() for v in audio_checkboxes]
        self.ptz_supported = [v.get() for v in ptz_checkboxes]
        self.enable_fullscreen_buttons = fullscreen_buttons_var.get()
        self.config_debug = debug_var.get()
        if archive_font_var is not None:
            ALLOWED_FONTS = {"arial", "helvetica", "courier", "times", "verdana", "tahoma", "trebuchet ms"}
            chosen = archive_font_var.get().lower()
            self.archive_font = chosen if chosen in ALLOWED_FONTS else "arial"
    
        # Save default playback speed
        try:
            self.default_playback_speed = float(playback_speed_var.get())
            if self.default_playback_speed not in self.speed_cycle:
                logging.warning(f"Invalid default_playback_speed: {self.default_playback_speed}, using default 1.0")
                self.default_playback_speed = 1.0
        except (ValueError, TypeError):
            logging.warning(f"Invalid default_playback_speed input, using default 1.0")
            self.default_playback_speed = 1.0

        # Save new stream reliability settings
        self.enable_retries = enable_retries_var.get()
        try:
            self.max_retry_attempts = int(max_retry_attempts_entry.get().strip())
            if self.max_retry_attempts < 1:
                logging.warning(f"Invalid max_retry_attempts: {self.max_retry_attempts}, using default 5")
                self.max_retry_attempts = 5
        except ValueError:
            logging.warning(f"Invalid max_retry_attempts input, using default 5")
            self.max_retry_attempts = 5
        try:
            self.initial_backoff_delay = float(initial_backoff_delay_entry.get().strip())
            if self.initial_backoff_delay <= 0:
                logging.warning(f"Invalid initial_backoff_delay: {self.initial_backoff_delay}, using default 2.0")
                self.initial_backoff_delay = 2.0
        except ValueError:
            logging.warning(f"Invalid initial_backoff_delay input, using default 2.0")
            self.initial_backoff_delay = 2.0
        self.enable_quality_downgrade = enable_quality_downgrade_var.get()
        try:
            self.drop_threshold = int(drop_threshold_entry.get().strip())
            if self.drop_threshold < 1:
                logging.warning(f"Invalid drop_threshold: {self.drop_threshold}, using default 8")
                self.drop_threshold = 8
        except ValueError:
            logging.warning(f"Invalid drop_threshold input, using default 8")
            self.drop_threshold = 8
        try:
            self.drop_window = float(drop_window_entry.get().strip())
            if self.drop_window <= 0:
                logging.warning(f"Invalid drop_window: {self.drop_window}, using default 30.0")
                self.drop_window = 30.0
        except ValueError:
            logging.warning(f"Invalid drop_window input, using default 30.0")
            self.drop_window = 30.0
        try:
            self.downgrade_cooldown = float(downgrade_cooldown_entry.get().strip())
            if self.downgrade_cooldown < 10:
                logging.warning(f"Invalid downgrade_cooldown: {self.downgrade_cooldown}, using default 120.0")
                self.downgrade_cooldown = 120.0
        except ValueError:
            logging.warning(f"Invalid downgrade_cooldown input, using default 120.0")
            self.downgrade_cooldown = 120.0
        self.enable_auto_revert_hq = enable_auto_revert_hq_var.get()
        try:
            self.stability_period = float(stability_period_entry.get().strip())
            if self.stability_period < 10:
                logging.warning(f"Invalid stability_period: {self.stability_period}, using default 300.0")
                self.stability_period = 300.0
        except ValueError:
            logging.warning(f"Invalid stability_period input, using default 300.0")
            self.stability_period = 300.0
        try:
            self.no_frame_timeout = float(no_frame_timeout_entry.get().strip())
            if self.no_frame_timeout < 5:
                logging.warning(f"Invalid no_frame_timeout: {self.no_frame_timeout}, using default 15.0")
                self.no_frame_timeout = 15.0
        except ValueError:
            logging.warning(f"Invalid no_frame_timeout input, using default 15.0")
            self.no_frame_timeout = 15.0

        try:
            ptz_resolution = ptz_resolution_var.get()
            if not isinstance(ptz_resolution, int) or ptz_resolution < 1 or ptz_resolution > 5:
                logging.warning(f"Invalid PTZ resolution: {ptz_resolution}, using default 3")
                ptz_resolution = 3
            self.ptz_resolution = ptz_resolution
        except Exception as e:
            logging.error(f"Failed to parse PTZ resolution: {e}, using default 3")
            self.ptz_resolution = 3

        # Handle VLC params from Text widget
        raw_params = vlc_params.get("1.0", "end-1c").strip()
        self.vlcparams = self.parse_vlcparams(raw_params)

        if save_window_size_var.get():
            if self.root.attributes("-fullscreen"):
                self.saved_window_size = "fullscreen"
            else:
                width = self.root.winfo_width()
                height = self.root.winfo_height()
                if width >= self.MIN_WIDTH and height >= self.MIN_HEIGHT:
                    self.saved_window_size = f"{width}x{height}"
                else:
                    logging.warning(f"Current window size {width}x{height} is below minsize {self.MIN_WIDTH}x{self.MIN_HEIGHT}, saving default")
                    self.saved_window_size = "1340x720"
         
        if self.debug_mode != self.config_debug:
            self.debug_mode = self.config_debug
            self._setup_logging(self.debug_mode)

        self.onvif_cams = {}
        self.ptz_click_counts = [0] * 4
        self.drop_timestamps = [[] for _ in range(4)]
        self.update_streams()
        self.save_config()

        # Update label bindings and rebuild config panel
        self.update_label_bindings()
        self.build_config_panel()

        dialog.destroy()
        threading.Thread(target=self.restart_streams, daemon=True).start()

    def check_network_connectivity(self, ip_input):
        """
        Check if the camera at the given IP (or IP:port) is reachable on its RTSP port.
        
        Supports formats like:
        - 192.168.1.100
        - 192.168.1.100:8554
        - 192.168.1.100:8554/cam1/stream1   ← cleans /cam1/stream1 part
        """
        try:
            # Split on colon, but only the first one (in case username:password@host:port)
            if ':' in ip_input:
                parts = ip_input.split(':', 1)  # split only on first colon
                host = parts[0].strip()
                port_str = parts[1].strip()

                # Remove any trailing path (/cam1/stream1 etc.)
                if '/' in port_str:
                    port_str = port_str.split('/', 1)[0].strip()

                # Try to convert to integer port
                try:
                    port = int(port_str)
                    if not (1 <= port <= 65535):
                        port = 554  # invalid port → fallback
                        logging.warning(f"Invalid port value '{port_str}' for {host} → using default 554")
                except ValueError:
                    port = 554  # not a number → fallback
                    logging.warning(f"Non-numeric port '{port_str}' for {host} → using default 554")
            else:
                host = ip_input.strip()
                port = 554

            # Now perform the actual connection check
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            result = sock.connect_ex((host, port))
            sock.close()

            if result == 0:
                logging.debug(f"Network check: Camera at {host}:{port} is reachable")
                return True
            else:
                logging.warning(f"Network check: Camera at {host}:{port} is not reachable (error code: {result})")
                return False

        except Exception as e:
            logging.warning(f"Network check failed for '{ip_input}': {e}")
            return False

    def debounce_layout_update(self):
        """Debounce layout updates to prevent excessive calls."""
        if hasattr(self, '_layout_debounce_id'):
            self.root.after_cancel(self._layout_debounce_id)
        self._layout_debounce_id = self.root.after(100, self.update_layout)

    def update_stream(self, index: int) -> None:
        if not 0 <= index <= 3:
            return
        
        # Ensure self.streams is initialized with enough slots
        if len(self.streams) < 4:
            self.streams.extend([""] * (4 - len(self.streams)))
        
        ip = self.ips[index]
        hq = self.hq_enabled[index]
        
        # Generate the stream URL
        if ip and self.username and self.password:
            stream = f"rtsp://{self.username}:{self.password}@{ip}/stream{'2' if not hq else '1'}"
            # Check if stream is unique (not already in other indices)
            seen_urls = {s for i, s in enumerate(self.streams) if s and i != index}
            if stream in seen_urls:
                stream = ""
        else:
            stream = ""
        
        # Update the specific index
        self.streams[index] = stream
        logging.info(f"Updated stream at index {index}: {stream}")

    def update_streams(self):
        self.streams = []
        seen_urls = set()
        for ip, hq in zip(self.ips, self.hq_enabled):
            if ip and self.username and self.password:
                stream = f"rtsp://{self.username}:{self.password}@{ip}/stream{'2' if not hq else '1'}"
                if stream in seen_urls:
                    stream = ""
                else:
                    seen_urls.add(stream)
            else:
                stream = ""
            self.streams.append(stream)
        logging.info(f"Updated streams: {self.streams}")

    def create_icon(self, icon_type, opacity=1.0):
        size = (40, 40) if icon_type in ["config", "back", "left", "right", "up", "down", "fullscreen", "minimize", "play", "resize"] else (100, 100) if icon_type in ["folder", "archive", "back"] else (40, 40)
        img = Image.new("RGBA", size, (0, 0, 0, 255))
        draw = ImageDraw.Draw(img)
        
        # Helper function to adjust color opacity
        def adjust_color(color, opacity):
            if color == "white":
                return (255, 255, 255, int(255 * opacity))
            elif color == "black":
                return (0, 0, 0, int(255 * opacity))
            return color

        if icon_type == "config":
            draw.rectangle((18, 22, 22, 34), fill="white")
            draw.rectangle((12, 10, 28, 20), fill="white")
        elif icon_type == "fullscreen":
            draw.rectangle((8, 8, 32, 32), outline="white", width=2)
            draw.line((10, 10, 13, 10), fill="white", width=2)
            draw.line((10, 10, 10, 13), fill="white", width=2)
            draw.line((30, 30, 27, 30), fill="white", width=2)
            draw.line((30, 30, 30, 27), fill="white", width=2)
        elif icon_type == "minimize":
            draw.rectangle((8, 8, 32, 32), outline="white", width=2)
            draw.line((8, 20, 32, 20), fill="white", width=2)
            draw.line((20, 8, 20, 32), fill="white", width=2)
        elif icon_type == "pause":
            draw.rectangle((12, 8, 18, 32), fill="white")
            draw.rectangle((22, 8, 28, 32), fill="white")
        elif icon_type == "speed":
            draw.polygon([(10, 8), (20, 20), (10, 32)], fill="white")
            draw.polygon([(20, 8), (30, 20), (20, 32)], fill="white")
        elif icon_type == "replay":
            draw.arc((10, 10, 30, 30), start=45, end=315, fill="white", width=3)
            draw.polygon([(28, 12), (32, 16), (28, 20)], fill="white")
        elif icon_type == "rewind":
            draw.polygon([(30, 8), (20, 20), (30, 32)], fill="white")
            draw.polygon([(20, 8), (10, 20), (20, 32)], fill="white")
        elif icon_type == "exit":
            draw.line((12, 12, 28, 28), fill="white", width=3)
            draw.line((12, 28, 28, 12), fill="white", width=3)
        elif icon_type == "resize":
            draw.rectangle((10, 10, 30, 30), outline="white", width=2)
            draw.line((10, 10, 8, 8), fill="white", width=2)
            draw.line((10, 10, 12, 8), fill="white", width=2)
            draw.line((30, 30, 32, 32), fill="white", width=2)
            draw.line((30, 30, 28, 32), fill="white", width=2)
            draw.line((30, 10, 32, 8), fill="white", width=2)
            draw.line((30, 10, 28, 8), fill="white", width=2)
            draw.line((10, 30, 8, 32), fill="white", width=2)
            draw.line((10, 30, 12, 32), fill="white", width=2)
        elif icon_type == "folder":
            draw.rectangle((20, 30, 80, 80), fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)
            draw.polygon([(20, 30), (30, 20), (40, 20), (40, 30)], fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)
            draw.line((20, 30, 40, 50), fill=adjust_color("black", opacity), width=2)
        elif icon_type == "archive":
            draw.rectangle((20, 20, 80, 80), outline=adjust_color("white", opacity), width=2)
            draw.polygon([(35, 25), (35, 75), (75, 50)], fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)
        elif icon_type == "play":
            draw.polygon([(12, 8), (32, 20), (12, 32)], outline="white", width=2, fill="white")
        elif icon_type == "disk":
            draw.rectangle([(8, 8), (32, 32)], outline="white", width=2, fill="black")
            draw.rectangle([(12, 10), (28, 18)], outline="white", width=1, fill="white")
            draw.rectangle([(16, 24), (24, 30)], outline="white", width=1, fill="white")      
        elif icon_type == "back":
            draw.polygon([(30, 10), (15, 20), (30, 30)], fill="white")
        elif icon_type == "left":
            draw.polygon([(30, 10), (15, 20), (30, 30)], fill="white")
        elif icon_type == "right":
            draw.polygon([(10, 10), (25, 20), (10, 30)], fill="white")
        elif icon_type == "up":
            draw.polygon([(10, 30), (20, 15), (30, 30)], fill="white")
        elif icon_type == "down":
            draw.polygon([(10, 10), (20, 25), (30, 10)], fill="white")
        elif icon_type == "fullscreen":
            draw.rectangle((8, 8, 32, 32), outline="white", width=2)
            draw.line((10, 10, 13, 10), fill="white", width=2)
            draw.line((10, 10, 10, 13), fill="white", width=2)
            draw.line((30, 30, 27, 30), fill="white", width=2)
            draw.line((30, 30, 30, 27), fill="white", width=2)
        elif icon_type == "minimize":
            draw.rectangle((8, 8, 32, 32), outline="white", width=2)
            draw.line((8, 20, 32, 20), fill="white", width=2)
            draw.line((20, 8, 20, 32), fill="white", width=2)
        return ImageTk.PhotoImage(img)

    def get_day_folder_icon(self, day_abbrev, is_clicked):
        """Return a (cached) folder icon with the weekday abbreviation
        (Mon/Tue/Wed/...) drawn prominently across the front, so day
        folders are easier to scan at a glance than a bare date label.
        """
        cache_key = (day_abbrev, is_clicked)
        cached = self.day_folder_icon_cache.get(cache_key)
        if cached is not None:
            return cached

        opacity = 0.6 if is_clicked else 1.0
        size = (100, 100)
        img = Image.new("RGBA", size, (0, 0, 0, 255))
        draw = ImageDraw.Draw(img)

        def adjust_color(color, opacity):
            if color == "white":
                return (255, 255, 255, int(255 * opacity))
            elif color == "black":
                return (0, 0, 0, int(255 * opacity))
            return color

        # Folder body, same shape as the standard folder icon.
        draw.rectangle((20, 30, 80, 80), fill=adjust_color("white", opacity),
                        outline=adjust_color("white", opacity), width=3)
        draw.polygon([(20, 30), (30, 20), (40, 20), (40, 30)],
                      fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)

        # Weekday abbreviation drawn across the front of the folder in a
        # dark color so it stands out against the white folder body.
        font = None
        for font_path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
            "arialbd.ttf",
        ):
            try:
                font = ImageFont.truetype(font_path, 24)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        text = day_abbrev
        text_color = adjust_color("black", opacity)
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
        except Exception:
            text_w, text_h = draw.textsize(text, font=font)

        text_x = 20 + (60 - text_w) // 2
        text_y = 30 + (50 - text_h) // 2 - 2
        draw.text((text_x, text_y), text, fill=text_color, font=font)

        photo = ImageTk.PhotoImage(img)
        self.day_folder_icon_cache[cache_key] = photo
        return photo

    def bind_stream_label(self, index):
        """Bind the label for a given stream based on its state."""
        try:
            # Unbind any existing click event
            self.labels[index].unbind("<Button-1>")
            
            # Check if stream is in failed state
            if self.labels[index].cget("text") == "Stream Failed, click to reconnect":
                # Bind retry action for failed streams
                self.labels[index].bind("<Button-1>", lambda event, idx=index: self.retry_stream_connection(idx))
                logging.debug(f"Stream {index}: Bound label to retry connection (failed state)")
            else:
                # Bind fullscreen action if fullscreen buttons are disabled and stream is active
                if not self.enable_fullscreen_buttons and self.streams[index]:
                    self.labels[index].bind("<Button-1>", lambda event, idx=index: self.handle_stream_click(idx))
                    logging.debug(f"Stream {index}: Bound label to fullscreen action")
                else:
                    logging.debug(f"Stream {index}: No binding applied (fullscreen buttons enabled or stream inactive)")
        except Exception as e:
            logging.error(f"Stream {index}: Failed to bind stream label: {e}")

    def update_label_bindings(self):
        """Update label bindings for all streams based on their state and fullscreen button settings."""
        for i in range(4):
            self.bind_stream_label(i)
        logging.debug("Updated label bindings for all streams")

    def enter_fullscreen(self):
        logging.debug("Entering fullscreen mode via up key")
        if self.is_fullscreen:
            logging.debug("Already in fullscreen mode, no action taken")
            return
        for i in range(4):
            if self.streams[i]:
                self.is_fullscreen = True
                self.fullscreen_index = i
                for j in range(4):
                    if not self.streams[j] or not self.audio_enabled[j]:
                        continue
                    if self.is_archive_mode[j]:
                        logging.debug(f"Stream {j}: Skipping audio management due to archive mode")
                        continue
                    if j == self.fullscreen_index:
                        self.set_audio_state(j, mute=False)
                    else:
                        self.set_audio_state(j, mute=True)
                self.build_config_panel()
                logging.info(f"Entered fullscreen mode for stream {i}")
                break
        else:
            logging.debug("No enabled streams to enter fullscreen mode")

    def exit_fullscreen(self, event=None):
        logging.debug(f"exit_fullscreen called (event={event}, is_fullscreen={self.is_fullscreen})")
        if self.is_fullscreen:
            idx = self.fullscreen_index

            # While fullscreen and in archive mode, right-click acts as a
            # "back" button: playing clip -> clip browser -> folder
            # browser -> live feed (still fullscreen). go_back() handles
            # each of these steps, including stopping any playing clip
            # before its embedded VLC widget is destroyed (avoiding a
            # BadWindow X error) and exiting archive mode entirely once
            # at the archive root (which returns to the live view while
            # remaining fullscreen).
            if idx is not None and idx >= 0 and self.is_archive_mode[idx]:
                self.go_back(idx)
                return

            # Already on the live feed: right-click exits fullscreen to
            # the grid view.
            self.is_fullscreen = False
            self.fullscreen_index = -1

            for i in range(4):
                if self.audio_enabled[i]:
                    self.set_audio_state(i, mute=True)

            self.build_config_panel()
            logging.debug("Exiting fullscreen mode")
        else:
            any_archive_mode = False
            for i in range(4):
                if self.is_archive_mode[i]:
                    any_archive_mode = True
                    self.toggle_archive_mode(i)

            if any_archive_mode:
                self.build_config_panel()
                logging.debug("Exiting archive mode")

    def build_config_panel(self):
        try:
            # Initialize config panel if not exists
            if not self.config_panel:
                self.config_panel = tk.Frame(self.root, bg="#222222", width=60)
                logging.debug("Created new config panel")

            # Initialize PTZ buttons if not exists
            if not self.ptz_buttons:
                self.ptz_buttons = []
                self.ptz_images = []
                for direction in ["up", "down", "left", "right"]:
                    img = self.icon_cache[direction]
                    button = tk.Button(
                        self.config_panel, image=img, bg="#222222", bd=0, cursor="hand2",
                        command=lambda d=direction: self.start_ptz_move(d)
                    )
                    button.bind("<ButtonRelease-1>", lambda event, d=direction: self.stop_ptz_move(d))
                    self.ptz_buttons.append(button)
                    self.ptz_images.append(img)
                    logging.debug(f"Created PTZ button: {direction}")

            # Initialize exit fullscreen button
            if not self.exit_fullscreen_button:
                self.exit_fullscreen_image = self.icon_cache["minimize"]
                self.exit_fullscreen_button = tk.Button(
                    self.config_panel, image=self.exit_fullscreen_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.exit_fullscreen, cursor="hand2"
                )
                self.exit_fullscreen_button.bind("<Button-1>", lambda e: logging.debug("Clicked exit_fullscreen button"))
                logging.debug("Created exit fullscreen button")

            # Initialize config button
            if not self.config_button:
                self.config_img = self.icon_cache["config"]
                self.config_button = tk.Button(
                    self.config_panel, image=self.config_img, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.show_config_dialog, cursor="hand2"
                )
                self.config_button.bind("<Button-1>", lambda e: logging.debug("Clicked config button"))
                logging.debug("Created config button")

            # Initialize archive mode button
            if not self.archive_mode_button:
                self.archive_mode_image = self.icon_cache["disk"]
                self.archive_mode_button = tk.Button(
                    self.config_panel, image=self.archive_mode_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.toggle_all_archive_mode, cursor="hand2"
                )
                self.archive_mode_button.bind("<Button-1>", lambda e: logging.debug("Clicked archive mode button"))
                logging.debug("Created archive mode button")

            # Initialize fullscreen buttons
            for i in range(4):
                if not self.fullscreen_buttons[i]:
                    img = self.icon_cache["fullscreen"]
                    self.fullscreen_buttons[i] = tk.Button(
                        self.panels[i], image=img, bg="black", bd=0, cursor="hand2",
                        command=lambda idx=i: self.handle_stream_click(idx),
                        state="disabled" if not self.enable_fullscreen_buttons else "normal"
                    )
                    self.fullscreen_buttons[i].bind("<Button-1>", lambda e, idx=i: logging.debug(f"Clicked fullscreen_{idx} button"))
                    self.fullscreen_images[i] = img
                    logging.debug(f"Created fullscreen button for stream {i}")

            # Initialize archive button if needed
            if self.is_fullscreen and self.fullscreen_index is not None:
                if not self.archive_buttons[self.fullscreen_index]:
                    img = self.icon_cache["disk"]
                    self.archive_images[self.fullscreen_index] = img
                    self.archive_buttons[self.fullscreen_index] = tk.Button(
                        self.config_panel, image=img, bg="#222222", bd=0, cursor="hand2",
                        command=lambda idx=self.fullscreen_index: self.toggle_archive_mode(idx)
                    )
                    self.archive_buttons[self.fullscreen_index].bind(
                        "<Button-1>", lambda e, idx=self.fullscreen_index: logging.debug(f"Clicked archive_{idx} button")
                    )
                    logging.debug(f"Created archive button for stream {self.fullscreen_index}")

            # Forget all buttons before re-packing
            for button in self.ptz_buttons + [self.exit_fullscreen_button, self.config_button, self.archive_mode_button] + \
                          [b for b in self.archive_buttons if b] + [b for b in self.fullscreen_buttons if b]:
                button.pack_forget()
                if button in self.fullscreen_buttons:
                    button.place_forget()

            # Update PTZ button states
            ptz_enabled = (self.is_fullscreen and self.fullscreen_index is not None and
                           self.ptz_supported[self.fullscreen_index] and not self.is_archive_mode[self.fullscreen_index])
            for button in self.ptz_buttons:
                button.config(state="normal" if ptz_enabled else "disabled")

            # Pack buttons based on state
            if self.is_fullscreen and self.fullscreen_index is not None:
                if (self.archive_dir and self.streams[self.fullscreen_index]):
                    self.archive_buttons[self.fullscreen_index].pack(pady=5, padx=10)
                if ptz_enabled:
                    for button in self.ptz_buttons:
                        button.pack(pady=5, padx=10)
                self.exit_fullscreen_button.pack(pady=5, padx=10)
                self.config_button.pack(pady=5, padx=10)
            else:
                # Pack archive mode button only in grid mode if archive_dir is valid
                if self.archive_dir:
                    self.archive_mode_button.pack(pady=5, padx=10)
                self.config_button.pack(pady=10, padx=10)
                # Place fullscreen buttons in grid mode
                for i in range(4):
                    if (self.enable_fullscreen_buttons and self.ips[i] and not self.is_archive_mode[i]):
                        self.fullscreen_buttons[i].configure(state="normal")
                        self.fullscreen_buttons[i].place(relx=1.0, rely=1.0, x=-35, y=-35, anchor="se")
                        self.fullscreen_buttons[i].lift()
                    elif self.fullscreen_buttons[i]:
                        self.fullscreen_buttons[i].place_forget()

            logging.debug("Config panel built successfully")
        except Exception as e:
            logging.debug(f"Error building config panel: {e}")

    
    def iterate_streams(self, direction):
        print(f"Iterating streams, direction={direction}")
        if not self.is_fullscreen:
            return
        if self.fullscreen_index is None:
            return

        enabled_streams = [i for i in range(4) if self.streams[i]]
        if not enabled_streams:
            return

        try:
            current_pos = enabled_streams.index(self.fullscreen_index)
        except ValueError:
            return

        new_pos = (current_pos + direction) % len(enabled_streams)
        new_index = enabled_streams[new_pos]
        self.fullscreen_index = new_index

        for i in range(4):
            if not self.streams[i] or not self.audio_enabled[i]:
                continue
            if i == self.fullscreen_index and not self.is_archive_mode[i]:
                self.set_audio_state(i, mute=False)
            else:
                self.set_audio_state(i, mute=True)

        self.build_config_panel()
        self.debounce_layout_update()
        print(f"Iterated to stream {new_index} (direction={direction})")


    def init_ui(self):
        """
        Initialize the user interface, applying saved window size and centering.
        """
        # Create config_panel
        self.config_panel = tk.Frame(self.root, bg="#222222", width=60)
        self.config_panel.pack(side="right", fill="y")
        
        # Create grid_frame
        self.grid_frame = tk.Frame(self.root, bg="#222222")
        self.grid_frame.pack(fill="both", expand=True)

        initial_width, initial_height = 960, 540
        for i in range(4):
            panel = tk.Frame(self.grid_frame, bg="black")
            self.panels[i] = panel
            label = tk.Label(panel, bg="black", text="Disabled", fg="white")
            self.labels[i] = label
            label.pack(fill="both", expand=True)
            x = 0 if i in (0, 2) else initial_width + 5
            y = 0 if i in (0, 1) else initial_height + 5
            panel.place(x=x, y=y, width=initial_width, height=initial_height)
            self.panel_sizes[i] = (initial_width, initial_height)
            # Use cached archive image
            self.archive_images[i] = self.icon_cache["disk"]
            self.archive_buttons[i] = None  # Created in build_config_panel
            self.archive_canvas[i] = tk.Canvas(panel, bg="#222222", highlightthickness=0)
            # Use cached fullscreen image
            self.fullscreen_images[i] = self.icon_cache["fullscreen"]
            self.fullscreen_buttons[i] = None  # Initialized later

        # Set initial label bindings
        self.update_label_bindings()

        # Apply saved window size and center the window
        self.apply_window_size(self.saved_window_size)

        # Key bindings
        def handle_fullscreen_toggle(event):
            is_fullscreen = not self.root.attributes("-fullscreen")
            self.root.attributes("-fullscreen", is_fullscreen)

        self.root.bind("<Alt-Return>", handle_fullscreen_toggle)
        self.root.bind("<Shift_L>", handle_fullscreen_toggle)
        self.root.bind("<Escape>", lambda e: self.cleanup())
        self.root.bind("<Up>", lambda e: self.enter_fullscreen())
        self.root.bind("<Down>", lambda e: self.exit_fullscreen())
        self.root.bind("<Button-3>", lambda e: self.exit_fullscreen())
        self.root.bind("<Left>", lambda e: self.iterate_streams(-1))
        self.root.bind("<Right>", lambda e: self.iterate_streams(1))
        self.root.bind("<Configure>", lambda e: self.debounce_layout_update())

        # Archive view navigation: Page Up/Down change page, Backspace
        # goes back to the parent folder, when in fullscreen archive mode.
        self.root.bind("<Prior>", lambda e: self.archive_change_page_shortcut(-1))   # Page Up
        self.root.bind("<Next>", lambda e: self.archive_change_page_shortcut(1))     # Page Down
        self.root.bind("<BackSpace>", lambda e: self.archive_go_back_shortcut())

        # Help overlay
        self.root.bind("<KeyPress-h>", lambda e: self.toggle_help_overlay())
        self.root.bind("<KeyPress-H>", lambda e: self.toggle_help_overlay())
        self.root.bind("<F1>", lambda e: self.toggle_help_overlay())

        # Initialize buttons via build_config_panel
        self.build_config_panel()

    def restart_streams(self):
        self.start_streams()

    def set_audio_state(self, index, mute=True):
        if not self.audio_enabled[index]:
            logging.debug(f"Stream {index}: Audio disabled, skipping audio state change")
            return

        # Check if stream is initializing
        with self.stream_init_lock:
            if self.stream_initializing[index]:
                logging.debug(f"Stream {index}: Skipped audio state change due to initializing state")
                return

        if self.media_players[index]:
            try:
                state = self.media_players[index].get_state()
                if state in (vlc.State.Error, vlc.State.Ended, vlc.State.Stopped):
                    logging.debug(f"Stream {index}: Skipped audio state change due to invalid state: {state}")
                    return
                self.media_players[index].audio_set_mute(mute)
                logging.debug(f"Stream {index}: Set audio mute={mute}")
            except Exception as e:
                logging.error(f"Stream {index}: Failed to set python-vlc audio state: {e}")
        else:
            logging.debug(f"Stream {index}: No media player, skipping audio state change")

    def update_stream_label(self, index, text, fg="white", image=""):
        """Update the stream label in a thread-safe manner."""
        try:
            self.root.after(0, lambda: self.labels[index].configure(image=image, text=text, fg=fg))
            logging.debug(f"Stream {index}: Updated label to '{text}'")
        except Exception as e:
            logging.error(f"Stream {index}: Failed to update label: {e}")

    def try_init_stream_with_retries(self, index):
        """Attempt to initialize a stream with retries, managing all label updates.

        Quality downgrades made here or by the monitor are session-only: hq_enabled
        is changed in memory but never saved, so the next app start uses the
        user-configured value from the config file.
        """
        with self.stream_init_lock:
            if self.stream_initializing[index]:
                logging.warning(f"Stream {index}: Already initializing, skipping retry")
                return False
            self.stream_initializing[index] = True
            logging.debug(f"Stream {index}: Marked as initializing")

        try:
            if not self.ips[index] or not self.streams[index]:
                self.update_stream_label(index, "Disabled")
                if self.fullscreen_buttons[index]:
                    self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget())
                logging.info(f"Stream {index}: Disabled (no IP or URL)")
                return False

            max_attempts = self.max_retry_attempts if self.enable_retries else 1
            backoff_delay = self.initial_backoff_delay
            max_backoff = 30.0

            for attempt in range(max_attempts):
                # Check if we have been asked to abort (e.g. user switched to
                # archive mode while we were retrying or sleeping in backoff).
                if self.stream_cleanup_events[index].is_set():
                    logging.info(f"Stream {index}: Abort signal received, stopping init")
                    return False

                # On the final retry attempt, drop to LQ for this session only
                if attempt == max_attempts - 1 and self.enable_quality_downgrade and self.hq_enabled[index]:
                    logging.info(f"Stream {index}: Final retry, switching to low quality for this session")
                    self.hq_enabled[index] = False
                    self.update_stream(index)
                    self.update_stream_label(index, "Final attempt, trying Low Quality...")
                elif attempt > 0:
                    self.update_stream_label(index, f"Retrying... (Attempt {attempt+1}/{max_attempts})")
                else:
                    self.update_stream_label(index, "Loading...")

                logging.info(f"Stream {index}: Attempt {attempt+1}/{max_attempts}")

                if not self.check_network_connectivity(self.ips[index]):
                    logging.warning(f"Stream {index}: Network check failed")
                    if attempt == max_attempts - 1:
                        self.update_stream_label(index, "Network Unreachable")
                        if self.fullscreen_buttons[index]:
                            self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget())
                        logging.error(f"Stream {index}: Network unreachable after all attempts")
                        return False
                    time.sleep(backoff_delay)
                    backoff_delay = min(backoff_delay * 2, max_backoff)
                    continue

                self.cleanup_stream(index)
                if self.init_stream(index):
                    logging.info(f"Stream {index}: Initialized successfully")
                    self.set_audio_state(index, mute=True)
                    self.root.after(0, lambda: self.bind_stream_label(index))
                    return True

                if attempt == max_attempts - 1:
                    self.cleanup_stream(index)
                    self.update_stream_label(index, "Stream Failed, click to reconnect")
                    self.bind_retry_connection(index)
                    if self.fullscreen_buttons[index]:
                        self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget())
                    logging.error(f"Stream {index}: All attempts failed")
                    return False

                logging.info(f"Stream {index}: Attempt {attempt+1} failed, retrying in {backoff_delay:.2f}s")
                time.sleep(backoff_delay)
                backoff_delay = min(backoff_delay * 2, max_backoff)

            return False
        except Exception as e:
            logging.error(f"Stream {index}: Unexpected error during retry: {e}")
            return False
        finally:
            with self.stream_init_lock:
                self.stream_initializing[index] = False
                logging.debug(f"Stream {index}: Cleared initializing state")

    def build_vlc_instance_args(self, extra_args=None):
        """Build the common libvlc instance argument list, with optional
        per-call extra args (e.g. archive-specific caching flags).

        Centralizing this avoids the two call sites (live stream init and
        archive playback) drifting apart - e.g. the --no-xlib /
        --vout=gl bug where one path was fixed and the other forgotten.
        """
        args = [
            '--no-video-title-show',
            '--rtsp-tcp',
            '--no-skip-frames',
            '--network-caching=2000',
            '--no-plugins-cache',
        ]
        if extra_args:
            args.extend(extra_args)
        args.extend(self.vlcparams)
        if sys.platform.startswith('win'):
            args.append('--aout=directsound')
        else:
            args.extend(['--aout=pulse', '--vout=gl'])
        if self.debug_mode:
            args.append('--verbose=2')
        return args

    def _vlc_log_handler(self, data, level, ctx, fmt, args):
        """libvlc log callback - forwards libvlc's internal log lines into
        our own logging output so hardware-decode / vout issues are visible
        without needing to run the vlc CLI manually."""
        try:
            buf = ctypes.create_string_buffer(2048)
            libc = ctypes.CDLL(None)
            libc.vsnprintf(buf, ctypes.c_size_t(len(buf)), fmt, args)
            message = buf.value.decode(errors="replace")
        except Exception:
            message = "<unformattable libvlc log message>"

        level_map = {
            0: logging.DEBUG,    # LIBVLC_DEBUG
            1: logging.INFO,     # LIBVLC_NOTICE
            2: logging.WARNING,  # LIBVLC_WARNING
            3: logging.ERROR,    # LIBVLC_ERROR
        }
        logging.log(level_map.get(level, logging.DEBUG), f"libvlc: {message}")

    def attach_vlc_logging(self, instance):
        """Attach the libvlc log callback to an instance, if debug logging
        is enabled. Safe to call even if libvlc_vprintf/log_set are
        unavailable on this python-vlc version."""
        if not self.debug_mode:
            return
        try:
            log_cb = vlc.LogCb(self._vlc_log_handler)
            # Keep a reference so it isn't garbage-collected while in use
            self._vlc_log_cb = log_cb
            instance.log_set(log_cb, None)
        except Exception as e:
            logging.debug(f"Could not attach libvlc log callback: {e}")

    def init_stream(self, index):
        """Initialize a stream using Python-VLC (libvlc auto-selects hardware decode if available)."""
        logging.info(f"Stream {index}: Initializing stream")
        try:
            xid = self.labels[index].winfo_id()
        except Exception as e:
            logging.error(f"Stream {index}: Failed to get window ID: {e}")
            return False

        timeout = 8
        start_wait = time.time()
        check_interval = 0.5
        required_frames = 5
        frame_times = []

        try:
            instance = vlc.Instance(self.build_vlc_instance_args(
                ['--live-caching=2000']
            ))
            if not instance:
                raise RuntimeError("Failed to create VLC instance")
            self.attach_vlc_logging(instance)
            self.vlc_instance = instance
            player = instance.media_player_new()
            if not player:
                raise RuntimeError("Failed to create VLC media player")
            self.media_players[index] = player
            media = instance.media_new(self.streams[index])
            player.set_media(media)
            player.set_xwindow(xid) if sys.platform.startswith("linux") else player.set_hwnd(xid)

            if player.play() == -1:
                raise RuntimeError("Failed to start VLC player")

            while time.time() - start_wait < timeout:
                stats = vlc.MediaStats()
                if player.get_media().get_stats(stats):
                    current_displayed = stats.displayed_pictures
                    new_frames = current_displayed - self.last_displayed_frames[index]
                    self.last_displayed_frames[index] = current_displayed
                    if new_frames > 0:
                        frame_times.append((time.time(), new_frames))
                        frame_times = [(t, f) for t, f in frame_times if time.time() - t < self.drop_window]
                        recent_frames = sum(f for _, f in frame_times)
                        logging.debug(f"Stream {index}: New frames: {new_frames}, Recent: {recent_frames}")
                        if recent_frames >= required_frames:
                            for _ in range(5):
                                time.sleep(0.5)
                                width, height = player.video_get_size(0) or (0, 0)
                                if width > 0 and height > 0:
                                    self.frame_shapes[index] = (width, height)
                                    break
                            player.video_set_scale(0)
                            self.last_dropped_frames[index] = stats.lost_pictures
                            threading.Thread(target=self.monitor_stream, args=(index, player), daemon=True).start()
                            return True
                if player.get_state() in (vlc.State.Error, vlc.State.Ended):
                    raise RuntimeError("Stream encountered error or ended")
                time.sleep(check_interval)
            logging.warning(f"Stream {index}: No frames detected within {timeout}s")
            return False
        except Exception as e:
            logging.error(f"Stream {index}: Python-VLC initialization failed: {e}")
            return False

    def cleanup_stream(self, index):
        """Clean up stream resources."""
        logging.info(f"Stream {index}: Cleaning up")
        self.stream_cleanup_events[index].set()

        try:
            # Stop media player
            if self.media_players[index]:
                try:
                    if self.media_players[index].get_state() not in (vlc.State.Stopped, vlc.State.Ended, vlc.State.Error):
                        self.media_players[index].stop()
                    self.media_players[index].release()
                    logging.debug(f"Stream {index}: Released media player")
                except Exception as e:
                    logging.error(f"Stream {index}: Error releasing media player: {e}")
                self.media_players[index] = None

            # Release VLC instance if unused
            if self.vlc_instance and not any(self.media_players[i] for i in range(4) if i != index):
                try:
                    self.vlc_instance.release()
                    logging.debug(f"Stream {index}: Released VLC instance")
                except Exception as e:
                    logging.error(f"Stream {index}: Error releasing VLC instance: {e}")
                self.vlc_instance = None

            # Reset stream state
            self.frame_shapes[index] = (0, 0)
            self.drop_timestamps[index] = []
            self.last_dropped_frames[index] = 0
            self.last_displayed_frames[index] = 0

            logging.info(f"Stream {index}: Cleanup completed")
        except Exception as e:
            logging.error(f"Stream {index}: Cleanup failed: {e}")
        finally:
            self.stream_cleanup_events[index].clear()
            logging.debug(f"Stream {index}: Cleared cleanup event")

    def _retry_stream_connection_thread(self, index):
        """Thread function to retry stream connection and restore bindings."""
        try:
            # Attempt to reinitialize the stream
            success = self.try_init_stream_with_retries(index)
            if success:
                logging.info(f"Stream {index}: Retry successful, restoring bindings")
                # Restore default label bindings (including fullscreen if enabled)
                self.root.after(0, lambda: self.bind_stream_label(index))
                # Update layout to ensure stream is displayed
                self.root.after(0, self.update_layout)
            else:
                logging.warning(f"Stream {index}: Retry failed, keeping retry binding")
                # Ensure retry binding remains
                self.root.after(0, lambda: self.bind_retry_connection(index))
        except Exception as e:
            logging.error(f"Stream {index}: Error during retry: {e}")
            # Restore retry binding on error
            self.root.after(0, lambda: self.bind_retry_connection(index))

    def retry_stream_connection(self, index):
        """Retry the stream connection and restore bindings if successful."""
        logging.info(f"Stream {index}: Retrying connection due to label click")
        # Start retry in a separate thread to avoid blocking the UI
        threading.Thread(target=self._retry_stream_connection_thread, args=(index,), daemon=True).start()

    def bind_retry_connection(self, index):
        """Bind the label to retry the stream connection."""
        try:
            # Unbind any existing click event
            self.labels[index].unbind("<Button-1>")
            # Bind retry action
            self.labels[index].bind("<Button-1>", lambda event, idx=index: self.retry_stream_connection(idx))
            logging.debug(f"Stream {index}: Bound label to retry stream connection")
        except Exception as e:
            logging.error(f"Stream {index}: Failed to bind retry connection: {e}")

    def monitor_stream(self, index, player):
        """Monitor a live stream for frame drops and state changes.

        Drop detection uses a single sliding-window list (drop_timestamps) that
        tracks how many polling intervals within drop_window seconds recorded any
        dropped frames.  drop_threshold is therefore "N bad polling ticks in the
        window", not raw frame count — keep that in mind when tuning.

        Quality changes are session-only: hq_enabled is mutated in memory but
        save_config() is never called here, so the user's configured quality is
        restored on the next app start.
        """
        logging.info(f"Monitoring stream {index}")

        last_check = time.time()
        last_stream_switch = 0        # tracks when we last switched quality
        last_stable_time = time.time()
        last_frame_time = time.time()
        no_frame_timeout = self.no_frame_timeout

        while self.running and self.media_players[index]:
            # Wait for cleanup event or poll timeout
            if self.stream_cleanup_events[index].wait(timeout=1.0):
                logging.info(f"Stream {index}: Cleanup event set, stopping monitoring")
                break

            try:
                current_time = time.time()
                dropped_frames = 0
                displayed_frames = 0

                if player is None:
                    logging.error(f"Stream {index}: No player, exiting monitor")
                    break

                state = player.get_state()
                if state in (vlc.State.Ended, vlc.State.Error):
                    logging.error(f"Stream {index} stopped: {state}")
                    self.cleanup_stream(index)
                    self.update_stream_label(index, "Stream Failed, click to reconnect")
                    self.bind_retry_connection(index)
                    break

                if current_time - last_check >= 1.0:
                    try:
                        stats = vlc.MediaStats()
                        media_obj = player.get_media()
                        if media_obj.get_stats(stats):
                            current_displayed = stats.displayed_pictures
                            displayed_frames = current_displayed - self.last_displayed_frames[index]
                            self.last_displayed_frames[index] = current_displayed

                            current_dropped = stats.lost_pictures
                            dropped_frames = current_dropped - self.last_dropped_frames[index]
                            self.last_dropped_frames[index] = current_dropped

                            # Guard against counter resets producing negative deltas
                            displayed_frames = max(0, displayed_frames)
                            dropped_frames = max(0, dropped_frames)

                            if dropped_frames > 0:
                                self.drop_timestamps[index].append(current_time)
                                logging.debug(f"Stream {index}: {dropped_frames} dropped frames this tick")

                            if displayed_frames > 0:
                                last_frame_time = current_time
                        else:
                            # Stats unavailable this tick — record as a drop event but
                            # do NOT advance last_frame_time so the no-frame timeout
                            # still fires if the stream is genuinely stalled.
                            self.drop_timestamps[index].append(current_time)
                            logging.debug(f"Stream {index}: Stats unavailable this tick")
                    except Exception as e:
                        logging.warning(f"Stream {index}: Error fetching VLC stats: {e}")
                        self.drop_timestamps[index].append(current_time)

                    last_check = current_time

                # Prune drop window
                self.drop_timestamps[index] = [
                    t for t in self.drop_timestamps[index]
                    if current_time - t < self.drop_window
                ]

                # No-frame timeout
                if current_time - last_frame_time > no_frame_timeout:
                    logging.error(f"Stream {index}: No frames for {no_frame_timeout}s, marking failed")
                    self.cleanup_stream(index)
                    self.update_stream_label(index, "Stream Failed, click to reconnect")
                    self.bind_retry_connection(index)
                    break

                # Quality downgrade (session-only — no save_config)
                if (self.enable_quality_downgrade
                        and self.hq_enabled[index]
                        and len(self.drop_timestamps[index]) >= self.drop_threshold):
                    if current_time - last_stream_switch < self.downgrade_cooldown:
                        logging.warning(f"Stream {index}: Downgrade throttled by cooldown")
                        self.update_stream_label(index, "Waiting: Stream Unstable")
                        continue
                    logging.warning(f"Stream {index}: Excessive drops, downgrading to LQ for this session")
                    self.update_stream_label(index, "Switching to Low Quality...")
                    self.hq_enabled[index] = False
                    self.update_stream(index)
                    last_stream_switch = current_time
                    self.drop_timestamps[index].clear()
                    last_stable_time = current_time
                    self.try_init_stream_with_retries(index)
                    return

                # Auto-revert to HQ (session-only — no save_config)
                # Requires: auto-revert enabled, currently on LQ, cooldown elapsed,
                # and no drops at all during the full stability_period.
                if (self.enable_auto_revert_hq
                        and not self.hq_enabled[index]
                        and current_time - last_stream_switch >= self.downgrade_cooldown):
                    if (current_time - last_stable_time >= self.stability_period
                            and len(self.drop_timestamps[index]) == 0):
                        logging.info(f"Stream {index}: Stable for {self.stability_period}s, reverting to HQ")
                        self.update_stream_label(index, "Reverting to High Quality...")
                        self.hq_enabled[index] = True
                        self.update_stream(index)
                        last_stream_switch = current_time
                        self.drop_timestamps[index].clear()
                        self.try_init_stream_with_retries(index)
                        return

                # Reset stability clock whenever a drop is recorded
                if self.drop_timestamps[index]:
                    last_stable_time = current_time

            except Exception as e:
                logging.error(f"Stream {index}: Monitoring error: {e}")
                self.update_stream_label(index, "Stream Failed")
                break

        logging.info(f"Stream {index} monitoring stopped")

    def start_streams(self):
        threads = []
        for i in range(4):
            # Skip stream if it's in archive mode
            if self.is_archive_mode[i]:
                continue
            if self.ips[i]:
                thread = threading.Thread(target=self.try_init_stream_with_retries, args=(i,), daemon=True)
                threads.append(thread)
                thread.start()
        for thread in threads:
            thread.join()
        for i in range(4):
            # Skip updating target dims for streams in archive mode
            if self.is_archive_mode[i]:
                continue
            if self.media_players[i]:
                self.root.after(0, lambda idx=i: self.update_target_dims(idx))

    def update_layout(self):
        """Update the layout of panels based on current state."""
        # State dictionary
        current_state = {
            'is_fullscreen': self.is_fullscreen,
            'is_fullscreen_index': self.fullscreen_index,
            'window_size': (self.grid_frame.winfo_width(), self.grid_frame.winfo_height())
        }

        # Skip update if state hasn't changed
        if hasattr(self, 'last_layout_state') and self.last_layout_state == current_state:
            logging.debug("Skipping update_layout: No state change")
            return

        # Check if window size changed
        size_changed = not hasattr(self, 'last_layout_state') or \
                       self.last_layout_state['window_size'] != current_state['window_size']

        self.last_layout_state = current_state

        # Update current window size
        x_offset = 60
        self.grid_frame.place(x=0, y=0, width=-x_offset, relwidth=1.0, relheight=1.0)
        w, h = self.grid_frame.winfo_width(), self.grid_frame.winfo_height()
        if w <= 10 or h <= 10:
            w, h = 1920, 1080  # Fallback dimensions

        # Debounced rendering for archive views if size changed
        if size_changed:
            self._debounced_render_archive_views()

        if self.is_fullscreen and self.fullscreen_index is not None:
            for i in range(4):
                if i == self.fullscreen_index:
                    self.panels[i].place(x=0, y=0, width=w, height=h)
                    self.panel_sizes[i] = (w, h)
                    self.update_target_dims(i)
                    if not self.is_archive_mode[i] and self.media_players[i]:
                        hwnd = self.labels[i].winfo_id()
                        self.media_players[i].set_hwnd(hwnd) if sys.platform.startswith("win") else self.media_players[i].set_xwindow(hwnd)
                else:
                    self.panels[i].place_forget()
                    if self.fullscreen_buttons[i]:
                        self.fullscreen_buttons[i].place_forget()
        else:
            ww, hh = w // 2, h // 2
            self.panel_sizes = [(ww, hh)] * 4
            self.panels[0].place(x=0, y=0, width=ww, height=hh)
            self.panels[1].place(x=ww, y=0, width=ww, height=hh)
            self.panels[2].place(x=0, y=hh, width=ww, height=hh)
            self.panels[3].place(x=ww, y=hh, width=ww, height=hh)
            for i in range(4):
                self.update_target_dims(i)

    @debounce(0.2)  # 200ms debounce
    def _debounced_render_archive_views(self):
        """Debounced rendering of archive views for panels in archive mode."""
        for i in range(4):
            if self.is_archive_mode[i] and self.current_archive_path[i]:
                self.render_archive_view(i)

    def update_target_dims(self, index):
        w, h = self.panel_sizes[index]
        if w <= 10 or h <= 10:
            self.target_dims[index] = (0, 0)
            return
        frame_w, frame_h = self.frame_shapes[index]
        if frame_w <= 0 or frame_h <= 0:
            self.target_dims[index] = (0, 0)
            return
        self.target_dims[index] = (w, h)

    def handle_stream_click(self, index):
        logging.debug(f"Handling stream click for index {index}")
        if not self.streams[index] and not self.is_archive_mode[index]:
            logging.debug(f"Stream {index} not enabled and not in archive mode, ignoring click")
            return

        if self.is_fullscreen and self.fullscreen_index == index:
            self.exit_fullscreen()
        else:
            self.is_fullscreen = True  # Set fullscreen state directly
            self.fullscreen_index = index
            # Manage audio
            for i in range(4):
                if not self.streams[i] or not self.audio_enabled[i]:
                    continue
                if self.is_archive_mode[i]:
                    logging.debug(f"Stream {i}: Skipping audio management due to archive mode")
                    continue
                if i == self.fullscreen_index and self.is_fullscreen:
                    self.set_audio_state(i, mute=False)
                else:
                    self.set_audio_state(i, mute=True)

            self.build_config_panel()
        
        logging.info(f"{'Entered' if self.is_fullscreen else 'Exited'} fullscreen mode for stream {index} via click")


    def toggle_all_archive_mode(self):
        """Toggle archive mode for all active streams based on current state."""
        if not self.archive_dir:
            logging.debug("Toggle all archive mode ignored: No archive directory configured")
            return

        # Check if any stream is in archive mode
        any_archive_mode = any(self.is_archive_mode[i] for i in range(4))

        if any_archive_mode:
            # Exit archive mode for all streams in archive mode. No disk
            # access required, safe to do synchronously.
            for i in range(4):
                if self.is_archive_mode[i]:
                    self.toggle_archive_mode(i, rebuild_ui=False)
                    logging.info(f"Stream {i}: Exited archive mode via global toggle")
            self.build_config_panel()
            return

        eligible = [i for i in range(4) if self.streams[i] and not self.is_archive_mode[i]]
        if not eligible:
            return

        for i in eligible:
            self.toggle_archive_mode(i, rebuild_ui=False)
            logging.info(f"Stream {i}: Entered archive mode via global toggle")
        self.build_config_panel()

    def toggle_archive_mode(self, index, rebuild_ui=True):
        if not self.archive_dir:
            logging.debug(f"Stream {index}: Toggle archive mode ignored, no archive directory configured")
            return

        # If stream init is in progress, signal it to abort via
        # stream_cleanup_events. _enter_archive_mode_thread checks this
        # before calling cleanup_stream() so the two threads never race
        # on the same libvlc player.
        if self.stream_initializing[index]:
            logging.info(f"Stream {index}: Init in progress, signalling abort for archive toggle")
            self.stream_cleanup_events[index].set()

        self.is_archive_mode[index] = not self.is_archive_mode[index]
        logging.info(f"Stream {index}: Archive mode {'enabled' if self.is_archive_mode[index] else 'disabled'}")

        if self.is_archive_mode[index]:
            # Entering archive mode. Swap to the archive canvas
            # immediately - cleanup_stream() (stopping/releasing the
            # live VLC player) and the directory probe both happen on a
            # background thread, since either can take noticeable time
            # (libvlc stop/release isn't instant, and a spun-down disk
            # can take seconds to wake).
            self.labels[index].pack_forget()
            self.archive_canvas[index].pack(fill="both", expand=True)
            self.archive_canvas[index].delete("all")

            if self.archive_buttons[index]:
                self.archive_buttons[index].config(state="normal")
            if rebuild_ui:
                self.build_config_panel()

            # Show "Loading..." immediately so the panel never looks frozen.
            loading_shown = threading.Event()
            panel_width, panel_height = self.panel_sizes[index]
            self.archive_canvas[index].create_text(
                panel_width // 2, panel_height // 2,
                text="Loading...", fill="white", font=("arial", -16)
            )
            threading.Thread(target=self._enter_archive_mode_thread, args=(index, loading_shown), daemon=True).start()
        else:
            # Exiting archive mode. Acquire archive_entry_locks[index] first
            # so we wait for any in-progress _enter_archive_mode_thread to
            # finish its cleanup_stream() call before we touch the player.
            # This prevents the libvlc segfault caused by two threads calling
            # stop()/release() on the same player simultaneously.
            with self.archive_entry_locks[index]:
                self.cleanup_archive_mode(index)

            # Start stream initialization in a separate thread
            threading.Thread(target=self.try_init_stream_with_retries, args=(index,), daemon=True).start()

            if rebuild_ui:
                self.build_config_panel()

    def _enter_archive_mode_thread(self, index, loading_shown):
        """Background-thread portion of entering archive mode: stop the
        live VLC player, read the archive directory, then hand off to
        render_archive_view on the main thread.

        The directory read happens here (off the main thread) so a slow
        NAS or spun-down HDD doesn't freeze the UI. "Loading..." is shown
        immediately when archive mode is entered, so there is no need for
        any special wakeup-detection logic.

        archive_entry_locks[index] is held for the entire duration so that
        a concurrent exit (right-click while still loading) waits for this
        thread to finish before calling cleanup_stream(), preventing two
        threads from releasing the same libvlc player simultaneously.
        """
        with self.archive_entry_locks[index]:
            self._enter_archive_mode_thread_locked(index, loading_shown)

    def _enter_archive_mode_thread_locked(self, index, loading_shown):
        """Actual body of _enter_archive_mode_thread, called with
        archive_entry_locks[index] already held."""
        # If init is still running, the cleanup event was already set by
        # toggle_archive_mode. Wait here until try_init_stream_with_retries
        # sees it and exits, so we never call cleanup_stream() while init
        # is mid-flight inside libvlc.
        deadline = time.time() + 10.0
        while self.stream_initializing[index] and time.time() < deadline:
            time.sleep(0.05)
        if self.stream_initializing[index]:
            logging.warning(f"Stream {index}: Init did not stop within timeout, proceeding anyway")
        self.cleanup_stream(index)

        root_path = os.path.normpath(os.path.join(self.archive_dir, f"cam{index+1}"))
        try:
            exists = os.path.isdir(root_path)
        except Exception as e:
            logging.warning(f"Stream {index}: Error accessing archive directory {root_path}: {e}")
            exists = False

        def finish():
            loading_shown.set()
            if not self.is_archive_mode[index]:
                # User toggled back out while we were waiting
                return
            if not exists:
                self.archive_canvas[index].delete("all")
                panel_width, panel_height = self.panel_sizes[index]
                self.archive_canvas[index].create_text(
                    panel_width // 2, panel_height // 2,
                    text="Archive directory not found", fill="white", font=("arial", -16)
                )
                return
            self.pagination_state[index] = {root_path: 0}
            self.current_archive_path[index] = root_path
            self.render_archive_view(index)

        self.root.after(0, finish)


    def get_cached_thumbnail(self, thumbnail_path, width, height):
        """Load and resize an archive thumbnail, caching the resulting
        PhotoImage so repeated renders (page changes, navigation,
        fullscreen toggles) don't re-decode/re-resize the same JPEG from
        disk every time. Cache is invalidated if the file's mtime changes.
        """
        try:
            mtime = os.path.getmtime(thumbnail_path)
        except OSError:
            mtime = None
        key = (thumbnail_path, width, height, mtime)

        cached = self.thumbnail_cache.get(key)
        if cached is not None:
            return cached

        with Image.open(thumbnail_path) as img:
            img = img.resize((width, height), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img)

        self.thumbnail_cache[key] = photo
        self.thumbnail_cache_order.append(key)
        if len(self.thumbnail_cache_order) > self.thumbnail_cache_max:
            oldest = self.thumbnail_cache_order.pop(0)
            self.thumbnail_cache.pop(oldest, None)

        return photo

    def render_archive_view(self, index):
        # Clear canvas
        self.archive_canvas[index].delete("all")

        # Initialize pagination state for the current path if not set
        path = os.path.normpath(self.current_archive_path[index])
        if path not in self.pagination_state[index]:
            self.pagination_state[index][path] = 0

        # Calculate layout parameters
        panel_width, panel_height = self.panel_sizes[index]
        icon_size = 100  # Default icon size (width in pixels)
        text_height = 30  # Approximate height for text labels
        item_width = icon_size + 20  # Icon + horizontal padding
        item_height = icon_size + text_height + 20  # Icon + text + vertical padding (default)
        margin_x, margin_y = 20, 60  # Left/top margins (top includes space for buttons and location text)
        
        # Check if path is a day folder (YYYY-MM-DD) and adjust icon size if thumbnails exist
        is_day_folder = re.match(r".*\d{4}-\d{2}-\d{2}$", path)
        thumbnail_width = None
        thumbnail_height = None
        use_thumbnails = False
        if is_day_folder:
            try:
                # Check for thumbnails in the 'thumbnails' subdirectory
                thumbnails_dir = os.path.join(path, "thumbnails")
                if os.path.isdir(thumbnails_dir):
                    items = os.listdir(path)
                    mp4_files = [item for item in items if item.endswith(".mp4")]
                    if mp4_files:
                        # Get the width and height of the first thumbnail (assume all are the same)
                        for mp4_file in mp4_files:
                            base_name = os.path.splitext(mp4_file)[0]
                            thumbnail_path = os.path.join(thumbnails_dir, f"{base_name}.jpg")
                            if os.path.exists(thumbnail_path):
                                with Image.open(thumbnail_path) as img:
                                    original_width, original_height = img.size
                                break
                        else:
                            original_width, original_height = None, None
                        if original_width and original_height:
                            # --- Modified: Set thumbnail height to 120px in grid mode ---
                            if not self.is_fullscreen:
                                thumbnail_height = 120  # Use 120px height in grid mode
                                # Calculate width to maintain aspect ratio
                                aspect_ratio = original_width / original_height
                                thumbnail_width = int(thumbnail_height * aspect_ratio)
                            else:
                                # In fullscreen mode, use original dimensions or scale appropriately
                                thumbnail_width, thumbnail_height = original_width, original_height
                            icon_size = thumbnail_width
                            item_width = icon_size + 20
                            item_height = thumbnail_height + text_height + 20  # Use actual thumbnail height
                            use_thumbnails = True
            except Exception as e:
                logging.warning(f"Stream {index}: Failed to check thumbnails in {path}: {e}")

        # Calculate number of columns and rows
        max_columns = (panel_width - 2 * margin_x) // item_width
        max_rows = (panel_height - margin_y - 20) // item_height  # 20 for bottom padding
        items_per_page = max_columns * max_rows
        if items_per_page < 1:
            items_per_page = 1  # Ensure at least one item per page
        self.items_per_page = items_per_page

        # Back button
        if not self.back_buttons[index]:
            back_img = self.icon_cache["back"]  # Use cached icon
            self.back_buttons[index] = tk.Button(
                self.archive_canvas[index],
                image=back_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.go_back(index)
            )
            self.back_buttons[index].image = back_img
        self.back_buttons[index].place(x=31, y=5)

        # List and sort items
        if not os.path.isdir(path):
            logging.error(f"Stream {index}: Path {path} is not a directory")
            return

        try:
            items = os.listdir(path)
            # Exclude 'thumbnails' directory in day folders
            if is_day_folder:
                items = [item for item in items if item != "thumbnails"]
        except Exception as e:
            logging.error(f"Stream {index}: Failed to list directory {path}: {e}")
            return
            
        # Sort function for both folders and videos
        def get_sort_key(item):
            try:
                full_path = os.path.join(path, item)
                if os.path.isdir(full_path):
                    # Folders (in root path) are sorted descending
                    return datetime.strptime(item, "%Y-%m-%d")
                elif item.endswith(".mp4"):
                    # Files (in folder path) are sorted ascending
                    match = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2})_(\d+m-\d+s)\.mp4$", item)
                    if not match:
                        logging.warning(f"Stream {index}: Invalid video format for {item}")
                        return datetime.min
                    date_str, time_str, _ = match.groups()
                    date_time = f"{date_str} {time_str.replace('-', ':')}"
                    return datetime.strptime(date_time, "%Y-%m-%d %H:%M")
                return datetime.min
            except Exception as e:
                logging.warning(f"Stream {index}: Failed to parse item {item}: {e}")
                return datetime.min

        # Determine sort order based on whether path is the root (folders) or subfolder (files)
        folder_path = os.path.normpath(os.path.join(self.archive_dir, f"cam{index+1}"))
        is_folder_path = os.path.normpath(path) == folder_path
        sorted_items = sorted(items, key=get_sort_key, reverse=is_folder_path)

        # Pagination logic
        total_items = len(sorted_items)
        total_pages = (total_items + items_per_page - 1) // items_per_page
        current_page = max(0, min(self.pagination_state[index][path], total_pages - 1))
        self.pagination_state[index][path] = current_page
        start_idx = current_page * items_per_page
        end_idx = min(start_idx + items_per_page, total_items)
        page_items = sorted_items[start_idx:end_idx]

        # Set left-aligned x-coordinate for items
        start_x = margin_x  # Fixed starting x-position (left margin)
        x, y = start_x, margin_y
        column = 0

        # Display location
        cam_index = path.find("/cam")
        if cam_index != -1:
            location = path[cam_index:].replace("/", " / ")
            location = re.sub(r'(?i)\bcam(\d+)\b', lambda m: 'CAM ' + m.group(1), location).upper()
            self.archive_canvas[index].create_text(
                80, 25, anchor="w", text=f"{location}", fill="white", font=(self.archive_font, -17)
            )

        # Render items for the current page
        page_images = []
        for item in page_items:
            full_path = os.path.join(path, item)
            is_clicked = full_path in self.clicked_items[index]
            
            if os.path.isdir(full_path):
                # Render folder icon. For day folders (YYYY-MM-DD), show
                # the weekday abbreviation on the folder itself and the
                # full date as smaller secondary text below, so the grid
                # is scannable by day rather than a sea of identical
                # folder icons with only a date underneath.
                day_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", item)
                if day_match:
                    try:
                        folder_date = datetime.strptime(item, "%Y-%m-%d")
                        day_abbrev = folder_date.strftime("%a")  # Mon, Tue, ...
                        folder_img = self.get_day_folder_icon(day_abbrev, is_clicked)
                    except Exception:
                        folder_img = self.icon_cache["folder_clicked" if is_clicked else "folder"]
                else:
                    folder_img = self.icon_cache["folder_clicked" if is_clicked else "folder"]

                folder_id = self.archive_canvas[index].create_image(x + item_width // 2, y + icon_size // 2, image=folder_img)
                if day_match:
                    text_id = self.archive_canvas[index].create_text(
                        x + item_width // 2, y + icon_size + 10, text=item, fill="white", font=(self.archive_font, -17), anchor="n"
                    )
                else:
                    text_id = self.archive_canvas[index].create_text(
                        x + item_width // 2, y + icon_size + 10, text=item[:10], fill="white", anchor="n"
                    )

                # Bind click event
                for id_ in (folder_id, text_id):
                    self.archive_canvas[index].tag_bind(
                        id_, "<Button-1>",
                        lambda e, p=full_path: self.handle_item_click(index, p, self.open_folder)
                    )
                    # Bind hover events for hand2 cursor
                    self.archive_canvas[index].tag_bind(
                        id_, "<Enter>",
                        lambda e: self.archive_canvas[index].config(cursor="hand2")
                    )
                    self.archive_canvas[index].tag_bind(
                        id_, "<Leave>",
                        lambda e: self.archive_canvas[index].config(cursor="")
                    )

                page_images.append(folder_img)

            elif item.endswith(".mp4"):
                # Render video file (thumbnail or icon)
                if use_thumbnails:
                    # Load thumbnail from thumbnails directory
                    base_name = os.path.splitext(item)[0]
                    thumbnail_path = os.path.join(path, "thumbnails", f"{base_name}.jpg")
                    if os.path.exists(thumbnail_path):
                        try:
                            video_img = self.get_cached_thumbnail(thumbnail_path, thumbnail_width, thumbnail_height)
                            video_id = self.archive_canvas[index].create_image(
                                x + item_width // 2, y + thumbnail_height // 2, image=video_img
                            )
                            page_images.append(video_img)
                            thumb_x = x + (item_width - thumbnail_width) // 2
                            border_id = self.archive_canvas[index].create_rectangle(
                                thumb_x, y, thumb_x + thumbnail_width, y + thumbnail_height,
                                outline='#ffffff', width=2
                            )
                            if is_clicked:
                                # Viewed indicator: colored border tightly
                                # around the thumbnail image itself (not
                                # the padded cell), so adjacent borders
                                # don't overlap.
                                thumb_x = x + (item_width - thumbnail_width) // 2
                                border_id = self.archive_canvas[index].create_rectangle(
                                    thumb_x, y, thumb_x + thumbnail_width, y + thumbnail_height,
                                    outline='#555555', width=2
                                )
                            # Bind click and hover events on the thumbnail image itself
                            self.archive_canvas[index].tag_bind(
                                video_id, "<Button-1>",
                                lambda e, p=full_path: self.handle_item_click(index, p, self.play_archive_video)
                            )
                            self.archive_canvas[index].tag_bind(
                                video_id, "<Enter>",
                                lambda e: self.archive_canvas[index].config(cursor="hand2")
                            )
                            self.archive_canvas[index].tag_bind(
                                video_id, "<Leave>",
                                lambda e: self.archive_canvas[index].config(cursor="")
                            )
                        except Exception as e:
                            logging.warning(f"Stream {index}: Failed to load thumbnail {thumbnail_path}: {e}")
                            # Fallback to cached icon
                            video_img = self.icon_cache["archive"]
                            video_id = self.archive_canvas[index].create_image(
                                x + item_width // 2, y + icon_size // 2, image=video_img
                            )
                            page_images.append(video_img)
                            if is_clicked:
                                icon_x = x + (item_width - icon_size) // 2
                                self.archive_canvas[index].create_rectangle(
                                    icon_x, y, icon_x + icon_size, y + icon_size,
                                    outline='#777777', width=2
                                )
                    else:
                        # Fallback to cached icon
                        video_img = self.icon_cache["archive"]
                        video_id = self.archive_canvas[index].create_image(
                            x + item_width // 2, y + icon_size // 2, image=video_img
                        )
                        page_images.append(video_img)
                        if is_clicked:
                            icon_x = x + (item_width - icon_size) // 2
                            self.archive_canvas[index].create_rectangle(
                                icon_x, y, icon_x + icon_size, y + icon_size,
                                outline='#777777', width=2
                            )
                else:
                    # Render cached video icon
                    video_img = self.icon_cache["archive"]
                    video_id = self.archive_canvas[index].create_image(
                        x + item_width // 2, y + icon_size // 2, image=video_img
                    )
                    page_images.append(video_img)
                    if is_clicked:
                        icon_x = x + (item_width - icon_size) // 2
                        self.archive_canvas[index].create_rectangle(
                            icon_x, y, icon_x + icon_size, y + icon_size,
                            outline='#555555', width=1
                        )

                label = f"{item.split('_')[1].replace('-', ':')} {item.split('_')[2].split('.')[0].replace('-', '')}"
                text_id = self.archive_canvas[index].create_text(
                    x + item_width // 2, y + (thumbnail_height if use_thumbnails else icon_size) + 10, text=label, fill="white", font=(self.archive_font, -17), anchor="n"
                )

                # Bind click and hover events for video image and text
                for id_ in (video_id, text_id):
                    self.archive_canvas[index].tag_bind(
                        id_, "<Button-1>",
                        lambda e, p=full_path: self.handle_item_click(index, p, self.play_archive_video)
                    )
                    self.archive_canvas[index].tag_bind(
                        id_, "<Enter>",
                        lambda e: self.archive_canvas[index].config(cursor="hand2")
                    )
                    self.archive_canvas[index].tag_bind(
                        id_, "<Leave>",
                        lambda e: self.archive_canvas[index].config(cursor="")
                    )
            
            column += 1
            x += item_width
            if column >= max_columns:
                x = start_x
                y += item_height
                column = 0

        # Keep references to all PhotoImages for this page so they aren't
        # garbage-collected, in a single assignment rather than rebuilding
        # the list on every item.
        self.archive_canvas[index].images = page_images

        # Navigation buttons (Previous/Next) and pagination text
        if not hasattr(self, 'nav_buttons'):
            self.nav_buttons = [{} for _ in range(len(self.panel_sizes))]
        if "prev" in self.nav_buttons[index]:
            self.nav_buttons[index]["prev"].place_forget()
        if "next" in self.nav_buttons[index]:
            self.nav_buttons[index]["next"].place_forget()

        if total_pages > 1:
            last_column_x = 5 + margin_x + (max_columns - 1) * item_width + item_width // 2

            # Previous button
            if "prev" not in self.nav_buttons[index]:
                prev_img = self.icon_cache["left"]  # Use cached icon
                self.nav_buttons[index]["prev"] = tk.Button(
                    self.archive_canvas[index],
                    image=prev_img,
                    bg="#222222",
                    bd=0,
                    cursor="hand2",
                    command=lambda: self.change_page(index, -1)
                )
                self.nav_buttons[index]["prev"].image = prev_img
            self.nav_buttons[index]["prev"].place(x=last_column_x - 40, y=5)
            self.nav_buttons[index]["prev"].config(state="normal" if current_page > 0 else "disabled")

            # Next button
            if "next" not in self.nav_buttons[index]:
                next_img = self.icon_cache["right"]  # Use cached icon
                self.nav_buttons[index]["next"] = tk.Button(
                    self.archive_canvas[index],
                    image=next_img,
                    bg="#222222",
                    bd=0,
                    cursor="hand2",
                    command=lambda: self.change_page(index, 1)
                )
                self.nav_buttons[index]["next"].image = next_img
            self.nav_buttons[index]["next"].place(x=last_column_x, y=5)
            self.nav_buttons[index]["next"].config(state="normal" if current_page < total_pages - 1 else "disabled")

            # Pagination text
            pagination_x = last_column_x - 100 
            pagination_y = 30
            logging.debug(f"Stream {index}: Rendering pagination text at x={pagination_x}, y={pagination_y}, text='Page {current_page + 1}/{total_pages}'")
            self.archive_canvas[index].create_text(
                pagination_x,
                pagination_y,
                text=f"PAGE {current_page + 1}/{total_pages}",
                fill="white",
                font=(self.archive_font, -17),
                anchor="center"
            )
        else:
            # No pagination needed, ensure pagination text is not rendered
            logging.debug(f"Stream {index}: No pagination needed (total_pages={total_pages}), skipping pagination buttons and text")
    
    def change_page(self, index, delta):
        """Update the current page for the current path and re-render the view."""
        path = os.path.normpath(self.current_archive_path[index])
        self.pagination_state[index][path] = self.pagination_state[index].get(path, 0) + delta
        self.render_archive_view(index)

    def _fullscreen_archive_index(self):
        """Return the stream index if currently in fullscreen archive
        mode, otherwise None. Used by keyboard shortcuts so Page Up/Down
        and Backspace only act on the visible archive browser."""
        if self.is_fullscreen and self.fullscreen_index is not None:
            idx = self.fullscreen_index
            if self.is_archive_mode[idx]:
                return idx
        return None

    def archive_change_page_shortcut(self, delta):
        """Page Up/Down handler: change archive page for the fullscreen
        stream, if it's currently showing the archive browser (i.e. not
        actively playing a clip)."""
        idx = self._fullscreen_archive_index()
        if idx is None:
            return
        if self.current_archive_path[idx] and self.media_players[idx] is None:
            self.change_page(idx, delta)

    def archive_go_back_shortcut(self):
        """Backspace handler: go back one level in the archive browser
        (or stop playback and return to the browser) for the fullscreen
        stream, if currently in archive mode."""
        idx = self._fullscreen_archive_index()
        if idx is None:
            return
        self.go_back(idx)

    def toggle_help_overlay(self):
        """Show or hide a help overlay listing keyboard shortcuts.
        Triggered by H or F1."""
        if self.help_overlay is not None:
            try:
                self.help_overlay.destroy()
            except Exception:
                pass
            self.help_overlay = None
            self.root.focus_set()
            return

        overlay = tk.Toplevel(self.root)
        overlay.title("Keyboard Shortcuts")
        overlay.transient(self.root)
        overlay.configure(bg="#222222")
        overlay.resizable(False, False)
        # Borderless, centered over the main window
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)

        shortcuts = [
            ("General", [
                ("H  /  F1", "Show or hide this help overlay"),
                ("Esc", "Quit the application"),
                ("Alt+Enter  /  Shift", "Toggle fullscreen"),
            ]),
            ("Navigation", [
                ("Up", "Enter fullscreen (current/last stream)"),
                ("Down  /  Right-click", "Exit fullscreen"),
                ("Left  /  Right", "Switch to previous / next stream (fullscreen)"),
            ]),
            ("Archive Browser (fullscreen)", [
                ("Page Up  /  Page Down", "Previous / next page of clips"),
                ("Backspace", "Go back / return to browser"),
            ]),
        ]

        reliability_info = [
            ("Max Retry Attempts",    f"{self.max_retry_attempts}",          "Attempts before marking stream as failed"),
            ("Initial Backoff",       f"{self.initial_backoff_delay}s",       "Wait before first retry; doubles each attempt (max 30s)"),
            ("Drop Window",           f"{int(self.drop_window)}s",            "Sliding window used to count unstable polling ticks"),
            ("Drop Threshold",        f"{self.drop_threshold} ticks",         "Bad ticks within the window before downgrading"),
            ("Downgrade Cooldown",    f"{int(self.downgrade_cooldown)}s",     "Minimum gap between quality switches"),
            ("Stability Period",      f"{int(self.stability_period)}s",       "Drop-free time on LQ before attempting HQ revert"),
            ("No-Frame Timeout",      f"{self.no_frame_timeout}s",            "Seconds with no frames before stream is marked failed"),
        ]

        container = tk.Frame(overlay, bg="#222222", padx=30, pady=24,
                              highlightbackground="#555555", highlightthickness=1)
        container.pack()

        title_label = tk.Label(container, text="Keyboard Shortcuts", bg="#222222", fg="white",
                                font=("arial", 18, "bold"))
        title_label.pack(anchor="w", pady=(0, 14))

        for section_title, items in shortcuts:
            section_label = tk.Label(container, text=section_title, bg="#222222", fg="#4a90d9",
                                      font=("arial", 13, "bold"))
            section_label.pack(anchor="w", pady=(10, 4))
            for key, desc in items:
                row = tk.Frame(container, bg="#222222")
                row.pack(anchor="w", fill="x")
                key_label = tk.Label(row, text=key, bg="#222222", fg="white",
                                      font=("consolas", 11, "bold"), width=22, anchor="w")
                key_label.pack(side="left")
                desc_label = tk.Label(row, text=desc, bg="#222222", fg="#cccccc",
                                       font=("arial", 11), anchor="w")
                desc_label.pack(side="left", padx=(10, 0))

        # Stream reliability section
        tk.Label(container, text="Stream Reliability  —  current settings",
                 bg="#222222", fg="#4a90d9", font=("arial", 13, "bold")).pack(anchor="w", pady=(16, 4))
        rel_grid = tk.Frame(container, bg="#222222")
        rel_grid.pack(anchor="w", fill="x")
        for r_idx, (label, value, tip) in enumerate(reliability_info):
            tk.Label(rel_grid, text=label, bg="#222222", fg="#cccccc",
                     font=("arial", 10), width=22, anchor="w").grid(row=r_idx, column=0, sticky="w", pady=1)
            tk.Label(rel_grid, text=value, bg="#222222", fg="white",
                     font=("consolas", 10, "bold"), width=12, anchor="w").grid(row=r_idx, column=1, sticky="w", padx=(6, 0), pady=1)
            tk.Label(rel_grid, text=tip, bg="#222222", fg="#777777",
                     font=("arial", 10, "italic"), anchor="w").grid(row=r_idx, column=2, sticky="w", padx=(10, 0), pady=1)

        hint_label = tk.Label(container, text="Press H, F1, or click anywhere to close",
                               bg="#222222", fg="#777777", font=("arial", 10, "italic"))
        hint_label.pack(anchor="w", pady=(16, 0))

        # Close on click anywhere, or H/F1/Escape again
        for widget in (overlay, container, title_label, hint_label):
            widget.bind("<Button-1>", lambda e: self.toggle_help_overlay())
        overlay.bind("<KeyPress-h>", lambda e: self.toggle_help_overlay())
        overlay.bind("<KeyPress-H>", lambda e: self.toggle_help_overlay())
        overlay.bind("<F1>", lambda e: self.toggle_help_overlay())
        overlay.bind("<Escape>", lambda e: self.toggle_help_overlay())

        # Center over the main window
        overlay.update_idletasks()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        ow = overlay.winfo_reqwidth()
        oh = overlay.winfo_reqheight()
        overlay.geometry(f"+{root_x + (root_w - ow) // 2}+{root_y + (root_h - oh) // 2}")

        overlay.focus_set()
        self.help_overlay = overlay

    def handle_item_click(self, index, path, callback):
        # Mark the item as clicked
        self.clicked_items[index].add(path)
        # Call the original callback
        callback(index, path)

    def open_folder(self, index, path):
        self.current_archive_path[index] = path
        path = os.path.normpath(path)
        if path not in self.pagination_state[index]:
            self.pagination_state[index][path] = 0
        self.render_archive_view(index)

    def go_back(self, index):
        # Stop video playback and clean up VLC resources
        self.cleanup_stream(index)

        # Destroy all children of self.labels[index]
        for widget in self.labels[index].winfo_children():
            widget.destroy()

        # Reset button and image references
        self.exit_buttons[index] = None
        self.pause_buttons[index] = None
        self.speed_buttons[index] = None
        self.replay_buttons[index] = None
        self.rewind_buttons[index] = None
        self.pause_images[index] = None
        self.speed_images[index] = None
        self.replay_images[index] = None
        self.rewind_images[index] = None

        # Reset playback state
        self.playback_speeds[index] = 1.0
        self.is_paused[index] = False
        self.video_ended[index] = False

        # Handle navigation
        if not self.current_archive_path[index]:
            logging.warning(f"Stream {index}: No current archive path, exiting archive mode")
            self.toggle_archive_mode(index)
            return

        # Normalize paths to handle Linux/Windows separators
        current_path = os.path.normpath(self.current_archive_path[index])
        archive_dir = os.path.normpath(self.archive_dir)
        archive_root = os.path.normpath(os.path.join(archive_dir, f"cam{index+1}"))

        # If already at the root folder view, exit archive mode
        if current_path == archive_root and not current_path.endswith(".mp4"):
            logging.info(f"Stream {index}: At archive root {current_path}, exiting archive mode")
            self.toggle_archive_mode(index)
            return

        # Determine parent path
        if current_path.endswith(".mp4"):
            parent_path = os.path.dirname(current_path)
        else:
            parent_path = os.path.dirname(current_path)

        # Reset pagination for video listing view (subfolder) when navigating up
        if current_path != archive_root and not current_path.endswith(".mp4"):
            self.pagination_state[index][current_path] = 0
            logging.info(f"Stream {index}: Reset pagination for video listing view {current_path} to page 1")

        # Check if parent_path is still within or equal to archive_dir
        if not os.path.commonprefix([parent_path, archive_dir]) == archive_dir or parent_path == archive_dir:
            # Reached or exceeded archive_dir, exit archive mode
            logging.info(f"Stream {index}: Reached archive_dir boundary, exiting archive mode")
            self.toggle_archive_mode(index)
            return
        else:
            # Update to parent directory
            self.current_archive_path[index] = parent_path
            parent_path = os.path.normpath(parent_path)
            if parent_path not in self.pagination_state[index]:
                self.pagination_state[index][parent_path] = 0
            logging.info(f"Stream {index}: Navigated back to {self.current_archive_path[index]}")

        # Restore archive view
        self.is_archive_mode[index] = True
        self.labels[index].pack_forget()
        self.archive_canvas[index].pack(fill="both", expand=True)
        self.render_archive_view(index)

    def play_archive_video(self, index, video_path):
        self.is_archive_mode[index] = True
        self.archive_canvas[index].pack_forget()
        self.labels[index].pack(fill="both", expand=True)

        self.update_stream_label(index, "Loading...")

        # Clean up any existing VLC frame
        for widget in self.labels[index].winfo_children():
            if isinstance(widget, tk.Frame):
                widget.destroy()

        # Create a Frame for VLC rendering
        try:
            vlc_frame = tk.Frame(self.labels[index], bg="")
            vlc_frame.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0, anchor="nw")
            vlc_frame.configure(highlightthickness=0)
        except Exception as e:
            logging.error(f"Stream {index}: Failed to create VLC frame: {e}")
            self.labels[index].configure(image="", text="Frame Creation Failed", fg="white")
            return

        # Player control buttons
        try:
            exit_img = self.icon_cache["exit"]  # Use cached icon
            self.exit_buttons[index] = tk.Button(
                self.labels[index],
                image=exit_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.go_back(index)
            )
            self.exit_buttons[index].image = exit_img

            pause_img = self.icon_cache["pause"]  # Use cached icon
            self.pause_buttons[index] = tk.Button(
                self.labels[index],
                image=pause_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.toggle_pause(index)
            )
            self.pause_buttons[index].image = pause_img
            self.pause_images[index] = pause_img

            speed_img = self.icon_cache["speed"]  # Use cached icon
            self.speed_buttons[index] = tk.Button(
                self.labels[index],
                image=speed_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.cycle_speed(index)
            )
            self.speed_buttons[index].image = speed_img
            self.speed_images[index] = speed_img

            replay_img = self.icon_cache["replay"]  # Use cached icon
            self.replay_buttons[index] = tk.Button(
                self.labels[index],
                image=replay_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.replay_video(index)
            )
            self.replay_buttons[index].image = replay_img
            self.replay_images[index] = replay_img

            rewind_img = self.icon_cache["rewind"]  # Use cached icon
            self.rewind_buttons[index] = tk.Button(
                self.labels[index],
                image=rewind_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.rewind_video(index)
            )
            self.rewind_buttons[index].image = rewind_img
            self.rewind_images[index] = rewind_img

            self.current_archive_path[index] = video_path
            self.exit_buttons[index].place(relx=0.0, rely=0.0, x=20, y=4, anchor="nw")
            self.rewind_buttons[index].place(relx=0.0, rely=0.0, x=60, y=4, anchor="nw")
            self.pause_buttons[index].place(relx=0.0, rely=0.0, x=100, y=4, anchor="nw")
            self.speed_buttons[index].place(relx=0.0, rely=0.0, x=140, y=4, anchor="nw")
            self.replay_buttons[index].place(relx=0.0, rely=0.0, x=180, y=4, anchor="nw")
            self.labels[index].update_idletasks()
            logging.info(f"Stream {index}: Buttons placed for video {video_path}")
        except Exception as e:
            logging.error(f"Stream {index}: Failed to create or place buttons: {e}")
            self.labels[index].configure(image="", text="Button Creation Failed", fg="white")
            vlc_frame.destroy()
            return

        # Reset playback state
        self.playback_speeds[index] = 1.0
        self.is_paused[index] = False
        self.video_ended[index] = False

        # Clean up previous VLC instances/processes
        self.cleanup_stream(index)

        # Start video playback
        try:
            xid = vlc_frame.winfo_id()
            instance = vlc.Instance(self.build_vlc_instance_args(
                ['--no-drop-late-frames']
            ))
            if instance is None:
                logging.error(f"Stream {index}: Failed to create VLC instance for archive video")
                self.labels[index].configure(image="", text="VLC Initialization Failed", fg="white")
                vlc_frame.destroy()
                return
            self.attach_vlc_logging(instance)
            self.vlc_instance = instance
            player = instance.media_player_new()
            if player is None:
                logging.error(f"Stream {index}: Failed to create VLC media player for archive video")
                self.labels[index].configure(image="", text="VLC Player Creation Failed", fg="white")
                instance.release()
                self.vlc_instance = None
                vlc_frame.destroy()
                return
            self.media_players[index] = player
            media = instance.media_new(video_path)
            player.set_media(media)
            player.set_hwnd(xid) if sys.platform.startswith("win") else player.set_xwindow(xid)
            event_manager = player.event_manager()
            playing_event = threading.Event()
            self.playback_speeds[index] = self.default_playback_speed

            def on_playing():
                playing_event.set()

            event_manager.event_attach(vlc.EventType.MediaPlayerPlaying, lambda e: on_playing())

            if player.play() == -1:
                logging.error(f"Stream {index}: Failed to start VLC player for archive video")
                self.labels[index].configure(image="", text="VLC Playback Failed", fg="white")
                player.release()
                instance.release()
                self.media_players[index] = None
                self.vlc_instance = None
                vlc_frame.destroy()
                return

            timeout = 5.0
            start_time = time.time()
            while time.time() - start_time < timeout:
                if playing_event.is_set():
                    self.set_audio_state(index, mute=False)
                    self.media_players[index].set_rate(self.default_playback_speed)
                    threading.Thread(target=self.monitor_vlc_playback, args=(index,), daemon=True).start()
                    logging.info(f"Stream {index}: Started python-vlc playback for archive video")
                    break
                time.sleep(0.1)
            else:
                logging.error(f"Stream {index}: Archive video failed to start within {timeout}s")
                self.labels[index].configure(image="", text="Playback Timeout", fg="white")
                player.release()
                instance.release()
                self.media_players[index] = None
                self.vlc_instance = None
                vlc_frame.destroy()
                return
        except Exception as e:
            logging.error(f"Stream {index}: Failed to start archive video playback: {e}")
            self.labels[index].configure(image="", text="Playback Failed", fg="white")
            vlc_frame.destroy()
            self.cleanup_stream(index)


    def toggle_pause(self, index):
        self.is_paused[index] = not self.is_paused[index]
        self.playback_speeds[index] = 1.0

        new_icon = self.icon_cache["play" if self.is_paused[index] else "pause"]  # Use cached icon
        self.pause_buttons[index].configure(image=new_icon)
        self.pause_buttons[index].image = new_icon
        self.pause_images[index] = new_icon

        if self.media_players[index]:
            try:
                self.media_players[index].pause()
                self.media_players[index].set_rate(1.0)
                self.set_audio_state(index, mute=False)
                logging.info(f"Stream {index} {'paused' if self.is_paused[index] else 'resumed'} at 1x speed")
            except Exception as e:
                logging.error(f"Error toggling pause for stream {index}: {e}")

    def rewind_video(self, index):
        if not self.current_archive_path[index]:
            logging.warning(f"Stream {index}: No video path set for rewind")
            return

        self.playback_speeds[index] = 1.0

        if self.media_players[index] and not self.video_ended[index]:
            try:
                current_time = self.media_players[index].get_time()
                new_time = max(0, current_time - 10000)
                self.media_players[index].set_time(new_time)
                self.media_players[index].set_rate(1.0)
                self.set_audio_state(index, mute=False)
                if self.is_paused[index]:
                    self.media_players[index].play()
                    self.is_paused[index] = False
                    new_icon = self.icon_cache["pause"]
                self.pause_buttons[index].configure(image=new_icon)
                self.pause_buttons[index].image = new_icon
                self.pause_images[index] = new_icon
                logging.info(f"Stream {index}: Rewound video by 10 seconds to {new_time/1000:.1f}s at 1x speed")
            except Exception as e:
                logging.error(f"Error rewinding video for stream {index}: {e}")
        else:
            self.play_archive_video(index, self.current_archive_path[index])
            logging.info(f"Stream {index}: Video ended, restarted for rewind at 1x speed")

    def replay_video(self, index):
        if not self.current_archive_path[index]:
            logging.warning(f"Stream {index}: No video path set for replay")
            return

        self.playback_speeds[index] = 1.0

        if self.media_players[index] and not self.video_ended[index]:
            try:
                self.media_players[index].set_time(0)
                self.media_players[index].set_rate(1.0)
                self.set_audio_state(index, mute=False)
                if self.is_paused[index]:
                    self.media_players[index].play()
                    self.is_paused[index] = False
                    new_icon = self.icon_cache["pause"]
                self.pause_buttons[index].configure(image=new_icon)
                self.pause_buttons[index].image = new_icon
                self.pause_images[index] = new_icon
                logging.info(f"Stream {index}: Replayed video at 1x speed")
            except Exception as e:
                logging.error(f"Error replaying video for stream {index}: {e}")
        else:
            self.play_archive_video(index, self.current_archive_path[index])
            logging.info(f"Stream {index}: Restarted video playback at 1x speed")

    def cycle_speed(self, index):
        current_speed = self.playback_speeds[index]
        speed_cycle = [1.0, 2.0, 4.0, 8.0]
        next_speed = speed_cycle[(speed_cycle.index(current_speed) + 1) % len(speed_cycle)]
        self.playback_speeds[index] = next_speed

        if self.media_players[index]:
            try:
                self.media_players[index].set_rate(next_speed)
                self.set_audio_state(index, mute=False)
                logging.info(f"Stream {index} playback speed set to x{next_speed}")
            except Exception as e:
                logging.error(f"Error setting playback speed for stream {index}: {e}")

    def monitor_vlc_playback(self, index):
        while self.running and not self.video_ended[index]:
            try:
                if self.media_players[index]:
                    state = self.media_players[index].get_state()
                    if state == vlc.State.Ended:
                        logging.info(f"Stream {index}: python-vlc playback ended")
                        self.video_ended[index] = True
                time.sleep(1.0)
            except Exception as e:
                logging.error(f"Error monitoring playback for stream {index}: {e}")
                self.video_ended[index] = True  # Mark as ended to exit loop
                break

    def get_onvif_camera(self, ip):
        if ip in self.onvif_cams:
            return self.onvif_cams[ip]
        try:
            from onvif import ONVIFCamera
            cam = ONVIFCamera(ip, 2020, self.username, self.password)
            media = cam.create_media_service()
            ptz = cam.create_ptz_service()
            profiles = media.GetProfiles()
            if not profiles:
                return None
            token = profiles[0].token
            self.onvif_cams[ip] = {
                "cam": cam,
                "ptz": ptz,
                "media": media,
                "token": token
            }
            return self.onvif_cams[ip]
        except Exception:
            return None

    def start_ptz_move(self, direction):
        if not self.is_fullscreen or self.fullscreen_index is None or not self.streams[self.fullscreen_index]:
            logging.debug("PTZ move ignored: Not in fullscreen or no stream")
            return
        ip = self.ips[self.fullscreen_index]
        if not ip or self.ptz_busy:
            logging.debug(f"PTZ move ignored: IP={ip}, ptz_busy={self.ptz_busy}")
            return
        
        with self.ptz_lock:
            if self.ptz_buttons_disabled:
                logging.debug("PTZ move ignored: Buttons are disabled")
                return
            self.ptz_busy = True
            self.ptz_moving = True
            self.disable_ptz_buttons()
            
        if direction in ["left", "right"]:
            self.ptz_click_counts[self.fullscreen_index] += 1
        
        logging.info(f"Starting PTZ move: direction={direction}, ip={ip}")
        threading.Thread(target=self.ptz_move_loop, args=(direction, ip), daemon=True).start()

    def stop_ptz_move(self, direction):
        if not self.is_fullscreen or self.fullscreen_index is None:
            logging.debug("Stop PTZ ignored: Not in fullscreen")
            return
        self.ptz_moving = False
        logging.debug(f"PTZ move stop requested for direction={direction}")

    def disable_ptz_buttons(self):
        """Disable all PTZ buttons on the main thread."""
        if not self.ptz_buttons_disabled:
            self.ptz_buttons_disabled = True
            self.root.after(0, lambda: [
                button.config(state="disabled") for button in self.ptz_buttons
            ])
            logging.debug("PTZ buttons disabled")

    def enable_ptz_buttons(self):
        """Enable all PTZ buttons on the main thread."""
        if self.ptz_buttons_disabled:
            self.ptz_buttons_disabled = False
            self.root.after(0, lambda: [
                button.config(state="normal") for button in self.ptz_buttons
            ])
            logging.debug("PTZ buttons enabled")

    def ptz_move_loop(self, direction, ip):
        try:
            with self.ptz_lock:
                # Send initial PTZ command
                self.send_ptz_command(ip, direction)
                logging.info(f"Sent PTZ command: direction={direction}, ip={ip}")
                
                # Disable buttons
                self.disable_ptz_buttons()
                
                # Wait for a fixed duration to match original movement amount
                movement_duration = 0.5  # Fixed duration for movement
                start_time = time.time()
                time.sleep(movement_duration)
                
                # Send stop command (or pulse_stop for left/right)
                if direction in ["left", "right"]:
                    self.send_ptz_command(ip, "pulse_stop")
                    logging.debug("Sent pulse_stop command")
                else:
                    self.send_ptz_command(ip, "stop")
                    logging.debug("Sent stop command")
                
                # Get ONVIF camera for status polling
                cam_info = self.get_onvif_camera(ip)
                if not cam_info:
                    logging.error(f"Failed to get ONVIF camera for ip={ip}")
                    # Re-enable buttons to avoid being stuck
                    self.ptz_moving = False
                    self.ptz_busy = False
                    self.enable_ptz_buttons()
                    return
                
                ptz = cam_info["ptz"]
                token = cam_info["token"]
                
                # Poll to confirm PTZ is idle before re-enabling buttons
                max_polling_time = 2.0  # Max time to wait for IDLE status
                polling_interval = 0.2  # Interval between status checks
                poll_start = time.time()
                while time.time() - poll_start < max_polling_time:
                    try:
                        status = ptz.GetStatus({"ProfileToken": token})
                        move_status = status.MoveStatus.PanTilt if hasattr(status.MoveStatus, 'PanTilt') else "UNKNOWN"
                        logging.debug(f"PTZ status after stop for ip={ip}: {move_status}")
                        if move_status == "IDLE":
                            logging.info(f"Confirmed PTZ idle for ip={ip} after {time.time() - start_time:.2f} seconds")
                            break
                        time.sleep(polling_interval)
                    except Exception as e:
                        logging.error(f"Error checking PTZ status for ip={ip}: {e}")
                        break  # Exit polling on error to avoid hanging
                
                logging.info(f"PTZ movement completed for ip={ip}, direction={direction}, total duration={time.time() - start_time:.2f} seconds")
                
        except Exception as e:
            logging.error(f"Error in PTZ move loop for ip={ip}, direction={direction}: {e}", exc_info=True)
        finally:
            self.ptz_moving = False
            self.ptz_busy = False
            self.enable_ptz_buttons()
            logging.debug(f"PTZ move loop finished for ip={ip}, direction={direction}")

    def send_ptz_command(self, ip, command):
        cam_info = self.get_onvif_camera(ip)
        if not cam_info:
            logging.error(f"Cannot send PTZ command: No ONVIF camera for ip={ip}")
            return
        try:
            ptz = cam_info["ptz"]
            token = cam_info["token"]
            if command == "stop":
                ptz.Stop({"ProfileToken": token})
                logging.debug(f"Sent PTZ stop command to ip={ip}")
            elif command == "pulse_stop":
                request = ptz.create_type("ContinuousMove")
                request.ProfileToken = token
                y_velocity = 0.001 if self.ptz_click_counts[self.fullscreen_index] % 2 == 1 else -0.001
                request.Velocity = {"PanTilt": {"x": 0, "y": y_velocity}, "Zoom": {"x": 0}}
                ptz.ContinuousMove(request)
                time.sleep(0.1)
                ptz.Stop({"ProfileToken": token})
                logging.debug(f"Sent PTZ pulse_stop command to ip={ip}, y_velocity={y_velocity}")
            else:
                request = ptz.create_type("ContinuousMove")
                request.ProfileToken = token
                velocity = {"PanTilt": {"x": 0, "y": 0}, "Zoom": {"x": 0}}
                # Speed scaling: speed = 0.025 * (4 ^ (resolution - 1)), capped at 1.0
                base_speed = 0.025  # Anchors resolution 1 at 0.025
                speed = min(1.0, base_speed * (4 ** (self.ptz_resolution - 1)))
                if command == "left":
                    velocity["PanTilt"]["x"] = -speed
                elif command == "right":
                    velocity["PanTilt"]["x"] = speed
                elif command == "up":
                    velocity["PanTilt"]["y"] = speed
                elif command == "down":
                    velocity["PanTilt"]["y"] = -speed
                else:
                    logging.warning(f"Invalid PTZ command: {command}")
                    return
                request.Velocity = velocity
                ptz.ContinuousMove(request)
                logging.info(f"Sent PTZ {command} command to ip={ip}, velocity={velocity}, resolution={self.ptz_resolution}, speed={speed:.4f}")
        except Exception as e:
            logging.error(f"Failed to send PTZ command {command} to ip={ip}: {e}", exc_info=True)


    def apply_window_size(self, size):
        """
        Apply the saved window size or fullscreen state and center the window.
        Args:
            size (str): Window size in the format 'WIDTHxHEIGHT' or 'fullscreen'.
        """
        logging.info(f"Applying window size: {size}")
        try:
            if size.lower() == "fullscreen":
                self.root.attributes("-fullscreen", True)
                self.root.update_idletasks()
                logging.debug("Set window to fullscreen mode")
            else:
                # Parse width and height from size string (e.g., '1340x720')
                try:
                    width, height = map(int, size.split("x"))
                except (ValueError, AttributeError) as e:
                    logging.warning(f"Invalid saved_window_size: {size}, using default {self.MIN_WIDTH}x{self.MIN_HEIGHT}")
                    width, height = self.MIN_WIDTH, self.MIN_HEIGHT

                # Clamp size to valid bounds
                width, height = self.clamp_size(width, height)

                # Ensure window is not maximized
                self.force_unmaximize(width, height)

                # Set window geometry and center it
                self.root.attributes("-fullscreen", False)
                self.root.geometry(f"{width}x{height}")
                self.center_window(width, height)
                logging.debug(f"Set window size to {width}x{height} and centered")
        except Exception as e:
            logging.error(f"Failed to apply window size {size}: {e}", exc_info=True)
            # Fallback to default size
            width, height = self.MIN_WIDTH, self.MIN_HEIGHT
            self.root.attributes("-fullscreen", False)
            self.force_unmaximize(width, height)
            self.root.geometry(f"{width}x{height}")
            self.center_window(width, height)
            logging.debug(f"Fallback to default size {width}x{height} and centered")

    def center_window(self, width, height):
        """
        Center the window on the screen, accounting for taskbar height on Windows.
        Args:
            width (int): Window width in pixels.
            height (int): Window height in pixels.
        """
        try:
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            taskbar_height = self.get_taskbar_height()

            if taskbar_height > 0:
                # Center in available space excluding taskbar (Windows)
                available_height = screen_height - taskbar_height
                x = (screen_width - width) // 2
                y = (available_height - height) // 2
                if y < 0:
                    y = 0
                    logging.debug("Adjusted y to 0 to avoid negative position")
            else:
                # Use full screen height (Linux or Windows detection failure)
                x = (screen_width - width) // 2
                y = (screen_height - height) // 2

            # Ensure non-negative coordinates
            x = max(0, x)
            y = max(0, y)

            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.root.update_idletasks()
            logging.debug(f"Centered window at {width}x{height}+{x}+{y}, taskbar height: {taskbar_height}px")
        except Exception as e:
            logging.error(f"Failed to center window: {e}", exc_info=True)
            # Fallback to default positioning
            self.root.geometry(f"{width}x{height}+0+0")
            self.root.update_idletasks()
            logging.debug(f"Fallback to position {width}x{height}+0+0")

    def clamp_size(self, width, height):
        """
        Ensure window size is within valid bounds.
        Args:
            width (int): Desired width.
            height (int): Desired height.
        Returns:
            tuple: Clamped (width, height).
        """
        screen_width = self.root.winfo_screenwidth() - 50  # Margin for panels/docks
        screen_height = self.root.winfo_screenheight() - 100
        return (max(self.MIN_WIDTH, min(width, screen_width)),
                max(self.MIN_HEIGHT, min(height, screen_height)))

    def force_unmaximize(self, width, height):
        """
        Ensure the window is not maximized before setting geometry.
        Args:
            width (int): Target width.
            height (int): Target height.
        Returns:
            bool: True if unmaximized successfully, False otherwise.
        """
        try:
            self.root.wm_state("normal")
            self.root.update_idletasks()
            for attempt in range(3):
                if self.root.wm_state() != "zoomed":
                    logging.debug(f"Unmaximized after {attempt+1} attempt(s)")
                    return True
                logging.debug(f"Still maximized (attempt {attempt+1}/3)")
                self.root.wm_state("normal")
                self.root.update_idletasks()
                time.sleep(0.1)
            logging.warning("Failed to unmaximize after 3 attempts, applying geometry anyway")
            return False
        except Exception as e:
            logging.error(f"Error forcing unmaximize: {e}", exc_info=True)
            return False
    
    def get_taskbar_height(self):
        """Detect taskbar height for Windows; return 0 for Linux."""
        if sys.platform.startswith("win"):
            try:
                # Query work area using SystemParametersInfo
                class RECT(ctypes.Structure):
                    _fields_ = [("left", ctypes.c_long),
                                ("top", ctypes.c_long),
                                ("right", ctypes.c_long),
                                ("bottom", ctypes.c_long)]
                
                rect = RECT()
                SPI_GETWORKAREA = 0x0030
                ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
                screen_height = self.root.winfo_screenheight()
                work_area_height = rect.bottom - rect.top
                taskbar_height = screen_height - work_area_height
                if taskbar_height > 0 and taskbar_height < screen_height // 2:  # Sanity check
                    logging.debug(f"Detected Windows taskbar height: {taskbar_height}px")
                    return taskbar_height
                logging.debug(f"Invalid Windows taskbar height: {taskbar_height}px")
            except Exception as e:
                logging.debug(f"Failed to detect Windows taskbar height: {e}")
        else:
            logging.debug("Linux detected, using full screen height for centering")
        return 0

    def cleanup_archive_mode(self, index):
        """Clean up archive mode state and UI for the specified stream index."""
        try:
            # Stop and release any VLC player playing a clip BEFORE
            # destroying the Tk widget it's embedded in (vlc_frame, a
            # child of self.labels[index]). Destroying the widget first
            # leaves libvlc's vout thread holding an XID for a window
            # that no longer exists, causing a BadWindow X error on its
            # next draw.
            if self.media_players[index]:
                self.cleanup_stream(index)

            # Destroy all child widgets in self.labels[index]
            for widget in self.labels[index].winfo_children():
                widget.destroy()
            logging.info(f"Stream {index}: Destroyed all child widgets in label")

            # Reset archive UI elements
            if self.archive_canvas[index]:
                self.archive_canvas[index].pack_forget()
            if self.back_buttons[index]:
                self.back_buttons[index].place_forget()

            # Reset player control buttons and images
            self.exit_buttons[index] = None
            self.pause_buttons[index] = None
            self.speed_buttons[index] = None
            self.replay_buttons[index] = None
            self.rewind_buttons[index] = None
            self.pause_images[index] = None
            self.speed_images[index] = None
            self.replay_images[index] = None
            self.rewind_images[index] = None

            # Reset archive state
            self.is_archive_mode[index] = False
            self.current_archive_path[index] = None
            self.playback_speeds[index] = 1.0
            self.is_paused[index] = False
            self.video_ended[index] = False
            self.pagination_state[index] = {}

            # Restore live view label
            self.labels[index].pack(fill="both", expand=True)

        except Exception as e:
            logging.error(f"Stream {index}: Failed to clean up archive mode: {e}")

    def cleanup_config_panel(self):
        logging.debug("Cleaning up config panel")
        try:
            if self.config_panel:
                self.config_panel.destroy()
                self.config_panel = None
            if self.ptz_buttons:
                for button in self.ptz_buttons:
                    button.destroy()
                self.ptz_buttons = []
                self.ptz_images = []
            if self.exit_fullscreen_button:
                self.exit_fullscreen_button.destroy()
                self.exit_fullscreen_button = None
                self.exit_fullscreen_image = None
            if self.config_button:
                self.config_button.destroy()
                self.config_button = None
                self.config_img = None
            if self.archive_mode_button:
                self.archive_mode_button.destroy()
                self.archive_mode_button = None
                self.archive_mode_image = None
                logging.debug("Destroyed archive mode button")
            # Destroy archive buttons
            for i in range(4):
                if self.archive_buttons[i]:
                    self.archive_buttons[i].destroy()
                    self.archive_buttons[i] = None
                    logging.debug(f"Destroyed archive button {i}")
            # Destroy fullscreen buttons
            for i in range(4):
                if self.fullscreen_buttons[i]:
                    self.fullscreen_buttons[i].destroy()
                    self.fullscreen_buttons[i] = None
                    logging.debug(f"Destroyed fullscreen button {i}")
            logging.debug("Config panel cleaned up successfully")
        except Exception as e:
            logging.debug(f"Error cleaning up config panel: {e}")


    def cleanup(self):
        self.enable_ptz_buttons()
        self.running = False

        if self.help_overlay is not None:
            try:
                self.help_overlay.destroy()
            except Exception:
                pass
            self.help_overlay = None

        if self.vlc_instance:
            try:
                self.vlc_instance.release()
                logging.info(f"Released VLC instance")
            except Exception as e:
                logging.error(f"Error releasing VLC instance: {e}")
            self.vlc_instance = None

        # Stop all streams and mute audio
        for i in range(4):
            try:
                self.cleanup_stream(i)

            except Exception as e:
                logging.error(f"Error during shutdown of stream {i}: {e}")

        self.onvif_cams.clear()

        for i in range(4):
            try:
                if self.archive_canvas[i]:
                    self.archive_canvas[i].destroy()
                if self.back_buttons[i]:
                    self.back_buttons[i].destroy()
                if self.exit_buttons[i]:
                    self.exit_buttons[i].destroy()
                if self.pause_buttons[i]:
                    self.pause_buttons[i].destroy()
                if self.speed_buttons[i]:
                    self.speed_buttons[i].destroy()
                if self.replay_buttons[i]:
                    self.replay_buttons[i].destroy()
                if self.rewind_buttons[i]:
                    self.rewind_buttons[i].destroy()
                self.archive_canvas[i] = None
                self.back_buttons[i] = None
                self.exit_buttons[i] = None
                self.pause_buttons[i] = None
                self.speed_buttons[i] = None
                self.replay_buttons[i] = None
                self.rewind_buttons[i] = None
            except Exception as e:
                logging.error(f"Error cleaning up UI for stream {i}: {e}")

        # Clean up config panel and all buttons
        self.cleanup_config_panel()

        time.sleep(0.5)
        try:
            self.root.destroy()
            logging.info("Shutdown completed")
        except Exception as e:
            logging.error(f"Error destroying Tkinter root: {e}")

if __name__ == "__main__":
    try:
        import ttkbootstrap as tb
        root = tb.Window(themename="darkly")
    except ImportError:
        root = tk.Tk()
    app = tapoStreamer(root)
    root.mainloop()