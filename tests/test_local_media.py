import tempfile,unittest,urllib.request
from pathlib import Path
from unittest.mock import Mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.local_media import media_url,disconnect_handler,MediaHandler

class MediaTests(unittest.TestCase):
    def test_ranges_and_directory_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            a=root/"a";b=root/"b";a.mkdir();b.mkdir()
            (a/"clip.mp4").write_bytes(b"abcdefghij")
            (b/"clip.mp4").write_bytes(b"SECOND")
            first=media_url(a/"clip.mp4");second=media_url(b/"clip.mp4")
            self.assertEqual(first,media_url(a/"clip.mp4"))
            with urllib.request.urlopen(urllib.request.Request(first,headers={"Range":"bytes=2-5"})) as response:
                self.assertEqual(response.status,206);self.assertEqual(response.read(),b"cdef")
                self.assertEqual(response.headers["Content-Range"],"bytes 2-5/10")
            with urllib.request.urlopen(second) as response:self.assertEqual(response.read(),b"SECOND")
            with urllib.request.urlopen(first) as response:self.assertEqual(response.read(),b"abcdefghij")

    def test_disconnect_during_transfer_is_benign(self):
        handler=object.__new__(MediaHandler)
        from unittest.mock import patch
        with patch("http.server.SimpleHTTPRequestHandler.handle",side_effect=ConnectionResetError(10054,"closed")):
            handler.handle()

    def test_async_handler_only_filters_known_disconnect(self):
        loop=Mock();previous=Mock();handler=disconnect_handler(previous)
        callback=Mock();callback.__qualname__="_ProactorBasePipeTransport._call_connection_lost"
        error=ConnectionResetError();error.winerror=10054
        handler(loop,{"exception":error,"handle":Mock(_callback=callback)})
        previous.assert_not_called()
        context={"exception":ValueError("real bug")}
        handler(loop,context);previous.assert_called_once_with(loop,context)
