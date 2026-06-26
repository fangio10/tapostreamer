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