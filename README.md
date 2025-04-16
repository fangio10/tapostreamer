# Tapo Streamer

**Tapo Streamer** is a powerful, open-source application for streaming and controlling multiple RTSP cameras with a sleek, modern interface. Built with Python and Tkinter, it supports up to four simultaneous streams, Pan-Tilt-Zoom (PTZ) controls, hardware acceleration, and audio management. Whether you're monitoring security cameras or managing live feeds, Tapo Streamer delivers a robust and customizable experience on Linux and Windows.

## Features

- **Multi-Camera Streaming**: View up to four RTSP streams simultaneously in a grid or fullscreen mode.
- **PTZ Controls**: Seamlessly control Pan, Tilt, and Zoom for ONVIF-compatible cameras.
- **Hardware Acceleration**: Leverage GPU acceleration on Linux for smooth, low-latency streaming (VLC CLI mode).
- **Audio Management**: Enable/disable audio per stream, with automatic muting in grid view and unmuting in fullscreen.
- **Cross-Platform**: Runs on Linux (with VLC CLI or python-vlc) and Windows (python-vlc).

### Prerequisites

- **VLC Media Player**: Required for streaming.
  - Linux: `sudo apt install vlc` (Ubuntu/Debian) or equivalent.
  - Windows: [Download VLC](https://www.videolan.org/vlc/)

### License
This project is licensed under the MIT License. See LICENSE for details.

### Acknowledgments
Built with Python, Tkinter, and VLC.

Thanks to the open-source community for libraries like python-vlc and onvif-zeep.

