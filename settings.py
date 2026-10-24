"""User settings, kept as JSON in the app's data folder."""

import json
import os
import sys

APP_NAME = "Call Captions"
APP_ID = "CallCaptions"

DEFAULTS = {
    "source": "discord",     # "discord" or "all" (everything you hear)
    "language": None,        # spoken language code; None = detect automatically
    "model": "auto",         # "auto", "large-v3", "medium" or "small"
    "position": "bottom",    # "bottom" or "top"
    "text_size": "medium",   # "small", "medium" or "large"
    "background": "medium",  # "light", "medium" or "dark"
    "welcomed": False,       # shown the "I'm in the tray" hint once
}


def data_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        path = os.path.join(base, APP_ID)
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        path = os.path.join(base, "call-captions")
    os.makedirs(path, exist_ok=True)
    return path


def load():
    try:
        with open(os.path.join(data_dir(), "settings.json"), encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    return {key: saved.get(key, default) for key, default in DEFAULTS.items()}


def save(values):
    path = os.path.join(data_dir(), "settings.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(values, f, indent=2)
    os.replace(path + ".tmp", path)
