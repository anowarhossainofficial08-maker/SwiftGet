"""Start SwiftGet:  python -m swiftget [URL ...]"""

import os
import sys


def _enable_dpi_awareness():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001 - older Windows
            pass


def _single_instance_lock():
    """Hold a lock file for the whole run so two copies never share the download list."""
    from .config import app_data_dir

    f = open(os.path.join(app_data_dir(), "instance.lock"), "a+")
    try:
        f.seek(0)
        if sys.platform.startswith("win"):
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main(argv=None):
    import tkinter as tk

    from .config import Settings
    from .engine import DownloadManager
    from .gui import AddDialog, App
    from .server import other_instance_running

    argv = sys.argv[1:] if argv is None else argv
    start_hidden = "--tray" in argv  # used by the Windows start-up entry
    argv = [a for a in argv if a != "--tray"]
    settings = Settings()
    if other_instance_running(int(settings["browser_port"]), show=not start_hidden):
        return 0  # already running (the window was brought to front)
    lock = _single_instance_lock()
    if lock is None:
        return 0  # another SwiftGet is starting up right now

    _enable_dpi_awareness()
    root = tk.Tk()
    manager = DownloadManager(settings)
    app = App(root, settings, manager, start_hidden=start_hidden)
    manager.start_loop()
    for url in argv:
        if url.lower().startswith(("http://", "https://", "ftp://")):
            root.after(500, lambda u=url: AddDialog(app, url=u))
    root.mainloop()
    # Everything is saved by now. Some tray back-ends leave non-daemon threads behind,
    # so end the process explicitly instead of waiting for them.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
