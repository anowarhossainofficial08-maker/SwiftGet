"""Tkinter user interface (modern, light / dark)."""

import datetime
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, VERSION, autostart, engine, video
from .engine import ACTIVE_STATES, COMPLETED, DOWNLOADING, ERROR, QUEUED, WAITING
from .server import IntegrationServer
from .theme import CATEGORY_COLORS, Theme, set_titlebar_dark
from .tray import Tray
from .utils import (CATEGORY_NAMES, category_for, expand_batch_pattern, extension_of, human_size,
                    human_speed, human_time, is_url, open_path, reveal_in_folder, sanitize_filename)
from .widgets import DownloadList, Sidebar, rounded_rect

ICON_BLUE = "#0067c0"


def make_icon(size=32, color=ICON_BLUE):
    """Round icon with a down arrow, drawn in code (no image files needed)."""
    img = tk.PhotoImage(width=size, height=size)
    c = (size - 1) / 2
    r = size / 2 - 0.5
    rows, outside = [], []
    for y in range(size):
        row = []
        for x in range(size):
            dx, dy = x - c, y - c
            nx, ny = (x + 0.5) / size, (y + 0.5) / size
            shaft = 0.42 <= nx <= 0.58 and 0.18 <= ny <= 0.55
            head = 0.48 <= ny <= 0.76 and abs(nx - 0.5) <= (0.76 - ny) * 1.1
            bar = 0.79 <= ny <= 0.85 and 0.28 <= nx <= 0.72
            if dx * dx + dy * dy > r * r:
                outside.append((x, y))
                row.append("#ffffff")
            else:
                row.append("#ffffff" if (shaft or head or bar) else color)
        rows.append("{" + " ".join(row) + "}")
    img.put(" ".join(rows))
    for x, y in outside:
        img.transparency_set(x, y, True)
    return img


def center_on(win, parent):
    win.update_idletasks()
    try:
        if not parent.winfo_viewable():  # main window hidden in the tray: centre on screen
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            win.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
            return
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{px + max(0, (pw - w) // 2)}+{py + max(0, (ph - h) // 3)}")
    except tk.TclError:
        pass


def bring_to_front(win):
    try:
        win.deiconify()
        win.lift()
        win.attributes("-topmost", True)
        win.after(300, lambda: win.attributes("-topmost", False))
        win.focus_force()
    except tk.TclError:
        pass


def resource_path(*parts):
    """Path of a bundled file, both from source and inside the PyInstaller .exe."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def firefox_addon_dir():
    exe_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    for c in (os.path.join(exe_dir, "firefox"), os.path.join(os.path.dirname(exe_dir), "dist")):
        if os.path.exists(os.path.join(c, "SwiftGet-firefox.xpi")):
            return c
    return os.path.join(exe_dir, "firefox")


def extension_dir():
    candidates = []
    exe_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    candidates.append(os.path.join(exe_dir, "browser-extension"))
    candidates.append(os.path.join(os.path.dirname(exe_dir), "browser-extension"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[-1]


FILTERS = [
    ("All", "All downloads", None),
    ("Active", "Downloading", "#0091ff"),
    ("Finished", "Completed", "#30a46c"),
    ("Unfinished", "Unfinished", "#f5a524"),
    ("Queue", "Queue", "#8e4ec6"),
    ("-", "Categories", None),
] + [(f"cat:{name}", name, CATEGORY_COLORS[name]) for name in CATEGORY_NAMES]

FILTER_TITLES = {key: label for key, label, _ in FILTERS if key != "-"}


class App:
    def __init__(self, root, settings, manager, start_hidden=False):
        self.root = root
        self.settings = settings
        self.manager = manager
        self.filter = "All"
        self.progress_windows = {}
        self._clip_last = None
        self.server = None
        self._message = ""
        self._message_until = 0

        root.title(f"{APP_NAME}")
        self.icon = make_icon(32)
        self.logo = make_icon(28)
        root.iconphoto(True, self.icon)
        ico = resource_path("assets", "swiftget.ico")
        if sys.platform.startswith("win") and os.path.exists(ico):
            try:
                root.iconbitmap(default=ico)
            except tk.TclError:
                pass
        self.theme = Theme(root, settings["theme"])
        k = self.theme.scale
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(int(1180 * k), int(sw * 0.9))}x{min(int(660 * k), int(sh * 0.85))}")
        root.minsize(min(int(860 * k), sw), min(int(460 * k), sh))
        self._build()
        self.theme.listeners.append(self._on_theme_changed)
        self._on_theme_changed()
        self._bind_keys()
        self.start_server()

        # tray icon + start with Windows (like IDM)
        self.tray = Tray(manager.events, resource_path("assets", "swiftget.ico"))
        if settings["close_to_tray"] or start_hidden:
            self.tray.start()
        if autostart.supported():
            if not settings["autostart_initialized"]:
                # First run: follow the installer's "start with Windows" choice; the portable
                # .exe (no installer) defaults to on.
                settings["start_with_windows"] = autostart.is_enabled() or not autostart.installed()
                settings["autostart_initialized"] = True
                settings.save()
            autostart.set_enabled(bool(settings["start_with_windows"]))
        if start_hidden and self.tray.running:
            root.withdraw()
        elif not settings["welcome_shown"]:
            root.after(700, lambda: WelcomeDialog(self))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._refresh)
        root.after(200, self._process_events)
        root.after(1500, self._poll_clipboard)

    # ------------------------------------------------------------------ layout
    def _build(self):
        self.sidebar = Sidebar(self.root, self, FILTERS)
        self.sidebar.pack(side="left", fill="y")

        main = ttk.Frame(self.root, padding=(20, 16, 16, 0))
        main.pack(side="left", fill="both", expand=True)
        self.main = main

        # header: title + primary actions
        header = ttk.Frame(main)
        header.pack(fill="x")
        title_box = ttk.Frame(header)
        title_box.pack(side="left")
        self.title_lbl = ttk.Label(title_box, text="All downloads", style="H1.TLabel")
        self.title_lbl.pack(anchor="w")
        self.subtitle_lbl = ttk.Label(title_box, text="", style="Muted.TLabel")
        self.subtitle_lbl.pack(anchor="w")

        ttk.Button(header, text="＋  New download", style="Accent.TButton", command=self.add_url).pack(
            side="right", padx=(6, 0))
        self.more_btn = ttk.Button(header, text="•••", width=4, command=self._show_more_menu)
        self.more_btn.pack(side="right", padx=(6, 0))
        self.theme_btn = ttk.Button(header, width=4, command=self.toggle_theme)
        self.theme_btn.pack(side="right", padx=(6, 0))
        ttk.Button(header, text="⚙  Settings", command=self.open_options).pack(side="right", padx=(6, 0))

        # toolbar: search + item actions
        bar = ttk.Frame(main)
        bar.pack(fill="x", pady=(14, 10))
        for text, cmd in (("◷  Scheduler", self.open_scheduler), ("■  Stop queue", self.manager.stop_queue),
                          ("▶▶  Start queue", self.start_queue)):
            ttk.Button(bar, text=text, command=cmd).pack(side="right", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="right", fill="y", padx=8, pady=4)
        for text, cmd in (("✕  Delete", self.delete_selected), ("❚❚  Pause", self.pause_selected),
                          ("▶  Resume", self.resume_selected)):
            ttk.Button(bar, text=text, command=cmd).pack(side="right", padx=(6, 0))
        # search box takes whatever width is left (packed last so buttons never get cut off)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(bar, textvariable=self.search_var, width=12)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._placeholder = "Search downloads"
        self._set_placeholder()
        self.search_entry.bind("<FocusIn>", self._clear_placeholder)
        self.search_entry.bind("<FocusOut>", lambda e: self._set_placeholder())
        self.search_var.trace_add("write", lambda *a: self._refresh(reschedule=False))

        # list
        list_frame = ttk.Frame(main)
        list_frame.pack(fill="both", expand=True)
        self.list = DownloadList(list_frame, self)
        self.list.vsb.pack(side="right", fill="y")
        self.list.pack(side="left", fill="both", expand=True)

        # status bar
        self.status = tk.Frame(main, height=34)
        self.status.pack(fill="x")
        self.status_dot = tk.Canvas(self.status, width=10, height=10, highlightthickness=0, bd=0)
        self.status_left = tk.Label(self.status, anchor="w")
        self.status_right = tk.Label(self.status, anchor="e")
        self.status_left.pack(side="left", pady=8)
        self.status_right.pack(side="right", pady=8)
        self.status_dot.pack(side="right", padx=(0, 6))

        # menus
        self.ctx = tk.Menu(self.root, tearoff=False)
        for entry in (("Open", self.open_selected), ("Show in folder", self.open_folder_selected), None,
                      ("Resume", self.resume_selected), ("Pause", self.pause_selected),
                      ("Redownload", self.redownload_selected), ("Show progress", self.progress_selected),
                      None, ("Copy address", self.copy_url_selected), ("Properties", self.properties_selected),
                      None, ("Delete…", self.delete_selected)):
            if entry is None:
                self.ctx.add_separator()
            else:
                self.ctx.add_command(label=entry[0], command=entry[1])

        self.more = tk.Menu(self.root, tearoff=False)
        self.more.add_command(label="Batch download…", accelerator="Ctrl+B", command=self.add_batch)
        self.more.add_separator()
        self.more.add_command(label="Resume all", command=self.manager.resume_all)
        self.more.add_command(label="Pause all", command=self.manager.pause_all)
        self.more.add_command(label="Clear completed", command=self.manager.remove_completed)
        self.more.add_separator()
        self.limit_var = tk.IntVar(value=int(self.settings["speed_limit_kb"] or 0))
        self.limiter_menu = tk.Menu(self.more, tearoff=False)
        for label, kb in (("Unlimited", 0), ("128 KB/s", 128), ("256 KB/s", 256), ("512 KB/s", 512),
                          ("1 MB/s", 1024), ("2 MB/s", 2048), ("5 MB/s", 5120), ("10 MB/s", 10240)):
            self.limiter_menu.add_radiobutton(label=label, value=kb, variable=self.limit_var,
                                              command=lambda kb=kb: self.set_speed_limit(kb))
        self.more.add_cascade(label="Speed limit", menu=self.limiter_menu)
        self.more.add_separator()
        self.more.add_command(label="Browser extension…", command=self.browser_help)
        self.more.add_command(label="About SwiftGet", command=self.about)
        self.more.add_separator()
        self.more.add_command(label="Exit", command=self.quit_app)

    def _on_theme_changed(self):
        c = self.theme.c
        for w in (self.status, self.status_left, self.status_right, self.status_dot):
            w.configure(bg=c["bg"])
        self.status_left.configure(fg=c["muted"], font=self.theme.font_small)
        self.status_right.configure(fg=c["muted"], font=self.theme.font_small)
        for m in (self.ctx, self.more, self.limiter_menu):
            self.theme.style_menu(m)
        self.theme_btn.configure(text="☀" if self.theme.dark else "☾")
        self.sidebar.redraw()
        self.list.redraw()
        for win in list(self.progress_windows.values()):
            win.restyle()

    def toggle_theme(self):
        mode = "light" if self.theme.dark else "dark"
        self.settings["theme"] = mode
        self.settings.save()
        self.theme.apply(mode)

    def _show_more_menu(self):
        b = self.more_btn
        self.more.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height() + 2)

    def show_context_menu(self, x, y):
        self.ctx.tk_popup(x, y)

    def _set_placeholder(self):
        if not self.search_var.get():
            self.search_entry.configure(foreground=self.theme.c["muted"])
            self._placeholder_on = True
            self.search_var.set(self._placeholder)

    def _clear_placeholder(self, _e=None):
        if getattr(self, "_placeholder_on", False):
            self._placeholder_on = False
            self.search_var.set("")
            self.search_entry.configure(foreground=self.theme.c["text"])

    def search_text(self):
        if getattr(self, "_placeholder_on", False):
            return ""
        return self.search_var.get().strip().lower()

    def _bind_keys(self):
        r = self.root
        r.bind("<Control-n>", lambda e: self.add_url())
        r.bind("<Control-b>", lambda e: self.add_batch())
        r.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self.list.bind("<Control-a>", lambda e: self.list.select_all())
        self.list.bind("<Delete>", lambda e: self.delete_selected())
        r.bind("<Control-v>", self._paste_url)

    # ----------------------------------------------------------------- refresh
    def _matches(self, d):
        f = self.filter
        q = self.search_text()
        if q and q not in d.filename.lower() and q not in d.url.lower():
            return False
        if f == "All":
            return True
        if f.startswith("cat:"):
            return d.category == f[4:]
        if f == "Active":
            return d.status in ACTIVE_STATES
        if f == "Unfinished":
            return d.status != COMPLETED
        if f == "Finished":
            return d.status == COMPLETED
        if f == "Queue":
            return d.status in (QUEUED, WAITING) or (d.from_queue and d.is_active)
        return True

    def on_filter(self, key):
        self.filter = key
        self.list.offset = 0
        self._refresh(reschedule=False)

    def _counts(self, items):
        counts = {"All": len(items)}
        for d in items:
            if d.status in ACTIVE_STATES:
                counts["Active"] = counts.get("Active", 0) + 1
            if d.status == COMPLETED:
                counts["Finished"] = counts.get("Finished", 0) + 1
            else:
                counts["Unfinished"] = counts.get("Unfinished", 0) + 1
            if d.status in (QUEUED, WAITING):
                counts["Queue"] = counts.get("Queue", 0) + 1
            key = f"cat:{d.category}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _refresh(self, reschedule=True):
        try:
            all_items = self.manager.snapshot()
            items = [d for d in all_items if self._matches(d)]
            items.sort(key=lambda d: d.added, reverse=True)  # newest first
            if self.root.winfo_viewable():
                self.list.set_items(items)
                self.sidebar.set_counts(self._counts(all_items))

            self.title_lbl.configure(text=FILTER_TITLES.get(self.filter, "Downloads"))
            n = len(items)
            self.subtitle_lbl.configure(text=f"{n} item{'s' if n != 1 else ''}")

            active = [d for d in all_items if d.status in ACTIVE_STATES]
            total_speed = sum(d.speed for d in active)
            parts = []
            if time.time() < self._message_until and self._message:
                parts.append(self._message)
            parts.append(f"↓ {human_speed(total_speed) or '0 B/s'}")
            parts.append(f"{len(active)} active")
            queued = sum(1 for d in all_items if d.status == QUEUED)
            if queued:
                parts.append(f"{queued} queued")
            if self.settings["speed_limit_kb"]:
                parts.append(f"limit {human_speed(int(self.settings['speed_limit_kb']) * 1024)}")
            self.status_left.configure(text="     ".join(parts))

            c = self.theme.c
            if not self.settings["browser_integration"]:
                text, dot = "Browser integration off", c["paused"]
            elif self.server and self.server.httpd:
                text, dot = f"Browser integration on  ·  port {self.server.port}", c["success"]
            else:
                text, dot = f"Port {self.server.port if self.server else ''} is busy", c["error"]
            self.status_right.configure(text=text)
            self.status_dot.delete("all")
            self.status_dot.create_oval(1, 1, 9, 9, fill=dot, outline="")

            pct = [d.progress for d in active if d.size > 0]
            self.root.title(f"{sum(pct) / len(pct):.0f}% · {APP_NAME}" if pct else APP_NAME)
            tip = f"{APP_NAME} – {len(active)} downloading, {human_speed(total_speed)}" if active else APP_NAME
            self.tray.set_title(tip)
        finally:
            if reschedule:
                self.root.after(500, self._refresh)

    def flash(self, message, seconds=6):
        self._message = message
        self._message_until = time.time() + seconds

    # ------------------------------------------------------------------ events
    def _process_events(self):
        try:
            while True:
                kind, payload = self.manager.events.get_nowait()
                if kind == "completed":
                    d = self.manager.get(payload)
                    if d:
                        win = self.progress_windows.pop(d.id, None)
                        if win:
                            win.destroy()
                        self.flash(f"Completed: {d.filename}")
                        if not self.root.winfo_viewable() and self.tray.running:
                            self.tray.notify("Download complete", d.filename)
                        elif self.settings["show_complete_dialog"]:
                            CompleteDialog(self, d)
                elif kind == "error":
                    d = self.manager.get(payload)
                    if d:
                        self.flash(f"Failed: {d.filename}")
                        if not self.root.winfo_viewable():
                            self.tray.notify("Download failed", f"{d.filename}\n{d.error}")
                elif kind == "browser_add":
                    self.handle_browser_add(payload)
                elif kind == "video_add":
                    d = self.manager.add_video(payload["url"], quality=payload.get("quality"),
                                               title=payload.get("title"), size=payload.get("size", -1))
                    self.flash(f"Video added: {d.filename}")
                    if self.settings["show_progress_window"]:
                        self.show_progress(d)
                elif kind == "show":
                    self.show_window()
                elif kind == "tray":
                    self._tray_action(payload)
                elif kind == "shutdown":
                    self._shutdown_computer()
                elif kind == "info":
                    self.flash(str(payload))
        except queue.Empty:
            pass
        self.root.after(250, self._process_events)

    def _poll_clipboard(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        text = (text or "").strip()
        if self._clip_last is None:
            self._clip_last = text  # ignore whatever was on the clipboard at start-up
        elif text != self._clip_last:
            self._clip_last = text
            if (self.settings["clipboard_monitor"] and is_url(text)
                    and extension_of(text.split("?")[0]) in self.settings.capture_extensions()):
                AddDialog(self, url=text)
        self.root.after(1000, self._poll_clipboard)

    def _paste_url(self, _event=None):
        if self.root.focus_get() in (self.search_entry,):
            return None
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return None
        if is_url(text):
            AddDialog(self, url=text)
            return "break"
        return None

    def handle_browser_add(self, data):
        url = str(data.get("url", ""))
        headers = {}
        if data.get("referrer"):
            headers["Referer"] = str(data["referrer"])
        if data.get("cookies"):
            headers["Cookie"] = str(data["cookies"])
        if data.get("userAgent"):
            headers["User-Agent"] = str(data["userAgent"])
        name = data.get("filename")
        name = sanitize_filename(os.path.basename(str(name).replace("\\", "/"))) if name else None
        if self.settings["browser_skip_dialog"]:
            d = self.manager.add(url, filename=name, headers=headers, start=True)
            self.flash(f"Added from browser: {d.filename}")
            if self.settings["show_progress_window"]:
                self.show_progress(d)
        else:
            AddDialog(self, url=url, filename=name, headers=headers)

    def _shutdown_computer(self):
        engine.shutdown_computer()
        self.show_window()
        if messagebox.askyesno(APP_NAME, "All scheduled downloads are finished.\n\n"
                               "The computer will shut down in about 60 seconds.\n"
                               "Do you want to CANCEL the shutdown?", parent=self.root):
            engine.cancel_shutdown()

    # ----------------------------------------------------------------- helpers
    def selected(self):
        return self.list.selection()

    def open_item(self, d):
        if d.status == COMPLETED:
            self._open(d)
        else:
            self.show_progress(d)

    def primary_action(self, d):
        """The round button on each card."""
        if d.status == COMPLETED:
            path = d.saved_path or d.path
            if os.path.exists(path):
                reveal_in_folder(path)
            else:
                messagebox.showerror(APP_NAME, f"File not found:\n{path}", parent=self.root)
        elif d.is_active or d.status == QUEUED:
            self.manager.pause(d)
        else:
            self.manager.resume(d)
            if self.settings["show_progress_window"]:
                self.show_progress(d)
        self.list.redraw()

    def _open(self, d):
        path = d.saved_path or d.path
        if not os.path.exists(path):
            messagebox.showerror(APP_NAME, f"File not found:\n{path}", parent=self.root)
            return
        try:
            open_path(path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)

    def show_progress(self, d):
        win = self.progress_windows.get(d.id)
        if win and win.winfo_exists():
            bring_to_front(win)
            return
        self.progress_windows[d.id] = ProgressWindow(self, d)

    # ----------------------------------------------------------------- actions
    def add_url(self):
        url = ""
        try:
            clip = self.root.clipboard_get().strip()
            if is_url(clip):
                url = clip
        except tk.TclError:
            pass
        AddDialog(self, url=url)

    def add_batch(self):
        BatchDialog(self)

    def resume_selected(self):
        for d in self.selected():
            if d.status != COMPLETED and not d.is_active:
                self.manager.resume(d)

    def pause_selected(self):
        for d in self.selected():
            self.manager.pause(d)

    def redownload_selected(self):
        items = self.selected()
        if items and messagebox.askyesno(APP_NAME, "Download the selected file(s) again from the start?",
                                         parent=self.root):
            for d in items:
                self.manager.redownload(d)

    def progress_selected(self):
        for d in self.selected():
            if d.status != COMPLETED:
                self.show_progress(d)

    def open_selected(self):
        for d in self.selected()[:1]:
            self._open(d)

    def open_folder_selected(self):
        for d in self.selected()[:1]:
            path = d.saved_path or d.path
            folder = os.path.dirname(path)
            if os.path.exists(path):
                reveal_in_folder(path)
            elif os.path.isdir(folder):
                open_path(folder)

    def copy_url_selected(self):
        items = self.selected()
        if items:
            text = "\n".join(d.url for d in items)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._clip_last = text

    def properties_selected(self):
        for d in self.selected()[:1]:
            PropertiesDialog(self, d)

    def delete_selected(self):
        items = self.selected()
        if items:
            DeleteDialog(self, items)

    def start_queue(self):
        self.manager.start_queue()
        self.flash("Queue started")

    def set_speed_limit(self, kb):
        self.settings["speed_limit_kb"] = int(kb)
        self.settings.save()
        self.manager.apply_settings()
        self.limit_var.set(int(kb))

    def open_options(self):
        OptionsDialog(self)

    def open_scheduler(self):
        SchedulerDialog(self)

    def start_server(self):
        if self.server:
            self.server.stop()
            self.server = None
        # Always listen (also used to bring an already running SwiftGet to the front);
        # the browser_integration setting decides whether browser downloads are accepted.
        self.server = IntegrationServer(self, int(self.settings["browser_port"]))
        self.server.start()

    def browser_help(self):
        ext_dir = extension_dir()
        messagebox.showinfo(
            "Browser extension",
            "Install the extension once to send browser downloads to SwiftGet:\n\n"
            "1. Open chrome://extensions (or edge://extensions)\n"
            "2. Turn on 'Developer mode'\n"
            "3. Click 'Load unpacked' and choose this folder:\n"
            f"   {ext_dir}\n\n"
            "Firefox: open about:addons › ⚙ › 'Install Add-on From File…' and pick "
            f"SwiftGet-firefox.xpi in:\n   {firefox_addon_dir()}\n"
            "(if Firefox says the add-on is not verified, use about:debugging › This Firefox › "
            "'Load Temporary Add-on…' with the same file — see the README).\n\n"
            "Downloads of the file types in Settings › Browser are then caught automatically, "
            "and you can right-click any link › 'Download with SwiftGet'.",
            parent=self.root)

    def about(self):
        messagebox.showinfo("About", f"{APP_NAME} Download Manager {VERSION}\n\n"
                            "Multi-connection downloads, pause/resume, queue, scheduler, "
                            "speed limiter and browser integration.\n\nFor personal use.",
                            parent=self.root)

    def show_window(self):
        self.root.deiconify()
        bring_to_front(self.root)

    def _tray_action(self, action):
        if action == "show":
            self.show_window()
        elif action == "new":
            self.add_url()
        elif action == "pause_all":
            self.manager.pause_all()
        elif action == "resume_all":
            self.manager.resume_all()
        elif action == "exit":
            self.quit_app()

    def on_close(self):
        """Window X button: keep running in the tray (like IDM)."""
        if self.settings["close_to_tray"] and (self.tray.running or self.tray.start()):
            self.root.withdraw()
            if not self.settings["tray_notice_shown"]:
                self.tray.notify(APP_NAME, "SwiftGet is still running here and will catch your downloads. "
                                           "Right-click this icon to exit.")
                self.settings["tray_notice_shown"] = True
                self.settings.save()
        else:
            self.quit_app()

    def quit_app(self):
        active = [d for d in self.manager.snapshot() if d.is_active]
        if active:
            self.show_window()
            if not messagebox.askyesno(
                    APP_NAME, f"{len(active)} download(s) are running.\n\n"
                    "They will be paused and can be resumed next time. Exit now?", parent=self.root):
                return
        if self.server:
            self.server.stop()
        self.tray.stop()
        self.manager.shutdown()
        self.root.destroy()


# ====================================================================== dialogs
class Dialog(tk.Toplevel):
    def __init__(self, app, title, modal=False, heading=None, subheading=None):
        super().__init__(app.root)
        self.app = app
        self.withdraw()
        self.title(title)
        self.resizable(True, False)
        if app.root.winfo_viewable():  # a hidden (tray) parent would hide the dialog too
            self.transient(app.root)
        self.configure(bg=app.theme.c["bg"])
        self.body = ttk.Frame(self, padding=(22, 18, 22, 18))
        self.body.pack(fill="both", expand=True)
        if heading:
            ttk.Label(self.body, text=heading, style="H1.TLabel").pack(anchor="w")
            if subheading:
                ttk.Label(self.body, text=subheading, style="Muted.TLabel", wraplength=560).pack(
                    anchor="w", pady=(2, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        self._modal = modal

    def finish_layout(self):
        center_on(self, self.app.root)
        self.deiconify()
        set_titlebar_dark(self, self.app.theme.dark)
        bring_to_front(self)
        if self._modal:
            try:
                self.grab_set()
            except tk.TclError:
                pass

    def button_row(self, parent, buttons):
        """buttons: list of (text, command, primary)"""
        row = ttk.Frame(parent)
        for text, cmd, primary in reversed(buttons):
            ttk.Button(row, text=text, command=cmd, style="Accent.TButton" if primary else "TButton").pack(
                side="right", padx=(8, 0))
        return row


class AddDialog(Dialog):
    def __init__(self, app, url="", filename=None, headers=None):
        super().__init__(app, "New download", heading="New download",
                         subheading="Paste a link. SwiftGet checks the file and downloads it with "
                                    "several connections at once.")
        self.headers = headers or {}
        self.name_edited = bool(filename)
        self.folder_edited = False
        self.probe_result = None
        self._probe_id = 0

        form = ttk.Frame(self.body)
        form.pack(fill="x", pady=(16, 0))
        form.columnconfigure(1, weight=1)

        def label(row, text):
            ttk.Label(form, text=text, style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=6,
                                                                 padx=(0, 14))

        label(0, "Address")
        self.url_var = tk.StringVar(value=url)
        url_entry = ttk.Entry(form, textvariable=self.url_var, width=72)
        url_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=6)
        url_entry.bind("<FocusOut>", lambda e: self.start_probe())
        url_entry.bind("<Return>", lambda e: self.start_probe())

        label(1, "File name")
        self.name_var = tk.StringVar(value=filename or "")
        name_entry = ttk.Entry(form, textvariable=self.name_var)
        name_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=6)
        name_entry.bind("<Key>", lambda e: setattr(self, "name_edited", True))

        label(2, "Save to")
        self.folder_var = tk.StringVar()
        folder_entry = ttk.Entry(form, textvariable=self.folder_var)
        folder_entry.grid(row=2, column=1, sticky="ew", pady=6)
        folder_entry.bind("<Key>", lambda e: setattr(self, "folder_edited", True))
        ttk.Button(form, text="Browse…", command=self._browse).grid(row=2, column=2, padx=(8, 0))

        label(3, "Category")
        opts = ttk.Frame(form)
        opts.grid(row=3, column=1, columnspan=2, sticky="w", pady=6)
        self.cat_var = tk.StringVar(value=category_for(filename or ""))
        cat = ttk.Combobox(opts, textvariable=self.cat_var, values=CATEGORY_NAMES, state="readonly", width=14)
        cat.pack(side="left")
        cat.bind("<<ComboboxSelected>>", lambda e: self._update_folder())
        ttk.Label(opts, text="Connections", style="Muted.TLabel").pack(side="left", padx=(24, 10))
        self.conn_var = tk.IntVar(value=int(app.settings["max_connections"]))
        ttk.Spinbox(opts, from_=1, to=32, textvariable=self.conn_var, width=5).pack(side="left")

        # info card
        self.info = tk.Canvas(self.body, height=int(64 * app.theme.scale), highlightthickness=0, bd=0)
        self.info.pack(fill="x", pady=(14, 0))
        self.info.bind("<Configure>", lambda e: self._draw_info())
        self.info_state = ("idle", "Enter an address to see the file details.", "")

        btns = self.button_row(self.body, [("Download later", lambda: self.submit(False), False),
                                           ("Cancel", self.destroy, False),
                                           ("Start download", lambda: self.submit(True), True)])
        btns.pack(fill="x", pady=(18, 0))
        self.bind("<Control-Return>", lambda e: self.submit(True))

        self._update_folder()
        self.finish_layout()
        url_entry.focus_set()
        if is_url(url):
            self.start_probe()

    def _draw_info(self):
        c, t = self.app.theme.c, self.app.theme
        cv = self.info
        cv.configure(bg=c["bg"])
        cv.delete("all")
        k = t.scale
        w = cv.winfo_width() / k
        state, line1, line2 = self.info_state
        rounded_rect(cv, 1, 1, w - 1, 63, 10, fill=c["surface"], outline=c["border"])
        name = self.name_var.get() or "file"
        color = CATEGORY_COLORS.get(category_for(name), CATEGORY_COLORS["Other"])
        if state == "error":
            color = c["error"]
        rounded_rect(cv, 12, 12, 52, 52, 9, fill=color if state != "idle" else c["track"], outline="")
        ext = (extension_of(name) or "?")[:4].upper() if state != "idle" else "?"
        cv.create_text(32, 32, text=ext, fill="#ffffff", font=t.font_badge)
        cv.create_text(66, 24 if line2 else 32, text=line1, anchor="w", fill=c["text"],
                       font=t.font_bold if state == "ok" else t.font)
        if line2:
            cv.create_text(66, 44, text=line2, anchor="w", fill=c["muted"], font=t.font_small)
        if k != 1.0:
            cv.scale("all", 0, 0, k, k)

    def _set_info(self, state, line1, line2=""):
        self.info_state = (state, line1, line2)
        self._draw_info()

    def _update_folder(self):
        if self.folder_edited:
            return
        base = self.app.settings["download_dir"]
        if self.app.settings["use_category_folders"]:
            base = os.path.join(base, self.cat_var.get() or "Other")
        self.folder_var.set(base)

    def _browse(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.folder_var.get() or os.path.expanduser("~"))
        if folder:
            self.folder_var.set(folder)
            self.folder_edited = True

    def start_probe(self):
        url = self.url_var.get().strip()
        if is_url(url) and video.is_video_page(url):
            # YouTube & co: a web page, not a file - offer the video qualities instead
            self.after(0, lambda: (self.destroy(), VideoDialog(self.app, url)))
            return
        if not is_url(url):
            if url:
                self._set_info("error", "That doesn't look like a download link.",
                               "Use an address that starts with http://, https:// or ftp://")
            return
        if self.probe_result and self.probe_result[0] == url:
            return
        self._probe_id += 1
        probe_id = self._probe_id
        self._set_info("busy", "Checking the file…", url if len(url) < 80 else url[:77] + "…")
        result = {}

        def work():
            try:
                result["d"] = self.app.manager.probe(url, self.headers)
            except Exception as exc:  # noqa: BLE001
                result["error"] = engine.friendly_error(exc)

        t = threading.Thread(target=work, daemon=True)
        t.start()
        self._poll_probe(t, result, url, probe_id)

    def _poll_probe(self, thread, result, url, probe_id):
        if not self.winfo_exists() or probe_id != self._probe_id:
            return
        if thread.is_alive():
            self.after(100, lambda: self._poll_probe(thread, result, url, probe_id))
            return
        if "error" in result:
            self._set_info("error", f"Couldn't check the file ({result['error']})",
                           "You can still try to download it.")
            return
        d = result["d"]
        self.probe_result = (url, d)
        if not self.name_edited or not self.name_var.get().strip():
            self.name_var.set(d.filename)
        self.cat_var.set(category_for(self.name_var.get()))
        self._update_folder()
        resume = "Resume supported · multi-connection" if d.resumable else "No resume · single connection"
        self._set_info("ok", self.name_var.get(), f"{human_size(d.size)}   ·   {resume}")

    def submit(self, start):
        url = self.url_var.get().strip()
        if not is_url(url):
            messagebox.showerror(APP_NAME, "Please enter a valid download address.", parent=self)
            return
        name = sanitize_filename(self.name_var.get().strip()) if self.name_var.get().strip() else None
        folder = self.folder_var.get().strip() or None
        try:
            conns = max(1, min(32, int(self.conn_var.get())))
        except (tk.TclError, ValueError):
            conns = None
        size, resumable = -1, None
        if self.probe_result and self.probe_result[0] == url:
            info = self.probe_result[1]
            size, resumable = info.size, info.resumable
            name = name or info.filename
        if folder and name and os.path.exists(os.path.join(folder, name)):
            if not messagebox.askyesno(APP_NAME, f"'{name}' already exists in this folder.\n\n"
                                       "Download anyway? (the new file gets a number added)", parent=self):
                return
        d = self.app.manager.add(url, filename=name, folder=folder, headers=self.headers, connections=conns,
                                 start=start, size=size, resumable=resumable)
        self.destroy()
        if start and self.app.settings["show_progress_window"]:
            self.app.show_progress(d)


class BatchDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "Batch download", heading="Batch download",
                         subheading="One address per line. Patterns like file[1-20].jpg, file[01-20].jpg "
                                    "or part[a-f].zip are expanded automatically.")
        self.resizable(True, True)
        self.text = tk.Text(self.body, width=84, height=12, wrap="none")
        app.theme.style_text(self.text)
        self.text.pack(fill="both", expand=True, pady=(14, 10))
        try:
            clip = app.root.clipboard_get()
            if any(is_url(line.strip()) for line in clip.splitlines()):
                self.text.insert("1.0", clip.strip())
        except tk.TclError:
            pass
        frm = ttk.Frame(self.body)
        frm.pack(fill="x")
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="Save to", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.folder_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(frm, text="Browse…", command=self._browse).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(frm, text="Leave empty to sort into category folders automatically.",
                  style="Muted.TLabel").grid(row=1, column=1, sticky="w", pady=(4, 0))
        self.button_row(self.body, [("Cancel", self.destroy, False),
                                    ("Add to queue", lambda: self.submit(False), False),
                                    ("Add and start", lambda: self.submit(True), True)]).pack(fill="x", pady=(16, 0))
        self.finish_layout()
        self.text.focus_set()

    def _browse(self):
        folder = filedialog.askdirectory(parent=self)
        if folder:
            self.folder_var.set(folder)

    def submit(self, start):
        urls = []
        for line in self.text.get("1.0", "end").splitlines():
            line = line.strip()
            if line:
                urls.extend(u for u in expand_batch_pattern(line) if is_url(u))
        if not urls:
            messagebox.showerror(APP_NAME, "No valid addresses found.", parent=self)
            return
        folder = self.folder_var.get().strip() or None
        for u in urls:
            self.app.manager.add(u, folder=folder, start=False)
        if start:
            self.app.manager.start_queue()
        self.app.flash(f"{len(urls)} download(s) added to the queue")
        self.destroy()


class ProgressWindow(tk.Toplevel):
    WIDTH = 560

    def __init__(self, app, d):
        super().__init__(app.root)
        self.app = app
        self.d = d
        self.withdraw()
        self.resizable(False, False)
        self.title(d.filename)
        self.configure(bg=app.theme.c["bg"])
        body = ttk.Frame(self, padding=(22, 18, 22, 18))
        body.pack(fill="both", expand=True)

        k = app.theme.scale
        self.head = tk.Canvas(body, width=int(self.WIDTH * k), height=int(58 * k), highlightthickness=0, bd=0)
        self.head.pack(fill="x")

        big = ttk.Frame(body)
        big.pack(fill="x", pady=(12, 0))
        self.pct_lbl = ttk.Label(big, text="0%", style="Big.TLabel")
        self.pct_lbl.pack(side="left")
        self.speed_lbl = ttk.Label(big, text="", style="Title.TLabel")
        self.speed_lbl.pack(side="right", anchor="s", pady=(0, 6))

        self.bar = tk.Canvas(body, width=int(self.WIDTH * k), height=int(26 * k), highlightthickness=0, bd=0)
        self.bar.pack(fill="x", pady=(6, 0))
        ttk.Label(body, text="Each blue block is one connection downloading its own part of the file.",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        stats = ttk.Frame(body)
        stats.pack(fill="x", pady=(14, 0))
        self.vars = {}
        for i, (key, label) in enumerate((("done", "Downloaded"), ("size", "File size"), ("eta", "Time left"),
                                          ("status", "Status"), ("conns", "Connections"),
                                          ("resume", "Resume"))):
            cell = ttk.Frame(stats)
            cell.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 30), pady=4)
            ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
            var = tk.StringVar(value="—")
            ttk.Label(cell, textvariable=var, style="Title.TLabel").pack(anchor="w")
            self.vars[key] = var
        for col in range(3):
            stats.columnconfigure(col, weight=1)

        self.details_open = False
        self.details_btn = ttk.Button(body, text="Show connections ▾", command=self.toggle_details)
        self.details_btn.pack(anchor="w", pady=(12, 0))
        self.details = ttk.Frame(body)
        self.conn_tree = ttk.Treeview(self.details, columns=("n", "dl", "info"), show="headings", height=6)
        for key, title, w in (("n", "#", 50), ("dl", "Downloaded", 140), ("info", "State", 300)):
            self.conn_tree.heading(key, text=title, anchor="w")
            self.conn_tree.column(key, width=w, anchor="w")
        self.conn_tree.pack(fill="x", pady=(8, 0))

        self.btns = ttk.Frame(body)
        self.btns.pack(fill="x", pady=(18, 0))
        ttk.Button(self.btns, text="Hide", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(self.btns, text="Cancel", command=self.cancel).pack(side="right", padx=(8, 0))
        self.toggle_btn = ttk.Button(self.btns, text="Pause", style="Accent.TButton", command=self.toggle)
        self.toggle_btn.pack(side="right")
        ttk.Button(self.btns, text="Open folder", command=self.open_folder).pack(side="left")

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda e: self.destroy())
        center_on(self, app.root)
        self.deiconify()
        set_titlebar_dark(self, app.theme.dark)
        self.update_view()

    def restyle(self):
        try:
            self.configure(bg=self.app.theme.c["bg"])
            set_titlebar_dark(self, self.app.theme.dark)
        except tk.TclError:
            pass

    def toggle_details(self):
        self.details_open = not self.details_open
        if self.details_open:
            self.details.pack(fill="x", before=self.btns)
            self.details_btn.configure(text="Hide connections ▴")
        else:
            self.details.pack_forget()
            self.details_btn.configure(text="Show connections ▾")

    def destroy(self):
        self.app.progress_windows.pop(self.d.id, None)
        super().destroy()

    def toggle(self):
        if self.d.is_active:
            self.app.manager.pause(self.d)
        elif self.d.status != COMPLETED:
            self.app.manager.resume(self.d)

    def cancel(self):
        self.app.manager.pause(self.d)
        self.destroy()

    def open_folder(self):
        folder = self.d.folder or os.path.dirname(self.d.path)
        if os.path.isdir(folder):
            open_path(folder)

    def _draw_head(self):
        c, t, d = self.app.theme.c, self.app.theme, self.d
        cv = self.head
        cv.configure(bg=c["bg"])
        cv.delete("all")
        color = CATEGORY_COLORS.get(d.category, CATEGORY_COLORS["Other"])
        rounded_rect(cv, 0, 6, 46, 52, 10, fill=color, outline="")
        cv.create_text(23, 29, text=(extension_of(d.filename) or "file")[:4].upper(), fill="#ffffff",
                       font=t.font_badge)
        from .widgets import ellipsize
        k = t.scale
        w = max(200, cv.winfo_width() - 62 * k)  # real pixels, for measuring text
        cv.create_text(62, 19, text=ellipsize(t.font_title, d.filename, w), anchor="w", fill=c["text"],
                       font=t.font_title)
        cv.create_text(62, 41, text=ellipsize(t.font_small, d.url, w), anchor="w", fill=c["muted"],
                       font=t.font_small)
        if k != 1.0:
            cv.scale("all", 0, 0, k, k)

    def _draw_bar(self):
        c, d = self.app.theme.c, self.d
        cv = self.bar
        cv.configure(bg=c["bg"])
        cv.delete("all")
        k = self.app.theme.scale
        w = max(10, cv.winfo_width() / k if cv.winfo_width() > 10 else self.WIDTH)
        h = 26
        rounded_rect(cv, 0, 0, w, h, 8, fill=c["track"], outline="")
        segs = d.segment_snapshot()
        rows = []
        if d.size > 0 and segs:
            for i, (start, end, pos, active) in enumerate(segs):
                x0 = start * w / d.size
                x1 = pos * w / d.size
                if x1 - x0 >= 1:
                    color = c["accent"] if d.status in ACTIVE_STATES else c["paused"]
                    cv.create_rectangle(x0, 3, x1, h - 3, fill=color, outline="")
                if active and pos <= end:  # the "head" of a running connection
                    cv.create_rectangle(max(x0, x1 - 2), 3, x1, h - 3, fill=c["success"], outline="")
                state = "Complete" if pos > end else ("Receiving data…" if active else "Waiting")
                rows.append((str(i + 1), human_size(pos - start), state))
        elif d.size > 0:
            x1 = d.progress * w / 100
            if x1 > 0:
                rounded_rect(cv, 0, 0, x1, h, 8, fill=c["accent"], outline="")
            if d.is_active:
                rows.append(("1", human_size(d.downloaded), "Receiving data…"))
        if k != 1.0:
            cv.scale("all", 0, 0, k, k)
        children = self.conn_tree.get_children()
        for i, row in enumerate(rows):
            if str(i) in children:
                self.conn_tree.item(str(i), values=row)
            else:
                self.conn_tree.insert("", "end", iid=str(i), values=row)
        for iid in children:
            if int(iid) >= len(rows):
                self.conn_tree.delete(iid)

    def update_view(self):
        if not self.winfo_exists():
            return
        d = self.d
        if self.app.manager.get(d.id) is None:
            self.destroy()
            return
        pct = d.progress
        self.title(f"{pct:.0f}% · {d.filename}")
        self.pct_lbl.configure(text=f"{pct:.1f}%" if d.size > 0 else human_size(d.downloaded))
        active = d.status in ACTIVE_STATES
        self.speed_lbl.configure(text=human_speed(d.speed) if active else "")
        status = getattr(d, "note", "") or {DOWNLOADING: "Downloading", ERROR: "Failed"}.get(d.status, d.status)
        self.vars["status"].set(status if d.status != ERROR else f"Failed: {d.error}"[:40])
        self.vars["size"].set(human_size(d.size))
        self.vars["done"].set(human_size(d.downloaded))
        self.vars["eta"].set(human_time(d.eta) if active and d.eta is not None else "—")
        self.vars["conns"].set(str(d.active_connections) if active else "—")
        self.vars["resume"].set(("Yes" if d.resumable else "No") if d.probed else "—")
        self.toggle_btn.configure(text="Pause" if d.is_active else "Resume")
        self._draw_head()
        self._draw_bar()
        self.after(400, self.update_view)


class CompleteDialog(Dialog):
    def __init__(self, app, d):
        super().__init__(app, "Download complete")
        self.d = d
        c, t = app.theme.c, app.theme
        k = t.scale
        head = tk.Canvas(self.body, width=int(480 * k), height=int(64 * k), highlightthickness=0, bd=0, bg=c["bg"])
        head.pack(fill="x")
        head.create_oval(2, 8, 50, 56, fill=c["success"], outline="")
        head.create_line(15, 32, 23, 40, 38, 24, fill="#ffffff", width=4, capstyle="round", joinstyle="round")
        head.create_text(66, 22, text="Download complete", anchor="w", fill=c["text"], font=t.font_h1)
        head.create_text(66, 46, text=f"{d.filename}  ·  {human_size(d.size)}", anchor="w", fill=c["muted"],
                         font=t.font)
        if k != 1.0:
            head.scale("all", 0, 0, k, k)
        ttk.Label(self.body, text=d.saved_path or "", style="Muted.TLabel", wraplength=480).pack(
            anchor="w", pady=(10, 0))
        self.dont_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.body, text="Don't show this again", variable=self.dont_show).pack(anchor="w",
                                                                                              pady=(12, 0))
        self.button_row(self.body, [("Close", self.close, False), ("Show in folder", self.open_folder, False),
                                    ("Open file", self.open_file, True)]).pack(fill="x", pady=(16, 0))
        self.finish_layout()

    def close(self):
        if self.dont_show.get():
            self.app.settings["show_complete_dialog"] = False
            self.app.settings.save()
        self.destroy()

    def open_file(self):
        self.app._open(self.d)
        self.close()

    def open_folder(self):
        if self.d.saved_path:
            reveal_in_folder(self.d.saved_path)
        self.close()


class DeleteDialog(Dialog):
    def __init__(self, app, items):
        title = f"Delete {len(items)} downloads?" if len(items) > 1 else "Delete download?"
        sub = "They will be removed from the list." if len(items) > 1 else \
            f"“{items[0].filename}” will be removed from the list."
        super().__init__(app, "Delete", modal=True, heading=title, subheading=sub)
        self.items = items
        self.delete_files = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.body, text="Also delete the file(s) from disk", variable=self.delete_files).pack(
            anchor="w", pady=(14, 0))
        self.button_row(self.body, [("Cancel", self.destroy, False), ("Delete", self.ok, True)]).pack(
            fill="x", pady=(18, 0))
        self.finish_layout()

    def ok(self):
        delete = self.delete_files.get()
        self.destroy()
        for d in self.items:
            win = self.app.progress_windows.pop(d.id, None)
            if win:
                win.destroy()
            self.app.manager.remove(d, delete_file=delete)


class PropertiesDialog(Dialog):
    def __init__(self, app, d):
        super().__init__(app, "Properties", heading="Properties", subheading=d.filename)
        self.d = d
        grid = ttk.Frame(self.body)
        grid.pack(fill="x", pady=(14, 0))
        grid.columnconfigure(1, weight=1)
        ttk.Label(grid, text="Address", style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=6,
                                                                   padx=(0, 14))
        self.url_var = tk.StringVar(value=d.url)
        state = "normal" if d.status != COMPLETED else "readonly"
        ttk.Entry(grid, textvariable=self.url_var, width=80, state=state).grid(row=0, column=1, sticky="ew")
        info = [
            ("Saved to", d.saved_path or d.path), ("Size", human_size(d.size)),
            ("Status", d.status + (f" – {d.error}" if d.error else "")),
            ("Resume", "Yes" if d.resumable else ("No" if d.probed else "Unknown")),
            ("Added", datetime.datetime.fromtimestamp(d.added).strftime("%d %b %Y, %H:%M:%S")),
        ]
        if d.finished_at:
            info.append(("Finished", datetime.datetime.fromtimestamp(d.finished_at).strftime("%d %b %Y, %H:%M:%S")))
        if d.headers.get("Referer"):
            info.append(("Referer", d.headers["Referer"]))
        for i, (k, v) in enumerate(info, start=1):
            ttk.Label(grid, text=k, style="Muted.TLabel").grid(row=i, column=0, sticky="nw", pady=4, padx=(0, 14))
            ttk.Label(grid, text=v, wraplength=560).grid(row=i, column=1, sticky="w", pady=4)
        ttk.Label(self.body, text="Link expired? Paste a fresh address above, press Save, then Resume — "
                                  "the progress is kept.", style="Muted.TLabel").pack(anchor="w", pady=(12, 0))
        self.button_row(self.body, [("Close", self.destroy, False), ("Save", self.save, True)]).pack(
            fill="x", pady=(16, 0))
        self.finish_layout()

    def save(self):
        url = self.url_var.get().strip()
        if url != self.d.url:
            if not is_url(url):
                messagebox.showerror(APP_NAME, "Invalid address.", parent=self)
                return
            if self.d.is_active:
                self.app.manager.pause(self.d)
            self.d.url = url
            self.app.manager._mark_dirty()
        self.destroy()


class OptionsDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "Settings", modal=True, heading="Settings")
        s = app.settings
        nb = ttk.Notebook(self.body)
        nb.pack(fill="both", expand=True, pady=(14, 0))
        self.v = {}

        def var(key, kind=tk.StringVar):
            v = kind(value=s[key])
            self.v[key] = v
            return v

        def page(title):
            f = ttk.Frame(nb, padding=16)
            f.columnconfigure(1, weight=1)
            nb.add(f, text=title)
            return f

        # General
        g = page("General")
        ttk.Label(g, text="Download folder").grid(row=0, column=0, sticky="w", pady=6, padx=(0, 12))
        ttk.Entry(g, textvariable=var("download_dir"), width=50).grid(row=0, column=1, sticky="ew")
        ttk.Button(g, text="Browse…", command=self._browse).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(g, text="Appearance").grid(row=1, column=0, sticky="w", pady=6)
        tf = ttk.Frame(g)
        tf.grid(row=1, column=1, sticky="w")
        theme = var("theme")
        for value, text in (("system", "Use system setting"), ("light", "Light"), ("dark", "Dark")):
            ttk.Radiobutton(tf, text=text, value=value, variable=theme).pack(side="left", padx=(0, 14))
        checks = [("use_category_folders", "Sort files into category folders (Video, Music, Programs…)"),
                  ("show_progress_window", "Show the progress window when a download starts"),
                  ("show_complete_dialog", "Show a notice when a download finishes"),
                  ("clipboard_monitor", "Watch the clipboard for download links"),
                  ("close_to_tray", "Keep running in the system tray when the window is closed")]
        if autostart.supported():
            checks.insert(0, ("start_with_windows", "Start SwiftGet automatically when Windows starts"))
        for i, (key, text) in enumerate(checks, start=2):
            ttk.Checkbutton(g, text=text, variable=var(key, tk.BooleanVar)).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=4)

        # Connection
        cpage = page("Connection")
        spins = [("max_connections", "Connections per download", 1, 32),
                 ("max_simultaneous", "Downloads at the same time (queue)", 1, 20),
                 ("speed_limit_kb", "Speed limit in KB/s (0 = unlimited)", 0, 1000000),
                 ("max_retries", "Retries per connection", 0, 100),
                 ("timeout", "Timeout (seconds)", 5, 300)]
        for i, (key, text, lo, hi) in enumerate(spins):
            ttk.Label(cpage, text=text).grid(row=i, column=0, sticky="w", pady=5, padx=(0, 12))
            ttk.Spinbox(cpage, from_=lo, to=hi, textvariable=var(key, tk.IntVar), width=10).grid(
                row=i, column=1, sticky="w")
        r = len(spins)
        ttk.Label(cpage, text="Proxy").grid(row=r, column=0, sticky="w", pady=(12, 5))
        pf = ttk.Frame(cpage)
        pf.grid(row=r, column=1, sticky="w", pady=(12, 5))
        mode = var("proxy_mode")
        for value, text in (("system", "System"), ("none", "None"), ("manual", "Manual")):
            ttk.Radiobutton(pf, text=text, value=value, variable=mode).pack(side="left", padx=(0, 10))
        ttk.Entry(pf, textvariable=var("proxy"), width=24).pack(side="left")
        ttk.Label(cpage, text="User-Agent").grid(row=r + 1, column=0, sticky="w", pady=5)
        ttk.Entry(cpage, textvariable=var("user_agent"), width=50).grid(row=r + 1, column=1, sticky="ew")
        ttk.Checkbutton(cpage, text="Verify HTTPS certificates (recommended)",
                        variable=var("verify_ssl", tk.BooleanVar)).grid(row=r + 2, column=0, columnspan=2,
                                                                        sticky="w", pady=5)

        # Browser
        br = page("Browser")
        ttk.Checkbutton(br, text="Enable browser integration", variable=var("browser_integration", tk.BooleanVar)
                        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(br, text="Start browser downloads right away (skip the dialog)",
                        variable=var("browser_skip_dialog", tk.BooleanVar)).grid(row=1, column=0, columnspan=2,
                                                                                 sticky="w", pady=4)
        ttk.Checkbutton(br, text="Show a “Download this video” button on videos (YouTube and other sites)",
                        variable=var("video_button", tk.BooleanVar)).grid(row=2, column=0, columnspan=2,
                                                                          sticky="w", pady=4)
        ttk.Label(br, text="Local port").grid(row=7, column=0, sticky="w", pady=6, padx=(0, 12))
        ttk.Spinbox(br, from_=1024, to=65535, textvariable=var("browser_port", tk.IntVar), width=8).grid(
            row=7, column=1, sticky="w")
        ttk.Label(br, text="Catch these file types").grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 4))
        self.ext_text = tk.Text(br, height=4, width=60, wrap="word")
        app.theme.style_text(self.ext_text)
        self.ext_text.insert("1.0", s["capture_extensions"])
        self.ext_text.grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Label(br, text="Ignore files smaller than (KB)").grid(row=5, column=0, sticky="w", pady=6)
        ttk.Spinbox(br, from_=0, to=10000000, textvariable=var("capture_min_size_kb", tk.IntVar), width=10).grid(
            row=5, column=1, sticky="w")
        ttk.Button(br, text="How to install the browser extension…", command=app.browser_help).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))

        self.button_row(self.body, [("Cancel", self.destroy, False), ("Save", self.save, True)]).pack(
            fill="x", pady=(16, 0))
        self.finish_layout()

    def _browse(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.v["download_dir"].get())
        if folder:
            self.v["download_dir"].set(folder)

    def save(self):
        s = self.app.settings
        old_port, old_theme = s["browser_port"], s["theme"]
        try:
            for key, v in self.v.items():
                s[key] = v.get()
        except tk.TclError:
            messagebox.showerror(APP_NAME, "Please enter valid numbers.", parent=self)
            return
        s["capture_extensions"] = " ".join(self.ext_text.get("1.0", "end").split())
        s.save()
        self.app.manager.apply_settings()
        self.app.limit_var.set(int(s["speed_limit_kb"] or 0))
        if old_port != s["browser_port"]:
            self.app.start_server()
        self.destroy()
        if autostart.supported():
            autostart.set_enabled(bool(s["start_with_windows"]))
        if s["close_to_tray"] and not self.app.tray.running:
            self.app.tray.start()
        if old_theme != s["theme"]:
            self.app.theme.apply(s["theme"])


class SchedulerDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "Scheduler", heading="Scheduler",
                         subheading="Downloads added with “Download later” wait in the queue. "
                                    "The scheduler starts the queue every day at the time you choose.")
        s = app.settings
        self.enabled = tk.BooleanVar(value=s["scheduler_enabled"])
        self.start_t = tk.StringVar(value=s["scheduler_start"])
        self.stop_t = tk.StringVar(value=s["scheduler_stop"])
        self.shutdown = tk.BooleanVar(value=s["scheduler_shutdown"])
        g = ttk.Frame(self.body)
        g.pack(fill="x", pady=(14, 0))
        ttk.Checkbutton(g, text="Enable scheduler", variable=self.enabled).grid(row=0, column=0, columnspan=2,
                                                                                 sticky="w", pady=4)
        ttk.Label(g, text="Start queue at (HH:MM)").grid(row=1, column=0, sticky="w", pady=6, padx=(0, 14))
        ttk.Entry(g, textvariable=self.start_t, width=8).grid(row=1, column=1, sticky="w")
        ttk.Label(g, text="Stop queue at (optional)").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(g, textvariable=self.stop_t, width=8).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(g, text="Shut down the computer when the queue is finished",
                        variable=self.shutdown).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.button_row(self.body, [("Start queue now", self.start_now, False), ("Cancel", self.destroy, False),
                                    ("Save", self.save, True)]).pack(fill="x", pady=(18, 0))
        self.finish_layout()

    @staticmethod
    def _valid(t):
        try:
            datetime.datetime.strptime(t, "%H:%M")
            return True
        except ValueError:
            return False

    @staticmethod
    def _normalize(t):
        return datetime.datetime.strptime(t, "%H:%M").strftime("%H:%M")

    def save(self):
        start, stop = self.start_t.get().strip(), self.stop_t.get().strip()
        if self.enabled.get() and not self._valid(start):
            messagebox.showerror(APP_NAME, "Start time must look like 02:30", parent=self)
            return
        if stop and not self._valid(stop):
            messagebox.showerror(APP_NAME, "Stop time must look like 07:00 (or be empty)", parent=self)
            return
        s = self.app.settings
        s["scheduler_enabled"] = self.enabled.get()
        s["scheduler_start"] = self._normalize(start) if self._valid(start) else start
        s["scheduler_stop"] = self._normalize(stop) if stop else ""
        s["scheduler_shutdown"] = self.shutdown.get()
        s.save()
        self.destroy()

    def start_now(self):
        self.app.manager.start_queue()
        self.app.manager._shutdown_armed = bool(self.shutdown.get())
        self.destroy()


class WelcomeDialog(Dialog):
    """Shown once on first start: explains the automatic behaviour and the browser extension."""

    def __init__(self, app):
        super().__init__(app, "Welcome", heading="Welcome to SwiftGet",
                         subheading="Everything runs automatically, just like other download managers.")
        app.settings["welcome_shown"] = True
        app.settings.save()
        points = [
            ("Always ready", "SwiftGet starts with Windows and waits in the system tray (next to the clock). "
                             "Closing the window keeps it running there."),
            ("Catches browser downloads", "Add the browser extension once (steps below). After that, files "
                                          "you download in Chrome or Edge open in SwiftGet automatically."),
            ("Fast and resumable", "Each file is downloaded with up to 32 connections. Pause any time, even "
                                   "turn off the PC, and continue later."),
        ]
        box = ttk.Frame(self.body)
        box.pack(fill="x", pady=(14, 0))
        for i, (title, text) in enumerate(points):
            ttk.Label(box, text=f"{i + 1}", style="Big.TLabel").grid(row=i, column=0, sticky="nw", padx=(0, 14))
            cell = ttk.Frame(box)
            cell.grid(row=i, column=1, sticky="w", pady=(4, 10))
            ttk.Label(cell, text=title, style="Title.TLabel").pack(anchor="w")
            ttk.Label(cell, text=text, style="Muted.TLabel", wraplength=480).pack(anchor="w")

        ttk.Separator(self.body).pack(fill="x", pady=(6, 12))
        ttk.Label(self.body, text="Add the browser extension (one time)", style="Title.TLabel").pack(anchor="w")
        steps = ("1.  Click “Copy address” and paste it into Chrome’s (or Edge’s) address bar.\n"
                 "2.  Turn on “Developer mode” (top right).\n"
                 "3.  Click “Load unpacked” and pick the folder that “Open extension folder” shows.")
        ttk.Label(self.body, text=steps, style="Muted.TLabel", justify="left").pack(anchor="w", pady=(4, 0))
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Copy address", command=self.copy_address).pack(side="left")
        ttk.Button(row, text="Open extension folder", command=self.open_folder).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Firefox…", command=app.browser_help).pack(side="left", padx=(8, 0))
        self.copied = ttk.Label(row, text="", style="Muted.TLabel")
        self.copied.pack(side="left", padx=(10, 0))

        self.button_row(self.body, [("Get started", self.destroy, True)]).pack(fill="x", pady=(18, 0))
        self.finish_layout()

    def copy_address(self):
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append("chrome://extensions")
        self.app._clip_last = "chrome://extensions"
        self.copied.configure(text="Copied! (Edge: edge://extensions)")

    def open_folder(self):
        folder = extension_dir()
        if os.path.isdir(folder):
            open_path(folder)
        else:
            messagebox.showinfo(APP_NAME, f"Extension folder:\n{folder}", parent=self)


class VideoDialog(Dialog):
    """Pick a quality for a YouTube (or other video site) page, like a video grabber."""

    def __init__(self, app, url):
        super().__init__(app, "Download video", heading="Download video",
                         subheading=url if len(url) < 90 else url[:87] + "…")
        self.url = url
        self.info = None
        self.choice = tk.StringVar()
        self.status_var = tk.StringVar(value="Reading the video page…")
        self.title_lbl = ttk.Label(self.body, text="", style="Title.TLabel", wraplength=560)
        self.title_lbl.pack(anchor="w", pady=(12, 0))
        ttk.Label(self.body, textvariable=self.status_var, style="Muted.TLabel", wraplength=560).pack(
            anchor="w", pady=(6, 0))
        self.list_frame = ttk.Frame(self.body)
        self.list_frame.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(12, 0))
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text="Save to", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.folder_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(row, text="Browse…", command=self._browse).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(row, text="Leave empty to save into the Video / Music folder automatically.",
                  style="Muted.TLabel").grid(row=1, column=1, sticky="w", pady=(4, 0))
        btns = self.button_row(self.body, [("Download later", lambda: self.submit(False), False),
                                           ("Cancel", self.destroy, False),
                                           ("Download", lambda: self.submit(True), True)])
        btns.pack(fill="x", pady=(16, 0))
        self.finish_layout()
        self._load()

    def _browse(self):
        folder = filedialog.askdirectory(parent=self)
        if folder:
            self.folder_var.set(folder)

    def _load(self):
        if not self.winfo_exists():
            return
        tools = self.app.manager.video_tools
        st = tools.ensure_async()
        if not tools.ready():
            if st["state"] == "error":
                self.status_var.set(st["error"])
                return
            self.status_var.set(st["message"] or "Setting up video support (first time only)…")
            self.after(700, self._load)
            return
        self.status_var.set("Reading the video page…")
        result = {}

        def work():
            try:
                result["info"] = video.get_info(tools, self.url)
            except Exception as exc:  # noqa: BLE001
                result["error"] = engine.friendly_error(exc)

        t = threading.Thread(target=work, daemon=True)
        t.start()
        self._poll(t, result)

    def _poll(self, thread, result):
        if not self.winfo_exists():
            return
        if thread.is_alive():
            self.after(150, lambda: self._poll(thread, result))
            return
        if "error" in result:
            self.status_var.set(f"Couldn't read this video: {result['error']}")
            return
        self.info = result["info"]
        self.title_lbl.configure(text=self.info["title"])
        self.status_var.set("Choose a quality:")
        for i, q in enumerate(self.info["qualities"]):
            size = f"  ·  {human_size(q['size'])}" if q.get("size", -1) > 0 else ""
            ttk.Radiobutton(self.list_frame, text=f"{q['label']}   {q['detail']}{size}", value=str(i),
                            variable=self.choice).pack(anchor="w", pady=2)
        self.choice.set("0")
        center_on(self, self.app.root)

    def submit(self, start):
        if not self.info:
            return
        q = self.info["qualities"][int(self.choice.get() or 0)]
        d = self.app.manager.add_video(self.url, quality=q["id"], title=self.info["title"],
                                       folder=self.folder_var.get().strip() or None,
                                       size=q.get("size", -1), start=start)
        self.destroy()
        if start and self.app.settings["show_progress_window"]:
            self.app.show_progress(d)
