from PIL import Image, ImageTk, ImageDraw, ImageFont
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont
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

    # Candidate fonts offered in Options > General > Font, in priority
    # order. These aren't assumed to be installed - _init_font_choices()
    # checks each one against the fonts Tk can actually see on this
    # machine and only offers ones that are really there. The list spans
    # Windows 10/11's built-in fonts (Verdana, Tahoma, Arial, Segoe UI)
    # and the default font packages on recent Linux desktops (DejaVu
    # Sans ships on Debian/Ubuntu/Fedora; Liberation Sans on
    # Fedora/RHEL; Noto Sans on GNOME-based distros), so most users see
    # a handful of real, working choices on either OS.
    FONT_CANDIDATES = ["Verdana", "Tahoma", "Arial", "Segoe UI", "DejaVu Sans", "Liberation Sans", "Noto Sans"]
    FONT_FALLBACK = "Helvetica"  # Always resolvable as a Tk built-in alias, even if nothing above matches

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
        self.watch_progress_file = os.path.join(config_dir, "watch_progress.json")

        # Initialize debug mode and logging
        self.debug_mode = args.debug
        self.speed_cycle = [1.0, 2.0, 4.0, 8.0]
        self._init_font_choices()  # Needs self.root; must run before load_config validates ui_font
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

        # Each stream owns its own dedicated libvlc Instance rather than
        # sharing one. The previous shared self.vlc_instance was silently
        # overwritten by whichever stream's init_stream() ran last when
        # multiple streams initialized concurrently (e.g. all 4 on exiting
        # archive mode), orphaning the other streams' instances with no
        # reference while their players were still using them. Any cleanup
        # could then release an instance another stream's player depended
        # on, causing a libvlc use-after-free/segfault. Per-stream instances
        # make that race structurally impossible.
        self.vlc_instances = [None] * 4
        self.stream_initializing = [False] * 4  # From prior refactor
        self.stream_init_lock = threading.Lock()  # From prior refactor
        self.stream_cleanup_events = [threading.Event() for _ in range(4)]  # Events for cleanup signaling
        # Locks that serialise archive-entry background threads against
        # concurrent toggle_archive_mode(exit) calls on the main thread.
        # Acquiring the lock in _enter_archive_mode_thread and checking it
        # in the exit path prevents two threads calling cleanup_stream()
        # simultaneously, which causes a libvlc segfault.
        self.archive_entry_locks = [threading.Lock() for _ in range(4)]
        # True while a toggle_archive_mode(index) transition (entry or exit)
        # is in flight for that stream. Set/cleared only on the main thread,
        # so checking-and-setting it at the very top of toggle_archive_mode
        # is atomic with respect to other UI events (clicks, right-clicks) -
        # there is no scheduling window for a second click to sneak in
        # between "set is_archive_mode" and "start the background thread",
        # which was the root cause of the grid-view rapid-toggle segfault
        # and hang (clicking again before the transition is fully resolved).
        self.archive_transitioning = [False] * 4
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
        self.audio_buttons = [None] * 4
        # Per-stream mute state for archive/event playback. Starts muted;
        # the user explicitly unmutes via the audio toggle button.
        self.archive_audio_muted = [True] * 4

        # --- Event mode state ---
        # True while the event overlay is open and we are playing back or
        # waiting between clips.  Checked by monitor_vlc_playback and go_back
        # to route end-of-clip handling away from the normal archive navigation.
        self.event_mode = False
        # Per-cam ordered queue of absolute clip paths to play for the current
        # event.  Populated by _start_event_playback before the first clip on
        # each cam is launched; each entry is popped when that clip finishes.
        self.event_clip_queues = [[] for _ in range(4)]
        # Cams that have at least one clip in the current event.
        self.event_active_cams = set()
        # Cams whose clip queue is exhausted (including cams with no clips).
        self.event_done_cams = set()
        # Weak reference to the event overlay Frame so _on_event_clip_ended
        # can re-show it without the overlay having to register itself globally.
        self.event_overlay = None
        # The event dict currently playing, used to mark it as played on
        # completion.  Points into the in-memory events list so mutating it
        # also updates the list.
        self.current_playing_event = None
        # Events button widget — created in build_config_panel, kept here so
        # its visibility can be toggled when the config setting changes.
        self.events_button = None
        self.events_button_image = None
        # Per-stream dict: video_path -> {"position": seconds, "duration": seconds}.
        # Populated live during playback (monitor_vlc_playback) and persisted
        # to watch_progress.json so progress survives an app restart. Writes
        # are debounced - only flushed when a video is exited (go_back) or on
        # app shutdown, not on every playback poll tick, to avoid hammering
        # disk for something that updates once a second per stream.
        self.watch_progress = {index: {} for index in range(4)}
        self.watch_progress_dirty = False
        # Folders the user has opened this session, just for the dimmed-icon
        # visual cue. Navigation history isn't worth persisting across
        # restarts the way video progress is, so this stays in-memory only.
        self.visited_folders = {index: set() for index in range(4)}

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
            "events": self.create_icon("events"),
            "delete": self.create_icon("delete"),
            "audio_on": self.create_icon("audio_on"),
            "audio_off": self.create_icon("audio_off"),
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

        self.load_watch_progress()
        self.init_ui()
        self.update_streams()
        self.root.after(0, lambda: threading.Thread(target=self.start_streams, daemon=True).start())

    def _init_font_choices(self):
        """Build the list of fonts offered in Options > General > Font.

        Rather than hardcoding font names that may not exist on a given
        machine (Arial in particular is rarely installed on stock
        Linux), this probes FONT_CANDIDATES against the fonts Tk can
        actually see here (tkinter.font.families()) and only offers
        ones that are genuinely available, so a selection always
        renders correctly. One bold-styled entry is always appended,
        built from the first available font, so there's always a
        higher-contrast option regardless of platform.
        """
        try:
            installed = {name.lower() for name in tkfont.families(self.root)}
        except Exception as e:
            logging.warning(f"Could not query installed fonts, using fallback: {e}")
            installed = set()

        available = [name for name in self.FONT_CANDIDATES if name.lower() in installed]
        if not available:
            available = [self.FONT_FALLBACK]
        available = available[:5]  # Keep the dropdown to a handful of choices

        self.font_choices = [{"label": name, "family": name, "weight": "normal"} for name in available]

        bold_base = available[0]
        self.font_choices.append({"label": f"{bold_base} Bold", "family": bold_base, "weight": "bold"})

        self.font_choice_labels = [choice["label"] for choice in self.font_choices]

    def app_font(self, size, style=None):
        """Return a Tk font tuple using the user's selected UI font.

        Used everywhere the app draws its own text (Options panel,
        archive browser, overlays) so the Font setting applies
        consistently rather than just in one place. Pass `style`
        ("bold"/"italic"/"bold italic") to force a weight regardless of
        the user's selection (e.g. section headers); omit it to use the
        weight from the selected font itself (e.g. "Verdana Bold").
        """
        choice = next(
            (c for c in self.font_choices if c["label"] == self.ui_font),
            self.font_choices[0]
        )
        weight = style if style is not None else choice["weight"]
        return (choice["family"], size, weight)

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
        self.ui_font = self.font_choice_labels[0]
        self.resume_playback = True
        self.motion_triggered_events = False
        self.event_overlap_window_mins = 1
        self.exclusive_archive_audio = True

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
                # "ui_font" replaces the older "archive_font" key (read as a
                # fallback for migration). Validated against the fonts
                # actually available on this machine; anything unrecognized
                # falls back to the first validated choice.
                raw_font = config.get("ui_font", config.get("archive_font", self.ui_font))
                matched_font = next(
                    (label for label in self.font_choice_labels if label.lower() == str(raw_font).lower()),
                    None
                )
                self.ui_font = matched_font if matched_font else self.font_choice_labels[0]
                self.resume_playback = bool(config.get("resume_playback", self.resume_playback))
                self.motion_triggered_events = bool(config.get("motion_triggered_events", self.motion_triggered_events))
                self.event_overlap_window_mins = int(config.get("event_overlap_window_mins", self.event_overlap_window_mins))
                if self.event_overlap_window_mins not in (1, 2, 3, 5):
                    self.event_overlap_window_mins = 1
                self.exclusive_archive_audio = bool(config.get("exclusive_archive_audio", self.exclusive_archive_audio))

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
            "ui_font": self.ui_font,
            "resume_playback": self.resume_playback,
            "motion_triggered_events": self.motion_triggered_events,
            "event_overlap_window_mins": self.event_overlap_window_mins,
            "exclusive_archive_audio": self.exclusive_archive_audio,
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

    def load_watch_progress(self):
        """Load per-video watch progress from disk into self.watch_progress.

        The file maps cam index (as a string key, since JSON object keys must
        be strings) to {video_path: {"position": s, "duration": s}}. Missing
        or corrupt files are treated as "no progress yet" rather than an
        error - this is convenience data, not critical config.
        """
        if not os.path.exists(self.watch_progress_file):
            return
        try:
            with open(self.watch_progress_file, "r") as f:
                data = json.load(f)
            for index in range(4):
                entries = data.get(str(index), {})
                if isinstance(entries, dict):
                    cleaned = {}
                    for path, info in entries.items():
                        try:
                            position = float(info.get("position", 0))
                            duration = float(info.get("duration", 0))
                            if duration > 0 and position >= 0:
                                cleaned[path] = {"position": position, "duration": duration}
                        except (TypeError, ValueError, AttributeError):
                            continue
                    self.watch_progress[index] = cleaned
            logging.debug("Loaded watch progress from disk")
        except Exception as e:
            logging.warning(f"Failed to load watch progress from {self.watch_progress_file}: {e}")

    def save_watch_progress(self):
        """Persist self.watch_progress to disk. Cheap enough to call on
        natural checkpoints (exiting a video, app shutdown) but deliberately
        NOT called on every playback poll tick - see watch_progress_dirty."""
        try:
            data = {str(index): self.watch_progress[index] for index in range(4)}
            os.makedirs(os.path.dirname(self.watch_progress_file), exist_ok=True)
            with open(self.watch_progress_file, "w") as f:
                json.dump(data, f, indent=2)
            self.watch_progress_dirty = False
            logging.debug("Saved watch progress to disk")
        except Exception as e:
            logging.warning(f"Failed to save watch progress to {self.watch_progress_file}: {e}")

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

        def add_section_header(frame, text, row):
            """Draw a bold section header + separator at `row`, return the next free row."""
            tk.Label(frame, text=text, font=self.app_font(10, "bold")).grid(
                row=row, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=(10, 2)
            )
            row += 1
            ttk.Separator(frame, orient="horizontal").grid(
                row=row, column=0, columnspan=2, sticky="we", padx=12, pady=(0, 4)
            )
            return row + 1

        # --- General Tab ---
        row = 0

        row = add_section_header(core_frame, "Connection", row)

        # Username
        tk.Label(core_frame, text="Username:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        username_entry = tk.Entry(core_frame, width=32)
        username_entry.insert(0, self.username)
        username_entry.grid(row=row, column=1, **WIDE)
        row += 1

        # Password
        tk.Label(core_frame, text="Password:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        password_entry = tk.Entry(core_frame, width=32)
        password_entry.insert(0, self.password)
        password_entry.grid(row=row, column=1, **WIDE)
        row += 1

        # Video Path
        tk.Label(core_frame, text="Video Path:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        archive_entry = tk.Entry(core_frame, width=32)
        archive_entry.insert(0, self.archive_dir)
        archive_entry.grid(row=row, column=1, **WIDE)
        row += 1

        row = add_section_header(core_frame, "Cameras", row)

        # Camera IPs and settings
        # Each cam row: label + IP entry in col 0-1, then HQ/Audio/PTZ checkboxes in a sub-frame in col 1
        ip_entries = []
        hq_checkboxes = []
        audio_checkboxes = []
        ptz_checkboxes = []
        for i in range(4):
            tk.Label(core_frame, text=f"Cam {i+1} IP:", font=self.app_font(10)).grid(row=row, column=0, **LBL)

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

        row = add_section_header(core_frame, "Playback & Display", row)

        # PTZ Travel
        tk.Label(core_frame, text="PTZ Travel:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        ptz_resolution_var = tk.IntVar(value=self.ptz_resolution)
        ttk.Combobox(
            core_frame, textvariable=ptz_resolution_var, values=[1, 2, 3, 4, 5], state="readonly", width=6
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        # Playback Speed
        tk.Label(core_frame, text="Playback Speed:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        playback_speed_var = tk.DoubleVar(value=self.default_playback_speed)
        ttk.Combobox(
            core_frame, textvariable=playback_speed_var, values=self.speed_cycle, state="readonly", width=6
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        # Font - applies wherever the app draws its own text (this dialog,
        # the archive browser, overlays), not just archive mode. Options are
        # limited to fonts actually installed on this machine; see
        # _init_font_choices().
        tk.Label(core_frame, text="Font:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        font_var = tk.StringVar(value=self.ui_font)
        ttk.Combobox(
            core_frame, textvariable=font_var, values=self.font_choice_labels, state="readonly", width=16
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        row = add_section_header(core_frame, "Behavior", row)

        # Show Stream Buttons
        fullscreen_buttons_var = tk.BooleanVar(value=self.enable_fullscreen_buttons)
        ttk.Checkbutton(core_frame, text="Show Stream Buttons", variable=fullscreen_buttons_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        # Resume Playback
        resume_playback_var = tk.BooleanVar(value=self.resume_playback)
        ttk.Checkbutton(
            core_frame, text="Resume Archive Clips From Last Position", variable=resume_playback_var
        ).grid(row=row, column=0, **SPAN)
        row += 1

        # Exclusive archive audio
        exclusive_audio_var = tk.BooleanVar(value=self.exclusive_archive_audio)
        ttk.Checkbutton(
            core_frame, text="Exclusive Archive Audio (unmuting one clip mutes others)",
            variable=exclusive_audio_var
        ).grid(row=row, column=0, **SPAN)
        row += 1

        # Motion Triggered Events
        motion_events_var = tk.BooleanVar(value=self.motion_triggered_events)
        ttk.Checkbutton(core_frame, text="Motion Triggered Events", variable=motion_events_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        # Event Overlap Window — enabled/disabled inline with the checkbox above
        tk.Label(core_frame, text="Event Overlap Window:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        event_overlap_var = tk.IntVar(value=self.event_overlap_window_mins)
        overlap_combo = ttk.Combobox(
            core_frame, textvariable=event_overlap_var, values=[1, 2, 3, 5], state="readonly", width=6
        )
        overlap_combo.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        tk.Label(core_frame, text="min", font=self.app_font(10)).grid(
            row=row, column=1, sticky="w", padx=(62, 0), pady=4
        )
        row += 1

        def _update_overlap_state(*_):
            overlap_combo.config(state="readonly" if motion_events_var.get() else "disabled")
        motion_events_var.trace_add("write", _update_overlap_state)
        _update_overlap_state()

        # Save Window Size
        save_window_size_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(core_frame, text="Save Window Size", variable=save_window_size_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        # --- Advanced Tab ---
        row = 0

        row = add_section_header(advanced_frame, "Stream Reliability", row)

        enable_retries_var = tk.BooleanVar(value=self.enable_retries)
        ttk.Checkbutton(advanced_frame, text="Enable Automatic Retries", variable=enable_retries_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        tk.Label(advanced_frame, text="Max Retry Attempts:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        max_retry_attempts_entry = tk.Entry(advanced_frame, width=10)
        max_retry_attempts_entry.insert(0, str(self.max_retry_attempts))
        max_retry_attempts_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Initial Backoff Delay (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        initial_backoff_delay_entry = tk.Entry(advanced_frame, width=10)
        initial_backoff_delay_entry.insert(0, str(self.initial_backoff_delay))
        initial_backoff_delay_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        row = add_section_header(advanced_frame, "Quality Downgrading", row)

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

        tk.Label(advanced_frame, text="Frame Drop Threshold:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        drop_threshold_entry = tk.Entry(advanced_frame, width=10)
        drop_threshold_entry.insert(0, str(self.drop_threshold))
        drop_threshold_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Frame Drop Window (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        drop_window_entry = tk.Entry(advanced_frame, width=10)
        drop_window_entry.insert(0, str(self.drop_window))
        drop_window_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Downgrade Cooldown (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        downgrade_cooldown_entry = tk.Entry(advanced_frame, width=10)
        downgrade_cooldown_entry.insert(0, str(self.downgrade_cooldown))
        downgrade_cooldown_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Stability Period (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        stability_period_entry = tk.Entry(advanced_frame, width=10)
        stability_period_entry.insert(0, str(self.stability_period))
        stability_period_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="No-Frame Timeout (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        no_frame_timeout_entry = tk.Entry(advanced_frame, width=10)
        no_frame_timeout_entry.insert(0, str(self.no_frame_timeout))
        no_frame_timeout_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        row = add_section_header(advanced_frame, "Events Cache", row)

        def _clear_events_cache():
            events_dir = self._events_dir()
            removed = 0
            errors = 0
            if os.path.isdir(events_dir):
                for root_dir, dirs, files in os.walk(events_dir):
                    for fname in files:
                        if fname.endswith(".json"):
                            try:
                                os.remove(os.path.join(root_dir, fname))
                                removed += 1
                            except Exception as e:
                                logging.warning(f"Could not remove events cache file: {e}")
                                errors += 1
            if errors:
                messagebox.showwarning(
                    "Events Cache",
                    f"Removed {removed} file(s), {errors} could not be deleted.",
                    parent=dialog
                )
            else:
                messagebox.showinfo(
                    "Events Cache",
                    f"Cleared {removed} cached event file(s)." if removed else "No cached event files found.",
                    parent=dialog
                )

        cache_row = tk.Frame(advanced_frame)
        cache_row.grid(row=row, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=4)
        tk.Button(
            cache_row, text="Clear Events Cache", font=self.app_font(10),
            command=_clear_events_cache
        ).pack(side="left")
        tk.Label(
            cache_row, text="Forces a fresh scan next time Events are opened",
            font=self.app_font(9), fg="#888888"
        ).pack(side="left", padx=(10, 0))
        row += 1

        row = add_section_header(advanced_frame, "VLC Options", row)

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
            button_frame, text="Save", width=10, font=self.app_font(10),
            command=lambda: self.save_streams(
                username_entry, password_entry, ip_entries,
                hq_checkboxes, audio_checkboxes, ptz_checkboxes,
                fullscreen_buttons_var, debug_var, archive_entry, vlc_params,
                ptz_resolution_var, save_window_size_var, dialog,
                enable_retries_var, max_retry_attempts_entry, initial_backoff_delay_entry,
                enable_quality_downgrade_var, drop_threshold_entry, drop_window_entry,
                downgrade_cooldown_entry, enable_auto_revert_hq_var, stability_period_entry,
                playback_speed_var, font_var, no_frame_timeout_entry, resume_playback_var,
                motion_events_var, event_overlap_var, exclusive_audio_var
            )
        ).pack(side="left", padx=5)

        tk.Button(
            button_frame, text="Cancel", width=10, font=self.app_font(10),
            command=dialog.destroy
        ).pack(side="left", padx=5)

        dialog.update_idletasks()

    def save_streams(self, username_entry, password_entry, ip_entries, hq_checkboxes, audio_checkboxes, ptz_checkboxes, fullscreen_buttons_var, debug_var, archive_entry, vlc_params, ptz_resolution_var, save_window_size_var, dialog, enable_retries_var, max_retry_attempts_entry, initial_backoff_delay_entry, enable_quality_downgrade_var, drop_threshold_entry, drop_window_entry, downgrade_cooldown_entry, enable_auto_revert_hq_var, stability_period_entry, playback_speed_var, font_var=None, no_frame_timeout_entry=None, resume_playback_var=None, motion_events_var=None, event_overlap_var=None, exclusive_audio_var=None):
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
        if font_var is not None:
            chosen = font_var.get()
            self.ui_font = chosen if chosen in self.font_choice_labels else self.font_choice_labels[0]
        if resume_playback_var is not None:
            self.resume_playback = resume_playback_var.get()
        if motion_events_var is not None:
            self.motion_triggered_events = motion_events_var.get()
        if event_overlap_var is not None:
            v = int(event_overlap_var.get())
            self.event_overlap_window_mins = v if v in (1, 2, 3, 5) else 1
        if exclusive_audio_var is not None:
            self.exclusive_archive_audio = exclusive_audio_var.get()
    
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
        elif icon_type == "events":
            # Calendar/lightning bolt: a small rectangle with a ⚡ inside
            draw.rectangle([(9, 11), (31, 31)], outline="white", width=2)
            draw.line([(9, 17), (31, 17)], fill="white", width=2)
            draw.line([(14, 9), (14, 14)], fill="white", width=2)
            draw.line([(26, 9), (26, 14)], fill="white", width=2)
            # lightning bolt inside calendar body
            draw.polygon([(22, 19), (18, 25), (21, 25), (18, 31), (24, 23), (21, 23)], fill="white")
        elif icon_type == "delete":
            # Trash can
            draw.rectangle([(13, 14), (27, 31)], outline="white", width=2)
            draw.line([(10, 14), (30, 14)], fill="white", width=2)
            draw.line([(17, 11), (23, 11)], fill="white", width=2)
            draw.line([(17, 18), (17, 28)], fill="white", width=1)
            draw.line([(20, 18), (20, 28)], fill="white", width=1)
            draw.line([(23, 18), (23, 28)], fill="white", width=1)
        elif icon_type == "audio_on":
            # Speaker with sound waves
            draw.polygon([(10, 14), (10, 26), (16, 26), (22, 32), (22, 8), (16, 14)], fill="white")
            draw.arc([(23, 13), (31, 27)], start=300, end=60, fill="white", width=2)
            draw.arc([(25, 10), (35, 30)], start=300, end=60, fill="white", width=2)
        elif icon_type == "audio_off":
            # Speaker with X (muted)
            draw.polygon([(10, 14), (10, 26), (16, 26), (22, 32), (22, 8), (16, 14)], fill="white")
            draw.line([(25, 14), (33, 26)], fill="white", width=2)
            draw.line([(33, 14), (25, 26)], fill="white", width=2)
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
                # In event mode the quadrants show archive clips or idle
                # placeholders — fullscreen-zoom on click is a live-view
                # behaviour and should not fire here.
                if self.event_mode:
                    logging.debug(f"Stream {index}: No binding applied (event mode active)")
                    return
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

        # --- Event mode intercept ---
        # Right-click has a two-step dismissal in event mode so the user
        # doesn't accidentally drop all the way back to live in one click.
        #
        # Step 1 — clips are playing in at least one quadrant:
        #   Stop all active players, black out their quadrants, re-show the
        #   event listing.  Equivalent to all clips ending simultaneously.
        #
        # Step 2 — overlay is visible (no clips playing):
        #   Exit event mode entirely and return to live streams.
        if self.event_mode:
            playing = self.event_active_cams - self.event_done_cams
            if playing:
                # Force-finish every cam that still has an active player.
                # Mark them all as done first so the last cam's completion
                # check inside _on_event_clip_ended sees a full done set and
                # triggers the overlay re-show rather than thinking there are
                # still cams outstanding.
                for i in list(playing):
                    self.event_clip_queues[i] = []   # clear queue so no next-clip is started
                    self.event_done_cams.add(i)
                    self.cleanup_stream(i)
                    for widget in self.labels[i].winfo_children():
                        widget.destroy()
                    self.exit_buttons[i]   = None
                    self.pause_buttons[i]  = None
                    self.speed_buttons[i]  = None
                    self.replay_buttons[i] = None
                    self.rewind_buttons[i] = None
                    self.audio_buttons[i]  = None
                    self.video_ended[i]    = False
                    self.labels[i].configure(image="", text=f"Cam {i + 1}", fg="#888888", bg="black")

                # All cams are now done — mark event played and re-show overlay
                if self.current_playing_event:
                    self.current_playing_event["played"] = True
                    self._save_events_json(
                        getattr(self, "_event_date_for_save", None),
                        getattr(self, "_event_list_for_save", [])
                    )
                    try:
                        if self._event_played_label and self._event_played_label.winfo_exists():
                            self._event_played_label.configure(text="✓")
                    except Exception:
                        pass

                # If a single-cam event entered fullscreen, drop back to grid
                # before re-showing the overlay so it centres over all panels.
                if getattr(self, '_event_entered_fullscreen', False):
                    self.is_fullscreen = False
                    self.fullscreen_index = -1
                    self._event_entered_fullscreen = False
                    self.update_layout()
                    self.build_config_panel()

                if self.event_overlay and self.event_overlay.winfo_exists():
                    ow, oh = getattr(self, "_event_overlay_size", (820, 500))
                    self.event_overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
                    self.event_overlay.lift()
            else:
                # Overlay is already showing, no clips running — exit to live
                self._exit_event_mode()
            return
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

            # Initialize events button (only relevant when motion_triggered_events is on)
            if not self.events_button:
                self.events_button_image = self.icon_cache["events"]
                self.events_button = tk.Button(
                    self.config_panel, image=self.events_button_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.toggle_event_mode, cursor="hand2"
                )
                self.events_button.bind("<Button-1>", lambda e: logging.debug("Clicked events button"))
                logging.debug("Created events button")

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
                          ([self.events_button] if self.events_button else []) + \
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
                # Pack archive mode button only in grid mode if archive_dir is valid.
                # Disable while any stream is still initializing so a premature
                # click can't race against a live libvlc player mid-setup.
                if self.archive_dir:
                    any_initializing = any(self.stream_initializing)
                    self.archive_mode_button.configure(
                        state="disabled" if any_initializing else "normal"
                    )
                    self.archive_mode_button.pack(pady=5, padx=10)
                # Pack events button in grid mode if motion_triggered_events is on
                # and archive_dir is set (clips needed for scanning).
                # Disable while any stream is still initializing so a premature
                # click can't race against a live libvlc player mid-setup.
                if self.motion_triggered_events and self.archive_dir:
                    any_initializing = any(self.stream_initializing)
                    self.events_button.configure(
                        state="disabled" if any_initializing else "normal"
                    )
                    self.events_button.pack(pady=5, padx=10)
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
        self.root.bind("<KeyPress-q>", lambda e: self.cleanup())
        self.root.bind("<KeyPress-Q>", lambda e: self.cleanup())
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

                # If init_stream failed because we were signalled to abort
                # (e.g. archive mode was entered), it has already released
                # its own player. Don't call cleanup_stream() again here —
                # that responsibility belongs to whoever set the event, and
                # calling it a second time risks racing with that thread's
                # own cleanup_stream() call once stream_initializing clears.
                if self.stream_cleanup_events[index].is_set():
                    logging.info(f"Stream {index}: Init failed due to abort signal, yielding cleanup to signaller")
                    return False

                if attempt == max_attempts - 1:
                    self.cleanup_stream(index)
                    self.update_stream_label(index, "Stream Failed, click to reconnect")
                    self.bind_retry_connection(index)
                    if self.fullscreen_buttons[index]:
                        self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget())
                    logging.error(f"Stream {index}: All attempts failed")
                    return False

                logging.info(f"Stream {index}: Attempt {attempt+1} failed, retrying in {backoff_delay:.2f}s")
                # Use the cleanup event as an interruptible sleep — if archive
                # mode is entered while we are in backoff, the event fires and
                # we wake immediately instead of waiting the full delay.
                self.stream_cleanup_events[index].wait(timeout=backoff_delay)
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
            self.vlc_instances[index] = instance
            player = instance.media_player_new()
            if not player:
                raise RuntimeError("Failed to create VLC media player")
            self.media_players[index] = player
            media = instance.media_new(self.streams[index])
            player.set_media(media)
            player.set_xwindow(xid) if sys.platform.startswith("linux") else player.set_hwnd(xid)

            if player.play() == -1:
                raise RuntimeError("Failed to start VLC player")

            # Mute immediately — audio must never play in grid view.
            # set_audio_state() can't be used here (it checks stream_initializing
            # which is True for this stream right now), so call libvlc directly.
            try:
                player.audio_set_mute(True)
            except Exception:
                pass

            while time.time() - start_wait < timeout:
                # Abort promptly if cleanup/archive-entry has been signalled.
                # We release the player we just created right here, rather
                # than leaving it for the caller, because the thread that
                # signalled the abort (e.g. _enter_archive_mode_thread_locked)
                # is waiting on stream_initializing and will proceed to do
                # its own cleanup the moment this function returns. If we
                # left the player referenced, two threads would both try to
                # stop()/release() it, which segfaults libvlc.
                if self.stream_cleanup_events[index].is_set():
                    logging.info(f"Stream {index}: Abort signal during frame wait, stopping init")
                    try:
                        if player.get_state() not in (vlc.State.Stopped, vlc.State.Ended, vlc.State.Error):
                            player.stop()
                        player.release()
                    except Exception as e:
                        logging.warning(f"Stream {index}: Error releasing player during abort: {e}")
                    self.media_players[index] = None
                    # Each stream now owns its instance outright, so we must
                    # also release it here - nothing else will.
                    try:
                        instance.release()
                    except Exception as e:
                        logging.warning(f"Stream {index}: Error releasing VLC instance during abort: {e}")
                    if self.vlc_instances[index] is instance:
                        self.vlc_instances[index] = None
                    return False
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

            # Release this stream's own VLC instance. Each stream owns its
            # instance exclusively (see vlc_instances in __init__), so there
            # is no cross-stream "still in use" check needed - releasing it
            # here can never affect another stream's player.
            if self.vlc_instances[index]:
                try:
                    self.vlc_instances[index].release()
                    logging.debug(f"Stream {index}: Released VLC instance")
                except Exception as e:
                    logging.error(f"Stream {index}: Error releasing VLC instance: {e}")
                self.vlc_instances[index] = None

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

    def _disable_stream_action_buttons(self):
        """Disable the archive-mode and events buttons on the main thread.

        Called before any batch of stream init threads starts so neither
        button can be clicked while libvlc players are still being set up.
        Must be called via root.after() when invoked from a background thread.
        """
        if self.archive_mode_button:
            self.archive_mode_button.configure(state="disabled")
        if self.events_button:
            self.events_button.configure(state="disabled")

    def _reenable_stream_action_buttons(self):
        """Re-enable the archive-mode and events buttons on the main thread,
        respecting whether each feature is actually configured.

        Called via root.after() once all stream init threads have joined.
        """
        if self.archive_mode_button and self.archive_dir:
            self.archive_mode_button.configure(state="normal")
        if self.events_button and self.motion_triggered_events and self.archive_dir:
            self.events_button.configure(state="normal")

    def start_streams(self):
        # Disable the archive and events buttons for the duration of stream
        # init so the user can't interact with either before any live libvlc
        # players exist.  Matches the same guard applied when exiting event mode.
        self.root.after(0, self._disable_stream_action_buttons)

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

        # All inits done — re-enable buttons that are appropriate for the
        # current config.
        self.root.after(0, self._reenable_stream_action_buttons)

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
            # access required, safe to do synchronously. toggle_archive_mode
            # silently no-ops for any stream that's mid-transition, so this
            # loop is safe to call unconditionally.
            for i in range(4):
                if self.is_archive_mode[i] and not self.archive_transitioning[i]:
                    self.toggle_archive_mode(i, rebuild_ui=False)
                    logging.info(f"Stream {i}: Exited archive mode via global toggle")
            self.build_config_panel()
            return

        eligible = [i for i in range(4) if self.streams[i] and not self.is_archive_mode[i]]
        if not eligible:
            return

        for i in eligible:
            if not self.archive_transitioning[i]:
                self.toggle_archive_mode(i, rebuild_ui=False)
                logging.info(f"Stream {i}: Entered archive mode via global toggle")
        self.build_config_panel()

    def toggle_archive_mode(self, index, rebuild_ui=True):
        if not self.archive_dir:
            logging.debug(f"Stream {index}: Toggle archive mode ignored, no archive directory configured")
            return

        # Reject re-entrant calls while a transition is already in flight
        # for this stream. This check-and-set happens synchronously on the
        # main thread (toggle_archive_mode is only ever called from Tk
        # event handlers), so there is no window for a rapid double-click
        # or click-then-right-click to start a second transition before the
        # first one has set up its background thread / locks. Without this,
        # two transitions could interleave their cleanup_stream() calls on
        # the same libvlc player and segfault, or leave is_archive_mode and
        # the visible canvas out of sync (the reported hang).
        if self.archive_transitioning[index]:
            logging.debug(f"Stream {index}: Archive transition already in progress, ignoring toggle")
            return
        self.archive_transitioning[index] = True

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
            # Also hide any nav buttons left over from a previous archive session
            # so they don't appear on top of the loading screen.
            loading_shown = threading.Event()
            if hasattr(self, "nav_buttons") and index < len(self.nav_buttons):
                for btn in self.nav_buttons[index].values():
                    btn.place_forget()
            if self.back_buttons[index]:
                self.back_buttons[index].place_forget()
            panel_width, panel_height = self.panel_sizes[index]
            self.archive_canvas[index].create_text(
                panel_width // 2, panel_height // 2,
                text="Loading...", fill="white", font=self.app_font(-16)
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

            # Disable both action buttons while this stream re-initializes so
            # the user can't click archive/events and race against the new player.
            self._disable_stream_action_buttons()

            # Start stream initialization in a separate thread
            def _init_and_reenable():
                self.try_init_stream_with_retries(index)
                self.root.after(0, self._reenable_stream_action_buttons)

            threading.Thread(target=_init_and_reenable, args=(), daemon=True).start()

            if rebuild_ui:
                self.build_config_panel()

            # Exit is fully synchronous from here, so the transition is
            # over now - clear the flag so the next toggle is accepted.
            self.archive_transitioning[index] = False

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
            try:
                if not self.is_archive_mode[index]:
                    # User toggled back out while we were waiting
                    return
                if not exists:
                    self.archive_canvas[index].delete("all")
                    panel_width, panel_height = self.panel_sizes[index]
                    self.archive_canvas[index].create_text(
                        panel_width // 2, panel_height // 2,
                        text="Archive directory not found", fill="white", font=self.app_font(-16)
                    )
                    return
                self.pagination_state[index] = {root_path: 0}
                self.current_archive_path[index] = root_path
                self.render_archive_view(index)
            finally:
                # The entry transition is fully resolved now (canvas shows
                # either the browser or an error state) - accept new toggles.
                self.archive_transitioning[index] = False

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

    def draw_progress_bar(self, index, x, y, width, height, progress):
        """Draw a thin YouTube-style red progress bar along the bottom edge
        of a thumbnail/icon rectangle (x, y, width, height), filled to
        progress['position'] / progress['duration']. A faint full-width
        track is drawn first so the unwatched portion is still visible,
        matching the familiar red-on-grey look.

        The surrounding white border is a 2px outline centered on this same
        rect, so 1px of it extends inward on every side. The bar is inset by
        that 1px on the left/right/bottom so it sits flush inside the border
        rather than drawing over it.
        """
        BORDER_INSET = 1
        bar_height = 4
        bar_x = x + BORDER_INSET
        bar_width = width - (BORDER_INSET * 2)
        bar_bottom = y + height - BORDER_INSET
        bar_y = bar_bottom - bar_height

        duration = progress.get("duration", 0)
        if not duration or duration <= 0:
            return
        fraction = max(0.0, min(1.0, progress.get("position", 0) / duration))

        # Track (unwatched portion background)
        self.archive_canvas[index].create_rectangle(
            bar_x, bar_y, bar_x + bar_width, bar_bottom,
            fill="#3d3d3d", outline=""
        )
        # Filled (watched) portion
        if fraction > 0:
            self.archive_canvas[index].create_rectangle(
                bar_x, bar_y, bar_x + (bar_width * fraction), bar_bottom,
                fill="#e62117", outline=""
            )

    def render_archive_view(self, index):
        # Hide nav buttons before clearing so they never appear on a
        # loading/transitioning canvas.
        if hasattr(self, "nav_buttons") and index < len(self.nav_buttons):
            if "prev" in self.nav_buttons[index]:
                self.nav_buttons[index]["prev"].place_forget()
            if "next" in self.nav_buttons[index]:
                self.nav_buttons[index]["next"].place_forget()

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
                    # Files (in folder path) are sorted ascending.
                    # Supports both old (HH-MM) and new (HH-MM-SS) filename formats.
                    match = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}(?:-\d{2})?)_(\d+m-\d+s)\.mp4$", item)
                    if not match:
                        logging.warning(f"Stream {index}: Invalid video format for {item}")
                        return datetime.min
                    date_str, time_str, _ = match.groups()
                    date_time = f"{date_str} {time_str.replace('-', ':')}"
                    fmt = "%Y-%m-%d %H:%M:%S" if time_str.count('-') == 2 else "%Y-%m-%d %H:%M"
                    return datetime.strptime(date_time, fmt)
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
                80, 25, anchor="w", text=f"{location}", fill="white", font=self.app_font(-17)
            )

        # Render items for the current page
        page_images = []
        for item in page_items:
            full_path = os.path.join(path, item)
            is_visited = os.path.normpath(full_path) in self.visited_folders[index]
            progress = self.watch_progress[index].get(full_path)

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
                        folder_img = self.get_day_folder_icon(day_abbrev, is_visited)
                    except Exception:
                        folder_img = self.icon_cache["folder_clicked" if is_visited else "folder"]
                else:
                    folder_img = self.icon_cache["folder_clicked" if is_visited else "folder"]

                folder_id = self.archive_canvas[index].create_image(x + item_width // 2, y + icon_size // 2, image=folder_img)
                if day_match:
                    text_id = self.archive_canvas[index].create_text(
                        x + item_width // 2, y + icon_size + 10, text=item, fill="white", font=self.app_font(-17), anchor="n"
                    )
                else:
                    text_id = self.archive_canvas[index].create_text(
                        x + item_width // 2, y + icon_size + 10, text=item[:10], fill="white", font=self.app_font(-17), anchor="n"
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
                            if progress:
                                self.draw_progress_bar(
                                    index, thumb_x, y, thumbnail_width, thumbnail_height, progress
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
                            if progress:
                                icon_x = x + (item_width - icon_size) // 2
                                self.draw_progress_bar(
                                    index, icon_x, y, icon_size, icon_size, progress
                                )
                    else:
                        # Fallback to cached icon
                        video_img = self.icon_cache["archive"]
                        video_id = self.archive_canvas[index].create_image(
                            x + item_width // 2, y + icon_size // 2, image=video_img
                        )
                        page_images.append(video_img)
                        if progress:
                            icon_x = x + (item_width - icon_size) // 2
                            self.draw_progress_bar(
                                index, icon_x, y, icon_size, icon_size, progress
                            )
                else:
                    # Render cached video icon
                    video_img = self.icon_cache["archive"]
                    video_id = self.archive_canvas[index].create_image(
                        x + item_width // 2, y + icon_size // 2, image=video_img
                    )
                    page_images.append(video_img)
                    if progress:
                        icon_x = x + (item_width - icon_size) // 2
                        self.draw_progress_bar(
                            index, icon_x, y, icon_size, icon_size, progress
                        )

                # Label shows HH:MM (and clip duration) for both old HH-MM
                # and new HH-MM-SS filename formats.
                time_parts = item.split('_')[1].split('-')[:2]
                time_label = ':'.join(time_parts)
                dur_label  = item.split('_')[2].split('.')[0].replace('-', '')
                label = f"{time_label} {dur_label}"
                text_id = self.archive_canvas[index].create_text(
                    x + item_width // 2, y + (thumbnail_height if use_thumbnails else icon_size) + 10, text=label, fill="white", font=self.app_font(-17), anchor="n"
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
                25,
                text=f"PAGE {current_page + 1}/{total_pages}",
                fill="white",
                font=self.app_font(-17),
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
        """Show or hide the keyboard-shortcuts panel.

        The panel is a plain tk.Frame placed over self.grid_frame with
        place() + lift() so it stays strictly inside the app window.
        Triggered by H.
        """
        if self.help_overlay is not None:
            try:
                self.help_overlay.destroy()
            except Exception:
                pass
            self.help_overlay = None
            self.root.focus_set()
            return

        # Build the panel as a Frame inside the grid area so it never
        # floats outside the app window.
        overlay = tk.Frame(
            self.grid_frame, bg="#222222",
            highlightbackground="#555555", highlightthickness=1
        )

        shortcuts = [
            ("General", [
                ("H", "Show or hide this help overlay"),
                ("Q", "Quit the application"),
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

        container = tk.Frame(overlay, bg="#222222", padx=30, pady=24)
        container.pack()

        title_label = tk.Label(container, text="Keyboard Shortcuts", bg="#222222", fg="white",
                                font=self.app_font(18, "bold"))
        title_label.pack(anchor="w", pady=(0, 14))

        for section_title, items in shortcuts:
            section_label = tk.Label(container, text=section_title, bg="#222222", fg="#4a90d9",
                                      font=self.app_font(13, "bold"))
            section_label.pack(anchor="w", pady=(10, 4))
            for key, desc in items:
                row = tk.Frame(container, bg="#222222")
                row.pack(anchor="w", fill="x")
                key_label = tk.Label(row, text=key, bg="#222222", fg="white",
                                      font=("consolas", 11, "bold"), width=22, anchor="w")
                key_label.pack(side="left")
                desc_label = tk.Label(row, text=desc, bg="#222222", fg="#cccccc",
                                       font=self.app_font(11), anchor="w")
                desc_label.pack(side="left", padx=(10, 0))

        # Stream reliability section
        tk.Label(container, text="Stream Reliability  —  current settings",
                 bg="#222222", fg="#4a90d9", font=self.app_font(13, "bold")).pack(anchor="w", pady=(16, 4))
        rel_grid = tk.Frame(container, bg="#222222")
        rel_grid.pack(anchor="w", fill="x")
        for r_idx, (label, value, tip) in enumerate(reliability_info):
            tk.Label(rel_grid, text=label, bg="#222222", fg="#cccccc",
                     font=self.app_font(10), width=22, anchor="w").grid(row=r_idx, column=0, sticky="w", pady=1)
            tk.Label(rel_grid, text=value, bg="#222222", fg="white",
                     font=("consolas", 10, "bold"), width=12, anchor="w").grid(row=r_idx, column=1, sticky="w", padx=(6, 0), pady=1)
            tk.Label(rel_grid, text=tip, bg="#222222", fg="#777777",
                     font=self.app_font(10, "italic"), anchor="w").grid(row=r_idx, column=2, sticky="w", padx=(10, 0), pady=1)

        hint_label = tk.Label(container, text="Press H, or click anywhere to close",
                               bg="#222222", fg="#777777", font=self.app_font(10, "italic"))
        hint_label.pack(anchor="w", pady=(16, 0))

        # Close on click anywhere in the panel.  Keyboard shortcuts H
        # already route through self.root bindings so no duplicate binds needed.
        for widget in (overlay, container, title_label, hint_label):
            widget.bind("<Button-1>", lambda e: self.toggle_help_overlay())

        # Measure content then centre over the grid area
        overlay.update_idletasks()
        overlay.place(relx=0.5, rely=0.5, anchor="center")
        overlay.lift()

        self.help_overlay = overlay

    def handle_item_click(self, index, path, callback):
        # Progress (and thus "has this been watched") is now tracked
        # automatically by monitor_vlc_playback once playback starts, so
        # there's nothing to record here - just dispatch to the real handler.
        callback(index, path)

    def open_folder(self, index, path):
        self.visited_folders[index].add(os.path.normpath(path))
        self.current_archive_path[index] = path
        path = os.path.normpath(path)
        if path not in self.pagination_state[index]:
            self.pagination_state[index][path] = 0
        self.render_archive_view(index)

    def go_back(self, index):
        # In event mode the exit button should follow the clip queue rather
        # than navigating the archive folder tree or tearing down the whole
        # session.  _on_event_clip_ended handles all three cases:
        #   - more clips in this cam's queue  → play next clip
        #   - queue empty, other cams still playing → black out this quadrant
        #   - queue empty, all cams done       → re-show event listing
        # It also calls cleanup_stream(index) internally so the still-running
        # VLC instance is stopped before the next action regardless of which
        # branch is taken.
        if self.event_mode:
            self._on_event_clip_ended(index)
            return

        # Flush watch progress before tearing down the player, while we still
        # have current_archive_path[index] pointing at the video we were on.
        if self.watch_progress_dirty:
            self.save_watch_progress()

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
        self.audio_buttons[index] = None
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

    # =========================================================================
    # Events feature
    # =========================================================================

    def _events_dir(self):
        """Return the base directory where per-day event JSON files are stored."""
        config_dir = os.path.dirname(self.config_file)
        return os.path.join(config_dir, "events")

    def _events_path(self, date):
        """Return the full path for a given date's event JSON file.

        Layout: <config_dir>/events/<YYYY>/<YYYYMMDD>.json
        Matches the year-subfolder structure agreed during design to avoid
        an unbounded flat directory as years accumulate.
        """
        return os.path.join(self._events_dir(), str(date.year), date.strftime("%Y%m%d") + ".json")

    def _scan_events_for_date(self, date):
        """Scan archive clips for date and cluster them into events.

        Reads filenames only — no file I/O beyond os.listdir.  Clips whose
        name does not match the expected pattern are silently skipped.

        Clustering algorithm (gap-based):
          1. Collect all clips from all cams with a parseable timestamp.
          2. Sort the complete list by clip start time.
          3. Walk the list, keeping a running "event window end".  A clip that
             starts within event_overlap_window_mins of the current window end
             extends the window and is merged into the current event cluster.
             A clip that starts after the window end opens a new event.
          4. Events that involve only a single cam are still kept — they are
             valid single-camera events.

        Returns a list of event dicts in the JSON schema described above.
        """
        from datetime import timedelta

        date_str = date.strftime("%Y-%m-%d")
        min_duration_s = 10          # Ignore clips shorter than this
        gap_limit = timedelta(minutes=self.event_overlap_window_mins)

        # --- Collect all parseable clips across all cams ---
        clip_re = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}(?:-\d{2})?)_(\d+)m-(\d+)s\.mp4$")
        all_clips = []  # (start_dt, end_dt, cam_index, abs_path)

        for cam_idx in range(4):
            if not self.ips[cam_idx]:
                continue
            day_folder = os.path.join(self.archive_dir, f"cam{cam_idx + 1}", date_str)
            if not os.path.isdir(day_folder):
                continue
            try:
                entries = os.listdir(day_folder)
            except OSError as e:
                logging.warning(f"Events scan: cannot list {day_folder}: {e}")
                continue
            for fname in entries:
                m = clip_re.match(fname)
                if not m:
                    continue
                d_str, t_str, mins_str, secs_str = m.groups()
                try:
                    start_dt = datetime.strptime(f"{d_str} {t_str.replace('-', ':')}", "%Y-%m-%d %H:%M:%S" if t_str.count('-') == 2 else "%Y-%m-%d %H:%M")
                    duration_s = int(mins_str) * 60 + int(secs_str)
                except (ValueError, TypeError):
                    continue
                if duration_s < min_duration_s:
                    continue
                from datetime import timedelta as _td
                end_dt = start_dt + _td(seconds=duration_s)
                all_clips.append((start_dt, end_dt, cam_idx, os.path.join(day_folder, fname)))

        if not all_clips:
            return []

        all_clips.sort(key=lambda c: c[0])

        # --- Cluster into events using gap-based merge ---
        events = []
        current_cluster = []     # list of clip tuples in this event
        window_end = None        # furthest end time seen so far in this cluster

        def _finalise_cluster(cluster):
            """Convert a raw clip cluster into the event dict schema."""
            cams_data = {}
            for cam_i in range(4):
                cams_data[str(cam_i + 1)] = {"enabled": False, "clips": []}

            e_start = cluster[0][0]
            e_end   = cluster[0][1]
            for s_dt, e_dt, ci, path in cluster:
                if s_dt < e_start:
                    e_start = s_dt
                if e_dt > e_end:
                    e_end = e_dt
                cams_data[str(ci + 1)]["clips"].append({
                    "path":       path,
                    "clip_start": s_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "clip_end":   e_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                cams_data[str(ci + 1)]["enabled"] = True

            # Sort clips within each cam by start time
            for cd in cams_data.values():
                cd["clips"].sort(key=lambda c: c["clip_start"])

            return {
                "id":      e_start.strftime("%Y%m%d_%H%M%S"),
                "start":   e_start.strftime("%Y-%m-%dT%H:%M:%S"),
                "end":     e_end.strftime("%Y-%m-%dT%H:%M:%S"),
                "played":  False,
                "cams":    cams_data,
            }

        for clip in all_clips:
            s_dt, e_dt, ci, path = clip
            if window_end is None or s_dt > window_end + gap_limit:
                # New event cluster
                if current_cluster:
                    events.append(_finalise_cluster(current_cluster))
                current_cluster = [clip]
                window_end = e_dt
            else:
                current_cluster.append(clip)
                if e_dt > window_end:
                    window_end = e_dt

        if current_cluster:
            events.append(_finalise_cluster(current_cluster))

        return events

    def _load_or_scan_events(self, date):
        """Return the events list for date, using cached JSON for past days.

        For today's date the cache is always regenerated because new clips may
        have appeared since the last open.  For past dates the JSON is trusted
        as immutable once written.
        """
        from datetime import date as _date
        json_path = self._events_path(date)
        today = _date.today()
        is_today = (date.year == today.year and date.month == today.month and date.day == today.day)

        if not is_today and os.path.exists(json_path):
            try:
                with open(json_path) as f:
                    data = json.load(f)
                return data.get("events", [])
            except Exception as e:
                logging.warning(f"Events: failed to read cache {json_path}: {e}")

        # Scan from archive
        events = self._scan_events_for_date(date)
        self._save_events_json(date, events)
        return events

    def _save_events_json(self, date, events):
        """Persist events list for date to its JSON file."""
        json_path = self._events_path(date)
        try:
            os.makedirs(os.path.dirname(json_path), exist_ok=True)
            payload = {
                "date":       date.strftime("%Y-%m-%d"),
                "scanned_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "events":     events,
            }
            with open(json_path, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logging.warning(f"Events: failed to write cache {json_path}: {e}")

    def toggle_event_mode(self):
        """Toggle between event-mode (overlay + coordinated playback) and live."""
        if self.event_mode:
            self._exit_event_mode()
        else:
            self._open_event_overlay()

    def _exit_event_mode(self):
        """Tear down event mode and return all quadrants to live streams.

        All live streams were stopped when entering event mode, so every cam
        with a configured IP needs to be restarted here, not just the ones
        that played archive clips.

        Streams are restarted concurrently (one thread per cam, matching the
        app-startup pattern in start_streams) rather than sequentially, so
        all four cams begin their network handshake at the same time.  Both
        the archive and events buttons are disabled for the duration and
        re-enabled on the main thread once every init thread has finished,
        preventing a re-entry click from racing against still-initializing
        libvlc players.
        """
        self.event_mode = False

        # If a single-cam event pushed us into fullscreen, return to grid view.
        if getattr(self, '_event_entered_fullscreen', False):
            self.is_fullscreen = False
            self.fullscreen_index = -1
            self._event_entered_fullscreen = False

        # Clean up any active archive-mode playback (event clips in progress).
        for i in list(self.event_active_cams):
            if self.is_archive_mode[i]:
                self.cleanup_archive_mode(i)

        self.event_active_cams  = set()
        self.event_done_cams    = set()
        self.event_clip_queues  = [[] for _ in range(4)]
        self.current_playing_event = None

        # Destroy the overlay
        if self.event_overlay and self.event_overlay.winfo_exists():
            self.event_overlay.destroy()
        self.event_overlay = None

        # All live streams were stopped on entry — restart every configured cam.
        cams_to_restart = [i for i in range(4) if self.ips[i] and self.streams[i]]
        if cams_to_restart:
            # Disable both action buttons immediately.
            self.root.after(0, self._disable_stream_action_buttons)

            def _restart_all():
                threads = [
                    threading.Thread(
                        target=self.try_init_stream_with_retries,
                        args=(i,),
                        daemon=True
                    )
                    for i in cams_to_restart
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                # All inits done — re-enable buttons and refresh the panel.
                def _on_done():
                    self._reenable_stream_action_buttons()
                    self.build_config_panel()
                self.root.after(0, _on_done)

            threading.Thread(target=_restart_all, daemon=True).start()
        else:
            self.build_config_panel()

        self.update_label_bindings()

    def _open_event_overlay(self, date=None):
        """Build and show the event listing overlay for date (default: today)."""
        from datetime import date as _date, timedelta as _td

        if date is None:
            date = _date.today()

        self.event_mode = True
        self.build_config_panel()   # re-pack to reflect active state

        # Stop all live streams and blank every quadrant with a per-cam
        # placeholder so no live video plays in the background while the
        # overlay is open.  All streams are restarted in _exit_event_mode.
        for i in range(4):
            if self.is_archive_mode[i]:
                self.cleanup_archive_mode(i)
            else:
                self.cleanup_stream(i)
            self.labels[i].configure(image="", text=f"Cam {i + 1}", fg="#888888", bg="black")

        # Clear label click bindings now that event_mode is True — prevents
        # a click on a blank quadrant from triggering fullscreen-zoom.
        self.update_label_bindings()

        # Load / scan events for this date
        events = self._load_or_scan_events(date)

        # --- Build the panel as a Frame inside the grid area ---
        if self.event_overlay and self.event_overlay.winfo_exists():
            self.event_overlay.destroy()

        overlay = tk.Frame(
            self.grid_frame, bg="#1a1a1a",
            highlightbackground="#444444", highlightthickness=1
        )
        self.event_overlay = overlay

        # Size and centre over the grid area
        self.root.update_idletasks()
        gw = self.grid_frame.winfo_width()
        gh = self.grid_frame.winfo_height()
        ow = min(820, max(500, gw - 40))
        oh = min(500, max(300, gh - 40))

        # Store the sizing args so _start_event_playback and _on_event_clip_ended
        # can re-show the panel without having access to the local ow/oh closure.
        self._event_overlay_size = (ow, oh)

        def _place_overlay():
            overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
            overlay.lift()

        # ---- Header bar ----
        hdr = tk.Frame(overlay, bg="#111111")
        hdr.pack(fill="x")

        tk.Label(hdr, text="Events", bg="#111111", fg="white",
                 font=self.app_font(13, "bold")).pack(side="left", padx=12, pady=8)

        # Day navigation
        nav_frame = tk.Frame(hdr, bg="#111111")
        nav_frame.pack(side="left", expand=True)

        prev_btn = tk.Button(nav_frame, text="◀", bg="#111111", fg="white", bd=0,
                             activebackground="#333333", cursor="hand2",
                             font=self.app_font(11))
        prev_btn.pack(side="left", padx=4)

        day_label_var = tk.StringVar(value=date.strftime("%A, %d %b %Y"))
        tk.Label(nav_frame, textvariable=day_label_var, bg="#111111", fg="#cccccc",
                 font=self.app_font(11), width=22).pack(side="left")

        next_btn = tk.Button(nav_frame, text="▶", bg="#111111", fg="white", bd=0,
                             activebackground="#333333", cursor="hand2",
                             font=self.app_font(11))
        next_btn.pack(side="left", padx=4)

        close_btn = tk.Button(hdr, text="✕", bg="#111111", fg="#aaaaaa", bd=0,
                              activebackground="#333333", cursor="hand2",
                              font=self.app_font(11),
                              command=self._exit_event_mode)
        close_btn.pack(side="right", padx=10, pady=6)

        ttk.Separator(overlay, orient="horizontal").pack(fill="x")

        # ---- Column headers ----
        cols_frame = tk.Frame(overlay, bg="#2a2a2a")
        cols_frame.pack(fill="x")
        COL_WIDTHS = [9, 9, 31, 5, 5, 4, 5, 7]  # action, time, label, 1, 2, 3, 4, watched
        COL_HEADS  = [" ", "Time", "Label", "1", "2", "3", "4", "Watched"]
        for w, h in zip(COL_WIDTHS, COL_HEADS):
            tk.Label(cols_frame, text=h, bg="#2a2a2a", fg="#888888",
                     font=self.app_font(9, "bold"), width=w).pack(side="left", pady=3)

        ttk.Separator(overlay, orient="horizontal").pack(fill="x")

        # ---- Scrollable event rows ----
        list_outer = tk.Frame(overlay, bg="#1a1a1a")
        list_outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_outer, bg="#1a1a1a", highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        rows_frame = tk.Frame(canvas, bg="#1a1a1a")
        canvas_win = canvas.create_window((0, 0), window=rows_frame, anchor="nw")

        def _on_rows_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(canvas_win, width=canvas.winfo_width())
        rows_frame.bind("<Configure>", _on_rows_configure)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_win, width=e.width))

        # ---- Mouse-wheel scrolling ----
        # Tk delivers scroll events to whichever widget the pointer is
        # directly over — not necessarily the canvas.  The helper below
        # walks the rows_frame subtree and binds all widgets so scrolling
        # works regardless of whether the pointer is over a label, checkbox,
        # separator, or the canvas background.  It is called once after the
        # initial render and again at the end of every _render_rows() refresh.
        #
        # Linux:   Button-4 = scroll up,  Button-5 = scroll down
        # Win/Mac: MouseWheel delta is ±120 per notch (positive = up)

        def _on_scroll_linux(event):
            canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

        def _on_scroll_win(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        def _bind_scroll(widget):
            widget.bind("<Button-4>",    _on_scroll_linux)
            widget.bind("<Button-5>",    _on_scroll_linux)
            widget.bind("<MouseWheel>",  _on_scroll_win)
            for child in widget.winfo_children():
                _bind_scroll(child)

        def _unbind_scroll(widget):
            for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
                try:
                    widget.unbind(seq)
                except Exception:
                    pass
            for child in widget.winfo_children():
                _unbind_scroll(child)

        # Bind canvas and list_outer so scrolling works over empty space too
        _bind_scroll(canvas)
        _bind_scroll(list_outer)

        # State held across day navigation refreshes
        state = {"date": date, "events": events}

        def _render_rows(evs):
            _unbind_scroll(rows_frame)
            for w in rows_frame.winfo_children():
                w.destroy()

            if not evs:
                tk.Label(rows_frame, text="No events found for this day.",
                         bg="#1a1a1a", fg="#666666",
                         font=self.app_font(10, "italic")).pack(pady=20)
                return

            # Unbind <Configure> while building rows.  Every pack() call
            # resizes rows_frame which fires <Configure> which calls
            # canvas.bbox("all"), forcing Tk to flush pending geometry to
            # the screen immediately — making rows paint one at a time.
            # Suppress that, build everything, then fire one final update.
            rows_frame.unbind("<Configure>")

            play_img   = self.icon_cache["play"]
            delete_img = self.icon_cache["delete"]

            for ev_idx, ev in enumerate(evs):
                row_bg = "#1e1e1e" if ev_idx % 2 == 0 else "#232323"
                row_f = tk.Frame(rows_frame, bg=row_bg)
                row_f.pack(fill="x", pady=1)

                # Use Labels rather than Buttons for the action icons.
                # tk.Button initialises against the system theme background
                # before the bg= override takes effect, causing a brief blue
                # flash on X11/GNOME.  Labels apply bg immediately at
                # construction time and are faster to create in bulk.
                pb = tk.Label(row_f, image=play_img, bg=row_bg, cursor="hand2")
                pb.pack(side="left", padx=(6, 2), pady=4)

                db = tk.Label(row_f, image=delete_img, bg=row_bg, cursor="hand2")
                db.pack(side="left", padx=(2, 8), pady=4)

                # Time range label
                try:
                    s = datetime.strptime(ev["start"], "%Y-%m-%dT%H:%M:%S")
                    e = datetime.strptime(ev["end"],   "%Y-%m-%dT%H:%M:%S")
                    time_txt = f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}"
                except Exception:
                    time_txt = ev.get("start", "?")[:16]

                tk.Label(row_f, text=time_txt, bg=row_bg, fg="white",
                         font=self.app_font(10), width=16, anchor="w").pack(side="left")

                # Editable event label
                label_var = tk.StringVar(value=ev.get("label", ""))
                label_entry = tk.Entry(
                    row_f, textvariable=label_var, width=16,
                    bg="#2a2a2a", fg="white", insertbackground="white",
                    relief="flat", highlightthickness=1,
                    highlightbackground="#444444", highlightcolor="#666666",
                    font=self.app_font(10)
                )
                label_entry.pack(side="left", padx=(4, 8), ipady=2)

                def _save_label(event_ref=ev, v=label_var):
                    event_ref["label"] = v.get().strip()
                    self._save_events_json(state["date"], state["events"])

                label_entry.bind("<FocusOut>", lambda e, fn=_save_label: fn())
                label_entry.bind("<Return>",   lambda e, fn=_save_label: fn())

                # Per-cam checkboxes
                cam_vars = {}
                for ci in range(1, 5):
                    cam_key  = str(ci)
                    cam_data = ev["cams"].get(cam_key, {"enabled": False, "clips": []})
                    has_clip = bool(cam_data.get("clips"))
                    var = tk.BooleanVar(value=cam_data.get("enabled", False))
                    cam_vars[cam_key] = var

                    cb = ttk.Checkbutton(row_f, variable=var,
                                         state="normal" if has_clip else "disabled")
                    cb.pack(side="left", padx=8)

                    def _on_toggle(ck=cam_key, v=var, event_ref=ev):
                        event_ref["cams"][ck]["enabled"] = v.get()
                        self._save_events_json(state["date"], state["events"])

                    var.trace_add("write", lambda *_, cb=_on_toggle: cb())

                # Played indicator
                played_txt = "✓" if ev.get("played") else ""
                played_lbl = tk.Label(row_f, text=played_txt, bg=row_bg,
                                      fg="#4a9d4a", font=self.app_font(11, "bold"),
                                      width=3)
                played_lbl.pack(side="left", padx=4)

                # Wire play/delete buttons (closures over ev, ev_idx, played_lbl)
                def _play(event_ref=ev, p_lbl=played_lbl):
                    enabled_cams = [
                        int(ck) - 1
                        for ck, cd in event_ref["cams"].items()
                        if cd.get("enabled") and cd.get("clips")
                    ]
                    if not enabled_cams:
                        messagebox.showwarning(
                            "No Cameras Selected",
                            "Enable at least one camera checkbox before playing.",
                            parent=self.root
                        )
                        return
                    overlay.place_forget()
                    self._start_event_playback(event_ref, p_lbl, state["date"], state["events"])

                def _delete(ev_ref=ev, idx=ev_idx):
                    if messagebox.askyesno(
                        "Delete Event",
                        "Remove this event?",
                        parent=self.root
                    ):
                        state["events"].pop(idx)
                        self._save_events_json(state["date"], state["events"])
                        _render_rows(state["events"])

                # Click and hover bindings for the Label-based icons.
                HOVER_BG = "#2e2e2e"
                for lbl, fn in ((pb, _play), (db, _delete)):
                    lbl.bind("<Button-1>",  lambda e, f=fn:     f())
                    lbl.bind("<Enter>",     lambda e, l=lbl:    l.configure(bg=HOVER_BG))
                    lbl.bind("<Leave>",     lambda e, l=lbl, b=row_bg: l.configure(bg=b))

                ttk.Separator(rows_frame, orient="horizontal").pack(fill="x")

            # All rows built — rebind Configure, do one layout pass, then
            # attach scroll bindings to the full rows_frame subtree.
            rows_frame.bind("<Configure>", _on_rows_configure)
            rows_frame.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(canvas_win, width=canvas.winfo_width())
            _bind_scroll(rows_frame)

        _render_rows(events)

        # ---- Day navigation wiring ----
        def _navigate(delta):
            from datetime import timedelta as _td2
            new_date = state["date"] + _td2(days=delta)
            if new_date > _date.today():
                return  # Can't navigate to the future
            state["date"]   = new_date
            state["events"] = self._load_or_scan_events(new_date)
            day_label_var.set(new_date.strftime("%A, %d %b %Y"))
            next_btn.configure(state="normal" if new_date < _date.today() else "disabled")
            _render_rows(state["events"])

        prev_btn.configure(command=lambda: _navigate(-1))
        next_btn.configure(command=lambda: _navigate(+1))
        next_btn.configure(state="disabled" if date >= _date.today() else "normal")

        # Show the panel
        _place_overlay()

    def _event_transfer_audio(self, index):
        """Transfer audio to cam index during event playback.

        If exclusive_archive_audio is enabled, mutes every other active
        event cam (updating both the libvlc player and the button icon if
        the button already exists) and marks index as unmuted so
        play_archive_video creates its button in the audio_on state.

        If exclusive_archive_audio is disabled this is a no-op — each cam
        keeps whatever mute state it had.
        """
        if not self.exclusive_archive_audio:
            return
        for i in range(4):
            if i == index:
                continue
            if not self.archive_audio_muted[i]:
                self.archive_audio_muted[i] = True
                if self.media_players[i]:
                    try:
                        self.media_players[i].audio_set_mute(True)
                    except Exception:
                        pass
                if self.audio_buttons[i]:
                    try:
                        self.audio_buttons[i].configure(image=self.icon_cache["audio_off"])
                    except Exception:
                        pass
        self.archive_audio_muted[index] = False

    def _start_event_playback(self, event, played_label_widget, date, events_list):
        """Kick off coordinated playback for event across all enabled cams.

        Populates event_clip_queues with bare path strings (no seek needed —
        every clip plays from its own beginning).  Multi-cam synchronisation
        is achieved by delaying the start of each cam by the difference
        between that cam's first clip_start and the event's global start,
        so they join playback at the same wall-clock moment as when recorded.

        Example:
          event.start  = 01:10:00
          cam1 clip_start = 01:10:00  → starts immediately  (0s delay)
          cam2 clip_start = 01:10:05  → starts after 5s     (5s delay)
          cam1 plays its first 5 seconds alone, then cam2 joins — exactly
          as it appeared on the live view when the motion was detected.
        """
        self.current_playing_event = event
        self.event_clip_queues  = [[] for _ in range(4)]
        self.event_active_cams  = set()
        self.event_done_cams    = set()

        # Parse the event's global start time for delay calculations.
        try:
            event_start_dt = datetime.strptime(event["start"], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            event_start_dt = None

        # Build queues and collect per-cam start delays before launching
        # anything, so all root.after() calls are registered atomically.
        cam_launches = []  # list of (ci, first_path, delay_ms)

        for cam_key, cam_data in event["cams"].items():
            if not cam_data.get("enabled"):
                continue
            clips = cam_data.get("clips", [])
            if not clips:
                continue
            ci = int(cam_key) - 1

            # Queue entries are (path, gap_ms) where gap_ms is the wall-clock
            # gap between the end of the previous clip and the start of this
            # one on the same cam.  For the first clip gap_ms is always 0
            # (the initial cross-cam delay is handled separately below).
            # Subsequent clips inherit any recording gap so pauses between
            # clips on the same cam are preserved during playback.
            queue = []
            for clip_idx, clip in enumerate(clips):
                if clip_idx == 0:
                    gap_ms = 0
                else:
                    try:
                        prev_end_dt  = datetime.strptime(clips[clip_idx - 1]["clip_end"],   "%Y-%m-%dT%H:%M:%S")
                        this_start_dt = datetime.strptime(clip["clip_start"],               "%Y-%m-%dT%H:%M:%S")
                        gap_s  = max(0.0, (this_start_dt - prev_end_dt).total_seconds())
                        gap_ms = int(gap_s * 1000)
                    except Exception:
                        gap_ms = 0
                queue.append((clip["path"], gap_ms))
            self.event_clip_queues[ci] = queue
            self.event_active_cams.add(ci)

            # Cross-cam start delay: how long after the event start this cam's
            # first clip begins.  Divided by playback speed so the offset
            # shrinks proportionally when playing at 2x/4x/8x.
            speed = max(1.0, self.default_playback_speed)
            if event_start_dt is not None:
                try:
                    clip_start_dt = datetime.strptime(clips[0]["clip_start"], "%Y-%m-%dT%H:%M:%S")
                    delay_s = max(0.0, (clip_start_dt - event_start_dt).total_seconds())
                    delay_ms = int(delay_s * 1000 / speed)
                except Exception:
                    delay_ms = 0
            else:
                delay_ms = 0

            first_path, _ = self.event_clip_queues[ci].pop(0)
            cam_launches.append((ci, first_path, delay_ms))

        if not self.event_active_cams:
            # Nothing to play — re-show overlay immediately
            if self.event_overlay and self.event_overlay.winfo_exists():
                ow, oh = getattr(self, "_event_overlay_size", (820, 500))
                self.event_overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
                self.event_overlay.lift()
            return

        # Store refs for the completion callback
        self._event_played_label   = played_label_widget
        self._event_date_for_save  = date
        self._event_list_for_save  = events_list

        # Single-cam events play in fullscreen so the clip fills the screen
        # rather than being confined to one quadrant.  Track whether we
        # entered fullscreen here so _exit_event_mode can restore the grid.
        self._event_entered_fullscreen = False
        if len(self.event_active_cams) == 1:
            ci_solo = next(iter(self.event_active_cams))
            if not self.is_fullscreen:
                self.is_fullscreen = True
                self.fullscreen_index = ci_solo
                self._event_entered_fullscreen = True
                self.update_layout()
                self.build_config_panel()

        # Enter archive mode for each active cam upfront so the quadrant
        # switches to its label widget before any delayed play fires.
        for ci, _, _ in cam_launches:
            if not self.is_archive_mode[ci]:
                self.is_archive_mode[ci] = True
                self.archive_canvas[ci].pack_forget()
                self.labels[ci].pack(fill="both", expand=True)

        # In event mode all cams start muted; audio is transferred to each
        # cam as it begins playing via _event_transfer_audio, so the most
        # recently started clip always carries the audio.
        for ci, _, _ in cam_launches:
            self.archive_audio_muted[ci] = True

        # Schedule each cam's first clip, delayed by its offset.
        # _event_transfer_audio is called just before play_archive_video so
        # the button is created with the correct icon on the first render.
        for ci, first_path, delay_ms in cam_launches:
            if delay_ms == 0:
                self._event_transfer_audio(ci)
                self.play_archive_video(ci, first_path)
            else:
                logging.info(f"Cam {ci + 1}: delaying event playback start by {delay_ms}ms")
                self.root.after(
                    delay_ms,
                    lambda c=ci, p=first_path: (
                        self._event_transfer_audio(c),
                        self.play_archive_video(c, p)
                    )
                )

    def _on_event_clip_ended(self, index):
        """Called (on main thread via root.after) when a clip finishes in event mode.

        If more clips remain for this cam, play the next one.  Otherwise mark
        this cam done.  When all active cams are done, mark the event played
        and re-show the overlay.
        """
        if not self.event_mode:
            return  # User exited event mode early — nothing to do

        if self.event_clip_queues[index]:
            # More clips in queue for this cam — play next.
            # Transfer audio to this cam (it's the most recently started).
            next_path, gap_ms = self.event_clip_queues[index].pop(0)
            # Clean up the just-ended player before starting the next one
            self.cleanup_stream(index)
            for widget in self.labels[index].winfo_children():
                widget.destroy()
            self.exit_buttons[index] = None
            self.pause_buttons[index] = None
            self.speed_buttons[index] = None
            self.replay_buttons[index] = None
            self.rewind_buttons[index] = None
            self.audio_buttons[index] = None
            self.video_ended[index] = False
            # Apply the inter-clip gap, scaled by the current playback speed
            # so a 30s gap at 2x only delays 15s of real time.
            speed = max(1.0, self.playback_speeds[index])
            adjusted_gap_ms = int(gap_ms / speed)
            if adjusted_gap_ms > 0:
                self.labels[index].configure(image="", text=f"Cam {index + 1}", fg="#888888", bg="black")
                self.root.after(
                    adjusted_gap_ms,
                    lambda i=index, p=next_path: (
                        self._event_transfer_audio(i),
                        self.play_archive_video(i, p)
                    )
                )
            else:
                self._event_transfer_audio(index)
                self.play_archive_video(index, next_path)
        else:
            # This cam's clips are all done — black it out
            self.cleanup_stream(index)
            for widget in self.labels[index].winfo_children():
                widget.destroy()
            self.exit_buttons[index] = None
            self.pause_buttons[index] = None
            self.speed_buttons[index] = None
            self.replay_buttons[index] = None
            self.rewind_buttons[index] = None
            self.audio_buttons[index] = None
            self.video_ended[index] = False
            self.labels[index].configure(image="", text=f"Cam {index + 1}", fg="#888888", bg="black")
            self.event_done_cams.add(index)

            if self.event_done_cams >= self.event_active_cams:
                # All cams finished — mark played and restore overlay
                if self.current_playing_event:
                    self.current_playing_event["played"] = True
                    self._save_events_json(
                        self._event_date_for_save,
                        self._event_list_for_save
                    )
                    # Update the ✓ label in the overlay row if it still exists
                    try:
                        if self._event_played_label and self._event_played_label.winfo_exists():
                            self._event_played_label.configure(text="✓")
                    except Exception:
                        pass

                if self.event_overlay and self.event_overlay.winfo_exists():
                    # If a single-cam event entered fullscreen, drop back to
                    # grid before re-showing the overlay.
                    if getattr(self, '_event_entered_fullscreen', False):
                        self.is_fullscreen = False
                        self.fullscreen_index = -1
                        self._event_entered_fullscreen = False
                        self.update_layout()
                        self.build_config_panel()
                    ow, oh = getattr(self, "_event_overlay_size", (820, 500))
                    self.event_overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
                    self.event_overlay.lift()

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

            # Audio toggle button — starts unmuted for archive mode; event
            # mode overrides this via archive_audio_muted before calling here.
            self.archive_audio_muted[index] = False
            audio_img = self.icon_cache["audio_on"]
            self.audio_buttons[index] = tk.Button(
                self.labels[index],
                image=audio_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda idx=index: self.toggle_archive_audio(idx)
            )
            self.audio_buttons[index].image = audio_img
            self.audio_buttons[index].place(relx=0.0, rely=0.0, x=220, y=4, anchor="nw")

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
            self.vlc_instances[index] = instance
            player = instance.media_player_new()
            if player is None:
                logging.error(f"Stream {index}: Failed to create VLC media player for archive video")
                self.labels[index].configure(image="", text="VLC Player Creation Failed", fg="white")
                instance.release()
                self.vlc_instances[index] = None
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
                self.vlc_instances[index] = None
                vlc_frame.destroy()
                return

            timeout = 5.0
            start_time = time.time()
            while time.time() - start_time < timeout:
                if playing_event.is_set():
                    self.set_audio_state(index, mute=self.archive_audio_muted[index])
                    self.media_players[index].set_rate(self.default_playback_speed)
                    # Resume from saved position only in archive browse mode.
                    # Event playback always starts from the beginning of each
                    # clip so consecutive clips play in full regardless of
                    # whether they were partially watched in archive mode.
                    if self.resume_playback and not self.event_mode:
                        saved = self.watch_progress[index].get(video_path)
                        if saved and saved.get("duration", 0) > 0:
                            resume_at = saved.get("position", 0)
                            if 0 < resume_at < saved["duration"] - 3:
                                try:
                                    self.media_players[index].set_time(int(resume_at * 1000))
                                    logging.info(f"Stream {index}: Resumed playback at {resume_at:.1f}s")
                                except Exception as e:
                                    logging.warning(f"Stream {index}: Failed to seek to saved position: {e}")
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
                self.vlc_instances[index] = None
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

    def toggle_archive_audio(self, index):
        """Toggle mute for an archive/event clip.

        If exclusive_archive_audio is enabled, unmuting one stream mutes
        all others first so only one clip ever plays audio at a time.
        """
        currently_muted = self.archive_audio_muted[index]
        new_muted = not currently_muted

        if not new_muted and self.exclusive_archive_audio:
            # Unmuting this stream — mute every other archive stream first.
            for i in range(4):
                if i != index and not self.archive_audio_muted[i]:
                    self.archive_audio_muted[i] = True
                    if self.media_players[i]:
                        try:
                            self.media_players[i].audio_set_mute(True)
                        except Exception:
                            pass
                    if self.audio_buttons[i]:
                        try:
                            self.audio_buttons[i].configure(image=self.icon_cache["audio_off"])
                        except Exception:
                            pass

        self.archive_audio_muted[index] = new_muted
        if self.media_players[index]:
            try:
                self.media_players[index].audio_set_mute(new_muted)
            except Exception as e:
                logging.error(f"Stream {index}: Failed to set archive audio mute: {e}")

        if self.audio_buttons[index]:
            icon = self.icon_cache["audio_off" if new_muted else "audio_on"]
            self.audio_buttons[index].configure(image=icon)

        logging.info(f"Stream {index}: Archive audio {'muted' if new_muted else 'unmuted'}")

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
        video_path = self.current_archive_path[index]
        while self.running and not self.video_ended[index]:
            try:
                if self.media_players[index]:
                    state = self.media_players[index].get_state()
                    if state == vlc.State.Ended:
                        logging.info(f"Stream {index}: python-vlc playback ended")
                        self.video_ended[index] = True
                        # In event mode hand off to the event coordinator instead
                        # of the normal go_back/archive-navigation path.
                        if self.event_mode:
                            self.root.after(0, self._on_event_clip_ended, index)
                            break
                        # Video finished naturally - store position == duration
                        # rather than deleting the entry, so the thumbnail
                        # shows a full red progress bar for a fully-watched
                        # clip instead of losing its progress bar entirely.
                        existing = self.watch_progress[index].get(video_path, {})
                        duration = existing.get("duration", 0)
                        if duration > 0:
                            self.watch_progress[index][video_path] = {
                                "position": duration,
                                "duration": duration,
                            }
                            self.watch_progress_dirty = True
                        break
                    # Record live position/duration (both in seconds) so the
                    # archive grid can render a YouTube-style progress bar and
                    # so playback can resume here next time, if enabled.
                    #
                    # At higher playback speeds (2x+) libvlc can momentarily
                    # report get_length() as 0/None or get_time() as -1 while
                    # it's working to keep up - these are transient, not the
                    # real duration changing. Only commit a reading when both
                    # values look sane, so a single bad tick can't be
                    # interpreted as "reset to zero progress" by anything
                    # reading watch_progress between this tick and the next
                    # good one.
                    position_ms = self.media_players[index].get_time()
                    duration_ms = self.media_players[index].get_length()
                    if position_ms is not None and position_ms > 0 and duration_ms and duration_ms > 0:
                        self.watch_progress[index][video_path] = {
                            "position": position_ms / 1000.0,
                            "duration": duration_ms / 1000.0,
                        }
                        self.watch_progress_dirty = True
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
            self.audio_buttons[index] = None
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

        # Safety-net flush in case the app is closed mid-video (e.g. window
        # close button) without going through go_back's own save.
        if self.watch_progress_dirty:
            self.save_watch_progress()

        if self.help_overlay is not None:
            try:
                self.help_overlay.destroy()
            except Exception:
                pass
            self.help_overlay = None

        for i in range(4):
            if self.vlc_instances[i]:
                try:
                    self.vlc_instances[i].release()
                    logging.info(f"Stream {i}: Released VLC instance")
                except Exception as e:
                    logging.error(f"Stream {i}: Error releasing VLC instance: {e}")
                self.vlc_instances[i] = None

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
                if self.audio_buttons[i]:
                    self.audio_buttons[i].destroy()
                self.archive_canvas[i] = None
                self.back_buttons[i] = None
                self.exit_buttons[i] = None
                self.pause_buttons[i] = None
                self.speed_buttons[i] = None
                self.replay_buttons[i] = None
                self.rewind_buttons[i] = None
                self.audio_buttons[i] = None
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