import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import media_download as downloader
from core.export_queue import read,save

class DownloadTests(unittest.TestCase):
    def test_platform_detection(self):
        cases = {
            "https://www.facebook.com/watch/?v=123": "facebook",
            "https://fb.watch/abc123/": "facebook",
            "https://m.facebook.com/reel/99": "facebook",
            "https://x.com/user/status/123": "x",
            "https://twitter.com/user/status/123": "x",
            "https://www.tiktok.com/@u/video/1": "tiktok",
            "https://youtu.be/abc": "youtube",
            "https://www.instagram.com/reel/xyz/": "instagram",
            "https://cdn.example.com/clip.mp4": "direct",
            "https://example.com/page": "other",
            "not a url": "other",
        }
        for url, expected in cases.items():
            self.assertEqual(downloader.platform_of(url), expected, url)
        self.assertTrue(downloader.sign_in_required("facebook"))
        self.assertTrue(downloader.sign_in_required("x"))
        self.assertFalse(downloader.sign_in_required("youtube"))

    def test_auth_failure_message_points_to_cookies(self):
        for message in ("Sign in to confirm your age", "This video is private",
                        "Login required", "Use --cookies for this content"):
            self.assertEqual(downloader.describe_failure(RuntimeError(message)), downloader.AUTH_ERROR)
        self.assertEqual(downloader.describe_failure(RuntimeError("HTTP Error 500")), downloader.GENERIC_ERROR)

    def test_failure_classes_are_specific(self):
        cases = {
            "[twitter] 123: No video could be found in this tweet": downloader.NO_VIDEO_ERROR,
            "This tweet does not contain a video": downloader.NO_VIDEO_ERROR,
            "This video is not available in your country": downloader.GEO_ERROR,
            "Unsupported URL: https://example.com/x": downloader.UNSUPPORTED_ERROR,
            "Video has been deleted": downloader.NOT_FOUND_ERROR,
            "HTTP Error 404: Not Found": downloader.NOT_FOUND_ERROR,
        }
        for message, expected in cases.items():
            self.assertEqual(downloader.describe_failure(RuntimeError(message)), expected, message)

    def test_facebook_and_x_jobs_stay_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            fb = downloader.job_folder("https://www.facebook.com/watch/?v=1", temp, 720)
            x = downloader.job_folder("https://x.com/u/status/2", temp, 720)
            self.assertNotEqual(fb, x)
            with patch("core.media_download.subprocess.Popen"):
                folder = downloader.launch("https://x.com/u/status/2", temp, 720)
            self.assertEqual(read(folder / "request.json")["platform"], "x")

    def test_format_selector_has_soft_and_last_resort_fallbacks(self):
        selector = downloader.format_selector(360)
        self.assertIn("bv*[height<=360]+ba", selector)
        self.assertIn("/b[height<=?360]", selector)
        self.assertTrue(selector.endswith("/b"))

    def test_url_validation(self):
        for url in ("file:///c:/video.mp4","ftp://host/video.mp4","https://user:pass@host/video.mp4","wrong"):
            with self.assertRaises(ValueError):downloader.validate_url(url)
        self.assertEqual(downloader.validate_url(" https://youtu.be/test#fragment "),"https://youtu.be/test")

    def test_reject_playlist_live_and_adult(self):
        for info in ({"_type":"playlist"},{"entries":[]},{"is_live":True},{"age_limit":18}):
            with self.assertRaises(ValueError):downloader.single_video(info)
        downloader.single_video({"duration":10})

    def test_finished_download_reused_only_if_intact(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"test.mp4";path.write_bytes(b"video")
            result={"path":str(path),"size":5,"mtime_ns":path.stat().st_mtime_ns}
            folder=downloader.job_folder("https://youtu.be/test",temp,720)
            save(folder/"result.json",result)
            with patch("core.media_download.subprocess.Popen") as popen:
                downloader.launch("https://youtu.be/test",temp,720,mode="download")
                popen.assert_not_called()
            self.assertTrue(read(folder/"status.json")["reused"])
            path.write_bytes(b"changed")
            self.assertFalse(downloader.intact(result))

    def test_duplicate_running_job_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=downloader.job_folder("https://youtu.be/test",temp,720)
            save(folder/"status.json",{"state":"running","heartbeat":time.time()})
            with self.assertRaises(RuntimeError):downloader.launch("https://youtu.be/test",temp)

    def test_cancel_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            save(folder/"request.json",{"url":"https://youtu.be/test","mode":"inspect","height":720,"directory":temp})
            (folder/"cancel").touch()
            with patch("yt_dlp.YoutubeDL") as ydl:downloader.run(folder)
            ydl.assert_not_called()
            self.assertEqual(read(folder/"status.json")["state"],"cancelled")

    def test_audio_jobs_are_separate_and_ignore_video_height(self):
        with tempfile.TemporaryDirectory() as temp:
            video=downloader.job_folder('https://youtu.be/test',temp,720)
            audio=downloader.job_folder('https://youtu.be/test',temp,720,'audio')
            self.assertNotEqual(video,audio)
            self.assertEqual(audio,downloader.job_folder('https://youtu.be/test',temp,1080,'audio'))
            with patch('core.media_download.subprocess.Popen'):
                folder=downloader.launch('https://youtu.be/test',temp,media_kind='audio')
            self.assertEqual(read(folder/'request.json')['media_kind'],'audio')
            with self.assertRaises(ValueError):downloader.launch('https://youtu.be/test',temp,media_kind='invalid')
