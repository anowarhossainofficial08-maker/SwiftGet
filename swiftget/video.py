"""Video downloads ("Download this video"), powered by yt-dlp.

* VideoTools finds yt-dlp, ffmpeg and deno. On Windows they are downloaded once into
  %APPDATA%\\SwiftGet\\tools the first time a video is requested (keeps the installer
  small, and yt-dlp can update itself when sites change).
* get_info() lists the available qualities of a video page.
* VideoDownload is a download-list item that runs yt-dlp and reports progress the same
  way normal downloads do, so the list, progress window, pause/resume and queue just work.
"""

import collections
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile

from .config import app_data_dir
from .engine import (ACTIVE_STATES, COMPLETED, CONNECTING, DOWNLOADING, ERROR, QUEUED, Download, DownloadError,
                     _Stopped, friendly_error)
from .utils import sanitize_filename

IS_WINDOWS = sys.platform.startswith("win")
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW: no console flashing

VIDEO_SITES = (
    "youtube.com", "youtu.be", "youtube-nocookie.com", "vimeo.com", "dailymotion.com", "facebook.com",
    "fb.watch", "instagram.com", "twitter.com", "x.com", "tiktok.com", "twitch.tv", "bilibili.com",
    "reddit.com", "soundcloud.com", "bandcamp.com", "ted.com", "streamable.com",
)

TOOL_DOWNLOADS = [
    # (name shown to the user, url, files to keep from it)
    ("yt-dlp", "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe", ["yt-dlp.exe"]),
    ("deno", "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
     ["deno.exe"]),
    ("ffmpeg", "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
               "ffmpeg-master-latest-win64-gpl.zip", ["ffmpeg.exe", "ffprobe.exe"]),
]
UPDATE_EVERY = 3 * 24 * 3600  # let yt-dlp update itself every few days


def is_video_page(url):
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == s or host.endswith("." + s) for s in VIDEO_SITES)


def _kill_tree(proc):
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:  # yt-dlp.exe starts a child process; kill the whole tree
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=NO_WINDOW)
        else:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except OSError:
        pass


class VideoTools:
    def __init__(self, manager, folder=None):
        self.manager = manager
        self.dir = folder or os.path.join(app_data_dir(), "tools")
        self.state = "idle"        # idle | preparing | error
        self.progress = 0.0
        self.message = ""
        self.error = ""
        self._lock = threading.Lock()
        self._thread = None
        self._update_checked = False

    # ----- locating ---------------------------------------------------------
    def _local(self, name):
        path = os.path.join(self.dir, name + (".exe" if IS_WINDOWS else ""))
        return path if os.path.exists(path) else None

    def ytdlp_cmd(self):
        exe = self._local("yt-dlp") or shutil.which("yt-dlp")
        if exe:
            return [exe]
        if not getattr(sys, "frozen", False):
            try:
                import yt_dlp  # noqa: F401
                return [sys.executable, "-m", "yt_dlp"]
            except ImportError:
                pass
        return None

    def ffmpeg_dir(self):
        exe = self._local("ffmpeg") or shutil.which("ffmpeg")
        return os.path.dirname(exe) if exe else None

    def deno_path(self):
        return self._local("deno") or shutil.which("deno")

    def missing(self):
        out = []
        if not self.ytdlp_cmd():
            out.append("yt-dlp")
        if not self.deno_path():
            out.append("deno")
        if not self.ffmpeg_dir():
            out.append("ffmpeg")
        return out

    def ready(self):
        """yt-dlp is required; on Windows we also want deno (YouTube) and ffmpeg (HD merging)."""
        if not self.ytdlp_cmd():
            return False
        return not IS_WINDOWS or not self.missing()

    def base_args(self):
        args = list(self.ytdlp_cmd()) + ["--no-colors", "--no-warnings"]
        deno = self.deno_path()
        if deno:
            args += ["--js-runtimes", f"deno:{deno}"]
        ff = self.ffmpeg_dir()
        if ff:
            args += ["--ffmpeg-location", ff]
        return args

    def env(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PATH"] = self.dir + os.pathsep + env.get("PATH", "")
        return env

    # ----- first-time setup (Windows) ----------------------------------------
    def status(self):
        return {"ready": self.ready(), "state": self.state, "progress": round(self.progress, 1),
                "message": self.message, "error": self.error}

    def ensure_async(self):
        """Start downloading missing tools in the background (Windows); returns status()."""
        with self._lock:
            if self.ready():
                self.maybe_update()
            elif self._thread is None or not self._thread.is_alive():
                if not IS_WINDOWS:
                    self.state = "error"
                    self.error = "Video support needs yt-dlp and ffmpeg: pip install yt-dlp (and install ffmpeg)"
                else:
                    self.state, self.error, self.progress = "preparing", "", 0.0
                    self.message = "Setting up video support (first time only)…"
                    self._thread = threading.Thread(target=self._setup, name="video-tools", daemon=True)
                    self._thread.start()
        return self.status()

    def _setup(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            todo = [t for t in TOOL_DOWNLOADS if not all(os.path.exists(os.path.join(self.dir, f)) for f in t[2])]
            for i, (name, url, keep) in enumerate(todo):
                base = i * 100.0 / len(todo)
                span = 100.0 / len(todo)

                def report(frac, name=name, base=base, span=span, i=i):
                    self.progress = base + span * frac
                    self.message = (f"Setting up video support (first time only): downloading {name} "
                                    f"({i + 1}/{len(todo)})… {self.progress:.0f}%")

                tmp = os.path.join(self.dir, f".{name}.download")
                self._fetch(url, tmp, report)
                if url.endswith(".zip"):
                    with zipfile.ZipFile(tmp) as z:
                        for member in z.namelist():
                            base_name = os.path.basename(member)
                            if base_name in keep:
                                with z.open(member) as src, open(os.path.join(self.dir, base_name), "wb") as dst:
                                    shutil.copyfileobj(src, dst)
                    os.remove(tmp)
                else:
                    os.replace(tmp, os.path.join(self.dir, keep[0]))
            self.state, self.progress, self.message = "idle", 100.0, "Video support is ready"
            self._update_checked = True  # just downloaded, it is the newest
        except Exception as exc:  # noqa: BLE001
            self.state = "error"
            self.error = f"Could not set up video support: {friendly_error(exc)}"

    def _fetch(self, url, path, report):
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "SwiftGet"})
        with self.manager.opener.open(req, timeout=60) as resp, open(path, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                report(done / total if total else 0.0)

    def maybe_update(self):
        """Let yt-dlp.exe update itself (sites change often). At most once per run, every few days."""
        exe = self._local("yt-dlp")
        if self._update_checked or not exe or not IS_WINDOWS:
            return
        self._update_checked = True
        if time.time() - os.path.getmtime(exe) < UPDATE_EVERY:
            return

        def run():
            try:
                subprocess.run([exe, "-U"], capture_output=True, timeout=300, creationflags=NO_WINDOW,
                               env=self.env())
                os.utime(exe)
            except (OSError, subprocess.SubprocessError):
                pass

        threading.Thread(target=run, name="yt-dlp-update", daemon=True).start()


# ----------------------------------------------------------------- video info
_info_cache = {}


def _size(fmt):
    return fmt.get("filesize") or fmt.get("filesize_approx") or 0


def summarize(info):
    """Turn yt-dlp's JSON into a short list of choices: 1080p, 720p, …, audio."""
    formats = info.get("formats") or [info]
    duration = info.get("duration") or 0
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max(audio, key=lambda f: (f.get("ext") == "m4a", f.get("abr") or 0, _size(f)), default=None)
    audio_size = _size(best_audio) if best_audio else 0

    by_height = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") == "none":
            continue
        cur = by_height.get(h)
        key = (f.get("ext") == "mp4", f.get("fps") or 0, f.get("tbr") or 0)
        if cur is None or key > cur[0]:
            by_height[h] = (key, f)
    qualities = []
    for h in sorted(by_height, reverse=True)[:7]:
        f = by_height[h][1]
        has_audio = f.get("acodec") not in (None, "none")
        size = _size(f) + (0 if has_audio else audio_size)
        fps = f.get("fps") or 0
        label = f"{h}p" + (f"{int(round(fps))}" if fps >= 50 else "")
        qualities.append({"id": str(h), "label": label, "detail": "MP4", "size": size or -1, "kind": "video"})
    if not qualities:
        qualities.append({"id": "best", "label": "Best quality", "detail": (info.get("ext") or "video").upper(),
                          "size": _size(info) or -1, "kind": "video"})
    if best_audio:
        qualities.append({"id": "audio", "label": "Audio only", "detail": "M4A", "size": audio_size or -1,
                          "kind": "audio"})
        qualities.append({"id": "mp3", "label": "Audio only", "detail": "MP3", "size": -1, "kind": "audio"})
    return {"title": info.get("title") or "video", "duration": duration,
            "thumbnail": info.get("thumbnail") or "", "qualities": qualities}


def get_info(tools, url, timeout=120):
    cached = _info_cache.get(url)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    cmd = tools.base_args() + ["-J", "--no-playlist", url]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=tools.env(), creationflags=NO_WINDOW,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = next((line[6:].strip() for line in reversed(err) if line.startswith("ERROR:")), None)
        raise DownloadError(msg or (err[-1] if err else "Could not read this video"))
    result = summarize(json.loads(proc.stdout.decode("utf-8", "replace")))
    _info_cache[url] = (time.time(), result)
    return result


def format_args(quality):
    if quality == "mp3":
        return ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    if quality == "audio":
        return ["-f", "ba[ext=m4a]/ba/b"]
    if quality and quality.isdigit():
        h = int(quality)
        return ["-f", f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b",
                "--merge-output-format", "mp4"]
    return ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]


# ------------------------------------------------------------- download item
PROGRESS_RE = re.compile(r"^SGP (\S+) (\S+) (\S+) (\S+) (\S+)$")


def _num(text):
    try:
        return float(text)
    except ValueError:
        return None


class VideoDownload(Download):
    kind = "video"

    def __init__(self, url, manager, quality="best", title=None, folder=None, size=-1, id=None):
        self.quality = quality or "best"
        ext = {"mp3": "mp3", "audio": "m4a"}.get(self.quality, "mp4")
        label = {"mp3": "", "audio": ""}.get(self.quality, f" [{quality}p]" if str(quality).isdigit() else "")
        name = sanitize_filename(f"{(title or 'video')[:150]}{label}.{ext}")
        super().__init__(url, manager, filename=name, folder=folder, id=id)
        self.title = title or ""
        self.size = size if size and size > 0 else -1
        self.resumable = True
        self.probed = True
        self.note = ""
        self.dest_files = []

    @property
    def is_audio(self):
        return self.quality in ("audio", "mp3")

    def segment_snapshot(self):
        return []

    def temp_files(self):
        out = []
        for f in self.dest_files:
            if f != self.saved_path:
                out += [f, f + ".part", f + ".ytdl"]
        return out

    def reset(self):
        self.stop()
        self.join(10)
        for f in self.temp_files():
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass
        self.dest_files = []
        self.single_downloaded = 0
        self.saved_path = None
        self.finished_at = None
        self.error = ""

    def _run(self):
        stop = self._stop
        proc = None
        try:
            self.status = CONNECTING
            tools = self.manager.video_tools
            if not tools.ready():
                tools.ensure_async()
                while not tools.ready():
                    if stop.wait(0.5):
                        raise _Stopped()
                    if tools.state == "error":
                        raise DownloadError(tools.error)
                    self.note = tools.message
            tools.maybe_update()
            if self.folder is None:
                self.folder = self.manager.auto_folder(self.filename)
            os.makedirs(self.folder, exist_ok=True)
            path_file = os.path.join(app_data_dir(), f"video-{self.id}.path")
            if os.path.exists(path_file):
                os.remove(path_file)
            template = "%(title).150B.%(ext)s" if self.is_audio else "%(title).150B%(height& [{}p]|)s.%(ext)s"
            cmd = tools.base_args() + [
                "--no-playlist", "--newline", "--no-mtime", "--continue", "-N", "8",
                "--progress-template",
                "download:SGP %(progress.downloaded_bytes)s %(progress.total_bytes)s "
                "%(progress.total_bytes_estimate)s %(progress.speed)s %(progress.eta)s",
                "--print-to-file", "after_move:%(filepath)s", path_file,
                "-P", self.folder, "-o", template,
            ] + format_args(self.quality) + [self.url]
            self.note = "Starting…"
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=tools.env(), creationflags=NO_WINDOW, text=True, encoding="utf-8",
                                    errors="replace", bufsize=1)

            def watch():
                while proc.poll() is None:
                    if stop.wait(0.3):
                        _kill_tree(proc)
                        return

            threading.Thread(target=watch, daemon=True).start()
            tail = collections.deque(maxlen=10)
            error = None
            done_bytes = 0       # finished streams (video, then audio)
            cur_dl = cur_total = 0
            for line in proc.stdout:
                line = line.rstrip()
                m = PROGRESS_RE.match(line)
                if m:
                    dl, total, est, speed, _eta = (_num(x) for x in m.groups())
                    total = total or est or 0
                    cur_dl, cur_total = dl or 0, max(cur_total, total)
                    with self._lock:
                        self.single_downloaded = int(done_bytes + cur_dl)
                        self._bytes = self.single_downloaded
                    if cur_total:
                        self.size = int(max(self.size, done_bytes + cur_total))
                    self.speed = speed or 0.0
                    self.status = DOWNLOADING
                    self.note = ""
                    self.active_connections = 0
                    continue
                tail.append(line)
                if line.startswith("[download] Destination:"):
                    done_bytes += max(cur_dl, cur_total)
                    cur_dl = cur_total = 0
                    dest = line.split(":", 1)[1].strip()
                    if dest not in self.dest_files:
                        self.dest_files.append(dest)
                    self.note = ""
                elif "has already been downloaded" in line:
                    self.note = ""
                elif line.startswith("[Merger]"):
                    self.note = "Joining video and audio…"
                elif line.startswith("[ExtractAudio]"):
                    self.note = "Converting audio…"
                elif line.startswith("ERROR:"):
                    error = line[6:].strip()
            rc = proc.wait()
            if stop.is_set():
                raise _Stopped()
            if rc != 0:
                raise DownloadError(error or (tail[-1] if tail else f"yt-dlp stopped (code {rc})"))
            final = None
            if os.path.exists(path_file):
                with open(path_file, encoding="utf-8", errors="replace") as f:
                    lines = [x.strip() for x in f if x.strip()]
                final = lines[-1] if lines else None
                os.remove(path_file)
            if final and os.path.exists(final):
                self.saved_path = final
                self.filename = os.path.basename(final)
                self.size = os.path.getsize(final)
            self.single_downloaded = max(self.size, 0)
            self.finished_at = time.time()
            self.status = COMPLETED
        except _Stopped:
            self.status = self._stop_status
        except Exception as exc:  # noqa: BLE001
            if stop.is_set():
                self.status = self._stop_status
            else:
                self.status = ERROR
                self.error = friendly_error(exc)
        finally:
            if proc is not None:
                _kill_tree(proc)
                if proc.stdout:
                    proc.stdout.close()
            self.speed = 0.0
            self.note = ""
            if self._restart and self.status != COMPLETED:
                self._restart = False
                self.status = QUEUED
            self.manager._on_download_finished(self)

    @property
    def is_active(self):
        return self.status in ACTIVE_STATES or bool(self._thread and self._thread.is_alive())

    def to_dict(self):
        d = super().to_dict()
        d.update({"kind": "video", "quality": self.quality, "title": self.title, "dest_files": self.dest_files})
        d["segments"] = []
        return d

    @classmethod
    def from_dict(cls, data, manager):
        d = cls(data["url"], manager, quality=data.get("quality", "best"), title=data.get("title"),
                folder=data.get("folder"), size=data.get("size", -1), id=data.get("id"))
        d.filename = data.get("filename", d.filename)
        status = data.get("status")
        d.status = "Paused" if status in ACTIVE_STATES else (status or "Paused")
        d.error = data.get("error", "")
        d.added = data.get("added", time.time())
        d.finished_at = data.get("finished_at")
        d.saved_path = data.get("saved_path")
        d.dest_files = data.get("dest_files", [])
        return d
