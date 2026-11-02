import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swiftget import engine, video  # noqa: E402
from swiftget.config import Settings  # noqa: E402

CLIP = bytes(range(256)) * 4096 * 3  # 3 MB fake "video"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/watch"):
            body = (b'<html><head><title>My Holiday Clip</title></head><body>'
                    b'<video src="/media/clip.mp4" width="640" height="360"></video></body></html>')
            ctype = "text/html"
        elif self.path.startswith("/media/slow.mp4"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(CLIP)))
            self.end_headers()
            for i in range(0, len(CLIP), 64 * 1024):
                try:
                    self.wfile.write(CLIP[i:i + 64 * 1024])
                except OSError:
                    return
                time.sleep(self.server.delay)
            return
        elif self.path.startswith("/media/clip.mp4"):
            body, ctype = CLIP, "video/mp4"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SummarizeTests(unittest.TestCase):
    def test_summarize_youtube_like(self):
        info = {"title": "Song", "formats": [
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "ext": "m4a", "abr": 128, "filesize": 4_000_000},
            {"format_id": "251", "vcodec": "none", "acodec": "opus", "ext": "webm", "abr": 160, "filesize": 4_500_000},
            {"format_id": "137", "vcodec": "avc1", "acodec": "none", "ext": "mp4", "height": 1080, "fps": 30,
             "filesize": 90_000_000},
            {"format_id": "248", "vcodec": "vp9", "acodec": "none", "ext": "webm", "height": 1080, "fps": 30,
             "filesize": 70_000_000},
            {"format_id": "298", "vcodec": "avc1", "acodec": "none", "ext": "mp4", "height": 720, "fps": 60,
             "filesize_approx": 50_000_000},
            {"format_id": "18", "vcodec": "avc1", "acodec": "mp4a", "ext": "mp4", "height": 360, "filesize": 20_000_000},
        ]}
        s = video.summarize(info)
        self.assertEqual(s["title"], "Song")
        labels = [(q["label"], q["detail"]) for q in s["qualities"]]
        self.assertEqual(labels[:3], [("1080p", "MP4"), ("720p60", "MP4"), ("360p", "MP4")])
        self.assertEqual(s["qualities"][0]["size"], 94_000_000)   # mp4 video + m4a audio
        self.assertEqual(s["qualities"][2]["size"], 20_000_000)   # already has audio
        self.assertIn(("Audio only", "M4A"), labels)
        self.assertIn(("Audio only", "MP3"), labels)

    def test_is_video_page(self):
        self.assertTrue(video.is_video_page("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(video.is_video_page("https://youtu.be/abc"))
        self.assertTrue(video.is_video_page("https://m.facebook.com/watch/?v=1"))
        self.assertFalse(video.is_video_page("https://example.com/file.zip"))
        self.assertFalse(video.is_video_page("https://notyoutube.com/x"))

    def test_format_args(self):
        self.assertIn("bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/b",
                      video.format_args("720"))
        self.assertIn("-x", video.format_args("mp3"))


class VideoDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = Settings(os.path.join(self.tmp, "s.json"))
        self.settings["download_dir"] = os.path.join(self.tmp, "dl")
        self.settings["proxy_mode"] = "none"
        self.manager = engine.DownloadManager(self.settings, data_dir=self.tmp)
        self.manager._video_tools = video.VideoTools(self.manager, folder=os.path.join(self.tmp, "tools"))
        if not self.manager.video_tools.ytdlp_cmd():
            self.skipTest("yt-dlp is not installed here")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.delay = 0.1
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    def tearDown(self):
        self.manager.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_info_and_download_from_page(self):
        info = video.get_info(self.manager.video_tools, self.base + "/watch?v=1")
        self.assertTrue(info["qualities"])
        d = self.manager.add_video(self.base + "/watch?v=1", quality=info["qualities"][0]["id"],
                                   title=info["title"])
        for _ in range(600):
            if d.status in (engine.COMPLETED, engine.ERROR) and not d.is_active:
                break
            time.sleep(0.1)
        self.assertEqual(d.status, engine.COMPLETED, d.error)
        self.assertTrue(os.path.exists(d.saved_path))
        with open(d.saved_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), hashlib.sha256(CLIP).hexdigest())
        self.assertEqual(d.size, len(CLIP))
        self.assertEqual(d.category, "Video")
        # saved and loaded again as a video item
        self.manager.save()
        m2 = engine.DownloadManager(self.settings, data_dir=self.tmp)
        d2 = m2.get(d.id)
        self.assertIsInstance(d2, video.VideoDownload)
        self.assertEqual(d2.saved_path, d.saved_path)

    def wait_done(self, d, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            self.manager.tick()
            if d.status in (engine.COMPLETED, engine.ERROR) and not d.is_active:
                return
            time.sleep(0.1)

    def test_pause_and_resume_video(self):
        d = self.manager.add_video(self.base + "/media/slow.mp4", title="slow clip")
        end = time.time() + 30
        while d.downloaded < 256 * 1024 and time.time() < end:
            time.sleep(0.1)
        self.assertGreater(d.downloaded, 0)
        self.manager.pause(d)
        end = time.time() + 15
        while d.is_active and time.time() < end:
            time.sleep(0.1)
        self.assertEqual(d.status, engine.PAUSED)
        self.httpd.delay = 0
        self.manager.resume(d)
        self.wait_done(d)
        self.assertEqual(d.status, engine.COMPLETED, d.error)
        with open(d.saved_path, "rb") as f:
            self.assertEqual(f.read(), CLIP)

    def test_bad_page_reports_error(self):
        d = self.manager.add_video(self.base + "/missing", title="x")
        for _ in range(600):
            if d.status in (engine.COMPLETED, engine.ERROR) and not d.is_active:
                break
            time.sleep(0.1)
        self.assertEqual(d.status, engine.ERROR)
        self.assertTrue(d.error)


if __name__ == "__main__":
    unittest.main()
