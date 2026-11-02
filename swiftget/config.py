"""Settings storage (JSON in the user's app-data folder)."""

import json
import os
import sys
import threading

from . import APP_NAME


def app_data_dir():
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def default_download_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads", APP_NAME)


DEFAULTS = {
    "download_dir": default_download_dir(),
    "use_category_folders": True,
    "max_connections": 8,
    "max_simultaneous": 3,
    "speed_limit_kb": 0,            # 0 = unlimited
    "max_retries": 10,
    "timeout": 30,
    "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "proxy_mode": "system",         # system | none | manual
    "proxy": "",                    # e.g. http://127.0.0.1:8080
    "verify_ssl": True,
    "show_complete_dialog": True,
    "show_progress_window": True,
    "theme": "system",              # system | light | dark
    "start_with_windows": True,     # like IDM: runs in the tray from Windows start-up
    "close_to_tray": True,          # the X button hides the window instead of quitting
    "tray_notice_shown": False,
    "autostart_initialized": False,
    "welcome_shown": False,
    "clipboard_monitor": True,
    "browser_integration": True,
    "browser_port": 9614,
    "browser_skip_dialog": False,
    "video_button": True,           # "Download this video" button on web pages
    "capture_extensions": ("zip rar 7z gz tar iso exe msi apk dmg pdf mp3 mp4 mkv avi mov "
                           "wmv flv webm m4a flac wav doc docx xls xlsx ppt pptx epub"),
    "capture_min_size_kb": 0,
    "scheduler_enabled": False,
    "scheduler_start": "02:00",
    "scheduler_stop": "",
    "scheduler_shutdown": False,
}


class Settings:
    def __init__(self, path=None):
        self.path = path or os.path.join(app_data_dir(), "settings.json")
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                self.data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value

    def get(self, key, default=None):
        return self.data.get(key, default)

    def capture_extensions(self):
        return [e.strip(". ").lower() for e in str(self["capture_extensions"]).replace(",", " ").split() if e.strip()]
