import json
import tempfile
import unittest
from pathlib import Path

from core import desktop_queue as queue


class QueueStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.a = self.root / "a.mp4"
        self.b = self.root / "b.mp4"
        self.a.write_bytes(b"x")
        self.b.write_bytes(b"y")

    def tearDown(self):
        self.temp.cleanup()

    def test_add_and_order(self):
        queue.add(self.a, {"clips": 3}, self.root)
        queue.add(self.b, {"clips": 5}, self.root)
        items = queue.load(self.root)["items"]
        self.assertEqual([Path(item["source"]).name for item in items], ["a.mp4", "b.mp4"])
        self.assertEqual(items[0]["state"], "pending")
        self.assertEqual(queue.next_pending(self.root)["source"], items[0]["source"])

    def test_duplicate_and_missing_are_rejected(self):
        queue.add(self.a, {}, self.root)
        with self.assertRaises(ValueError):
            queue.add(self.a, {}, self.root)
        with self.assertRaises(ValueError):
            queue.add(self.root / "missing.mp4", {}, self.root)

    def test_update_remove_and_clear(self):
        first = queue.add(self.a, {}, self.root)
        second = queue.add(self.b, {}, self.root)
        queue.update(first["id"], self.root, state="done", folder=str(self.root / "p1"))
        self.assertEqual(queue.load(self.root)["items"][0]["state"], "done")
        self.assertEqual(queue.summary(self.root)["done"], 1)
        self.assertEqual(queue.summary(self.root)["pending"], 1)
        queue.clear_finished(self.root)
        self.assertEqual([item["id"] for item in queue.load(self.root)["items"]], [second["id"]])
        queue.remove(second["id"], self.root)
        self.assertEqual(queue.load(self.root)["items"], [])

    def test_recover_running_items(self):
        item = queue.add(self.a, {}, self.root)
        queue.update(item["id"], self.root, state="running")
        queue.recover(self.root)
        self.assertEqual(queue.load(self.root)["items"][0]["state"], "pending")

    def test_reset_failed_returns_items_to_pending(self):
        first = queue.add(self.a, {}, self.root)
        second = queue.add(self.b, {}, self.root)
        queue.update(first["id"], self.root, state="failed", error="boom")
        queue.update(second["id"], self.root, state="done")
        self.assertEqual(queue.reset_failed(self.root), 1)
        items = {item["id"]: item for item in queue.load(self.root)["items"]}
        self.assertEqual(items[first["id"]]["state"], "pending")
        self.assertEqual(items[first["id"]]["error"], "")
        self.assertEqual(items[second["id"]]["state"], "done")
        self.assertEqual(queue.reset_failed(self.root), 0)

    def test_corrupt_file_falls_back(self):
        self.assertEqual(queue.load(self.root)["items"], [])
        queue.path(self.root).write_text("{broken", encoding="utf-8")
        self.assertEqual(queue.load(self.root)["items"], [])
        queue.path(self.root).write_text(json.dumps({"items": "nope"}), encoding="utf-8")
        self.assertEqual(queue.load(self.root)["items"], [])

    def test_settings_are_stored_per_item(self):
        queue.add(self.a, {"mode": "highlights", "clips": 4}, self.root)
        item = queue.load(self.root)["items"][0]
        self.assertEqual(item["settings"], {"mode": "highlights", "clips": 4})


if __name__ == "__main__":
    unittest.main()
