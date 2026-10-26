import unittest

from core.streaming import StreamError, classify


def combined_info(**overrides):
    info = {
        "_type": "video",
        "url": "https://cdn.example/video.mp4?token=1",
        "title": "محاضرة",
        "extractor_key": "Facebook",
        "duration": 120.0,
        "ext": "mp4",
        "acodec": "aac",
        "vcodec": "h264",
        "http_headers": {"User-Agent": "UA", "Referer": "https://site/"},
    }
    info.update(overrides)
    return info


def dash_info():
    return {
        "_type": "video",
        "title": "YouTube demo",
        "extractor_key": "Youtube",
        "duration": 60.0,
        "http_headers": {"User-Agent": "UA"},
        "requested_formats": [
            {"url": "https://cdn.example/video-only.mp4", "vcodec": "avc1", "acodec": "none"},
            {"url": "https://cdn.example/audio-only.m4a", "vcodec": "none", "acodec": "mp4a"},
        ],
    }


class StreamClassifyTests(unittest.TestCase):
    def test_single_file_plays_through_the_proxy(self):
        result = classify(combined_info())
        self.assertEqual(result["mode"], "http")
        self.assertTrue(result["url"].startswith("https://cdn.example/video.mp4"))
        self.assertTrue(result["has_audio"])
        self.assertEqual(result["headers"]["User-Agent"], "UA")
        self.assertEqual(result["site"], "Facebook")

    def test_dash_streams_are_muxed(self):
        result = classify(dash_info())
        self.assertEqual(result["mode"], "mux")
        self.assertEqual(result["video_url"], "https://cdn.example/video-only.mp4")
        self.assertEqual(result["audio_url"], "https://cdn.example/audio-only.m4a")
        self.assertEqual(result["url"], "")
        self.assertTrue(result["has_audio"])
        self.assertEqual(result["headers"]["User-Agent"], "UA")

    def test_playlist_uses_first_entry(self):
        entry = combined_info(title="الحلقة الأولى")
        result = classify({"_type": "playlist", "entries": [entry]})
        self.assertEqual(result["title"], "الحلقة الأولى")
        self.assertEqual(result["mode"], "http")

    def test_empty_playlist_is_rejected(self):
        with self.assertRaises(StreamError):
            classify({"_type": "playlist", "entries": []})

    def test_missing_url_is_rejected(self):
        with self.assertRaises(StreamError):
            classify({"_type": "video", "title": "x"})
        with self.assertRaises(StreamError):
            classify(None)

    def test_video_only_single_format_is_flagged(self):
        info = combined_info(acodec="none")
        result = classify(info)
        self.assertEqual(result["mode"], "http")
        self.assertFalse(result["has_audio"])


if __name__ == "__main__":
    unittest.main()
