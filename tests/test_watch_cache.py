import tempfile
import unittest
from pathlib import Path

from core import watch_cache


class WatchCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_target_is_stable_and_unique_per_url(self):
        first = watch_cache.target_for("https://a/video", self.root)
        again = watch_cache.target_for("https://a/video", self.root)
        other = watch_cache.target_for("https://b/video", self.root)
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertTrue(first.name.endswith(".mp4"))
        self.assertEqual(first.parent, self.root)

    def test_cleanup_removes_only_old_files(self):
        old = self.root / "old.mp4"
        fresh = self.root / "fresh.mp4"
        old.write_bytes(b"x")
        fresh.write_bytes(b"y")
        import os
        os.utime(old, (1000, 1000))
        self.assertEqual(watch_cache.cleanup(max_age_seconds=60, root=self.root), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_remove_handles_missing_files(self):
        self.assertFalse(watch_cache.remove(self.root / "missing.mp4"))
        target = self.root / "clip.mp4"
        target.write_bytes(b"x")
        self.assertTrue(watch_cache.remove(target))
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
