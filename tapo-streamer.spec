# -*- mode: python ; coding: utf-8 -*-
import os
import tkinter
from PyInstaller.utils.hooks import collect_data_files
import sys
import site
import platform

# Determine platform
is_windows = platform.system() == "Windows"
is_linux = platform.system() == "Linux"

# Locate WSDL files dynamically
wsdl_files = []
if is_windows:
    site_packages = os.path.join(sys.base_prefix, "Lib", "site-packages")
    wsdl_path = os.path.join(site_packages, "wsdl")
    if os.path.exists(wsdl_path):
        wsdl_files.extend(
            [(os.path.join(wsdl_path, f), "wsdl") for f in os.listdir(wsdl_path) if os.path.isfile(os.path.join(wsdl_path, f))]
        )
else:
    for sp in site.getsitepackages():
        wsdl_path = os.path.join(sp, "wsdl")
        if os.path.exists(wsdl_path):
            wsdl_files.extend(
                [(os.path.join(wsdl_path, f), "wsdl") for f in os.listdir(wsdl_path) if os.path.isfile(os.path.join(wsdl_path, f))]
            )

# Add VLC plugins
vlc_plugin_path = "/usr/lib/x86_64-linux-gnu/vlc/plugins"  # Adjust if different
vlc_datas = [(vlc_plugin_path, "vlc/plugins")] if os.path.exists(vlc_plugin_path) else []

# 3. Create a dynamic runtime hook to force the binary to use our bundled assets
with open('runtime_tcl_fix.py', 'w') as f:
    f.write('''import os\nimport sys\n''')
    f.write('''base_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)\n''')
    f.write('''internal_dir = os.path.join(base_dir, '_internal')\n''')
    f.write('''os.environ['TCL_LIBRARY'] = os.path.join(internal_dir, 'tcl_assets', 'tcl8.6')\n''')
    f.write('''os.environ['TK_LIBRARY'] = os.path.join(internal_dir, 'tcl_assets', 'tk8.6')\n''')

a = Analysis(
    ['tapo-streamer.py'],
    pathex=[],
    binaries=[],
    datas=[
        *wsdl_files,
        ('cam.png', '.'),
        *vlc_datas,
    ],
    hiddenimports=[
        'PIL', 
        'PIL.ImageTk', 
        'PIL._tkinter_finder', 
        '_tkinter',
        'tkinter',
        'vlc',
        'zeep',
        'onvif',
        'zeep.transports',
        'zeep.plugins',
        'zeep.wsdl.utils',
        'zeep.wsdl.wsdl',
        'zeep.wsdl.definitions',
        'zeep.wsdl.messages',
        'zeep.wsdl.bindings.soap',
        'zeep.wsdl.bindings.http',
        'onvif.client',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['runtime_tcl_fix.py'],  # Injected the dynamic environmental wrapper hook
    excludes=[
        'libvlc.so', 'libvlccore.so', 'libvlc.so.5',
        'ffmpeg', 'ffmpeg-python', 'numpy',
        'aiohttp', 'cryptography'
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='tapo-streamer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='tapo-streamer',
)
