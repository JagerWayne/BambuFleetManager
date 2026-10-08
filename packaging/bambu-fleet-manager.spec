# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Bambu Fleet Manager tray app.

Build with:  pyinstaller packaging/bambu-fleet-manager.spec --noconfirm --clean
Produces a one-folder bundle in ``dist/BambuFleetManager`` whose exe is
``BambuFleetManager.exe`` (windowed; no console).
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

datas = [
    (os.path.join(ROOT, "templates"), "templates"),
    (os.path.join(ROOT, "static"), "static"),
    (os.path.join(ROOT, "assets"), "assets"),
    (os.path.join(ROOT, "docs"), "docs"),
    (os.path.join(ROOT, "VERSION"), "."),
    (os.path.join(ROOT, "config", "printers.json.example"), "config"),
    (os.path.join(ROOT, "config", "settings.json.example"), "config"),
]
datas += collect_data_files("imageio_ffmpeg")

hiddenimports = [
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "paho.mqtt.client",
    "imageio_ffmpeg",
]
hiddenimports += collect_submodules("paho")

block_cipher = None

a = Analysis(
    [os.path.join(ROOT, "tray", "bambu_tray.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PyQt5", "PySide2", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BambuFleetManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(ROOT, "assets", "bambu.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BambuFleetManager",
)
