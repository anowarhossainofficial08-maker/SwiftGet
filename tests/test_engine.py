import hashlib
import json
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swiftget import engine  # noqa: E402
from swiftget.config import Settings  # noqa: E402
from swiftget.utils import expand_batch_pattern, filename_from_headers, human_size, sanitize_filename  # noqa: E402

DATA = random.Random(42).randbytes(6 * 1024 * 1024 + 12345)
SHA = hashlib.sha256(DATA).hexdigest()


class FileHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.requests += 1
        if self.path.startswith("/missing"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rng = self.headers.get("Range")
        start, end = 0, len(DATA) - 1
        if rng and srv.ranges:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), len(DATA) - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(end - start + 1))
        if self.path.startswith("/cd"):
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.bin")
        self.end_headers()
        pos = start
        sent = 0
        while pos <= end:
            n = min(32 * 1024, end - pos + 1)
            try:
                self.wfile.write(DATA[pos:pos + n])
            except (BrokenPipeError, ConnectionResetError):
                return
            pos += n
            sent += n
            if srv.flaky and sent > 200 * 1024 and random.random() < 0.05:
                self.close_connection = True
                return  # drop the connection mid-transfer
            if srv.delay:
                time.sleep(srv.delay)


class TestServer:
    def __init__(self, ranges=True, flaky=False, delay=0.0):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FileHandler)
        self.httpd.daemon_threads = True
        self.httpd.ranges = ranges
        self.httpd.flaky = flaky
        self.httpd.delay = delay
        self.httpd.requests = 0
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path="/file.bin"):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def wait_for(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = Settings(os.path.join(self.tmp, "settings.json"))
        self.settings["download_dir"] = os.path.join(self.tmp, "dl")
        self.settings["proxy_mode"] = "none"
        self.settings["max_retries"] = 20
        self.manager = engine.DownloadManager(self.settings, data_dir=self.tmp)
        self.manager.apply_settings()
        self.servers = []

    def tearDown(self):
        self.manager.shutdown()
        for s in self.servers:
            s.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def server(self, **kw):
        s = TestServer(**kw)
        self.servers.append(s)
        return s

    def assert_file_ok(self, d):
        self.assertEqual(d.status, engine.COMPLETED, d.error)
        with open(d.saved_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), SHA)
        self.assertFalse(os.path.exists(d.saved_path + ".swg"))

    def finished(self, d):
        return lambda: d.status in (engine.COMPLETED, engine.ERROR, engine.PAUSED) and not d.is_active

    def test_segmented_download(self):
        srv = self.server()
        d = self.manager.add(srv.url(), connections=8)
        self.assertTrue(wait_for(self.finished(d)))
        self.assert_file_ok(d)
        self.assertEqual(d.filename, "file.bin")
        self.assertEqual(d.category, "Other")
        self.assertTrue(d.resumable)
        self.assertGreaterEqual(srv.httpd.requests, 8)

    def test_no_range_support_single_connection(self):
        srv = self.server(ranges=False)
        d = self.manager.add(srv.url(), connections=8)
        self.assertTrue(wait_for(self.finished(d)))
        self.assert_file_ok(d)
        self.assertFalse(d.resumable)

    def test_pause_and_resume(self):
        srv = self.server(delay=0.004)
        d = self.manager.add(srv.url(), connections=4)
        self.assertTrue(wait_for(lambda: d.downloaded > 512 * 1024))
        self.manager.pause(d)
        self.assertTrue(wait_for(lambda: not d.is_active))
        self.assertEqual(d.status, engine.PAUSED)
        got = d.downloaded
        self.assertGreater(got, 0)
        self.assertLess(got, len(DATA))
        srv.httpd.delay = 0
        self.manager.resume(d)
        self.assertTrue(wait_for(self.finished(d)))
        self.assert_file_ok(d)

    def test_quick_pause_resume_cycles(self):
        # Pause and resume again right away, several times, while connections are mid-read.
        srv = self.server(delay=0.05)
        d = self.manager.add(srv.url(), connections=6)
        self.assertTrue(wait_for(lambda: d.downloaded > 256 * 1024))
        for _ in range(4):
            self.manager.pause(d)
            time.sleep(0.05)
            self.manager.resume(d)
            time.sleep(0.4)
        srv.httpd.delay = 0
        for _ in range(600):
            self.manager.tick()
            if d.status in (engine.COMPLETED, engine.ERROR) and not d.is_active:
                break
            time.sleep(0.05)
        self.assert_file_ok(d)

    def test_stalled_connection_then_resume(self):
        # A connection is stuck in a slow read when the user pauses and resumes.
        srv = self.server(delay=1.5)
        d = self.manager.add(srv.url(), connections=4)
        self.assertTrue(wait_for(lambda: d.downloaded > 0))
        self.manager.pause(d)
        self.assertTrue(wait_for(lambda: not d._thread.is_alive(), timeout=20))
        srv.httpd.delay = 0
        self.manager.resume(d)
        for _ in range(600):
            self.manager.tick()
            if d.status in (engine.COMPLETED, engine.ERROR) and not d.is_active:
                break
            time.sleep(0.05)
        time.sleep(2)  # let any old connection finish its read
        self.assert_file_ok(d)

    def test_same_file_twice_at_once(self):
        srv = self.server(delay=0.002)
        d1 = self.manager.add(srv.url(), connections=4)
        d2 = self.manager.add(srv.url(), connections=4)
        self.assertTrue(wait_for(self.finished(d1)))
        self.assertTrue(wait_for(self.finished(d2)))
        self.assert_file_ok(d1)
        self.assert_file_ok(d2)
        self.assertNotEqual(d1.saved_path, d2.saved_path)

    def test_delete_while_downloading(self):
        srv = self.server(delay=0.3)
        d = self.manager.add(srv.url(), connections=4)
        self.assertTrue(wait_for(lambda: d.downloaded > 0))
        part = d.part_path
        self.assertTrue(os.path.exists(part))
        t0 = time.time()
        cleanup = self.manager.remove(d, delete_file=True)
        self.assertLess(time.time() - t0, 0.5)  # the window must not freeze
        self.assertIsNone(self.manager.get(d.id))
        cleanup.join(40)
        self.assertFalse(os.path.exists(part))

    def test_resume_after_restart(self):
        srv = self.server(delay=0.004)
        d = self.manager.add(srv.url(), connections=4)
        self.assertTrue(wait_for(lambda: d.downloaded > 512 * 1024))
        self.manager.shutdown()  # simulates closing the program
        with open(self.manager.data_path) as f:
            saved = json.load(f)["downloads"][0]
        self.assertEqual(saved["status"], engine.PAUSED)
        self.assertTrue(saved["segments"])

        srv.httpd.delay = 0
        manager2 = engine.DownloadManager(self.settings, data_dir=self.tmp)
        d2 = manager2.get(d.id)
        self.assertIsNotNone(d2)
        before = d2.downloaded
        self.assertGreater(before, 0)
        manager2.resume(d2)
        self.assertTrue(wait_for(self.finished(d2)))
        self.assert_file_ok(d2)
        manager2.shutdown()

    def test_flaky_server_retries(self):
        srv = self.server(flaky=True)
        d = self.manager.add(srv.url(), connections=6)
        self.assertTrue(wait_for(self.finished(d), timeout=120))
        self.assert_file_ok(d)

    def test_content_disposition_and_category_folder(self):
        srv = self.server()
        d = self.manager.add(srv.url("/cd?id=5"))
        self.assertTrue(wait_for(self.finished(d)))
        self.assert_file_ok(d)
        self.assertEqual(d.filename, "résumé.bin")
        self.assertEqual(os.path.dirname(d.saved_path), os.path.join(self.tmp, "dl", "Other"))

    def test_duplicate_name_gets_number(self):
        srv = self.server()
        d1 = self.manager.add(srv.url())
        self.assertTrue(wait_for(self.finished(d1)))
        d2 = self.manager.add(srv.url())
        self.assertTrue(wait_for(self.finished(d2)))
        self.assert_file_ok(d2)
        self.assertEqual(d2.filename, "file (1).bin")

    def test_http_error(self):
        srv = self.server()
        d = self.manager.add(srv.url("/missing.zip"))
        self.assertTrue(wait_for(self.finished(d)))
        self.assertEqual(d.status, engine.ERROR)
        self.assertIn("404", d.error)

    def test_queue_respects_limit(self):
        srv = self.server(delay=0.002)
        self.settings["max_simultaneous"] = 2
        items = [self.manager.add(srv.url(f"/f{i}.bin"), start=False) for i in range(4)]
        self.manager.start_queue()
        self.manager.tick()
        self.assertEqual(sum(1 for d in items if d.is_active), 2)
        self.assertEqual(sum(1 for d in items if d.status == engine.QUEUED), 2)
        srv.httpd.delay = 0
        for _ in range(400):
            self.manager.tick()
            if all(d.status == engine.COMPLETED for d in items):
                break
            time.sleep(0.05)
        for d in items:
            self.assert_file_ok(d)

    def test_speed_limit(self):
        srv = self.server()
        self.settings["speed_limit_kb"] = 2048  # 2 MB/s
        self.manager.apply_settings()
        t0 = time.time()
        d = self.manager.add(srv.url(), connections=4)
        self.assertTrue(wait_for(self.finished(d)))
        self.assert_file_ok(d)
        self.assertGreater(time.time() - t0, 2.0)  # 6 MB at 2 MB/s ~ 3 s (1 s burst allowed)

    def test_probe(self):
        srv = self.server()
        info = self.manager.probe(srv.url("/cd"))
        self.assertEqual(info.size, len(DATA))
        self.assertTrue(info.resumable)
        self.assertEqual(info.filename, "résumé.bin")


class UtilTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize_filename('a<b>:c?.txt'), "a_b__c_.txt")
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_filename(""), "download")

    def test_filename_from_headers(self):
        self.assertEqual(filename_from_headers({"Content-Disposition": 'attachment; filename="x y.zip"'},
                                               "http://h/dl?id=1"), "x y.zip")
        self.assertEqual(filename_from_headers({}, "http://h/a/b%20c.pdf?x=1"), "b c.pdf")
        self.assertEqual(filename_from_headers({}, "http://h/get", "http://cdn/real.iso"), "real.iso")

    def test_batch_pattern(self):
        self.assertEqual(expand_batch_pattern("http://h/f[1-3].jpg"),
                         ["http://h/f1.jpg", "http://h/f2.jpg", "http://h/f3.jpg"])
        self.assertEqual(expand_batch_pattern("http://h/f[08-10].jpg"),
                         ["http://h/f08.jpg", "http://h/f09.jpg", "http://h/f10.jpg"])
        self.assertEqual(expand_batch_pattern("http://h/p[a-c].zip")[2], "http://h/pc.zip")
        self.assertEqual(expand_batch_pattern("http://h/x.zip"), ["http://h/x.zip"])

    def test_human_size(self):
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(1536), "1.50 KB")
        self.assertEqual(human_size(-1), "Unknown")


class ServerTests(unittest.TestCase):
    def test_integration_server(self):
        from swiftget.server import IntegrationServer

        tmp = tempfile.mkdtemp()
        try:
            settings = Settings(os.path.join(tmp, "s.json"))

            class App:
                pass

            app = App()
            app.settings = settings
            app.manager = engine.DownloadManager(settings, data_dir=tmp)
            srv = IntegrationServer(app, 0)
            self.assertTrue(srv.start())
            port = srv.httpd.server_address[1]
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/ping") as r:
                self.assertEqual(json.loads(r.read())["app"], "SwiftGet")
            body = json.dumps({"url": "https://example.com/a.zip"}).encode()
            # without the custom header -> rejected
            req = urllib.request.Request(f"http://127.0.0.1:{port}/download", data=body, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                opener.open(req)
            self.assertEqual(ctx.exception.code, 403)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/download", data=body, method="POST",
                                         headers={"X-SwiftGet": "1"})
            with opener.open(req) as r:
                self.assertTrue(json.loads(r.read())["ok"])
            kind, payload = app.manager.events.get(timeout=2)
            self.assertEqual(kind, "browser_add")
            self.assertEqual(payload["url"], "https://example.com/a.zip")
            # switched off in Settings -> refused, so the browser downloads the file itself
            settings["browser_integration"] = False
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                opener.open(req)
            self.assertEqual(ctx.exception.code, 403)
            with opener.open(f"http://127.0.0.1:{port}/config") as r:
                self.assertFalse(json.loads(r.read())["enabled"])
            srv.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
