# -*- mode: python ; coding: utf-8 -*-
# Shared PyInstaller spec for the pdf-drm viewer. Run on each target OS with
# that OS's own Python venv -- PyInstaller does not cross-compile.
#   macOS:   pyinstaller viewer.spec        -> dist/SecureViewer.app
#   Windows: pyinstaller viewer.spec        -> dist/SecureViewer/SecureViewer.exe
import sys
from pathlib import Path

root = Path(SPECPATH)

a = Analysis(
    [str(root / "viewer" / "main.py")],
    pathex=[str(root)],
    binaries=[],
    # The window/taskbar icon is loaded at runtime from viewer/assets/icon.png.
    datas=[(str(root / "viewer" / "assets" / "icon.png"), "viewer/assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SecureViewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # .exe icon (Windows only; on macOS the icon belongs to the .app bundle below).
    icon=str(root / "viewer" / "assets" / "icon.ico") if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SecureViewer",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="SecureViewer.app",
        icon=str(root / "viewer" / "assets" / "icon.icns"),
        bundle_identifier="com.pdfdrm.secureviewer",
        info_plist={
            "NSHighResolutionCapable": "True",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleDisplayName": "Secure PDF Viewer",
        },
    )
