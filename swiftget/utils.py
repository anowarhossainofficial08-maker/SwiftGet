"""Small helpers: formatting, file names, categories."""

import os
import re
import sys
import subprocess
import urllib.parse
from email.message import Message

CATEGORIES = {
    "Compressed": ["zip", "rar", "7z", "tar", "gz", "bz2", "xz", "tgz", "iso", "cab", "arj", "lz", "zst"],
    "Documents": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "odt", "ods", "odp",
                  "epub", "csv", "md"],
    "Music": ["mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus", "aif", "aiff"],
    "Programs": ["exe", "msi", "apk", "dmg", "pkg", "deb", "rpm", "appimage", "msix", "bat", "jar"],
    "Video": ["mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp", "ts", "vob"],
    "Images": ["jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "tif", "tiff", "ico", "heic"],
}
CATEGORY_NAMES = list(CATEGORIES.keys()) + ["Other"]

URL_RE = re.compile(r"^(https?|ftp)://[^\s/$.?#].[^\s]*$", re.IGNORECASE)

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def is_url(text):
    return bool(text) and bool(URL_RE.match(text.strip()))


def extension_of(filename):
    _, ext = os.path.splitext(filename or "")
    return ext[1:].lower()


def category_for(filename):
    ext = extension_of(filename)
    for name, exts in CATEGORIES.items():
        if ext in exts:
            return name
    return "Other"


def sanitize_filename(name):
    name = _INVALID_CHARS.sub("_", name or "").strip().strip(".")
    if not name:
        name = "download"
    base = os.path.splitext(name)[0].upper()
    if base in _RESERVED:
        name = "_" + name
    if len(name) > 200:
        root, ext = os.path.splitext(name)
        name = root[: 200 - len(ext)] + ext
    return name


def filename_from_url(url):
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(os.path.basename(path.rstrip("/")))
    return sanitize_filename(name) if name else "download"


def filename_from_headers(headers, url, final_url=None):
    """Pick a file name from Content-Disposition, falling back to the URL(s)."""
    cd = headers.get("Content-Disposition") if headers else None
    if cd:
        msg = Message()
        msg["content-disposition"] = cd
        try:
            name = msg.get_filename()
        except Exception:  # malformed header
            name = None
        if name:
            return sanitize_filename(os.path.basename(name.replace("\\", "/")))
    name = filename_from_url(url)
    if "." not in name and final_url and final_url != url:
        alt = filename_from_url(final_url)
        if "." in alt:
            name = alt
    if "." not in name and headers:
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        guess = {
            "application/pdf": ".pdf", "application/zip": ".zip", "video/mp4": ".mp4",
            "audio/mpeg": ".mp3", "image/jpeg": ".jpg", "image/png": ".png",
            "application/x-msdownload": ".exe", "text/html": ".html",
        }.get(ctype)
        if guess:
            name += guess
    return name


_BATCH_RE = re.compile(r"\[(\d+)-(\d+)\]|\[([a-zA-Z])-([a-zA-Z])\]")


def expand_batch_pattern(url, limit=10000):
    """Expand one [1-10], [01-10] or [a-z] pattern in a URL into many URLs."""
    m = _BATCH_RE.search(url)
    if not m:
        return [url]
    pre, post = url[: m.start()], url[m.end():]
    if m.group(1) is not None:
        a, b = m.group(1), m.group(2)
        width = len(a) if a.startswith("0") and len(a) > 1 else 0
        lo, hi = int(a), int(b)
        step = 1 if hi >= lo else -1
        values = [str(i).zfill(width) for i in range(lo, hi + step, step)]
    else:
        lo, hi = ord(m.group(3)), ord(m.group(4))
        step = 1 if hi >= lo else -1
        values = [chr(i) for i in range(lo, hi + step, step)]
    return [pre + v + post for v in values[:limit]]


def unique_path(path):
    """Return path, or 'name (1).ext', 'name (2).ext' ... if it already exists."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root} ({i}){ext}"):
        i += 1
    return f"{root} ({i}){ext}"


def human_size(n):
    if n is None or n < 0:
        return "Unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{n} B"


def human_speed(bps):
    if not bps:
        return ""
    return human_size(bps) + "/s"


def human_time(seconds):
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def open_path(path):
    """Open a file or folder with the OS default handler."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 - user requested action
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_in_folder(path):
    if sys.platform.startswith("win") and os.path.exists(path):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        open_path(os.path.dirname(path) if os.path.isfile(path) else path)
