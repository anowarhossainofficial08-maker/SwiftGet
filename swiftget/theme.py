"""Colours, fonts and the light / dark theme.

Uses the Sun Valley ttk theme (Windows 11 look, package `sv-ttk`) when it is
installed, otherwise falls back to a flat theme built on ttk's 'clam'.
"""

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

try:
    import sv_ttk
except ImportError:  # still works without it, just less pretty
    sv_ttk = None

PALETTES = {
    "light": {
        "bg": "#fafafa", "surface": "#ffffff", "sidebar": "#f0f0f0", "border": "#e5e5e5",
        "text": "#1b1b1b", "muted": "#6e6e6e", "hover": "#f4f7fc", "select": "#e1ecfb",
        "accent": "#0067c0", "accent_text": "#ffffff", "track": "#e3e3e3",
        "success": "#0f7b0f", "error": "#c42b1c", "warning": "#9d5d00", "paused": "#8a8a8a",
        "field": "#ffffff",
    },
    "dark": {
        "bg": "#1c1c1c", "surface": "#262626", "sidebar": "#202020", "border": "#333333",
        "text": "#f3f3f3", "muted": "#a3a3a3", "hover": "#2d2d2d", "select": "#1f3a56",
        "accent": "#4cc2ff", "accent_text": "#062033", "track": "#3b3b3b",
        "success": "#6ccb5f", "error": "#ff99a4", "warning": "#fce100", "paused": "#9a9a9a",
        "field": "#2b2b2b",
    },
}

CATEGORY_COLORS = {
    "Video": "#e5484d", "Music": "#8e4ec6", "Programs": "#0091ff", "Compressed": "#f76b15",
    "Documents": "#30a46c", "Images": "#d6409f", "Other": "#7c7c7c",
}


def system_prefers_dark():
    if sys.platform.startswith("win"):
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
        except OSError:
            return False
    return False


def set_titlebar_dark(win, dark):
    """Dark window title bar on Windows 10/11 (no effect elsewhere)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
    except Exception:  # noqa: BLE001
        pass


class Theme:
    def __init__(self, root, mode="system"):
        self.root = root
        self.listeners = []
        # Hand-drawn parts are laid out for 96 DPI and scaled by this (fonts scale by themselves).
        try:
            self.scale = max(1.0, round(root.winfo_fpixels("1i") / 96.0, 2))
        except tk.TclError:
            self.scale = 1.0
        self._make_fonts()
        self.apply(mode)

    # ------------------------------------------------------------------ fonts
    def _make_fonts(self):
        families = set(tkfont.families(self.root))
        for fam in ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter",
                    "Ubuntu", "Cantarell", "DejaVu Sans"):
            if fam in families:
                family = fam
                break
        else:
            family = tkfont.nametofont("TkDefaultFont").actual("family")
        display = "Segoe UI Variable Display" if "Segoe UI Variable Display" in families else family
        self.family = family
        self.font = tkfont.Font(root=self.root, family=family, size=10)
        self.font_small = tkfont.Font(root=self.root, family=family, size=9)
        self.font_bold = tkfont.Font(root=self.root, family=family, size=10, weight="bold")
        self.font_title = tkfont.Font(root=self.root, family=display, size=11, weight="bold")
        self.font_h1 = tkfont.Font(root=self.root, family=display, size=17, weight="bold")
        self.font_big = tkfont.Font(root=self.root, family=display, size=24, weight="bold")
        self.font_badge = tkfont.Font(root=self.root, family=family, size=8, weight="bold")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=family, size=10)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ theme
    def resolve(self, mode):
        if mode in ("light", "dark"):
            return mode
        return "dark" if system_prefers_dark() else "light"

    @property
    def dark(self):
        return self.name == "dark"

    def apply(self, mode):
        self.mode = mode
        self.name = self.resolve(mode)
        self.c = PALETTES[self.name]
        c = self.c
        style = ttk.Style(self.root)
        if sv_ttk is not None:
            sv_ttk.set_theme(self.name, self.root)
        else:
            style.theme_use("clam")
            style.configure(".", background=c["bg"], foreground=c["text"], fieldbackground=c["field"],
                            bordercolor=c["border"], lightcolor=c["surface"], darkcolor=c["border"],
                            troughcolor=c["track"], focuscolor=c["accent"], selectbackground=c["accent"],
                            selectforeground=c["accent_text"], insertcolor=c["text"])
            style.configure("TButton", padding=(10, 4), background=c["surface"], relief="flat")
            style.map("TButton", background=[("active", c["hover"]), ("pressed", c["select"])])
            style.configure("Accent.TButton", background=c["accent"], foreground=c["accent_text"])
            style.map("Accent.TButton", background=[("active", c["accent"]), ("pressed", c["accent"])])
            style.configure("TEntry", padding=6)
            style.configure("Treeview", background=c["surface"], fieldbackground=c["surface"],
                            foreground=c["text"], bordercolor=c["border"])
            style.configure("Treeview.Heading", background=c["bg"], foreground=c["muted"], relief="flat")
            style.configure("TNotebook", background=c["bg"])
            style.configure("TNotebook.Tab", padding=(12, 6))
            style.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor=c["track"],
                            bordercolor=c["track"], lightcolor=c["accent"], darkcolor=c["accent"])
            self.root.option_add("*Menu.background", c["surface"])
            self.root.option_add("*Menu.foreground", c["text"])
        style.configure("Treeview", rowheight=self.font.metrics("linespace") + 12)
        style.configure("Muted.TLabel", foreground=c["muted"])
        style.configure("Title.TLabel", font=self.font_title)
        style.configure("H1.TLabel", font=self.font_h1)
        style.configure("Big.TLabel", font=self.font_big, foreground=c["accent"])
        style.configure("Card.TFrame", background=c["surface"])
        style.configure("Card.TLabel", background=c["surface"])
        style.configure("CardMuted.TLabel", background=c["surface"], foreground=c["muted"])
        self.root.configure(bg=c["bg"])
        for win in [self.root] + self._toplevels(self.root):
            set_titlebar_dark(win, self.dark)
        for cb in list(self.listeners):
            try:
                cb()
            except tk.TclError:
                self.listeners.remove(cb)

    def _toplevels(self, widget):
        out = []
        for child in widget.winfo_children():
            if isinstance(child, tk.Toplevel):
                out.append(child)
            out.extend(self._toplevels(child))
        return out

    def style_text(self, text_widget):
        c = self.c
        text_widget.configure(bg=c["field"], fg=c["text"], insertbackground=c["text"],
                              selectbackground=c["accent"], selectforeground=c["accent_text"],
                              relief="flat", highlightthickness=1, highlightbackground=c["border"],
                              highlightcolor=c["accent"], padx=8, pady=6, font=self.font)

    def style_menu(self, menu):
        c = self.c
        menu.configure(bg=c["surface"], fg=c["text"], activebackground=c["select"],
                       activeforeground=c["text"], bd=0, relief="flat", font=self.font)
