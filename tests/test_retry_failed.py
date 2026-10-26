import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from config import Config
from core.desktop_pipeline import retry_failed
from core.export_queue import read, save


class RetryFailedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source.mp4"
        self.source.write_bytes(b"x")
        self.analysis = {
            "video_path": str(self.source),
            "output_dir": str(self.folder / "output"),
            "source_identity": {"path": str(self.source), "size": 1},
            "duration": 10.0,
            "segments": [],
            "final_clips": [
                dict(index=1, start=0.5, end=2.0, score=90, reason="", text="a", title=""),
                dict(index=2, start=3.0, end=5.0, score=80, reason="", text="b", title=""),
            ],
            "config": asdict(Config(video_path=str(self.source))),
        }
        save(self.folder / "analysis.json", self.analysis)
        self.review = [
            dict(index=1, selected=True, start=0.5, end=2.0, title="محفوظ", focus="largest"),
            dict(index=2, selected=True, start=3.0, end=5.0, title="", focus="left"),
        ]
        save(self.folder / "desktop_review.json", self.review)
        save(self.folder / "export_job" / "status.json",
             {"state": "failed", "completed": [], "failed": [2], "total": 2})

    def tearDown(self):
        self.temp.cleanup()

    def test_only_failed_clips_are_relaunched(self):
        with patch("core.export_queue.launch") as launch:
            count = retry_failed(self.folder)
        self.assertEqual(count, 1)
        launch.assert_called_once()
        queue, clips, _cfg, _segments, options = launch.call_args.args
        self.assertEqual(str(queue), str(self.folder / "export_job"))
        self.assertEqual([clip.index for clip in clips], [2])
        self.assertEqual(clips[0].start, 3.0)
        self.assertEqual(options["focus"], {"2": "left"})
        self.assertFalse(options["preview"])

    def test_review_decisions_are_not_overwritten(self):
        with patch("core.export_queue.launch"):
            retry_failed(self.folder)
        saved = read(self.folder / "desktop_review.json")
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["title"], "محفوظ")

    def test_nothing_failed_is_reported(self):
        save(self.folder / "export_job" / "status.json", {"state": "finished", "failed": []})
        with patch("core.export_queue.launch") as launch:
            with self.assertRaises(ValueError):
                retry_failed(self.folder)
        launch.assert_not_called()

    def test_missing_analysis_is_reported(self):
        (self.folder / "analysis.json").unlink()
        with self.assertRaises(ValueError):
            retry_failed(self.folder)


class RetryOptionsTests(unittest.TestCase):
    def test_out_of_range_failed_index_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "s.mp4"
            source.write_bytes(b"x")
            save(folder / "analysis.json", {
                "video_path": str(source), "output_dir": str(folder / "out"),
                "source_identity": {"path": str(source)}, "duration": 4.0, "segments": [],
                "final_clips": [dict(index=1, start=0.0, end=2.0, score=1, reason="", text="", title="")],
                "config": asdict(Config(video_path=str(source))),
            })
            save(folder / "export_job" / "status.json", {"failed": [1]})
            save(folder / "desktop_review.json",
                 [dict(index=1, selected=True, start=0.0, end=9.0, title="", focus="largest")])
            with patch("core.export_queue.launch") as launch:
                with self.assertRaises(ValueError):
                    retry_failed(folder)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
