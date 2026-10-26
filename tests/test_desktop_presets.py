import json
import tempfile
import unittest
from pathlib import Path

from core import desktop_presets as presets


def settings(**overrides):
    base = {
        "mode": "cover", "part_length": 90, "clips": 12, "platform": "none",
        "captions": True, "title": False, "screening": True, "whisper_model": "medium",
        "projects_root": "", "transient_ui_flag": "should-not-be-stored",
    }
    base.update(overrides)
    return base


class PresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_keeps_only_known_keys(self):
        presets.save_preset("محاضرة", settings(), self.root)
        data = presets.load(self.root)
        self.assertIn("محاضرة", data["presets"])
        stored = data["presets"]["محاضرة"]
        self.assertEqual(stored["part_length"], 90)
        self.assertTrue(stored["captions"])
        self.assertNotIn("transient_ui_flag", stored)
        self.assertEqual(data["last_name"], "محاضرة")

    def test_invalid_names_are_rejected(self):
        for name in ("", "   ", "a" * 41, "bad/name", "bad:name"):
            with self.assertRaises(ValueError, msg=repr(name)):
                presets.save_preset(name, settings(), self.root)

    def test_delete_clears_selection(self):
        presets.save_preset("بودكاست", settings(mode="highlights"), self.root)
        presets.delete_preset("بودكاست", self.root)
        data = presets.load(self.root)
        self.assertEqual(data["presets"], {})
        self.assertEqual(data["last_name"], "")

    def test_remember_keeps_last_settings_and_valid_name(self):
        presets.save_preset("شورتس", settings(platform="shorts"), self.root)
        presets.remember(settings(clips=3), "شورتس", self.root)
        data = presets.load(self.root)
        self.assertEqual(data["last"]["clips"], 3)
        self.assertEqual(data["last_name"], "شورتس")
        # A name that no longer exists must not stay selected.
        presets.remember(settings(clips=4), "غير موجود", self.root)
        self.assertEqual(presets.load(self.root)["last_name"], "شورتس")
        presets.delete_preset("شورتس", self.root)
        presets.remember(settings(clips=5), "", self.root)
        self.assertEqual(presets.load(self.root)["last_name"], "")

    def test_corrupt_or_missing_file_falls_back(self):
        self.assertEqual(presets.load(self.root)["presets"], {})
        presets.path(self.root).write_text("{not json", encoding="utf-8")
        self.assertEqual(presets.load(self.root)["presets"], {})
        presets.path(self.root).write_text(json.dumps(["wrong"]), encoding="utf-8")
        self.assertEqual(presets.load(self.root)["presets"], {})

    def test_snapshot_is_json_safe(self):
        stored = presets.snapshot(settings())
        json.dumps(stored, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
