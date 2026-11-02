"""Download engine.

* Multi-connection (segmented) downloads using HTTP Range requests.
* Dynamic segmentation: when a connection finishes its part, the biggest
  remaining part of another connection is split in half and a new
  connection takes over the second half (so all connections stay busy).
* Pause / resume, also after closing the program (state saved to JSON).
* Automatic retry with back-off, global speed limiter, download queue,
  simple scheduler.
"""

import datetime
import json
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from .config import app_data_dir
from .utils import category_for, filename_from_headers, filename_from_url, unique_path

CHUNK_SIZE = 64 * 1024
MIN_SEGMENT = 1024 * 1024      # initial parts are at least 1 MB each
MIN_SPLIT = 256 * 1024         # split a running part only if >= 2 * this remains

QUEUED = "Queued"              # waits for a free slot, then starts automatically
WAITING = "Not started"        # "download later" - starts when the queue is started
CONNECTING = "Connecting"
DOWNLOADING = "Downloading"
PAUSED = "Paused"
COMPLETED = "Completed"
ERROR = "Error"
ACTIVE_STATES = (CONNECTING, DOWNLOADING)


class DownloadError(Exception):
    pass


class _Stopped(Exception):
    pass


def friendly_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "Connection timed out"
        return f"Connection failed: {reason}"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Connection timed out"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or exc.__class__.__name__


class RateLimiter:
    """Token bucket shared by every connection (global speed limit)."""

    def __init__(self, bytes_per_sec=0):
        self._lock = threading.Lock()
        self.rate = 0
        self.tokens = 0.0
        self.last = time.monotonic()
        self.set_rate(bytes_per_sec)

    def set_rate(self, bytes_per_sec):
        with self._lock:
            self.rate = max(0, int(bytes_per_sec or 0))
            self.tokens = float(self.rate)
            self.last = time.monotonic()

    def consume(self, n, stop_event=None):
        with self._lock:
            if self.rate <= 0:
                return
            now = time.monotonic()
            self.tokens = min(float(self.rate), self.tokens + (now - self.last) * self.rate)
            self.last = now
            self.tokens -= n
            wait = -self.tokens / self.rate if self.tokens < 0 else 0
        if wait > 0:
            if stop_event is not None:
                stop_event.wait(wait)
            else:
                time.sleep(wait)


class Segment:
    """Byte range [start, end] (inclusive); pos = next byte to write."""

    __slots__ = ("start", "end", "pos", "failures", "active")

    def __init__(self, start, end, pos=None):
        self.start = start
        self.end = end
        self.pos = start if pos is None else pos
        self.failures = 0
        self.active = False

    @property
    def remaining(self):
        return max(0, self.end - self.pos + 1)

    @property
    def done(self):
        return self.pos > self.end

    def to_list(self):
        return [self.start, self.end, self.pos]

    @classmethod
    def from_list(cls, data):
        return cls(int(data[0]), int(data[1]), int(data[2]))


class Download:
    def __init__(self, url, manager, filename=None, folder=None, headers=None, connections=None, id=None):
        self.id = id or uuid.uuid4().hex[:12]
        self.url = url.strip()
        self.manager = manager
        self.filename = filename or filename_from_url(self.url)
        self.filename_locked = bool(filename)
        self.folder = folder or None
        self.headers = dict(headers or {})
        self.connections = connections
        self.size = -1
        self.resumable = False
        self.probed = False
        self.segments = []
        self.single_downloaded = 0
        self.status = WAITING
        self.error = ""
        self.added = time.time()
        self.finished_at = None
        self.saved_path = None
        self.from_queue = False
        self.part_name = None          # temp file name, unique per download
        # runtime only
        self._gen = 0                  # bumped on every start; stale connections check it
        self._restart = False          # "resume" was pressed while the old run was still stopping
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stop_status = PAUSED
        self._thread = None
        self._bytes = 0
        self._speed_t = time.monotonic()
        self._speed_b = 0
        self.speed = 0.0
        self.active_connections = 0

    # ----- info -----------------------------------------------------------
    @property
    def category(self):
        return category_for(self.filename)

    @property
    def settings(self):
        return self.manager.settings

    @property
    def path(self):
        if self.saved_path:
            return self.saved_path
        return os.path.join(self.folder or self.manager.auto_folder(self.filename), self.filename)

    @property
    def part_path(self):
        return os.path.join(self.folder, self.part_name or (self.filename + ".swg"))

    @property
    def downloaded(self):
        if self.status == COMPLETED and self.size >= 0:
            return self.size
        with self._lock:
            if self.segments:
                return sum(s.pos - s.start for s in self.segments)
        return self.single_downloaded

    @property
    def progress(self):
        if self.status == COMPLETED:
            return 100.0
        if self.size and self.size > 0:
            return min(100.0, self.downloaded * 100.0 / self.size)
        return 0.0

    @property
    def eta(self):
        if self.speed > 1 and self.size > 0:
            return (self.size - self.downloaded) / self.speed
        return None

    @property
    def is_active(self):
        return self.status in ACTIVE_STATES or bool(self._thread and self._thread.is_alive())

    def segment_snapshot(self):
        with self._lock:
            return [(s.start, s.end, s.pos, s.active) for s in self.segments]

    def temp_files(self):
        """Unfinished files on disk that belong to this download."""
        return [self.part_path] if self.folder else []

    # ----- control --------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            if self._stop.is_set():
                self._restart = True  # start again as soon as the old run has stopped
            return
        self._gen += 1
        self._stop = threading.Event()  # fresh flag: connections of an older run keep seeing theirs set
        self._stop_status = PAUSED
        self.error = ""
        self.status = CONNECTING
        self._speed_t = time.monotonic()
        self._speed_b = self._bytes
        self._thread = threading.Thread(target=self._run, name=f"dl-{self.id}", daemon=True)
        self._thread.start()

    def stop(self, new_status=PAUSED):
        self._restart = False
        self._stop_status = new_status
        self._stop.set()
        if not (self._thread and self._thread.is_alive()) and self.status != COMPLETED:
            self.status = new_status

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def reset(self):
        """Forget all progress (used by 'redownload')."""
        self.stop()
        self.join(5)
        if self.folder and os.path.exists(self.part_path):
            try:
                os.remove(self.part_path)
            except OSError:
                pass
        self.part_name = None
        self.probed = False
        self.size = -1
        self.resumable = False
        self.segments = []
        self.single_downloaded = 0
        self.saved_path = None
        self.finished_at = None
        self.error = ""

    # ----- worker thread --------------------------------------------------
    def _run(self):
        try:
            if not self.probed:
                self._probe()
            if self.folder is None:
                self.folder = self.manager.auto_folder(self.filename)
            if not self.part_name:
                self.part_name = f"{self.filename}.{self.id[:8]}.swg"
            os.makedirs(self.folder, exist_ok=True)
            if self._stop.is_set():
                raise _Stopped()
            self.status = DOWNLOADING
            if self.resumable and self.size > 0:
                self._download_segmented()
            else:
                self._download_single()
            if self._stop.is_set():
                raise _Stopped()
            self._finalize()
        except _Stopped:
            self.status = self._stop_status
        except Exception as exc:  # noqa: BLE001 - shown to the user
            if self._stop.is_set():
                self.status = self._stop_status
            else:
                self.status = ERROR
                self.error = friendly_error(exc)
        finally:
            self.speed = 0.0
            self.active_connections = 0
            if self._restart and self.status != COMPLETED:
                self._restart = False
                self.status = QUEUED  # the manager starts it again within half a second
            self.manager._on_download_finished(self)

    def _request(self, start=None, end=None):
        headers = {
            "User-Agent": self.settings["user_agent"],
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        headers.update({k: v for k, v in self.headers.items() if v})
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        req = urllib.request.Request(self.url, headers=headers)
        return self.manager.opener.open(req, timeout=float(self.settings["timeout"]))

    def _probe(self):
        self.status = CONNECTING
        try:
            resp = self._request(0, 0)
        except urllib.error.HTTPError as exc:
            if exc.code == 416:  # empty file
                self.size, self.resumable, self.probed = 0, False, True
                return
            raise
        with resp:
            hdrs = resp.headers
            if resp.status == 206:
                match = re.match(r"bytes\s+\d+-\d+/(\d+)", hdrs.get("Content-Range", ""))
                self.size = int(match.group(1)) if match else -1
                self.resumable = self.size > 0
            else:
                length = hdrs.get("Content-Length")
                self.size = int(length) if length and length.isdigit() else -1
                self.resumable = False
            if not self.filename_locked:
                self.filename = filename_from_headers(hdrs, self.url, resp.geturl())
        self.probed = True

    def _update_speed(self):
        now = time.monotonic()
        dt = now - self._speed_t
        if dt >= 0.5:
            current = self._bytes
            inst = (current - self._speed_b) / dt
            self.speed = inst if self.speed == 0 else self.speed * 0.6 + inst * 0.4
            self._speed_t, self._speed_b = now, current

    def _initial_segments(self, connections):
        count = max(1, min(connections, self.size // MIN_SEGMENT or 1))
        step = self.size // count
        segs = []
        for i in range(count):
            start = i * step
            end = self.size - 1 if i == count - 1 else (i + 1) * step - 1
            segs.append(Segment(start, end))
        return segs

    def _download_segmented(self):
        connections = max(1, int(self.connections or self.settings["max_connections"]))
        part = self.part_path
        need_fresh = not self.segments or not os.path.exists(part) or os.path.getsize(part) != self.size
        if need_fresh:
            with self._lock:
                self.segments = self._initial_segments(connections)
            with open(part, "wb") as f:
                f.truncate(self.size)
        with self._lock:
            for s in self.segments:
                s.failures = 0
                s.active = False

        max_retries = int(self.settings["max_retries"])
        last_error = [None]
        threads = []
        gen, stop = self._gen, self._stop

        def launch(seg):
            seg.active = True
            t = threading.Thread(target=self._segment_worker, args=(seg, last_error, max_retries, gen, stop),
                                 daemon=True)
            threads.append(t)
            t.start()

        while not self._stop.is_set():
            with self._lock:
                pending = [s for s in self.segments if not s.done]
                active = [s for s in pending if s.active]
                idle = [s for s in pending if not s.active]
            if not pending:
                break
            for seg in idle:
                if seg.failures > max_retries:
                    if not active:
                        raise DownloadError(friendly_error(last_error[0]) if last_error[0] else "Download failed")
                    continue
                if len(active) >= connections:
                    break
                launch(seg)
                active.append(seg)
            # dynamic segmentation - keep every connection busy
            while len(active) < connections:
                new_seg = self._split_largest()
                if new_seg is None:
                    break
                launch(new_seg)
                active.append(new_seg)
            self.active_connections = len(active)
            self._update_speed()
            self._stop.wait(0.25)

        deadline = time.monotonic() + 3  # a connection stuck in a slow read is ignored via self._gen
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))

    def _split_largest(self):
        with self._lock:
            candidates = [s for s in self.segments if s.active and not s.done]
            if not candidates:
                return None
            big = max(candidates, key=lambda s: s.remaining)
            if big.remaining < 2 * MIN_SPLIT:
                return None
            mid = big.pos + big.remaining // 2
            new_seg = Segment(mid, big.end)
            big.end = mid - 1
            self.segments.append(new_seg)
            self.segments.sort(key=lambda s: s.start)
            return new_seg

    def _segment_worker(self, seg, last_error, max_retries, gen, stop):
        """Download one part. `gen`/`stop` belong to the run that started this connection;
        if the user pauses and resumes, this connection becomes stale and must not touch
        the part any more (a new connection takes over)."""
        def current():
            return gen == self._gen and not stop.is_set()

        try:
            while current() and not seg.done:
                try:
                    with self._lock:
                        start, end = seg.pos, seg.end
                    resp = self._request(start, end)
                    with resp:
                        if resp.status != 206:
                            seg.failures = 10 ** 6
                            raise DownloadError("Server does not support resuming anymore")
                        with open(self.part_path, "r+b") as f:
                            f.seek(start)
                            while current():
                                chunk = resp.read(CHUNK_SIZE)
                                if not chunk:
                                    break
                                self.manager.limiter.consume(len(chunk), stop)
                                with self._lock:
                                    if not current():
                                        return
                                    allowed = seg.end - seg.pos + 1
                                    if allowed <= 0:
                                        break
                                    if len(chunk) > allowed:
                                        chunk = chunk[:allowed]
                                # Safe outside the lock: a split never moves seg.end
                                # below pos + 2*MIN_SPLIT, which is > CHUNK_SIZE.
                                f.write(chunk)
                                with self._lock:
                                    if gen != self._gen:
                                        return
                                    seg.pos += len(chunk)
                                    self._bytes += len(chunk)
                                    seg.failures = 0
                                if seg.done:
                                    break
                    if not seg.done and current():
                        raise DownloadError("Connection closed before the part was complete")
                except Exception as exc:  # noqa: BLE001
                    if not current():
                        break
                    last_error[0] = exc
                    if seg.failures < 10 ** 6:
                        seg.failures += 1
                    if seg.failures > max_retries:
                        break
                    stop.wait(min(1.5 ** seg.failures, 20))
        finally:
            with self._lock:
                if gen == self._gen:
                    seg.active = False

    def _download_single(self):
        self.active_connections = 1
        with self._lock:
            self.segments = []
        self.single_downloaded = 0
        attempts = 0
        max_retries = int(self.settings["max_retries"])
        while True:
            try:
                resp = self._request()
                break
            except urllib.error.HTTPError:
                raise
            except Exception:  # noqa: BLE001 - network hiccup, retry
                attempts += 1
                if attempts > max_retries or self._stop.is_set():
                    raise
                self._stop.wait(min(1.5 ** attempts, 20))
        with resp:
            if self.size < 0:
                length = resp.headers.get("Content-Length")
                if length and length.isdigit():
                    self.size = int(length)
            with open(self.part_path, "wb") as f:
                while not self._stop.is_set():
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    self.manager.limiter.consume(len(chunk), self._stop)
                    f.write(chunk)
                    with self._lock:
                        self.single_downloaded += len(chunk)
                        self._bytes += len(chunk)
                    self._update_speed()
        if self._stop.is_set():
            return
        if self.size >= 0 and self.single_downloaded < self.size:
            raise DownloadError("Connection closed before the file was complete")
        self.size = self.single_downloaded

    def _finalize(self):
        target = unique_path(os.path.join(self.folder, self.filename))
        os.replace(self.part_path, target)
        with self._lock:
            self.segments = []
        self.saved_path = target
        self.filename = os.path.basename(target)
        self.finished_at = time.time()
        self.status = COMPLETED

    # ----- persistence ----------------------------------------------------
    def to_dict(self):
        with self._lock:
            segs = [s.to_list() for s in self.segments]
        status = self.status
        if status in ACTIVE_STATES:
            status = PAUSED
        elif status == QUEUED:
            status = WAITING
        return {
            "id": self.id, "url": self.url, "filename": self.filename,
            "filename_locked": self.filename_locked, "folder": self.folder,
            "headers": self.headers, "connections": self.connections, "size": self.size,
            "resumable": self.resumable, "probed": self.probed, "segments": segs,
            "status": status, "error": self.error, "added": self.added,
            "finished_at": self.finished_at, "saved_path": self.saved_path,
            "part_name": self.part_name,
        }

    @classmethod
    def from_dict(cls, data, manager):
        d = cls(data["url"], manager, filename=data.get("filename"), folder=data.get("folder"),
                headers=data.get("headers"), connections=data.get("connections"), id=data.get("id"))
        d.filename_locked = data.get("filename_locked", False)
        d.size = data.get("size", -1)
        d.resumable = data.get("resumable", False)
        d.probed = data.get("probed", False)
        d.segments = [Segment.from_list(s) for s in data.get("segments", [])]
        status = data.get("status", PAUSED)
        d.status = PAUSED if status in ACTIVE_STATES else status
        d.error = data.get("error", "")
        d.added = data.get("added", time.time())
        d.finished_at = data.get("finished_at")
        d.saved_path = data.get("saved_path")
        # downloads saved by version 1.0.0 used "<name>.swg"
        d.part_name = data["part_name"] if "part_name" in data else data.get("filename", "download") + ".swg"
        return d


class DownloadManager:
    def __init__(self, settings, data_dir=None):
        self.settings = settings
        self.data_path = os.path.join(data_dir or app_data_dir(), "downloads.json")
        self.downloads = []
        self.events = queue.Queue()
        self.limiter = RateLimiter()
        self.opener = None
        self._lock = threading.RLock()
        self._loop_thread = None
        self._loop_stop = threading.Event()
        self._last_save = 0.0
        self._dirty = False
        self._sched_fired = set()
        self._shutdown_armed = False
        self._video_tools = None
        self.apply_settings()
        self.load()

    # ----- settings ---------------------------------------------------------
    def apply_settings(self):
        self.limiter.set_rate(int(self.settings["speed_limit_kb"] or 0) * 1024)
        handlers = []
        mode = self.settings["proxy_mode"]
        if mode == "none":
            handlers.append(urllib.request.ProxyHandler({}))
        elif mode == "manual" and self.settings["proxy"]:
            proxy = self.settings["proxy"]
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        if self.settings["verify_ssl"]:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl._create_unverified_context()  # noqa: S323 - explicit user option
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    def auto_folder(self, filename):
        base = self.settings["download_dir"]
        if self.settings["use_category_folders"]:
            return os.path.join(base, category_for(filename))
        return base

    # ----- list operations ----------------------------------------------------
    def get(self, download_id):
        with self._lock:
            for d in self.downloads:
                if d.id == download_id:
                    return d
        return None

    def snapshot(self):
        with self._lock:
            return list(self.downloads)

    def probe(self, url, headers=None):
        """Ask the server for file name / size (used by the 'Add URL' dialog)."""
        d = Download(url, self, headers=headers)
        d._probe()
        return d

    @property
    def video_tools(self):
        if self._video_tools is None:
            from .video import VideoTools
            self._video_tools = VideoTools(self)
        return self._video_tools

    def add_video(self, url, quality="best", title=None, folder=None, size=-1, start=True):
        from .video import VideoDownload
        d = VideoDownload(url, self, quality=quality, title=title, folder=folder, size=size)
        with self._lock:
            self.downloads.append(d)
        if start:
            d.start()
        else:
            d.status = WAITING
        self._mark_dirty()
        return d

    def add(self, url, filename=None, folder=None, headers=None, connections=None, start=True,
            size=-1, resumable=None):
        d = Download(url, self, filename=filename, folder=folder, headers=headers, connections=connections)
        if resumable is not None and size and size > 0:
            # info already known from the add dialog's check, skip the extra request
            d.size, d.resumable, d.probed = size, resumable, True
        with self._lock:
            self.downloads.append(d)
        if start:
            d.start()
        else:
            d.status = WAITING
        self._mark_dirty()
        return d

    def resume(self, d):
        if d.status in (PAUSED, ERROR, WAITING, QUEUED):
            d.from_queue = False
            d.start()
            self._mark_dirty()

    def pause(self, d):
        if d.status == COMPLETED:
            return
        d.from_queue = False
        d.stop(PAUSED)
        self._mark_dirty()

    def pause_all(self):
        for d in self.snapshot():
            if d.is_active or d.status == QUEUED:
                self.pause(d)

    def resume_all(self):
        for d in self.snapshot():
            if d.status in (PAUSED, ERROR):
                d.status = QUEUED
                d.from_queue = True
        self._mark_dirty()

    def start_queue(self):
        for d in self.snapshot():
            if d.status in (WAITING, PAUSED, ERROR):
                d.status = QUEUED
                d.from_queue = True
        self._mark_dirty()

    def stop_queue(self):
        for d in self.snapshot():
            if d.status == QUEUED:
                d.status = WAITING
            elif d.is_active and d.from_queue:
                d.stop(WAITING)
        self._shutdown_armed = False
        self._mark_dirty()

    def redownload(self, d):
        d.reset()
        d.from_queue = False
        d.start()
        self._mark_dirty()

    def remove(self, d, delete_file=False):
        """Remove from the list right away; stopping and deleting files happens in the
        background so the window never freezes on a slow connection."""
        was_completed = d.status == COMPLETED
        d.stop(PAUSED)
        with self._lock:
            if d in self.downloads:
                self.downloads.remove(d)
        self._mark_dirty()
        paths = []
        if delete_file:
            paths.extend(d.temp_files())
            if was_completed and d.saved_path:
                paths.append(d.saved_path)

        def cleanup():
            d.join(30)
            for p in paths:
                for _ in range(10):  # Windows: a closing connection may still hold the file briefly
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                        break
                    except OSError:
                        time.sleep(0.5)

        t = threading.Thread(target=cleanup, name=f"remove-{d.id}", daemon=True)
        t.start()
        return t

    def remove_completed(self):
        with self._lock:
            self.downloads = [d for d in self.downloads if d.status != COMPLETED]
        self._mark_dirty()

    # ----- background loop ------------------------------------------------------
    def start_loop(self):
        if self._loop_thread and self._loop_thread.is_alive():
            return
        self._loop_stop.clear()
        self._loop_thread = threading.Thread(target=self._loop, name="manager", daemon=True)
        self._loop_thread.start()

    def _loop(self):
        while not self._loop_stop.wait(0.5):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                print("manager tick error:", exc, file=sys.stderr)

    def tick(self, now=None):
        downloads = self.snapshot()
        active = sum(1 for d in downloads if d.is_active)
        limit = max(1, int(self.settings["max_simultaneous"]))
        for d in downloads:
            if active >= limit:
                break
            if d.status == QUEUED and not d.is_active:
                d.from_queue = True
                d.start()
                active += 1
        self._check_scheduler(now or datetime.datetime.now())
        if self._shutdown_armed and not any(d.is_active or d.status == QUEUED for d in self.snapshot()):
            self._shutdown_armed = False
            self.events.put(("shutdown", None))
        if self._dirty or (active and time.monotonic() - self._last_save > 2.0):
            self.save()

    def _check_scheduler(self, now):
        s = self.settings
        if not s["scheduler_enabled"]:
            return
        hm = now.strftime("%H:%M")
        key_day = now.strftime("%Y-%m-%d")
        if s["scheduler_start"] and hm == s["scheduler_start"] and ("start", key_day) not in self._sched_fired:
            self._sched_fired.add(("start", key_day))
            self.start_queue()
            self._shutdown_armed = bool(s["scheduler_shutdown"])
            self.events.put(("info", "Scheduler: queue started"))
        if s["scheduler_stop"] and hm == s["scheduler_stop"] and ("stop", key_day) not in self._sched_fired:
            self._sched_fired.add(("stop", key_day))
            self.stop_queue()
            self.events.put(("info", "Scheduler: queue stopped"))

    def _mark_dirty(self):
        self._dirty = True

    def _on_download_finished(self, d):
        self._mark_dirty()
        if d.status == COMPLETED:
            self.events.put(("completed", d.id))
        elif d.status == ERROR:
            self.events.put(("error", d.id))

    # ----- persistence ----------------------------------------------------------
    def save(self):
        with self._lock:
            data = [d.to_dict() for d in self.downloads]
            self._dirty = False
            self._last_save = time.monotonic()
        tmp = self.data_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "downloads": data}, f)
            os.replace(tmp, self.data_path)
        except OSError as exc:
            print("could not save download list:", exc, file=sys.stderr)

    def load(self):
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        items = raw.get("downloads", []) if isinstance(raw, dict) else []
        for item in items:
            try:
                if item.get("kind") == "video":
                    from .video import VideoDownload
                    self.downloads.append(VideoDownload.from_dict(item, self))
                    continue
                self.downloads.append(Download.from_dict(item, self))
            except (KeyError, TypeError, ValueError):
                continue

    def shutdown(self):
        """Stop everything (keeps progress) - called when the app closes."""
        self._loop_stop.set()
        for d in self.snapshot():
            if d.is_active:
                d.stop(PAUSED)
        for d in self.snapshot():
            d.join(5)
        self.save()


def shutdown_computer():
    if sys.platform.startswith("win"):
        subprocess.Popen(["shutdown", "/s", "/t", "60"])
    elif sys.platform == "darwin":
        subprocess.Popen(["osascript", "-e", 'tell app "System Events" to shut down'])
    else:
        subprocess.Popen(["shutdown", "-h", "+1"])


def cancel_shutdown():
    if sys.platform.startswith("win"):
        subprocess.Popen(["shutdown", "/a"])
    elif not sys.platform == "darwin":
        subprocess.Popen(["shutdown", "-c"])
