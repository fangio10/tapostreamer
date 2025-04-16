import numpy as np
from PIL import Image, ImageTk, ImageDraw
import tkinter as tk
from tkinter import ttk
import json
import os
import threading
import time
import logging
import sys
import socket
import subprocess
import psutil

# Attempt to import python-vlc for Windows or Linux without hardware acceleration
try:
    import vlc
except ImportError:
    if sys.platform.startswith("win"):
        print("ERROR: python-vlc not installed. Run 'pip install python-vlc'")
        sys.exit(1)
    else:
        print("WARNING: python-vlc not installed. Falling back to VLC CLI if hardware acceleration is enabled.")

# Logging setup
#logging.basicConfig(
#    filename='vlc_errors.log',
#    level=logging.DEBUG,
#    format='%(asctime)s - %(levelname)s - %(message)s'
#)

class RTSPViewer:
    def __init__(self, root):
        # Suppress all logging
        logging.getLogger().setLevel(100)  # Set level above all standard levels
        
        self.root = root
        self.root.title("Tapo Streamer")
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except:
            pass
        if getattr(sys, 'frozen', False):
            base_path = sys._MEIPASS
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(base_path, "cam.png")
        try:
            img = Image.open(icon_path)
            img_titlebar = img.resize((64, 64), Image.LANCZOS)
            icon = ImageTk.PhotoImage(img_titlebar)
            self.root.iconphoto(True, icon)
        except Exception as e:
            print(f"Error loading icon: {e}")
        self.root.configure(bg="#222222")
        self.root.geometry("2304x1296")
        self.root.state("normal")  # Ensure window is not maximized
        self.root.minsize(1280, 720)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self.cleanup)
        self.username = ""
        self.password = ""
        self.ips = ["", "", "", ""]
        self.hq_enabled = [True, True, True, True]
        self.audio_enabled = [True, True, True, True]
        self.use_hardware_acceleration = sys.platform.startswith("linux")  # Default to True on Linux
        self.streams = ["", "", "", ""]
        if sys.platform.startswith("win"):
            config_dir = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "TapoStreamer")
        else:
            config_dir = os.path.join(os.path.expanduser("~"), ".tapo-streamer")
        os.makedirs(config_dir, exist_ok=True)
        self.config_file = os.path.join(config_dir, "config.json")
        self.vlc_instances = [None] * 4
        self.media_players = [None] * 4
        self.vlc_processes = [None] * 4  # For VLC CLI on Linux
        self.panels = [None] * 4
        self.labels = [None] * 4
        self.ptz_buttons = []
        self.ptz_images = []
        self.fullscreen_buttons = [None] * 4
        self.fullscreen_images = [None] * 4
        self.minimize_images = [None] * 4
        self.running = True
        self.is_fullscreen = False
        self.fullscreen_index = None
        self.panel_sizes = [(0, 0)] * 4
        self.target_dims = [(0, 0)] * 4
        self.ptz_moving = False
        self.ptz_busy = False
        self.onvif_cams = {}
        self.ptz_click_counts = [0] * 4
        self.ptz_lock = threading.Lock()
        self.frame_shapes = [(0, 0) for _ in range(4)]
        self.top_panel_visible = True
        self.hide_top_panel_on_launch = False
        self.drop_counts = [0] * 4
        self.drop_timestamps = [[] for _ in range(4)]
        self.last_layout_update = 0
        self.load_config()
        self.update_streams()
        self.init_ui()
        self.update_layout()
        self.root.after(0, lambda: threading.Thread(target=self.start_streams, daemon=True).start())

    def load_config(self):
        # Initialize defaults
        self.username = ""
        self.password = ""
        self.ips = ["", "", "", ""]
        self.hq_enabled = [True, True, True, True]
        self.audio_enabled = [True, True, True, True]
        self.hide_top_panel_on_launch = False
        self.use_hardware_acceleration = sys.platform.startswith("linux")
        # Load config if exists
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    config = json.load(f)
                self.username = config.get("username", self.username)
                self.password = config.get("password", self.password)
                self.ips = config.get("ips", self.ips)
                self.hq_enabled = [
                    bool(config.get("hq_enabled", self.hq_enabled)[i])
                    for i in range(4)
                ]
                self.audio_enabled = config.get("audio_enabled", self.audio_enabled)
                self.hide_top_panel_on_launch = config.get("hide_top_panel_on_launch", self.hide_top_panel_on_launch)
                self.use_hardware_acceleration = config.get("use_hardware_acceleration", self.use_hardware_acceleration)
                logging.info(f"Loaded config from {self.config_file}: {config}")
            except Exception as e:
                logging.error(f"Failed to load config from {self.config_file}: {e}")
        else:
            logging.info(f"No config file found at {self.config_file}; using defaults")

    def save_config(self):
        config = {
            "username": self.username,
            "password": self.password,
            "ips": self.ips,
            "hq_enabled": self.hq_enabled,
            "audio_enabled": self.audio_enabled,
            "hide_top_panel_on_launch": self.hide_top_panel_on_launch,
            "use_hardware_acceleration": self.use_hardware_acceleration
        }
        try:
            with open(self.config_file, "w") as f:
                json.dump(config, f, indent=4)
            logging.info(f"Saved config to {self.config_file}")
        except Exception as e:
            logging.error(f"Failed to save config to {self.config_file}: {e}")

    def debounce_layout_update(self):
        self.update_layout()  # Force update on any configure event

    def toggle_top_panel(self, event):
        if self.top_panel_visible:
            self.top_panel.pack_forget()
            self.top_panel_visible = False
            logging.info("Top panel hidden")
        else:
            self.top_panel.pack(side="top", fill="x")
            self.top_panel_visible = True
            logging.info("Top panel shown")
        self.update_layout()

    def update_streams(self):
        self.streams = []
        seen_urls = set()
        for ip, hq in zip(self.ips, self.hq_enabled):
            if ip and self.username and self.password:
                stream = f"rtsp://{self.username}:{self.password}@{ip}:554/stream{'2' if not hq else '1'}"
                if stream in seen_urls:
                    stream = ""
                else:
                    seen_urls.add(stream)
            else:
                stream = ""
            self.streams.append(stream)
        logging.info(f"Updated streams: {self.streams}")

    def create_icon(self, icon_type):
        size = (30, 30) if icon_type in ["fullscreen", "minimize"] else (40, 40)
        img = Image.new("RGBA", size, (0, 0, 0, 180))
        draw = ImageDraw.Draw(img)
        if icon_type == "config":
            draw.ellipse((8, 8, 32, 32), outline="white", width=2)
            draw.ellipse((14, 14, 26, 26), outline="white", width=2)
            for i in range(8):
                angle = i * 45
                x1 = 20 + 12 * np.cos(np.radians(angle))
                y1 = 20 + 12 * np.sin(np.radians(angle))
                x2 = 20 + 16 * np.cos(np.radians(angle))
                y2 = 20 + 16 * np.sin(np.radians(angle))
                draw.line((x1, y1, x2, y2), fill="white", width=2)
            draw.ellipse((18, 18, 22, 22), fill="white")
        elif icon_type == "left":
            draw.polygon([(30, 10), (15, 20), (30, 30)], fill="white")
        elif icon_type == "right":
            draw.polygon([(10, 10), (25, 20), (10, 30)], fill="white")
        elif icon_type == "up":
            draw.polygon([(10, 30), (20, 15), (30, 30)], fill="white")
        elif icon_type == "down":
            draw.polygon([(10, 10), (20, 25), (30, 10)], fill="white")
        elif icon_type == "fullscreen":
            draw.rectangle((6, 6, 24, 24), outline="white", width=2)
            draw.line((8, 8, 11, 8), fill="white", width=2)
            draw.line((8, 8, 8, 11), fill="white", width=2)
            draw.line((22, 22, 19, 22), fill="white", width=2)
            draw.line((22, 22, 22, 19), fill="white", width=2)
        elif icon_type == "minimize":
            draw.rectangle((6, 6, 24, 24), outline="white", width=2)
            draw.line((11, 11, 19, 11), fill="white", width=2)
            draw.line((11, 19, 19, 19), fill="white", width=2)
        return ImageTk.PhotoImage(img)


    def init_ui(self):
        self.top_panel = tk.Frame(self.root, bg="#222222", height=50)
        if not self.hide_top_panel_on_launch:
            self.top_panel.pack(side="top", fill="x")
        else:
            self.top_panel_visible = False
            logging.info("Top panel hidden on launch")
        self.config_img = self.create_icon("config")
        self.config_button = tk.Button(
            self.top_panel, image=self.config_img, bg="#222222", bd=0,
            command=self.show_config_dialog, cursor="hand2"
        )
        self.config_button.pack(side="left", padx=10, pady=5)
        for direction in ["down", "up", "right", "left"]:
            img = self.create_icon(direction)
            button = tk.Button(
                self.top_panel, image=img, bg="#222222", bd=0, cursor="hand2",
                command=lambda d=direction: self.start_ptz_move(d)
            )
            button.bind("<ButtonRelease-1>", lambda event, d=direction: self.stop_ptz_move(d))
            self.ptz_buttons.append(button)
            self.ptz_images.append(img)
        self.grid_frame = tk.Frame(self.root, bg="#222222")
        self.grid_frame.pack(fill="both", expand=True)
        initial_width, initial_height = 960, 540
        for i in range(4):
            panel = tk.Frame(self.grid_frame, bg="black")
            self.panels[i] = panel
            label = tk.Label(panel, bg="black", text="Loading...", fg="white")
            self.labels[i] = label
            label.pack(fill="both", expand=True)
            x = 0 if i in (0, 2) else initial_width + 5
            y = 0 if i in (0, 1) else initial_height + 5
            panel.place(x=x, y=y, width=initial_width, height=initial_height)
            self.panel_sizes[i] = (initial_width, initial_height)
            self.fullscreen_images[i] = self.create_icon("fullscreen")
            self.minimize_images[i] = self.create_icon("minimize")
            self.fullscreen_buttons[i] = tk.Button(
                panel,
                image=self.fullscreen_images[i],
                bg="black",
                bd=0,
                cursor="hand2",
                command=lambda idx=i: self.handle_stream_click(idx)
            )
        self.root.bind("<Configure>", lambda e: self.update_layout())
        self.root.bind("<space>", self.toggle_top_panel)

    def show_config_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Configure Streams")
        dialog.geometry("400x500")  # Increased height for new checkbox
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(dialog, text="Username:").place(x=20, y=20)
        username_entry = tk.Entry(dialog, width=30)
        username_entry.insert(0, self.username)
        username_entry.place(x=100, y=20)

        tk.Label(dialog, text="Password:").place(x=20, y=60)
        password_entry = tk.Entry(dialog, width=30)
        password_entry.insert(0, self.password)
        password_entry.place(x=100, y=60)

        ip_entries = []
        hq_checkboxes = []
        audio_checkboxes = []
        for i in range(4):
            tk.Label(dialog, text=f"Camera {i+1} IP:").place(x=20, y=100 + i*70)
            ip_entry = tk.Entry(dialog, width=20)
            ip_entry.insert(0, self.ips[i])
            ip_entry.place(x=100, y=100 + i*70)
            ip_entries.append(ip_entry)

            hq_var = tk.BooleanVar(value=self.hq_enabled[i])
            hq_cb = ttk.Checkbutton(dialog, text="HQ", variable=hq_var)
            hq_cb.place(x=250, y=100 + i*70)
            hq_checkboxes.append(hq_var)

            audio_var = tk.BooleanVar(value=self.audio_enabled[i])
            audio_cb = ttk.Checkbutton(dialog, text="Audio", variable=audio_var)
            audio_cb.place(x=300, y=100 + i*70)
            audio_checkboxes.append(audio_var)

        # Add hardware acceleration checkbox (Linux only)
        if sys.platform.startswith("linux"):
            hw_accel_var = tk.BooleanVar(value=self.use_hardware_acceleration)
            hw_accel_cb = ttk.Checkbutton(dialog, text="Use Hardware Acceleration (VLC CLI)", variable=hw_accel_var)
            hw_accel_cb.place(x=20, y=380)
        else:
            hw_accel_var = None

        hide_panel_var = tk.BooleanVar(value=self.hide_top_panel_on_launch)
        hide_panel_cb = ttk.Checkbutton(dialog, text="Hide options panel on launch", variable=hide_panel_var)
        hide_panel_cb.place(x=20, y=410 if sys.platform.startswith("linux") else 380)

        tk.Button(
            dialog, text="Save",
            command=lambda: self.save_streams(
                username_entry, password_entry, ip_entries,
                hq_checkboxes, audio_checkboxes, hw_accel_var, hide_panel_var, dialog
            )
        ).place(x=150, y=460 if sys.platform.startswith("linux") else 430)
        tk.Button(dialog, text="Cancel", command=dialog.destroy).place(x=250, y=460 if sys.platform.startswith("linux") else 430)

    def save_streams(self, username_entry, password_entry, ip_entries, hq_checkboxes, audio_checkboxes, hw_accel_var, hide_panel_var, dialog):
        # Update configuration
        self.username = username_entry.get().strip()
        self.password = password_entry.get().strip()
        self.ips = [e.get().strip() for e in ip_entries]
        self.hq_enabled = [v.get() for v in hq_checkboxes]
        self.audio_enabled = [v.get() for v in audio_checkboxes]
        self.hide_top_panel_on_launch = hide_panel_var.get()
        if hw_accel_var is not None:
            self.use_hardware_acceleration = hw_accel_var.get()

        # Reset state for streams
        self.onvif_cams = {}
        self.ptz_click_counts = [0] * 4
        self.drop_counts = [0] * 4
        self.drop_timestamps = [[] for _ in range(4)]
        self.update_streams()
        self.save_config()

        # Close dialog and restart streams
        dialog.destroy()
        logging.info("Configuration saved, restarting streams")
        threading.Thread(target=self.restart_streams, daemon=True).start()

    def restart_streams(self):
        logging.info("Restarting all streams")
        # Stop all streams
        self.stop_streams()
        # Update UI to show loading/disabled state
        self.root.after(0, lambda: [
            self.labels[i].configure(image="", text="Disabled" if not self.ips[i] else "Loading...", fg="white") or
            self.fullscreen_buttons[i].place_forget()
            for i in range(4)
        ])
        # Start streams
        self.start_streams()
        logging.info("Stream restart completed")

    def init_stream(self, index):
        if not self.ips[index] or not self.streams[index]:
            self.root.after(0, lambda: self.labels[index].configure(image="", text="Disabled", fg="white"))
            self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget() if self.fullscreen_buttons[index] else None)
            logging.info(f"No IP or stream URL for index {index}, marked as Disabled")
            return

        logging.info(f"Initializing stream {index}: {self.streams[index]} (HQ={self.hq_enabled[index]})")
        start_time = time.time()
        max_attempts = 3
        backoff_delay = 1.0

        for attempt in range(max_attempts):
            is_final = attempt == max_attempts - 1
            try:
                # Test network latency
                ip = self.ips[index]
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    start = time.time()
                    sock.connect((ip, 554))
                    latency = (time.time() - start) * 1000
                    logging.info(f"Stream {index}: Network latency to {ip}: {latency:.2f}ms")
                    sock.close()
                except Exception as e:
                    logging.warning(f"Stream {index}: Latency test failed: {e}")
                    if is_final:
                        raise RuntimeError("Network unreachable")

                # Get Tkinter window ID
                try:
                    xid = self.labels[index].winfo_id()
                    logging.debug(f"Stream {index}: Tkinter XWindow ID {xid}")
                except Exception as e:
                    logging.error(f"Stream {index}: Failed to get window ID: {e}")
                    raise RuntimeError("Failed to get Tkinter window ID")

                if sys.platform.startswith("win") or (sys.platform.startswith("linux") and not self.use_hardware_acceleration):
                    # Windows or Linux without hardware acceleration: Use python-vlc
                    try:
                        instance = vlc.Instance([
                            '--no-video-title-show',
                            '--network-caching=5000',
                            '--deinterlace=auto',
                            '--no-drop-late-frames',
                            '--no-skip-frames',
                            '--live-caching=5000',
                            '--no-xlib'
                        ])
                        if instance is None:
                            raise RuntimeError("Failed to create VLC instance")
                        self.vlc_instances[index] = instance
                        player = instance.media_player_new()
                        if player is None:
                            raise RuntimeError("Failed to create VLC media player")
                        self.media_players[index] = player
                        media = instance.media_new(self.streams[index])
                        media.get_mrl()
                        player.set_media(media)
                        player.set_xwindow(xid) if sys.platform.startswith("linux") else player.set_hwnd(xid)
                        player.audio_set_volume(0)  # Start muted
                        event_manager = player.event_manager()
                        playing_event = threading.Event()
                        error_event = threading.Event()

                        def on_playing():
                            logging.debug(f"Stream {index} attempt {attempt+1}/{max_attempts}: Playing event received")
                            playing_event.set()

                        def on_error():
                            logging.debug(f"Stream {index} attempt {attempt+1}/{max_attempts}: Error event received")
                            error_event.set()

                        event_manager.event_attach(vlc.EventType.MediaPlayerPlaying, lambda e: on_playing())
                        event_manager.event_attach(vlc.EventType.MediaPlayerEncounteredError, lambda e: on_error())
                        if player.play() == -1:
                            raise RuntimeError("Failed to start VLC player")
                        logging.debug(f"Stream {index}: Started playback")
                        timeout = 7.0
                        start_wait = time.time()
                        while time.time() - start_wait < timeout:
                            if playing_event.is_set():
                                max_attempts_resolution = 5
                                for res_attempt in range(max_attempts_resolution):
                                    time.sleep(0.5)
                                    width, height = player.video_get_size(0) or (0, 0)
                                    if width > 0 and height > 0:
                                        self.frame_shapes[index] = (width, height)
                                        logging.info(f"Stream {index} resolution: {self.frame_shapes[index]}")
                                        break
                                    logging.debug(f"Stream {index}: Attempt {res_attempt+1}/{max_attempts_resolution} to get resolution, got ({width}, {height})")
                                else:
                                    self.frame_shapes[index] = (2304, 1296)
                                    logging.warning(f"Stream {index}: Failed to get resolution, using fallback (2304, 1296)")
                                player.video_set_scale(0)
                                self.drop_counts[index] = 0
                                self.drop_timestamps[index] = []
                                self.root.after(0, lambda: self.fullscreen_buttons[index].place(relx=1.0, rely=0.0, x=-10, y=10, anchor="ne"))
                                threading.Thread(target=self.monitor_stream, args=(index, player), daemon=True).start()
                                return
                            elif error_event.is_set():
                                raise RuntimeError("Stream encountered error during playback")
                            time.sleep(0.1)
                        raise RuntimeError("Stream failed to start playing within timeout")
                    except Exception as e:
                        logging.error(f"Stream {index} python-vlc attempt {attempt+1}/{max_attempts} failed: {e}", exc_info=True)
                        raise
                else:
                    # Linux with hardware acceleration: Use VLC CLI
                    vlc_cmd = [
                        'cvlc',
                        self.streams[index],
                        '--no-video-title-show',
                        '--network-caching=2000',
                        '--live-caching=2000',
                        '--drop-late-frames',
                        '--skip-frames',
                        '--rtsp-tcp',
                        '--avcodec-hw=any',
                        f'--drawable-xid={xid}',
                        '--quiet',
                        '--alsa-audio-device=default',  # Explicitly set ALSA device
                        '--no-sout-audio',             # Ensure audio is sent to output
                        '--gain=1.0',
                        '--volume=100'                   # Set audio gain to normal
                    ]
                    try:
                        process = subprocess.Popen(
                            vlc_cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            universal_newlines=True
                        )
                        self.vlc_processes[index] = process
                        logging.debug(f"Stream {index}: Started VLC CLI process PID {process.pid}")
                    except Exception as e:
                        logging.error(f"Stream {index}: Failed to start VLC CLI: {e}")
                        raise RuntimeError("Failed to start VLC CLI")

                    timeout = 7.0
                    start_wait = time.time()
                    playing = False
                    while time.time() - start_wait < timeout:
                        if process.poll() is not None:
                            stderr = process.stderr.read()
                            logging.error(f"Stream {index}: VLC CLI exited: {stderr}")
                            raise RuntimeError("VLC CLI process failed")
                        try:
                            proc = psutil.Process(process.pid)
                            cpu_percent = proc.cpu_percent(interval=0.1)
                            if cpu_percent > 0:
                                playing = True
                                break
                        except psutil.NoSuchProcess:
                            logging.error(f"Stream {index}: VLC CLI process vanished")
                            raise RuntimeError("VLC CLI process vanished")
                        time.sleep(0.1)

                    if not playing:
                        process.terminate()
                        raise RuntimeError("Stream failed to start within timeout")

                    # Mute audio initially (grid view)
                    if self.audio_enabled[index]:
                        self._mute_vlc_cli_audio(index)
                    self.frame_shapes[index] = (2304, 1296)
                    self.drop_counts[index] = 0
                    self.drop_timestamps[index] = []
                    self.root.after(0, lambda: self.fullscreen_buttons[index].place(relx=1.0, rely=0.0, x=-10, y=10, anchor="ne"))
                    threading.Thread(target=self.monitor_stream, args=(index, None), daemon=True).start()
                    return

            except Exception as e:
                logging.error(f"Stream {index} attempt {attempt+1}/{max_attempts} failed (hq={self.hq_enabled[index]}): {e}")
                # Cleanup
                if self.media_players[index]:
                    self.media_players[index].stop()
                    self.media_players[index] = None
                if self.vlc_instances[index]:
                    self.vlc_instances[index].release()
                    self.vlc_instances[index] = None
                if self.vlc_processes[index]:
                    try:
                        self.vlc_processes[index].terminate()
                        self.vlc_processes[index].wait(timeout=2)
                        self.vlc_processes[index] = None
                    except Exception as e:
                        logging.error(f"Stream {index}: Cleanup failed: {e}")
                if is_final:
                    break
                time.sleep(backoff_delay)
                backoff_delay *= 2

        self.root.after(0, lambda: self.labels[index].configure(image="", text="Stream Failed", fg="white"))
        self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget() if self.fullscreen_buttons[index] else None)
        logging.error(f"Stream {index} failed after {max_attempts} attempts")

    def _get_vlc_sink_input(self, pid):
        """Get PulseAudio sink input ID for a given VLC process PID."""
        try:
            # Run pactl list sink-inputs and parse output
            result = subprocess.run(['pactl', 'list', 'sink-inputs'], capture_output=True, text=True, check=True)
            output = result.stdout
            current_sink = None
            for line in output.splitlines():
                line = line.strip()
                if line.startswith('Sink Input #'):
                    current_sink = line.split('#')[1]
                elif f'application.process.id = "{pid}"' in line:
                    logging.debug(f"Found sink input {current_sink} for PID {pid}")
                    return current_sink
            logging.debug(f"No sink input found for PID {pid}")
            return None
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to get sink input for PID {pid}: {e}")
            return None
        except Exception as e:
            logging.error(f"Error querying PulseAudio sink inputs for PID {pid}: {e}")
            return None

    def _mute_vlc_cli_audio(self, index):
        """Mute the audio for a VLC CLI process and set volume to 0."""
        if not self.vlc_processes[index]:
            logging.warning(f"Stream {index}: No VLC process to mute")
            return
        pid = self.vlc_processes[index].pid
        sink_input = self._get_vlc_sink_input(pid)
        if sink_input:
            try:
                # Mute the sink input
                subprocess.run(['pactl', 'set-sink-input-mute', sink_input, '1'], check=True)
                # Set volume to 0
                subprocess.run(['pactl', 'set-sink-input-volume', sink_input, '0%'], check=True)
                logging.info(f"Stream {index} (PID {pid}): Muted and set volume to 0% (sink input {sink_input})")
            except subprocess.CalledProcessError as e:
                logging.error(f"Stream {index} (PID {pid}): Failed to mute or set volume (sink input {sink_input}): {e}")
        else:
            logging.warning(f"Stream {index} (PID {pid}): No sink input found, audio not muted")

    def _unmute_vlc_cli_audio(self, index):
        """Unmute the audio for a VLC CLI process and set volume to 100%."""
        if not self.vlc_processes[index]:
            logging.warning(f"Stream {index}: No VLC process to unmute")
            return
        pid = self.vlc_processes[index].pid
        max_attempts = 5
        retry_delay = 0.5  # Seconds between retries

        for attempt in range(max_attempts):
            sink_input = self._get_vlc_sink_input(pid)
            if sink_input:
                try:
                    # Unmute the sink input
                    subprocess.run(['pactl', 'set-sink-input-mute', sink_input, '0'], check=True)
                    # Set volume to 100% (65536)
                    subprocess.run(['pactl', 'set-sink-input-volume', sink_input, '100%'], check=True)
                    logging.info(f"Stream {index} (PID {pid}): Unmuted and set volume to 100% (sink input {sink_input}, attempt {attempt+1})")
                    return
                except subprocess.CalledProcessError as e:
                    logging.error(f"Stream {index} (PID {pid}): Failed to unmute or set volume (sink input {sink_input}, attempt {attempt+1}): {e}")
                    return
            logging.debug(f"Stream {index} (PID {pid}): Sink input not found, attempt {attempt+1}/{max_attempts}")
            if attempt < max_attempts - 1:
                time.sleep(retry_delay)

        logging.error(f"Stream {index} (PID {pid}): Failed to unmute or set volume after {max_attempts} attempts, no sink input found")

    def monitor_stream(self, index, player):
        last_check = time.time()
        failure_count = 0
        max_failures = 3
        backoff_delay = 15.0
        last_stream_switch = 0

        while self.running and (self.media_players[index] or self.vlc_processes[index]):
            try:
                current_time = time.time()
                if sys.platform.startswith("win") or (sys.platform.startswith("linux") and not self.use_hardware_acceleration):
                    # Python-vlc monitoring
                    if player is None:
                        break
                    state = player.get_state()
                    if state in (vlc.State.Ended, vlc.State.Error):
                        logging.error(f"Stream {index} stopped: {state}")
                        self.root.after(0, lambda: self.labels[index].configure(image="", text="Stream Failed", fg="white"))
                        self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget() if self.fullscreen_buttons[index] else None)
                        break
                    if current_time - last_check >= 1.0:
                        if player.get_state() == vlc.State.Buffering:
                            self.drop_counts[index] += 1
                            self.drop_timestamps[index].append(current_time)
                            logging.debug(f"Stream {index} drop detected (buffering), count: {self.drop_counts[index]}")
                else:
                    # VLC CLI monitoring
                    if self.vlc_processes[index] is None:
                        break
                    if self.vlc_processes[index].poll() is not None:
                        logging.error(f"Stream {index} VLC CLI process terminated")
                        self.root.after(0, lambda: self.labels[index].configure(image="", text="Stream Failed", fg="white"))
                        self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget() if self.fullscreen_buttons[index] else None)
                        break
                    if current_time - last_check >= 4.0:
                        try:
                            proc = psutil.Process(self.vlc_processes[index].pid)
                            cpu_percent = proc.cpu_percent(interval=0.1)
                            if cpu_percent == 0:
                                self.drop_counts[index] += 1
                                self.drop_timestamps[index].append(current_time)
                                logging.debug(f"Stream {index} drop detected (no CPU activity), count: {self.drop_counts[index]}")
                        except psutil.NoSuchProcess:
                            logging.error(f"Stream {index}: VLC CLI process vanished")
                            break

                self.drop_timestamps[index] = [t for t in self.drop_timestamps[index] if current_time - t < 5.0]
                if len(self.drop_timestamps[index]) >= 10 and self.hq_enabled[index]:
                    if current_time - last_stream_switch < 60.0:
                        logging.warning(f"Stream {index} switch throttled (last switch {current_time - last_stream_switch:.1f}s ago)")
                        self.root.after(0, lambda: self.labels[index].configure(image="", text="Waiting: Stream Unstable", fg="white"))
                        time.sleep(backoff_delay)
                        backoff_delay = min(backoff_delay * 2, 120.0)
                        continue

                    logging.warning(f"Stream {index} excessive drops ({len(self.drop_timestamps[index])} in 5s), switching to stream2")
                    self.hq_enabled[index] = False
                    self.update_streams()
                    self.save_config()
                    failure_count += 1
                    last_stream_switch = current_time

                    # Cleanup
                    if self.media_players[index]:
                        self.media_players[index].stop()
                        self.media_players[index] = None
                    if self.vlc_instances[index]:
                        self.vlc_instances[index].release()
                        self.vlc_instances[index] = None
                    if self.vlc_processes[index]:
                        try:
                            self.vlc_processes[index].terminate()
                            if self.running:  # Only wait if app is still running
                                self.vlc_processes[index].wait(timeout=5)
                            logging.info(f"Terminated VLC CLI process {index} during stream switch")
                        except subprocess.TimeoutExpired:
                            logging.warning(f"VLC CLI process {index} did not terminate within 5 seconds during stream switch, sending SIGKILL")
                            self.vlc_processes[index].kill()
                            try:
                                self.vlc_processes[index].wait(timeout=1)
                                logging.info(f"VLC CLI process {index} killed during stream switch")
                            except subprocess.TimeoutExpired:
                                logging.error(f"VLC CLI process {index} could not be killed during stream switch")
                        except Exception as e:
                            logging.error(f"Error terminating VLC CLI process {index} during stream switch: {e}")
                        self.vlc_processes[index] = None

                    self.root.after(0, lambda: self.labels[index].configure(image="", text="Loading...", fg="white"))

                    if failure_count >= max_failures:
                        logging.error(f"Stream {index} failed {failure_count} times, pausing for {backoff_delay}s")
                        self.root.after(0, lambda: self.labels[index].configure(image="", text="Paused: Stream Unstable", fg="white"))
                        time.sleep(backoff_delay)
                        backoff_delay = min(backoff_delay * 2, 120.0)
                        failure_count = 0
                        self.drop_counts[index] = 0
                        self.drop_timestamps[index] = []
                        continue

                    self.init_stream(index)
                    return

                last_check = current_time
                time.sleep(2.0 if self.vlc_processes[index] else 0.5)
            except Exception as e:
                logging.error(f"Error monitoring stream {index}: {e}")
                self.root.after(0, lambda: self.labels[index].configure(image="", text="Stream Failed", fg="white"))
                self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget() if self.fullscreen_buttons[index] else None)
                break

        # Final cleanup (suppress TimeoutExpired during shutdown)
        if self.media_players[index]:
            try:
                self.media_players[index].stop()
                logging.info(f"Stopped media player {index}")
            except Exception as e:
                logging.error(f"Error stopping media player {index}: {e}")
            self.media_players[index] = None
        if self.vlc_instances[index]:
            try:
                self.vlc_instances[index].release()
                logging.info(f"Released VLC instance {index}")
            except Exception as e:
                logging.error(f"Error releasing VLC instance {index}: {e}")
            self.vlc_instances[index] = None
        if self.vlc_processes[index]:
            try:
                pid = self.vlc_processes[index].pid
                self.vlc_processes[index].terminate()
                if self.running:  # Only wait if app is still running
                    self.vlc_processes[index].wait(timeout=5)
                logging.info(f"Terminated VLC CLI process {index} (PID {pid})")
            except subprocess.TimeoutExpired:
                logging.warning(f"VLC CLI process {index} (PID {pid}) did not terminate within 5 seconds, sending SIGKILL")
                self.vlc_processes[index].kill()
                try:
                    self.vlc_processes[index].wait(timeout=1)
                    logging.info(f"VLC CLI process {index} (PID {pid}) killed")
                except subprocess.TimeoutExpired:
                    logging.error(f"VLC CLI process {index} (PID {pid}) could not be killed")
            except Exception as e:
                logging.error(f"Error terminating VLC CLI process {index}: {e}")
            self.vlc_processes[index] = None
        logging.info(f"Stream {index} terminated")

    def start_streams(self):
        for i in range(4):
            if self.media_players[i]:
                self.media_players[i].stop()
                self.media_players[i] = None
            if self.vlc_instances[i]:
                self.vlc_instances[i].release()
                self.vlc_instances[i] = None
            if self.vlc_processes[i]:
                self.vlc_processes[i].terminate()
                self.vlc_processes[i].wait(timeout=2)
                self.vlc_processes[i] = None
            text = "Disabled" if not self.ips[i] else "Loading..."
            self.root.after(0, lambda idx=i, txt=text: self.labels[idx].configure(image="", text=txt, fg="white"))
        threads = []
        for i in range(4):
            if self.running and self.ips[i]:
                thread = threading.Thread(target=self.init_stream, args=(i,), daemon=True)
                threads.append(thread)
                thread.start()
        for thread in threads:
            thread.join()
        for i in range(4):
            if self.media_players[i] or self.vlc_processes[i]:
                self.root.after(0, lambda idx=i: self.update_target_dims(idx))

    def stop_streams(self):
        logging.info("Stopping all streams")
        self.running = False
        for i in range(4):
            try:
                # Stop python-vlc media player
                if self.media_players[i]:
                    try:
                        self.media_players[i].stop()
                        logging.info(f"Stopped media player {i}")
                    except Exception as e:
                        logging.error(f"Error stopping media player {i}: {e}")
                    self.media_players[i] = None
                # Release python-vlc instance
                if self.vlc_instances[i]:
                    try:
                        self.vlc_instances[i].release()
                        logging.info(f"Released VLC instance {i}")
                    except Exception as e:
                        logging.error(f"Error releasing VLC instance {i}: {e}")
                    self.vlc_instances[i] = None
                # Terminate VLC CLI process
                if self.vlc_processes[i]:
                    try:
                        # Ensure audio is muted to prevent playback during shutdown
                        if self.audio_enabled[i]:
                            self._mute_vlc_cli_audio(i)
                        pid = self.vlc_processes[i].pid
                        self.vlc_processes[i].terminate()
                        self.vlc_processes[i].wait(timeout=5)  # 5-second timeout
                        logging.info(f"Terminated VLC CLI process {i} (PID {pid})")
                    except subprocess.TimeoutExpired:
                        logging.warning(f"VLC CLI process {i} (PID {pid}) did not terminate within 5 seconds, sending SIGKILL")
                        self.vlc_processes[i].kill()
                        try:
                            self.vlc_processes[i].wait(timeout=1)  # Short wait after SIGKILL
                            logging.info(f"VLC CLI process {i} (PID {pid}) killed")
                        except subprocess.TimeoutExpired:
                            logging.error(f"VLC CLI process {i} (PID {pid}) could not be killed")
                    except Exception as e:
                        logging.error(f"Error terminating VLC CLI process {i}: {e}")
                    self.vlc_processes[i] = None
                # Hide fullscreen button
                if self.fullscreen_buttons[i]:
                    self.root.after(0, lambda idx=i: self.fullscreen_buttons[idx].place_forget())
            except Exception as e:
                logging.error(f"Error during cleanup of stream {i}: {e}")
        time.sleep(0.5)
        self.running = True
        logging.info("All streams stopped and cleaned up")

    def handle_stream_click(self, index):
        if not self.streams[index]:
            return
        logging.info(f"Stream {index} clicked, fullscreen: {self.is_fullscreen}, current index: {self.fullscreen_index}")
        if self.is_fullscreen and self.fullscreen_index == index:
            self.is_fullscreen = False
            self.fullscreen_index = None
        else:
            self.is_fullscreen = True
            self.fullscreen_index = index

        # Manage audio for all streams
        for i in range(4):
            if not self.streams[i] or not self.audio_enabled[i]:
                continue
            if sys.platform.startswith("win") or (sys.platform.startswith("linux") and not self.use_hardware_acceleration):
                # python-vlc audio control
                if self.media_players[i]:
                    try:
                        if i == self.fullscreen_index and self.is_fullscreen:
                            time.sleep(0.15)
                            self.media_players[i].audio_set_volume(0)
                            time.sleep(0.05)
                            start_time = time.time()
                            max_attempts = 7
                            for attempt in range(max_attempts):
                                if time.time() - start_time > 0.5:
                                    logging.warning(f"Stream {i} volume setting timed out after {attempt} attempts")
                                    break
                                self.media_players[i].audio_set_volume(100)
                                time.sleep(0.05)
                                current_volume = self.media_players[i].audio_get_volume()
                                player_state = self.media_players[i].get_state()
                                if current_volume == 100:
                                    logging.debug(f"Stream {i} volume successfully set to 100 on attempt {attempt+1}, state: {player_state}")
                                    break
                                logging.debug(f"Stream {i} volume set attempt {attempt+1} failed, current: {current_volume}, state: {player_state}")
                            else:
                                logging.warning(f"Stream {i} failed to set volume to 100 after {max_attempts} attempts")
                        else:
                            self.media_players[i].audio_set_volume(0)
                            logging.debug(f"Stream {i} volume set to 0")
                    except Exception as e:
                        logging.error(f"Failed to set audio volume for stream {i}: {e}")
            else:
                # VLC CLI audio control
                if self.vlc_processes[i]:
                    try:
                        if i == self.fullscreen_index and self.is_fullscreen:
                            # Delay unmuting and volume setting for the selected stream
                            threading.Timer(0.5, lambda idx=i: self._unmute_vlc_cli_audio(idx)).start()
                        else:
                            # Immediately mute all other streams
                            self._mute_vlc_cli_audio(i)
                    except Exception as e:
                        logging.error(f"Failed to manage audio for VLC CLI stream {i}: {e}")

        self.update_layout()

    def update_layout(self):
        logging.info(f"Updating layout, fullscreen: {self.is_fullscreen}, index: {self.fullscreen_index}")
        if self.is_fullscreen:
            for i in range(4):
                if i == self.fullscreen_index:
                    w, h = self.grid_frame.winfo_width(), self.grid_frame.winfo_height()
                    if w <= 10 or h <= 10:
                        w, h = 1280, 670
                    self.panels[i].place(x=0, y=0, width=w, height=h)
                    self.panel_sizes[i] = (w, h)
                    self.update_target_dims(i)
                    if self.media_players[i] and (sys.platform.startswith("win") or (sys.platform.startswith("linux") and not self.use_hardware_acceleration)):
                        hwnd = self.labels[i].winfo_id()
                        self.media_players[i].set_hwnd(hwnd) if sys.platform.startswith("win") else self.media_players[i].set_xwindow(hwnd)
                    if self.fullscreen_buttons[i] and self.streams[i]:
                        self.fullscreen_buttons[i].configure(image=self.minimize_images[i])
                        self.fullscreen_buttons[i].place(relx=1.0, rely=1.0, x=-35, y=-35, anchor="se")
                else:
                    self.panels[i].place_forget()
                    if self.fullscreen_buttons[i]:
                        self.fullscreen_buttons[i].place_forget()
                y_offset = 0 if not self.top_panel_visible else 50
                self.grid_frame.place(x=0, y=y_offset, relwidth=1.0, relheight=1.0)
                if self.top_panel_visible and self.fullscreen_index is not None and self.streams[self.fullscreen_index]:
                    for button in self.ptz_buttons:
                        button.pack(side="right", padx=5, pady=5)
                else:
                    for button in self.ptz_buttons:
                        button.pack_forget()
        else:
            w, h = self.grid_frame.winfo_width(), self.grid_frame.winfo_height()
            if w <= 10 or h <= 10:
                w, h = 1920, 1080
            ww, hh = w // 2, h // 2
            self.panel_sizes = [(ww, hh)] * 4
            self.panels[0].place(x=0, y=0, width=ww, height=hh)
            self.panels[1].place(x=ww, y=0, width=ww, height=hh)
            self.panels[2].place(x=0, y=hh, width=ww, height=hh)
            self.panels[3].place(x=ww, y=hh, width=ww, height=hh)
            for i in range(4):
                self.update_target_dims(i)
                if self.media_players[i] and (sys.platform.startswith("win") or (sys.platform.startswith("linux") and not self.use_hardware_acceleration)):
                    hwnd = self.labels[i].winfo_id()
                    self.media_players[i].set_hwnd(hwnd) if sys.platform.startswith("win") else self.media_players[i].set_xwindow(hwnd)
                    if self.audio_enabled[i]:
                        self.media_players[i].audio_set_volume(0)  # Mute in grid view
                if self.vlc_processes[i] and self.audio_enabled[i]:
                    self._mute_vlc_cli_audio(i)  # Mute VLC CLI in grid view
                if self.fullscreen_buttons[i] and self.streams[i]:
                    self.fullscreen_buttons[i].configure(image=self.fullscreen_images[i])
                    self.fullscreen_buttons[i].place(relx=1.0, rely=1.0, x=-35, y=-35, anchor="se")
                elif self.fullscreen_buttons[i]:
                    self.fullscreen_buttons[i].place_forget()
            y_offset = 0 if not self.top_panel_visible else 50
            self.grid_frame.place(x=0, y=y_offset, relwidth=1.0, relheight=1.0)
            if self.top_panel_visible:
                for button in self.ptz_buttons:
                    button.pack_forget()

    def update_target_dims(self, index):
        w, h = self.panel_sizes[index]
        if w <= 10 or h <= 10:
            self.target_dims[index] = (0, 0)
            logging.debug(f"Stream {index}: Invalid panel size ({w}, {h}), target_dims=(0, 0)")
            return
        frame_w, frame_h = self.frame_shapes[index]
        if frame_w <= 0 or frame_h <= 0:
            self.target_dims[index] = (0, 0)
            logging.debug(f"Stream {index}: Invalid frame shape ({frame_w}, {frame_h}), target_dims=(0, 0)")
            return
        self.target_dims[index] = (w, h)
        logging.debug(f"Stream {index}: Panel=({w}, {h}), Frame=({frame_w}, {frame_h}), Target=({w}, {h})")

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
            return
        ip = self.ips[self.fullscreen_index]
        if not ip or self.ptz_busy:
            return
        self.ptz_busy = True
        if direction in ["left", "right"]:
            self.ptz_click_counts[self.fullscreen_index] += 1
        self.ptz_moving = True
        threading.Thread(target=self.ptz_move_loop, args=(direction, ip), daemon=True).start()

    def stop_ptz_move(self, direction):
        if not self.is_fullscreen or self.fullscreen_index is None:
            return
        self.ptz_moving = False
        ip = self.ips[self.fullscreen_index]
        if ip:
            self.send_ptz_command(ip, "stop")
        self.ptz_busy = False

    def ptz_move_loop(self, direction, ip):
        try:
            with self.ptz_lock:
                self.send_ptz_command(ip, direction)
                time.sleep(1.0)
                if direction in ["left", "right"]:
                    self.send_ptz_command(ip, "pulse_stop")
                elif self.ptz_moving:
                    self.send_ptz_command(ip, "stop")
        finally:
            self.ptz_busy = False

    def send_ptz_command(self, ip, command):
        cam_info = self.get_onvif_camera(ip)
        if not cam_info:
            return
        try:
            ptz = cam_info["ptz"]
            token = cam_info["token"]
            if command == "stop":
                ptz.Stop({"ProfileToken": token})
                return
            elif command == "pulse_stop":
                request = ptz.create_type("ContinuousMove")
                request.ProfileToken = token
                y_velocity = 0.001 if self.ptz_click_counts[self.fullscreen_index] % 2 == 1 else -0.001
                request.Velocity = {"PanTilt": {"x": 0, "y": y_velocity}, "Zoom": {"x": 0}}
                ptz.ContinuousMove(request)
                time.sleep(0.1)
                ptz.Stop({"ProfileToken": token})
                return
            request = ptz.create_type("ContinuousMove")
            request.ProfileToken = token
            velocity = {"PanTilt": {"x": 0, "y": 0}, "Zoom": {"x": 0}}
            speed = 0.1
            if command == "left":
                velocity["PanTilt"]["x"] = -speed
            elif command == "right":
                velocity["PanTilt"]["x"] = speed
            elif command == "up":
                velocity["PanTilt"]["y"] = speed
            elif command == "down":
                velocity["PanTilt"]["y"] = -speed
            else:
                return
            request.Velocity = velocity
            ptz.ContinuousMove(request)
        except Exception:
            pass

    def shutdown_instantly(self):
        logging.info("Initiating instant shutdown")
        self.running = False

        # Destroy Tkinter root
        try:
            self.root.destroy()
            logging.info("Tkinter root destroyed")
        except Exception as e:
            logging.error(f"Error destroying Tkinter root: {e}")

        # Cleanup streams
        for i in range(4):
            try:
                # Stop python-vlc media player
                if self.media_players[i]:
                    try:
                        self.media_players[i].stop()
                        logging.info(f"Stopped media player {i}")
                    except Exception as e:
                        logging.error(f"Error stopping media player {i}: {e}")
                    self.media_players[i] = None
                # Release python-vlc instance
                if self.vlc_instances[i]:
                    try:
                        self.vlc_instances[i].release()
                        logging.info(f"Released VLC instance {i}")
                    except Exception as e:
                        logging.error(f"Error releasing VLC instance {i}: {e}")
                    self.vlc_instances[i] = None
                # Terminate VLC CLI process
                if self.vlc_processes[i]:
                    try:
                        # Ensure audio is muted to prevent playback during shutdown
                        if self.audio_enabled[i]:
                            self._mute_vlc_cli_audio(i)
                        pid = self.vlc_processes[i].pid
                        self.vlc_processes[i].terminate()
                        self.vlc_processes[i].wait(timeout=5)  # 5-second timeout
                        logging.info(f"Terminated VLC CLI process {i} (PID {pid})")
                    except subprocess.TimeoutExpired:
                        logging.warning(f"VLC CLI process {i} (PID {pid}) did not terminate within 5 seconds, sending SIGKILL")
                        self.vlc_processes[i].kill()
                        try:
                            self.vlc_processes[i].wait(timeout=1)  # Short wait after SIGKILL
                            logging.info(f"VLC CLI process {i} (PID {pid}) killed")
                        except subprocess.TimeoutExpired:
                            logging.error(f"VLC CLI process {i} (PID {pid}) could not be killed")
                    except Exception as e:
                        logging.error(f"Error terminating VLC CLI process {i}: {e}")
                    self.vlc_processes[i] = None
            except Exception as e:
                logging.error(f"Error during shutdown of stream {i}: {e}")

        # Clear ONVIF camera references
        try:
            self.onvif_cams = {}
            logging.info("Cleared ONVIF camera references")
        except Exception as e:
            logging.error(f"Error clearing ONVIF cameras: {e}")

        logging.info("Shutdown completed")


    def cleanup(self):
        self.shutdown_instantly()

if __name__ == "__main__":
    root = tk.Tk()
    app = RTSPViewer(root)
    root.mainloop()
