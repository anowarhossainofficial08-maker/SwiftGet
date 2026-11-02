"""Custom-drawn widgets: the download list (cards with inline progress) and the sidebar."""

import datetime
import tkinter as tk
from tkinter import ttk

from .engine import ACTIVE_STATES, COMPLETED, CONNECTING, DOWNLOADING, ERROR, PAUSED, QUEUED, WAITING
from .theme import CATEGORY_COLORS
from .utils import extension_of, human_size, human_speed, human_time


def rounded_rect(canvas, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return canvas.create_polygon(pts, smooth=True, **kw)


class _Ellipsizer:
    def __init__(self):
        self.cache = {}

    def __call__(self, font, text, width):
        key = (str(font), text, int(width))
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        if font.measure(text) <= width:
            out = text
        else:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.measure(text[:mid] + "…") <= width:
                    lo = mid
                else:
                    hi = mid - 1
            out = text[:lo] + "…"
        if len(self.cache) > 4000:
            self.cache.clear()
        self.cache[key] = out
        return out


ellipsize = _Ellipsizer()


class DownloadList(tk.Canvas):
    """Virtualised list of download 'cards' drawn on a single canvas."""

    ROW_H = 72
    PAD_X = 12

    def __init__(self, master, app):
        super().__init__(master, highlightthickness=0, bd=0)
        self.app = app
        self.theme = app.theme
        self.items = []
        self.selected = set()
        self.anchor = None
        self.hover = None
        self.hover_btn = None
        self.offset = 0
        self.vsb = ttk.Scrollbar(master, orient="vertical", command=self._yview)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda e: self.scroll_by(-3 * 24))
        self.bind("<Button-5>", lambda e: self.scroll_by(3 * 24))
        self.bind("<Button-1>", self._on_click)
        self.bind("<Control-Button-1>", lambda e: self._on_click(e, toggle=True))
        self.bind("<Shift-Button-1>", lambda e: self._on_click(e, extend=True))
        self.bind("<Double-Button-1>", self._on_double)
        self.bind("<Button-3>", self._on_right)
        self.bind("<Button-2>", self._on_right)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._set_hover(None, None))
        self.bind("<Up>", lambda e: self._move(-1))
        self.bind("<Down>", lambda e: self._move(1))
        self.bind("<Return>", self._on_double)

    # ----------------------------------------------------------------- data
    def set_items(self, items):
        self.items = items
        ids = {d.id for d in items}
        self.selected &= ids
        self.redraw()

    def selection(self):
        return [d for d in self.items if d.id in self.selected]

    def select_all(self):
        self.selected = {d.id for d in self.items}
        self.redraw()

    # ---------------------------------------------------- DPI (logical units)
    @property
    def k(self):
        return self.theme.scale

    def _logical_w(self):
        return self.winfo_width() / self.k

    def _logical_h(self):
        return self.winfo_height() / self.k

    # -------------------------------------------------------------- scrolling
    def _content_h(self):
        return len(self.items) * self.ROW_H + 8

    def _clamp(self):
        h = self._logical_h()
        self.offset = max(0, min(self.offset, max(0, self._content_h() - h)))

    def scroll_by(self, dy):
        self.offset += dy
        self._clamp()
        self.redraw()

    def _on_wheel(self, e):
        self.scroll_by(-int(e.delta / 120 * 60) if abs(e.delta) >= 120 else -e.delta)

    def _yview(self, *args):
        total = max(1, self._content_h())
        h = self._logical_h()
        if args[0] == "moveto":
            self.offset = int(float(args[1]) * total)
        elif args[0] == "scroll":
            n = int(args[1])
            self.offset += n * (h if args[2] == "pages" else 40)
        self._clamp()
        self.redraw()

    def _update_scrollbar(self):
        total = max(1, self._content_h())
        h = max(1, self._logical_h())
        first = self.offset / total
        last = min(1.0, (self.offset + h) / total)
        self.vsb.set(first, last)
        needed = first > 0 or last < 1.0
        if needed and not self.vsb.winfo_ismapped():
            self.vsb.pack(side="right", fill="y", before=self)
        elif not needed and self.vsb.winfo_ismapped():
            self.vsb.pack_forget()

    # ------------------------------------------------------------- hit tests
    def _row_at(self, y):
        idx = int((y / self.k + self.offset - 4) // self.ROW_H)
        return idx if 0 <= idx < len(self.items) else None

    def _button_at(self, x, y, idx):
        if idx is None:
            return False
        w = self._logical_w()
        x, y = x / self.k, y / self.k
        cy = 4 + idx * self.ROW_H - self.offset + self.ROW_H / 2
        cx = w - self.PAD_X - 30
        return (x - cx) ** 2 + (y - cy) ** 2 <= 17 ** 2

    def _on_click(self, e, toggle=False, extend=False):
        self.focus_set()
        idx = self._row_at(e.y)
        if idx is None:
            if not (toggle or extend):
                self.selected.clear()
            self.redraw()
            return
        d = self.items[idx]
        if self._button_at(e.x, e.y, idx) and not (toggle or extend):
            self.selected = {d.id}
            self.app.primary_action(d)
        elif toggle:
            self.selected ^= {d.id}
            self.anchor = d.id
        elif extend and self.anchor:
            ids = [x.id for x in self.items]
            a = ids.index(self.anchor) if self.anchor in ids else idx
            lo, hi = sorted((a, idx))
            self.selected = set(ids[lo:hi + 1])
        else:
            self.selected = {d.id}
            self.anchor = d.id
        self.redraw()

    def _on_double(self, e):
        if str(e.type) == "KeyPress":
            items = self.selection()[:1]
        else:
            idx = self._row_at(e.y)
            if idx is None or self._button_at(e.x, e.y, idx):
                return
            items = [self.items[idx]]
        if items:
            self.app.open_item(items[0])

    def _on_right(self, e):
        idx = self._row_at(e.y)
        if idx is None:
            return
        d = self.items[idx]
        if d.id not in self.selected:
            self.selected = {d.id}
            self.anchor = d.id
            self.redraw()
        self.app.show_context_menu(e.x_root, e.y_root)

    def _on_motion(self, e):
        idx = self._row_at(e.y)
        btn = self._button_at(e.x, e.y, idx)
        self._set_hover(idx, btn)

    def _set_hover(self, idx, btn):
        hid = self.items[idx].id if idx is not None and idx < len(self.items) else None
        if hid != self.hover or btn != self.hover_btn:
            self.hover, self.hover_btn = hid, btn
            self.configure(cursor="hand2" if btn else "")
            self.redraw()

    def _move(self, step):
        if not self.items:
            return
        ids = [d.id for d in self.items]
        cur = ids.index(self.anchor) if self.anchor in ids else -1
        nxt = max(0, min(len(ids) - 1, cur + step))
        self.selected = {ids[nxt]}
        self.anchor = ids[nxt]
        top = 4 + nxt * self.ROW_H
        if top < self.offset:
            self.offset = top
        elif top + self.ROW_H > self.offset + self._logical_h():
            self.offset = top + self.ROW_H - self._logical_h()
        self._clamp()
        self.redraw()

    # ---------------------------------------------------------------- drawing
    def redraw(self):
        c = self.theme.c
        self.configure(bg=c["bg"])
        self.delete("all")
        w, h = self._logical_w(), self._logical_h()
        if w < 50:
            return
        self._clamp()
        if not self.items:
            self._draw_empty(w, h)
        else:
            first = max(0, int(self.offset // self.ROW_H) - 1)
            last = min(len(self.items), int((self.offset + h) // self.ROW_H) + 2)
            for idx in range(first, last):
                self._draw_row(idx, self.items[idx], w)
        if self.k != 1.0:
            self.scale("all", 0, 0, self.k, self.k)
        self._update_scrollbar()

    def _draw_empty(self, w, h):
        c, t = self.theme.c, self.theme
        cx, cy = w / 2, h / 2 - 30
        self.create_oval(cx - 34, cy - 34, cx + 34, cy + 34, fill=c["select"], outline="")
        self.create_line(cx, cy - 16, cx, cy + 10, fill=c["accent"], width=5, capstyle="round")
        self.create_line(cx - 12, cy, cx, cy + 12, cx + 12, cy, fill=c["accent"], width=5,
                         capstyle="round", joinstyle="round")
        self.create_line(cx - 15, cy + 20, cx + 15, cy + 20, fill=c["accent"], width=4, capstyle="round")
        msg = "No downloads here yet" if not self.app.search_text() else "Nothing matches your search"
        self.create_text(cx, cy + 62, text=msg, fill=c["text"], font=t.font_title)
        self.create_text(cx, cy + 86, fill=c["muted"], font=t.font,
                         text="Click “New download”, press Ctrl+V with a link copied, "
                              "or download from your browser.")

    def _status_line(self, d):
        note = getattr(d, "note", "")
        if note and d.status in (DOWNLOADING, CONNECTING):
            return note, "muted"
        size = human_size(d.size) if d.size >= 0 else "Unknown size"
        if d.status == DOWNLOADING:
            parts = [f"{human_size(d.downloaded)} of {size}" if d.size > 0 else human_size(d.downloaded)]
            if d.speed:
                parts.append(human_speed(d.speed))
            if d.eta is not None:
                parts.append(f"{human_time(d.eta)} left")
            if d.active_connections:
                parts.append(f"{d.active_connections} connection{'s' if d.active_connections != 1 else ''}")
            return "  ·  ".join(parts), "muted"
        if d.status == CONNECTING:
            return "Connecting…", "muted"
        if d.status == PAUSED:
            if d.size > 0:
                return f"Paused  ·  {human_size(d.downloaded)} of {size}", "muted"
            return "Paused", "muted"
        if d.status == QUEUED:
            return "Queued  ·  starts when a slot is free", "muted"
        if d.status == WAITING:
            return "In queue  ·  starts with “Start queue” or the scheduler", "muted"
        if d.status == COMPLETED:
            when = datetime.datetime.fromtimestamp(d.finished_at or d.added).strftime("%d %b %Y, %H:%M")
            return f"{size}  ·  Completed {when}", "muted"
        if d.status == ERROR:
            return f"Failed  ·  {d.error}", "error"
        return d.status, "muted"

    def _draw_row(self, idx, d, w):
        c, t = self.theme.c, self.theme
        y0 = 4 + idx * self.ROW_H - self.offset
        x0, x1 = self.PAD_X, w - self.PAD_X
        sel = d.id in self.selected
        hov = d.id == self.hover
        fill = c["select"] if sel else (c["hover"] if hov else c["surface"])
        rounded_rect(self, x0, y0 + 3, x1, y0 + self.ROW_H - 3, 10, fill=fill,
                     outline=c["accent"] if sel else c["border"])

        # file-type badge
        bx, by = x0 + 14, y0 + self.ROW_H / 2 - 20
        color = CATEGORY_COLORS.get(d.category, CATEGORY_COLORS["Other"])
        rounded_rect(self, bx, by, bx + 40, by + 40, 9, fill=color, outline="")
        ext = (extension_of(d.filename) or "file")[:4].upper()
        self.create_text(bx + 20, by + 20, text=ext, fill="#ffffff", font=t.font_badge)

        # right side: percentage + round action button
        right_w = 128
        cx, cy = x1 - 30, y0 + self.ROW_H / 2
        active = d.status in ACTIVE_STATES
        if d.status == COMPLETED:
            pct_text, pct_color = "Done", c["success"]
        elif d.size > 0 and d.status in (DOWNLOADING, PAUSED, CONNECTING, ERROR):
            pct_text, pct_color = f"{d.progress:.0f}%", c["accent"] if active else c["muted"]
        else:
            pct_text, pct_color = "", c["muted"]
        self.create_text(cx - 30, cy, text=pct_text, anchor="e", fill=pct_color, font=t.font_title)
        btn_hover = hov and self.hover_btn
        self.create_oval(cx - 16, cy - 16, cx + 16, cy + 16,
                         fill=c["accent"] if btn_hover else (c["surface"] if not sel else c["select"]),
                         outline=c["accent"] if not btn_hover else "", width=1.5)
        icon = c["accent_text"] if btn_hover else c["accent"]
        if active or d.status == QUEUED:  # pause icon
            self.create_rectangle(cx - 6, cy - 7, cx - 2, cy + 7, fill=icon, outline="")
            self.create_rectangle(cx + 2, cy - 7, cx + 6, cy + 7, fill=icon, outline="")
        elif d.status == COMPLETED:  # folder icon
            self.create_polygon(cx - 8, cy - 6, cx - 3, cy - 6, cx - 1, cy - 4, cx + 8, cy - 4,
                                cx + 8, cy + 7, cx - 8, cy + 7, fill=icon, outline="")
        elif d.status == ERROR:  # retry arrow
            self.create_arc(cx - 7, cy - 7, cx + 7, cy + 7, start=60, extent=280, style="arc",
                            outline=icon, width=2.2)
            self.create_polygon(cx + 3, cy - 10, cx + 9, cy - 5, cx + 2, cy - 3, fill=icon, outline="")
        else:  # play icon
            self.create_polygon(cx - 4, cy - 8, cx - 4, cy + 8, cx + 8, cy, fill=icon, outline="")

        # name + status text
        tx = bx + 54
        text_w = x1 - right_w - tx
        bar_y = y0 + 54
        show_bar = d.status not in (COMPLETED,) and (d.size > 0 or active)
        name_y = y0 + (22 if show_bar else 26)
        self.create_text(tx, name_y, text=ellipsize(t.font_bold, d.filename, text_w * self.k), anchor="w",
                         fill=c["text"], font=t.font_bold)
        status, tone = self._status_line(d)
        self.create_text(tx, name_y + 19, text=ellipsize(t.font_small, status, text_w * self.k), anchor="w",
                         fill=c["error"] if tone == "error" else c["muted"], font=t.font_small)

        # progress bar
        if show_bar:
            bx0, bx1 = tx, x1 - right_w + 40
            self.create_line(bx0, bar_y, bx1, bar_y, fill=c["track"], width=4 * self.k, capstyle="round")
            frac = d.progress / 100.0 if d.size > 0 else 0
            if frac > 0:
                color = {DOWNLOADING: c["accent"], CONNECTING: c["accent"], ERROR: c["error"]}.get(
                    d.status, c["paused"])
                self.create_line(bx0, bar_y, bx0 + max(4, (bx1 - bx0) * frac), bar_y, fill=color, width=4 * self.k,
                                 capstyle="round")


class Sidebar(tk.Canvas):
    ITEM_H = 36

    def __init__(self, master, app, entries):
        super().__init__(master, highlightthickness=0, bd=0, width=int(220 * app.theme.scale))
        self.app = app
        self.theme = app.theme
        self.entries = entries  # list of (key, label, color) or ("-", header, None)
        self.current = entries[0][0]
        self.counts = {}
        self.hover = None
        self._hits = []
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))

    def set_counts(self, counts):
        if counts != self.counts:
            self.counts = counts
            self.redraw()

    def _key_at(self, y):
        y = y / self.theme.scale
        for key, y0, y1 in self._hits:
            if y0 <= y < y1:
                return key
        return None

    def _click(self, e):
        key = self._key_at(e.y)
        if key:
            self.current = key
            self.redraw()
            self.app.on_filter(key)

    def _motion(self, e):
        self._set_hover(self._key_at(e.y))

    def _set_hover(self, key):
        if key != self.hover:
            self.hover = key
            self.configure(cursor="hand2" if key else "")
            self.redraw()

    def redraw(self):
        c, t = self.theme.c, self.theme
        self.configure(bg=c["sidebar"])
        self.delete("all")
        k = self.theme.scale
        w = self.winfo_width() / k
        # logo
        self.create_image(22, 30, image=self.app.logo, anchor="w")
        self.create_text(60, 30, text="SwiftGet", anchor="w", fill=c["text"], font=t.font_h1)
        y = 70
        self._hits = []
        for key, label, color in self.entries:
            if key == "-":
                y += 10
                self.create_text(22, y + 8, text=label.upper(), anchor="w", fill=c["muted"], font=t.font_badge)
                y += 24
                continue
            y0, y1 = y, y + self.ITEM_H
            selected = key == self.current
            if selected or key == self.hover:
                rounded_rect(self, 10, y0 + 2, w - 10, y1 - 2, 8,
                             fill=c["surface"] if selected else c["hover"], outline="")
            if selected:
                rounded_rect(self, 10, y0 + 10, 13, y1 - 10, 2, fill=c["accent"], outline="")
            dot = color or c["muted"]
            self.create_oval(24, y0 + 14, 32, y0 + 22, fill=dot, outline="")
            self.create_text(42, y0 + self.ITEM_H / 2, text=label, anchor="w",
                             fill=c["text"], font=t.font_bold if selected else t.font)
            n = self.counts.get(key, 0)
            if n:
                self.create_text(w - 22, y0 + self.ITEM_H / 2, text=str(n), anchor="e", fill=c["muted"],
                                 font=t.font_small)
            self._hits.append((key, y0, y1))
            y = y1
        if k != 1.0:
            self.scale("all", 0, 0, k, k)
