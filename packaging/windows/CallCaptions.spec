# PyInstaller spec for the app. On Windows, build with packaging/windows/build.ps1.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

root = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
windows = sys.platform == "win32"

a = Analysis(
    [os.path.join(root, "captions.py")],
    pathex=[root],
    # CTranslate2 loads its DLLs (including cuDNN's loader) from its package folder.
    binaries=collect_dynamic_libs("ctranslate2"),
    # Silero VAD model used to skip non-speech.
    datas=collect_data_files("faster_whisper"),
    hiddenimports=["pynvml"] + (["audio_windows", "comtypes", "psutil"] if windows else []),
    excludes=["tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CallCaptions",
    console=False,
    icon=os.path.join(root, "packaging", "icon.ico") if windows else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CallCaptions", upx=False)
