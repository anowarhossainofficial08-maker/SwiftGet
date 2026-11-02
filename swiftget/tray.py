"""System-tray icon (notification area), like other download managers.

Uses `pystray` when installed. Menu clicks happen on pystray's own thread, so
they are only posted to the manager's event queue; the Tk thread handles them.
"""

import os

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # noqa: BLE001 - optional dependency (or no tray backend)
    pystray = None


def _draw_icon(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((1, 1, size - 2, size - 2), fill=(0, 103, 192, 255))
    s = size / 64
    white = (255, 255, 255, 255)
    d.rectangle((27 * s, 12 * s, 37 * s, 35 * s), fill=white)
    d.polygon([(15 * s, 31 * s), (49 * s, 31 * s), (32 * s, 49 * s)], fill=white)
    d.rectangle((18 * s, 51 * s, 46 * s, 55 * s), fill=white)
    return img


class Tray:
    def __init__(self, events, icon_path=None):
        self.events = events
        self.icon = None
        self.icon_path = icon_path
        self._title = ""

    @staticmethod
    def available():
        return pystray is not None

    def _post(self, kind):
        return lambda icon=None, item=None: self.events.put(("tray", kind))

    def start(self):
        if pystray is None or self.icon is not None:
            return False
        image = None
        if self.icon_path and os.path.exists(self.icon_path):
            try:
                image = Image.open(self.icon_path)
                image.load()
            except Exception:  # noqa: BLE001
                image = None
        image = image or _draw_icon()
        menu = pystray.Menu(
            pystray.MenuItem("Open SwiftGet", self._post("show"), default=True),
            pystray.MenuItem("New download…", self._post("new")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Resume all", self._post("resume_all")),
            pystray.MenuItem("Pause all", self._post("pause_all")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._post("exit")),
        )
        try:
            self.icon = pystray.Icon("SwiftGet", image, "SwiftGet", menu)
            self.icon.run_detached()
            return True
        except Exception:  # noqa: BLE001 - e.g. no tray on this desktop
            self.icon = None
            return False

    @property
    def running(self):
        return self.icon is not None

    def set_title(self, text):
        text = text[:120]
        if self.icon is not None and text != self._title:
            self._title = text
            try:
                self.icon.title = text
            except Exception:  # noqa: BLE001
                pass

    def notify(self, title, message):
        if self.icon is None:
            return False
        try:
            self.icon.notify(message, title)
            return True
        except Exception:  # noqa: BLE001
            return False

    def stop(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass
            self.icon = None
