#!/usr/bin/env python3
"""Call Captions: live English captions for what people say in a voice call.

It listens to Discord (or everything you hear), translates speech to English with
Whisper on your NVIDIA GPU (or the CPU), and shows it in a translucent, click-through
overlay. Settings live in the tray icon's menu; these flags override them for one run:

    captions --source all        # everything you hear, not just Discord
    captions --lang ja           # skip auto-detection if you know the language
    captions --model medium      # smaller, faster model
    captions --file clip.mp3     # test with an audio file (needs ffmpeg)
"""

import argparse
import multiprocessing
import os
import plistlib
import signal
import sys
import time
import traceback

import gpu
import settings
from settings import APP_ID, APP_NAME, BUNDLE_ID, data_dir

SCALES = {"small": 0.82, "medium": 1.0, "large": 1.25}
OPACITIES = {"light": 0.4, "medium": 0.62, "dark": 0.82}
LANGUAGES = [
    ("Detect automatically", None), ("Arabic", "ar"), ("Chinese", "zh"), ("Dutch", "nl"),
    ("French", "fr"), ("German", "de"), ("Hindi", "hi"), ("Indonesian", "id"), ("Italian", "it"),
    ("Japanese", "ja"), ("Korean", "ko"), ("Polish", "pl"), ("Portuguese", "pt"), ("Russian", "ru"),
    ("Spanish", "es"), ("Swedish", "sv"), ("Tagalog", "tl"), ("Thai", "th"), ("Turkish", "tr"),
    ("Ukrainian", "uk"), ("Vietnamese", "vi"),
]
MODELS = [("Automatic (recommended)", "auto"), ("Best — large-v3", "large-v3"),
          ("Balanced — medium", "medium"), ("Fastest — small", "small")]
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LAUNCH_AGENT = os.path.expanduser(f"~/Library/LaunchAgents/{BUNDLE_ID}.plist")
TRAY = "menu bar" if sys.platform == "darwin" else "system tray"


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="'discord' (just the Discord app), 'all' (everything you hear), "
                                     "or on Linux part of an output device's name")
    ap.add_argument("--file", help="translate an audio file instead of live audio")
    ap.add_argument("--lang", help="spoken language code (e.g. es, ja, ru); auto-detected if omitted")
    ap.add_argument("--model", help="large-v3, medium or small (default: picked for your GPU)")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"),
                    help="cuda: use the GPU (NVIDIA, or the Apple GPU on a Mac); cpu: never use it")
    ap.add_argument("--position", choices=("bottom", "top"))
    ap.add_argument("--screen", type=int, help="monitor number (0 = first); defaults to the primary one")
    ap.add_argument("--font-size", type=int, help="caption text size in pixels")
    ap.add_argument("--opacity", type=float, help="background opacity, 0-1")
    ap.add_argument("--lines", type=int, default=3, help="max captions on screen at once")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.source == "headset":
        args.source = "all"
    return args


def log_path():
    return os.path.join(data_dir(), "captions.log")


def log_to_file_if_windowless():
    """A windowed app has no console, so keep what would be printed in a log file."""
    # Windows gives a windowed .exe no stdout at all; a Mac app's goes nowhere.
    mac_app = sys.platform == "darwin" and getattr(sys, "frozen", False) and not sys.stdout.isatty()
    if sys.stdout is not None and sys.stderr is not None and not mac_app:
        return False
    path = log_path()
    if os.path.exists(path) and os.path.getsize(path) > 5_000_000:
        os.replace(path, path + ".old")
    sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", buffering=1)
    print(f"\n=== {APP_NAME} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    return True


def autostart_enabled():
    if sys.platform == "darwin":
        return os.path.exists(LAUNCH_AGENT)
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_ID)
        return True
    except OSError:
        return False


def set_autostart(enabled):
    if sys.platform == "darwin":
        if enabled:
            # sys.executable is ".../Call Captions.app/Contents/MacOS/CallCaptions".
            app = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
            os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
            with open(LAUNCH_AGENT, "wb") as f:
                plistlib.dump({"Label": BUNDLE_ID, "ProgramArguments": ["/usr/bin/open", "-a", app],
                               "RunAtLoad": True}, f)
        elif os.path.exists(LAUNCH_AGENT):
            os.remove(LAUNCH_AGENT)
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(key, APP_ID)
            except FileNotFoundError:
                pass


def run_app(args, logging_to_file):
    from PySide6.QtCore import QLockFile, QObject, QTimer, QUrl, Signal
    from PySide6.QtGui import QActionGroup, QDesktopServices, QGuiApplication
    from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

    from appicon import app_icon, tray_icon
    from audio import FileSource, make_source
    from overlay import CaptionOverlay
    from translator import Translator

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())
    if sys.platform == "darwin":
        # A menu bar app, so no Dock icon. (The packaged app's Info.plist says so too.)
        import AppKit

        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)

    lock = QLockFile(os.path.join(data_dir(), "running.lock"))
    if not args.file and not lock.tryLock(100):
        QMessageBox.information(None, APP_NAME, f"{APP_NAME} is already running. Its icon is in the {TRAY}.")
        return 0

    prefs = settings.load()
    current = dict(prefs)  # this run: saved settings, overridden by any flags
    for key, value in (("source", args.source), ("language", args.lang),
                       ("model", args.model), ("position", args.position)):
        if value:
            current[key] = value

    screens = QGuiApplication.screens()
    screen = screens[args.screen] if args.screen is not None and args.screen < len(screens) else None
    overlay = CaptionOverlay(screen, args.lines)

    def apply_look():
        opacity = args.opacity if args.opacity is not None else OPACITIES.get(current["background"], 0.62)
        overlay.configure(current["position"], SCALES.get(current["text_size"], 1.0), opacity, args.font_size)

    apply_look()
    overlay.show()

    class Bridge(QObject):  # carries results from worker threads to the GUI thread
        partial = Signal(int, str)
        final = Signal(int, str)
        status = Signal(str, bool)
        plan = Signal(str)
        ready = Signal()
        quit = Signal()

    bridge = Bridge()
    bridge.partial.connect(overlay.show_partial)
    bridge.final.connect(overlay.show_final)
    bridge.status.connect(overlay.show_status)
    bridge.quit.connect(lambda: QTimer.singleShot(6000, app.quit))

    def on_final(uid, text, language):
        if text:
            print(f"{time.strftime('%H:%M:%S')} [{language or '??'}] {text}", flush=True)
        bridge.final.emit(uid, text)

    def on_status(text, sticky=False):
        if " · " not in text:  # progress updates would flood the log
            print(f"· {text}", flush=True)
        bridge.status.emit(text, sticky)

    translator = Translator(current["model"] or "auto", args.device, current["language"],
                            on_partial=lambda uid, text, _language: bridge.partial.emit(uid, text),
                            on_final=on_final, on_status=on_status,
                            on_plan=lambda plan: bridge.plan.emit(plan.describe()))

    state = {"source": None, "ready": False, "generation": 0}

    def start_source():
        state["ready"] = True
        state["generation"] += 1
        generation = state["generation"]
        if state["source"]:
            state["source"].stop()
            translator.flush()  # drop audio the old source already queued

        def feed(chunk):  # an old source can deliver a few more chunks before it notices
            if state["generation"] == generation:
                translator.feed(chunk)

        if args.file:
            state["source"] = FileSource(args.file, feed, on_status, on_end=bridge.quit.emit)
        else:
            state["source"] = make_source(current["source"], feed, on_status)
        state["source"].start()

    bridge.ready.connect(start_source)
    translator.start(on_ready=bridge.ready.emit)

    def change(key, value):
        if current[key] == value:
            return
        current[key] = prefs[key] = value
        settings.save(prefs)
        if key == "source":
            if state["ready"] and not args.file:
                start_source()
        elif key == "language":
            translator.set_language(value)
            overlay.show_status("Detecting the language automatically" if value is None
                                else f"Expecting {dict((c, n) for n, c in LANGUAGES).get(value, value)}")
        elif key == "model":
            translator.set_model(value)
            overlay.show_status("Switching model…", True)
        else:
            apply_look()

    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(tray_icon(), app)
        tray.setToolTip(APP_NAME)
        menu = QMenu()
        submenus = []  # keep the Python wrappers alive

        def add_choice(title, key, options):
            sub = menu.addMenu(title)
            group = QActionGroup(sub)
            for label, value in options:
                action = sub.addAction(label)
                action.setCheckable(True)
                action.setChecked(current[key] == value)
                group.addAction(action)
                action.triggered.connect(lambda _checked=False, v=value: change(key, v))
            submenus.append((sub, group))
            return sub

        info = menu.addAction("Starting…")
        info.setEnabled(False)

        def show_plan(text):
            info.setText(f"Running on {text}")
            tray.setToolTip(f"{APP_NAME} — {text}")

        bridge.plan.connect(show_plan)
        menu.addSeparator()
        pause = menu.addAction("Pause captions")
        pause.setCheckable(True)

        def toggle_pause(paused):
            translator.paused = paused
            overlay.show_status("Captions paused" if paused else "Captions resumed")

        pause.toggled.connect(toggle_pause)
        add_choice("Listen to", "source", [("Discord only", "discord"), ("Everything I hear", "all")]) \
            .setEnabled(not args.file)
        languages = LANGUAGES + ([(current["language"], current["language"])]
                                 if current["language"] not in dict((c, n) for n, c in LANGUAGES) else [])
        add_choice("Spoken language", "language", languages)
        add_choice("Quality", "model", MODELS)
        menu.addSeparator()
        add_choice("Position", "position", [("Bottom", "bottom"), ("Top", "top")])
        add_choice("Text size", "text_size", [("Small", "small"), ("Medium", "medium"), ("Large", "large")])
        add_choice("Background", "background", [("Light", "light"), ("Medium", "medium"), ("Dark", "dark")])
        menu.addAction("Clear captions", overlay.clear)
        menu.addSeparator()
        if sys.platform in ("win32", "darwin") and getattr(sys, "frozen", False):
            autostart = menu.addAction("Open at login" if sys.platform == "darwin" else "Start with Windows")
            autostart.setCheckable(True)
            autostart.setChecked(autostart_enabled())
            autostart.toggled.connect(set_autostart)
        if logging_to_file:
            menu.addAction("Open log", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(log_path())))
        menu.addAction("Quit", app.quit)
        tray.setContextMenu(menu)
        tray.show()
        if not prefs["welcomed"]:
            click = "Click" if sys.platform == "darwin" else "Right-click"
            tray.showMessage(APP_NAME, "Captions appear near the bottom of your screen. "
                                       f"{click} this icon for settings.", app_icon(), 8000)
            prefs["welcomed"] = True
            settings.save(prefs)

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    wake = QTimer(interval=200, timeout=lambda: None)  # lets Python see Ctrl+C while Qt runs
    wake.start()

    code = app.exec()
    if state["source"]:
        state["source"].stop()
    return code


def selftest():
    """Checks a build works: imports, overlay drawing, a tiny model on the CPU, and the
    platform's audio capture plumbing (and, on Macs, the Apple GPU engine). The builds run
    it before packaging."""
    import numpy as np

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    failures = []

    def check(name, fn):
        try:
            result = fn()
            print(f"PASS {name}" + (f": {result}" if result else ""), flush=True)
        except Exception:
            failures.append(name)
            print(f"FAIL {name}", flush=True)
            traceback.print_exc()

    def imports():
        import ctranslate2
        import faster_whisper
        import PySide6.QtWidgets  # noqa: F401

        return f"ctranslate2 {ctranslate2.__version__}, faster-whisper {faster_whisper.__version__}"

    def resampler():
        from audio import Resampler

        tone = np.sin(2 * np.pi * 1000 * np.arange(48000) / 48000).astype(np.float32)
        out = Resampler(48000).process(tone)
        peak = int(np.argmax(np.abs(np.fft.rfft(out[:16000]))))
        if abs(len(out) - 16000) > 2 or peak != 1000:
            raise RuntimeError(f"bad resample: {len(out)} samples, peak at {peak} Hz")

    def overlay():
        from PySide6.QtWidgets import QApplication

        from overlay import CaptionOverlay

        app = QApplication.instance() or QApplication([])
        view = CaptionOverlay()
        view.show()
        view.show_status("Self-test")
        view.show_final(1, "Hello there")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            app.processEvents()
        image = view.grab().toImage()
        if not any(image.pixelColor(x, y).alpha() for x in range(0, image.width(), 6)
                   for y in range(0, image.height(), 6)):
            raise RuntimeError("the overlay drew nothing")

    def whisper():
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model

        path = download_model("tiny", cache_dir=os.path.join(data_dir(), "models"))
        model = WhisperModel(path, device="cpu", compute_type="int8")
        noise = (np.random.default_rng(0).standard_normal(32000) * 0.01).astype(np.float32)
        segments, info = model.transcribe(noise, task="translate", beam_size=1)
        list(segments)
        from faster_whisper.vad import get_speech_timestamps

        get_speech_timestamps(noise)  # the bundled voice-detection model loads and runs
        return f"tiny model and voice detection ran on the CPU (heard {info.language})"

    def windows_audio():
        import audio_windows

        return audio_windows.selftest()

    def mac_audio():
        import audio_mac

        return audio_mac.selftest()

    def apple_gpu():
        import whisper_mlx
        from huggingface_hub import snapshot_download

        for name in whisper_mlx.SUBMODULES:  # bundled, and importable without numba/scipy
            whisper_mlx._submodule(name)
        if not whisper_mlx.available():
            return "MLX imports fine; no Metal GPU on this machine"
        path = snapshot_download(whisper_mlx.MODEL_REPOS["tiny"], cache_dir=os.path.join(data_dir(), "models"),
                                 allow_patterns=["config.json", "weights.*"])
        noise = (np.random.default_rng(0).standard_normal(32000) * 0.01).astype(np.float32)
        _, info = whisper_mlx.MlxWhisperModel(path).transcribe(noise, task="translate")
        return f"tiny model ran on the Apple GPU (heard {info.language})"

    check("imports", imports)
    check("resampler", resampler)
    check("gpu detection", lambda: str(gpu.make_plan()))
    check("overlay", overlay)
    check("whisper", whisper)
    if sys.platform == "win32":
        check("windows audio", windows_audio)
    if sys.platform == "darwin":
        check("apple gpu", apple_gpu)
        check("mac audio", mac_audio)
    print("SELFTEST " + (f"FAILED: {', '.join(failures)}" if failures else "OK"), flush=True)
    return 1 if failures else 0


def main():
    args = parse_args()
    logging_to_file = log_to_file_if_windowless()
    if args.selftest:
        sys.exit(selftest())
    if sys.platform.startswith("linux"):
        if args.device != "cpu":
            gpu.reexec_with_linux_cuda_libs()
        # Under Wayland, KWin won't let a regular window stay on top or choose its own
        # position, so run the overlay through XWayland where both work.
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    sys.exit(run_app(args, logging_to_file))


if __name__ == "__main__":
    # In the bundled app, libraries that use multiprocessing (tqdm, during model downloads)
    # re-launch this executable for their helper process; this makes it act as that helper.
    multiprocessing.freeze_support()
    main()
