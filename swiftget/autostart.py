"""Start SwiftGet automatically when Windows starts (HKCU\\...\\Run)."""

import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "SwiftGet"


def supported():
    return sys.platform.startswith("win")


def installed():
    """True when running from a copy installed by SwiftGet-Setup.exe (it leaves an uninstaller)."""
    if not getattr(sys, "frozen", False):
        return False
    folder = os.path.dirname(sys.executable)
    return any(name.lower().startswith("unins") and name.lower().endswith(".exe") for name in os.listdir(folder))


def launch_command():
    """Command line that starts SwiftGet hidden in the tray."""
    if getattr(sys, "frozen", False):  # the PyInstaller .exe
        return f'"{sys.executable}" --tray'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    run_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py")
    return f'"{exe}" "{run_py}" --tray'


def is_enabled():
    if not supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def set_enabled(enabled):
    """Add or remove the Run entry. Also refreshes the path if the .exe was moved."""
    if not supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
