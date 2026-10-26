import http.server
import threading
import unittest
import urllib.error
import urllib.request

from core.streaming import StreamProxy


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """Upstream that honours Range and records the headers it received."""

    protocol_version = "HTTP/1.1"
    payload = b""
    seen_headers: list[dict] = []

    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802
        type(self).seen_headers.append(dict(self.headers))
        data = type(self).payload
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            start, _, end = header[6:].partition("-")
            start = int(start or 0)
            end = int(end) if end else len(data) - 1
            body = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StreamProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _RangeHandler.payload = bytes(range(256)) * 4096  # 1 MiB
        cls.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        cls.upstream.daemon_threads = True
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        cls.upstream_url = f"http://127.0.0.1:{cls.upstream.server_address[1]}/video.mp4"

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def test_full_body_and_header_injection(self):
        _RangeHandler.seen_headers.clear()
        proxy = StreamProxy(self.upstream_url, {"User-Agent": "lvc-test", "Referer": "https://site/"})
        url = proxy.start()
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(len(body), len(_RangeHandler.payload))
                self.assertEqual(body[:16], _RangeHandler.payload[:16])
        finally:
            proxy.stop()
        self.assertTrue(_RangeHandler.seen_headers)
        headers = _RangeHandler.seen_headers[0]
        self.assertEqual(headers.get("User-Agent"), "lvc-test")
        self.assertEqual(headers.get("Referer"), "https://site/")

    def test_range_request_is_forwarded(self):
        proxy = StreamProxy(self.upstream_url)
        url = proxy.start()
        try:
            request = urllib.request.Request(url, headers={"Range": "bytes=100-1123"})
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read()
                self.assertEqual(response.status, 206)
                self.assertEqual(response.headers["Content-Range"],
                                 f"bytes 100-1123/{len(_RangeHandler.payload)}")
                self.assertEqual(body, _RangeHandler.payload[100:1124])
        finally:
            proxy.stop()

    def test_wrong_token_is_refused(self):
        proxy = StreamProxy(self.upstream_url)
        url = proxy.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url.replace(proxy.token, "wrong-token"), timeout=10)
            self.assertEqual(caught.exception.code, 404)
        finally:
            proxy.stop()

    def test_stop_is_idempotent(self):
        proxy = StreamProxy(self.upstream_url)
        proxy.start()
        proxy.stop()
        proxy.stop()
        self.assertIsNone(proxy._server)


if __name__ == "__main__":
    unittest.main()
