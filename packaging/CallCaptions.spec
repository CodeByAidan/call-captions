# PyInstaller spec for the app. Build with packaging/windows/build.ps1 or packaging/macos/build.sh.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

root = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, root)
from settings import APP_NAME, BUNDLE_ID  # noqa: E402

windows = sys.platform == "win32"
mac = sys.platform == "darwin"
version = os.environ.get("APP_VERSION", "1.0.0")

hiddenimports = ["pynvml"]
binaries = collect_dynamic_libs("ctranslate2")  # loaded from its package folder
datas = collect_data_files("faster_whisper")    # Silero VAD model used to skip non-speech
excludes = ["tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf"]
if windows:
    hiddenimports += ["audio_windows", "comtypes", "psutil"]
if mac:
    import whisper_mlx

    # whisper_mlx loads mlx-whisper's modules by name, and MLX's compiled core imports Python
    # modules of its own, so PyInstaller can't see either.
    hiddenimports += ["audio_mac", "objc", "AppKit", "Foundation", *collect_submodules("mlx"),
                      *(f"mlx_whisper.{name}" for name in whisper_mlx.SUBMODULES)]
    binaries += collect_dynamic_libs("mlx")
    datas += collect_data_files("mlx_whisper") + collect_data_files("mlx", includes=["**/*.metallib"])
    # Only mlx-whisper's long-form transcriber needs these, and the app never loads it.
    excludes += ["mlx_whisper.transcribe", "mlx_whisper.timing", "mlx_whisper.torch_whisper",
                 "mlx_whisper.cli", "mlx_whisper.writers", "numba", "scipy", "torch"]

a = Analysis(
    [os.path.join(root, "captions.py")],
    pathex=[root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CallCaptions",
    console=False,
    icon=os.path.join(root, "packaging", "icon.icns" if mac else "icon.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CallCaptions", upx=False)

if mac:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=os.path.join(root, "packaging", "icon.icns"),
        bundle_identifier=BUNDLE_ID,
        version=version,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "LSMinimumSystemVersion": "14.2",
            "LSUIElement": True,  # menu bar app: no Dock icon
            "NSHighResolutionCapable": True,
            "NSAudioCaptureUsageDescription":
                f"{APP_NAME} listens to Discord (or everything you hear) to show what people say as captions.",
        },
    )
