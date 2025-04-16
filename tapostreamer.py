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

try:
    import vlc
except ImportError:
    print("ERROR: python-vlc not installed. Run 'pip install python-vlc'")
    sys.exit(1)

# Logging setup
logging.basicConfig(
    filename='vlc_errors.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

logging.basicConfig(level=logging.WARNING)

class RTSPViewer:
    def __init__(self, root):
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
            img_titlebar = img.resize((24, 24), Image.LANCZOS)
            icon = ImageTk.PhotoImage(img_titlebar)
            self.root.iconphoto(True, icon)
        except Exception as e:
            print(f"Error loading icon: {e}")
        self.root.configure(bg="#222222")
        self.root.geometry("2304x1296")
        self.root.minsize(1280, 720)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self.cleanup)
        self.username = ""
        self.password = ""
        self.ips = ["", "", "", ""]
        self.hq_enabled = [True, True, True, True]
        self.audio_enabled = [True, True, True, True]
        self.streams = ["", "", "", ""]
        if sys.platform.startswith("win"):
            config_dir = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "TapoStreamer")
        else:
            config_dir = os.path.join(os.path.expanduser("~"), ".tapo-streamer")
        os.makedirs(config_dir, exist_ok=True)
        self.config_file = os.path.join(config_dir, "config.json")
        self.vlc_instances = [None] * 4
        self.media_players = [None] * 4
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
        # Load config if exists
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    config = json.load(f)
                self.username = config.get("username", self.username)
                self.password = config.get("password", self.password)
                self.ips = config.get("ips", self.ips)
                # Ensure hq_enabled is a boolean list
                self.hq_enabled = [
                    bool(config.get("hq_enabled", self.hq_enabled)[i])
                    for i in range(4)
                ]
                self.audio_enabled = config.get("audio_enabled", self.audio_enabled)
                self.hide_top_panel_on_launch = config.get("hide_top_panel_on_launch", self.hide_top_panel_on_launch)
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
            "hide_top_panel_on_launch": self.hide_top_panel_on_launch
        }
        try:
            with open(self.config_file, "w") as f:
                json.dump(config, f, indent=4)
            logging.info(f"Saved config to {self.config_file}")
        except Exception as e:
            logging.error(f"Failed to save config to {self.config_file}: {e}")

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

    def show_config_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Configure Streams")
        dialog.geometry("400x450")
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

        hide_panel_var = tk.BooleanVar(value=self.hide_top_panel_on_launch)
        hide_panel_cb = ttk.Checkbutton(dialog, text="Hide options panel on launch", variable=hide_panel_var)
        hide_panel_cb.place(x=20, y=380)

        tk.Button(
            dialog, text="Save",
            command=lambda: self.save_streams(
                username_entry, password_entry, ip_entries,
                hq_checkboxes, audio_checkboxes, hide_panel_var, dialog
            )
        ).place(x=150, y=410)
        tk.Button(dialog, text="Cancel", command=dialog.destroy).place(x=250, y=410)

    def save_streams(self, username_entry, password_entry, ip_entries, hq_checkboxes, audio_checkboxes, hide_panel_var, dialog):
        self.username = username_entry.get().strip()
        self.password = password_entry.get().strip()
        self.ips = [e.get().strip() for e in ip_entries]
        self.hq_enabled = [v.get() for v in hq_checkboxes]
        self.audio_enabled = [v.get() for v in audio_checkboxes]
        self.hide_top_panel_on_launch = hide_panel_var.get()
        self.onvif_cams = {}
        self.ptz_click_counts = [0] * 4
        self.drop_counts = [0] * 4
        self.drop_timestamps = [[] for _ in range(4)]
        self.update_streams()
        self.save_config()
        dialog.destroy()
        threading.Thread(target=self.restart_streams, daemon=True).start()

    def restart_streams(self):
        self.stop_streams()
        self.root.after(0, lambda: [
            self.labels[i].configure(image="", text="Loading Streams...", fg="white") or
            self.fullscreen_buttons[i].place_forget()
            for i in range(4)
        ])
        self.start_streams()

    def init_stream(self, index):
        if not self.ips[index]:
            self.root.after(0, lambda: self.labels[index].configure(image="", text="Disabled", fg="white"))
            self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget() if self.fullscreen_buttons[index] else None)
            logging.info(f"No IP defined for index {index}, marked as Disabled")
            return
        if not self.streams[index]:
            self.root.after(0, lambda: self.labels[index].configure(image="", text="Disabled", fg="white"))
            self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget() if self.fullscreen_buttons[index] else None)
            logging.warning(f"No stream URL for index {index}")
            return
        logging.info(f"Initializing stream {index}: {self.streams[index]} (HQ={self.hq_enabled[index]})")
        start_time = time.time()
        max_attempts = 3
        for attempt in range(max_attempts):
            is_final = attempt == max_attempts - 1
            try:
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

                logging.debug(f"WAYLAND_DISPLAY: {os.environ.get('WAYLAND_DISPLAY')}")
                logging.debug(f"XDG_SESSION_TYPE: {os.environ.get('XDG_SESSION_TYPE')}")
                instance = vlc.Instance([
                    '--no-video-title-show',
                    '--network-caching=3000',
                    '--audio-resampler=soxr',
                    '--no-audio-replay-gain',
                    '--deinterlace=auto',
                    '--no-drop-late-frames',
                    '--no-skip-frames',
                    '--live-caching=3000',
                    '--vout=wayland',  # or x11 if that works
                    '--avcodec-hw=vaapi',
                    '--no-xlib',
                    '--verbose=2'
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
                try:
                    hwnd = self.labels[index].winfo_id()
                    player.set_hwnd(self.labels[index].winfo_id())


                    #player.set_xwindow(hwnd)  # Try set_xwindow first
                    logging.debug(f"Stream {index}: Set XWindow ID {hwnd} (Wayland fallback)")
                except Exception as e:
                    logging.error(f"Stream {index}: Failed to set window ID: {e}")

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
                xid = self.labels[index].winfo_id()
                player.set_xwindow(xid)
                logging.debug(f"Stream {index}: Set XWindow ID {xid}")

                player.audio_set_volume(0)
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
                        logging.debug(f"Stream {index}: Finalizing setup")
                        max_attempts = 5
                        for attempt in range(max_attempts):
                            time.sleep(0.5)
                            width, height = player.video_get_size(0) or (0, 0)
                            if width > 0 and height > 0:
                                self.frame_shapes[index] = (width, height)
                                logging.info(f"Stream {index} resolution: {self.frame_shapes[index]}")
                                break
                            logging.debug(f"Stream {index}: Attempt {attempt+1}/{max_attempts} to get resolution, got ({width}, {height})")
                        else:
                            self.frame_shapes[index] = (2304, 1296)
                            logging.warning(f"Stream {index}: Failed to get resolution after {max_attempts} attempts, using fallback (2304, 1296)")
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
                logging.error(f"Stream {index} attempt {attempt+1}/{max_attempts} failed (hq={self.hq_enabled[index]}): {e}", exc_info=True)
                if self.media_players[index]:
                    self.media_players[index].stop()
                    self.media_players[index] = None
                if self.vlc_instances[index]:
                    self.vlc_instances[index].release()
                    self.vlc_instances[index] = None
                if is_final:
                    break
                time.sleep(1.0)
        self.root.after(0, lambda: self.labels[index].configure(image="", text="Stream Failed", fg="white"))
        self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget() if self.fullscreen_buttons[index] else None)
        logging.error(f"Stream {index} failed after {max_attempts} attempts")

    def monitor_stream(self, index, player):
        last_check = time.time()
        while self.running and self.media_players[index]:
            try:
                state = player.get_state()
                if state in (vlc.State.Ended, vlc.State.Error):
                    logging.error(f"Stream {index} stopped: {state}")
                    self.root.after(0, lambda: self.labels[index].configure(image="", text="Stream Failed", fg="white"))
                    self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget() if self.fullscreen_buttons[index] else None)
                    break
                current_time = time.time()
                if current_time - last_check >= 1.0:
                    if player.get_state() == vlc.State.Buffering:
                        self.drop_counts[index] += 1
                        self.drop_timestamps[index].append(current_time)
                        logging.debug(f"Stream {index} drop detected (buffering), count: {self.drop_counts[index]}")
                    self.drop_timestamps[index] = [t for t in self.drop_timestamps[index] if current_time - t < 5.0]
                    if len(self.drop_timestamps[index]) >= 10 and self.hq_enabled[index]:
                        logging.warning(f"Stream {index} excessive drops ({len(self.drop_timestamps[index])} in 5s), switching to stream2")
                        self.hq_enabled[index] = False
                        self.update_streams()
                        self.save_config()
                        player.stop()
                        self.media_players[index] = None
                        self.vlc_instances[index].release()
                        self.vlc_instances[index] = None
                        self.root.after(0, lambda: self.labels[index].configure(image="", text="Loading...", fg="white"))
                        self.init_stream(index)
                        return
                    last_check = current_time
                time.sleep(0.5)
            except Exception as e:
                logging.error(f"Error monitoring stream {index}: {e}")
                self.root.after(0, lambda: self.labels[index].configure(image="", text="Stream Failed", fg="white"))
                self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget() if self.fullscreen_buttons[index] else None)
                break
        if self.media_players[index]:
            self.media_players[index].stop()
            self.media_players[index] = None
        if self.vlc_instances[index]:
            self.vlc_instances[index].release()
            self.vlc_instances[index] = None
        logging.info(f"Stream {index} terminated")

    def start_streams(self):
        for i in range(4):
            if self.media_players[i]:
                self.media_players[i].stop()
                self.media_players[i] = None
            if self.vlc_instances[i]:
                self.vlc_instances[i].release()
                self.vlc_instances[i] = None
            text = "Disabled" if not self.ips[i] else "Loading..."
            self.root.after(0, lambda idx=i, txt=text: self.labels[idx].configure(image="", text=txt, fg="white"))
        threads = []
        for i in range(4):
            if self.running and self.ips[i]:
                thread = threading.Thread(target=self.init_stream, args=(i,), daemon=True)
                threads.append(thread)
                thread.start()
        # Wait for all threads to complete (optional, ensures layout updates after initialization)
        for thread in threads:
            thread.join()
        for i in range(4):
            if self.media_players[i]:
                self.root.after(0, lambda idx=i: self.update_target_dims(idx))

    def restart_streams(self):
        self.stop_streams()
        self.root.after(0, lambda: [
            self.labels[i].configure(image="", text="Disabled" if not self.ips[i] else "Loading...", fg="white") or
            self.fullscreen_buttons[i].place_forget()
            for i in range(4)
        ])
        self.start_streams()

    def stop_streams(self):
        self.running = False
        for i in range(4):
            if self.media_players[i]:
                try:
                    self.media_players[i].stop()
                    logging.info(f"Stopped media player {i}")
                except Exception as e:
                    logging.error(f"Error stopping media player {i}: {e}")
                self.media_players[i] = None
            if self.vlc_instances[i]:
                try:
                    self.vlc_instances[i].release()
                    logging.info(f"Released VLC instance {i}")
                except Exception as e:
                    logging.error(f"Error releasing VLC instance {i}: {e}")
                self.vlc_instances[i] = None
            if self.fullscreen_buttons[i]:
                self.root.after(0, lambda idx=i: self.fullscreen_buttons[idx].place_forget())
        time.sleep(1.0)
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
        for i in range(4):
            if self.media_players[i]:
                try:
                    if i == self.fullscreen_index and self.is_fullscreen and self.audio_enabled[i]:
                        # Wait for VLC to stabilize
                        time.sleep(0.15)  # Increased from 0.1 to 0.15 for more reliability
                        # Toggle audio to reset VLC's audio pipeline
                        self.media_players[i].audio_set_volume(0)
                        time.sleep(0.05)
                        # Retry setting volume to 100 with timeout
                        start_time = time.time()
                        max_attempts = 7
                        for attempt in range(max_attempts):
                            if time.time() - start_time > 0.5:  # 500ms total timeout
                                logging.warning(f"Stream {i} volume setting timed out after {attempt} attempts")
                                break
                            self.media_players[i].audio_set_volume(100)  # Set to 100%
                            time.sleep(0.05)  # Brief delay between retries
                            current_volume = self.media_players[i].audio_get_volume()
                            player_state = self.media_players[i].get_state()
                            if current_volume == 100:
                                logging.debug(f"Stream {i} volume successfully set to 100 on attempt {attempt+1}, state: {player_state}")
                                break
                            logging.debug(f"Stream {i} volume set attempt {attempt+1} failed, current: {current_volume}, state: {player_state}")
                        else:
                            logging.warning(f"Stream {i} failed to set volume to 100 after {max_attempts} attempts, final volume: {current_volume}")
                    else:
                        self.media_players[i].audio_set_volume(0)
                        logging.debug(f"Stream {i} volume set to 0")
                except Exception as e:
                    logging.error(f"Failed to set audio volume for stream {i}: {e}")
        self.update_layout()

    def update_layout(self):
        logging.info(f"Updating layout, fullscreen: {self.is_fullscreen}, index: {self.fullscreen_index}")
        if self.is_fullscreen:
            for i in range(4):
                if i == self.fullscreen_index:
                    w, h = self.grid_frame.winfo_width(), self.grid_frame.winfo_height()
                    if w <= 10 or h <= 10:
                        w, h = 1280, 670
                    # Revert to original: stretch to full grid_frame size
                    self.panels[i].place(x=0, y=0, width=w, height=h)
                    self.panel_sizes[i] = (w, h)
                    self.update_target_dims(i)
                    if self.media_players[i]:
                        hwnd = self.labels[i].winfo_id()
                        self.media_players[i].set_hwnd(hwnd)
                    if self.fullscreen_buttons[i] and self.streams[i]:
                        self.fullscreen_buttons[i].configure(image=self.minimize_images[i])
                        self.fullscreen_buttons[i].place(relx=1.0, rely=0.0, x=-35, y=5, anchor="ne")
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
                if self.media_players[i]:
                    hwnd = self.labels[i].winfo_id()
                    self.media_players[i].set_hwnd(hwnd)
                    self.media_players[i].audio_set_volume(0)
                if self.fullscreen_buttons[i] and self.streams[i]:
                    self.fullscreen_buttons[i].configure(image=self.fullscreen_images[i])
                    self.fullscreen_buttons[i].place(relx=1.0, rely=0.0, x=-35, y=5, anchor="ne")
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
        # Simplified: target matches panel size
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
        """
        Instantly and gracefully shut down the application, releasing resources without delays.
        """
        logging.info("Initiating instant shutdown")
        self.running = False  # Stop all stream monitoring loops

        # Destroy the Tkinter root immediately to halt UI updates
        try:
            self.root.destroy()
            logging.info("Tkinter root destroyed")
        except Exception as e:
            logging.error(f"Error destroying Tkinter root: {e}")

        # Release VLC resources
        for i in range(4):
            try:
                if self.media_players[i]:
                    self.media_players[i].stop()
                    self.media_players[i] = None
                    logging.info(f"Stopped media player {i}")
                if self.vlc_instances[i]:
                    self.vlc_instances[i].release()
                    self.vlc_instances[i] = None
                    logging.info(f"Released VLC instance {i}")
            except Exception as e:
                logging.error(f"Error releasing VLC resources for stream {i}: {e}")

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