import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import project_library


class LibraryVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "project"
        self.folder.mkdir()
        self.source = self.root / "lecture.mp4"
        self.source.write_bytes(b"x")
        self.snapshot = self.folder / "analysis.json"
        self.snapshot.write_text("{}", encoding="utf-8")
        self._patch = [
            patch.object(project_library, "INDEX", self.root / "index.json"),
            patch.object(project_library, "HIDDEN", self.root / "hidden.json"),
        ]
        for item in self._patch:
            item.start()

    def tearDown(self):
        for item in self._patch:
            item.stop()
        self.temp.cleanup()

    def register(self):
        project_library.save(project_library.INDEX, {
            "key1": {"name": "lecture.mp4", "snapshot": str(self.snapshot),
                     "source": str(self.source), "clips": 3, "work_dir": str(self.folder)}
        })

    def test_unregister_removes_entry_and_hides_it(self):
        self.register()
        self.assertTrue(project_library.projects())
        self.assertTrue(project_library.unregister(self.snapshot))
        self.assertEqual(project_library.projects(), {})
        self.assertIn(str(self.snapshot.resolve()), project_library.hidden())

    def test_files_are_untouched_and_still_protected(self):
        self.register()
        project_library.unregister(self.snapshot)
        self.assertTrue(self.snapshot.is_file())
        self.assertTrue(project_library.protects(self.folder))

    def test_unregister_by_folder_also_hides(self):
        self.register()
        self.assertTrue(project_library.unregister(self.folder))
        self.assertIn(str(self.snapshot.resolve()), [str(Path(item)) for item in project_library.hidden()])

    def test_hide_is_idempotent(self):
        project_library.hide(self.snapshot)
        project_library.hide(self.snapshot)
        self.assertEqual(len(project_library.hidden()), 1)

    def test_unknown_project_is_still_hidden(self):
        self.assertFalse(project_library.unregister(self.root / "ghost"))
        self.assertEqual(len(project_library.hidden()), 1)


if __name__ == "__main__":
    unittest.main()
