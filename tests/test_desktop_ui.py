import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import asdict
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    QT_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the machine
    QT_AVAILABLE = False

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed")
class DesktopUiTests(unittest.TestCase):
    """Safety net for desktop_app.py: pages, language, review panel, validation."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls._argv = sys.argv
        sys.argv = ["desktop_app.py", "--smoke"]
        cls._tmp = tempfile.TemporaryDirectory()
        from core import desktop_presets

        cls._presets_file = desktop_presets.FILE
        desktop_presets.FILE = Path(cls._tmp.name) / "presets.json"
        import desktop_app

        cls.module = desktop_app
        cls.module.apply_dark_palette(cls.app)
        cls.app.setStyleSheet(cls.module.QSS)
        cls.window = cls._build_window()

    @classmethod
    def tearDownClass(cls):
        cls.window.deleteLater()
        cls.app.processEvents()
        from core import desktop_presets

        desktop_presets.FILE = cls._presets_file
        sys.argv = cls._argv
        cls._tmp.cleanup()

    @classmethod
    def _build_window(cls):
        captured = {}
        original = cls.module.run_smoke

        def capture(application, window_class):
            captured["window"] = window_class()
            return 0

        cls.module.run_smoke = capture
        try:
            cls.module.main()
        finally:
            cls.module.run_smoke = original
        window = captured["window"]
        window.show()
        cls.app.processEvents()
        return window

    def project(self):
        from config import Config
        from core.export_queue import save
        from utils.resume import video_identity

        folder = Path(tempfile.mkdtemp(prefix="lvc-uitest-", dir=self._tmp.name))
        source = folder / "source.mp4"
        source.write_bytes(b"x" * 5000)
        clips = [
            dict(index=index, start=0.5, end=2.0, score=80 - index, reason="سبب تجريبي",
                 text="نص المقطع " + str(index), title="")
            for index in (1, 2)
        ]
        save(folder / "analysis.json", dict(
            video_path=str(source), output_dir=str(folder / "output"), duration=3.0,
            final_clips=clips, segments=[], source_identity=video_identity(source),
            config=asdict(Config(video_path=str(source)))))
        return folder

    def test_every_page_exists_and_navigates(self):
        for key in ("lib", "imp", "watch", "queue", "ana", "rev", "exp", "tools"):
            self.window.go_to(key)
            self.app.processEvents()
            self.assertTrue(self.window.top_title.text().strip(), key)
            self.assertTrue(self.window.nav_buttons[key].isChecked(), key)

    def test_primary_action_is_pinned_on_the_import_page(self):
        self.window.go_to("imp")
        self.app.processEvents()
        page = self.window.import_page
        button = page.start_btn
        top = button.mapTo(page, button.rect().topLeft()).y()
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(top + button.height(), page.height())

    def test_language_switch_keeps_values(self):
        page = self.window.import_page
        page.clips_spin.setValue(9)
        self.window.ui_language.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(page.clips_spin.value(), 9)
        self.window.go_to("imp")
        self.app.processEvents()
        self.assertEqual(self.window.top_title.text(), "Import")
        self.window.ui_language.setCurrentIndex(0)
        self.app.processEvents()
        self.assertEqual(self.window.top_title.text(), "استيراد")

    def test_review_panel_follows_the_selected_clip(self):
        folder = self.project()
        self.window.open_project(folder)
        self.app.processEvents()
        page = self.window.review_page
        page.table.setCurrentCell(1, 1)
        self.app.processEvents()
        self.assertIn("2", page.detail_meta.text())
        self.assertEqual(page.excerpt.text(), "نص المقطع 2")
        self.assertIn("سبب تجريبي", page.reason_label.text())
        self.assertTrue(page.export_button.isEnabled())

    def test_invalid_timing_blocks_export(self):
        folder = self.project()
        self.window.open_project(folder)
        self.app.processEvents()
        page = self.window.review_page
        page.table.cellWidget(0, 3).setValue(0.1)
        self.app.processEvents()
        self.assertFalse(page.export_button.isEnabled())
        self.assertIn("توقيت غير صالح", page.invalid_label.text())
        page.table.cellWidget(0, 3).setValue(2.0)
        self.app.processEvents()
        self.assertTrue(page.export_button.isEnabled())

    def test_clip_order_and_undo(self):
        folder = self.project()
        self.window.open_project(folder)
        self.app.processEvents()
        page = self.window.review_page
        self.assertEqual([row["index"] for row in page.rows()], [1, 2])
        page.table.setCurrentCell(0, 1)
        page.move_selected(1)
        self.app.processEvents()
        self.assertEqual([row["index"] for row in page.rows()], [2, 1])
        page.undo()
        self.app.processEvents()
        self.assertEqual([row["index"] for row in page.rows()], [1, 2])

    def test_undo_restores_rejected_selection(self):
        folder = self.project()
        self.window.open_project(folder)
        self.app.processEvents()
        page = self.window.review_page
        page.select_all(False)
        self.app.processEvents()
        self.assertFalse(any(row["selected"] for row in page.rows()))
        page.undo()
        self.app.processEvents()
        self.assertTrue(all(row["selected"] for row in page.rows()))

    def test_watch_speed_and_seek_controls(self):
        self.window.go_to("watch")
        self.app.processEvents()
        page = self.window.watch_page
        page.speed.setCurrentIndex(page.speed.findData(1.5))
        page.apply_speed()
        self.assertAlmostEqual(page.player.playbackRate(), 1.5, places=2)
        page.speed.setCurrentIndex(page.speed.findData(1.0))
        page.apply_speed()
        self.assertAlmostEqual(page.player.playbackRate(), 1.0, places=2)
        # Without a seekable stream the skip buttons must not move the position.
        page._seekable = False
        page.nudge(10)
        self.assertEqual(page.player.position(), 0)
        # The rolling mux duration must not replace the real metadata length.
        page.info = {"duration": 1274}
        page.on_position(2000)
        self.assertEqual(page.clock.text(), "00:02 / 21:14")

    def test_download_to_watch_plays_and_cleans_up(self):
        self.window.go_to("watch")
        self.app.processEvents()
        page = self.window.watch_page
        local = Path(self._tmp.name) / "local-copy.mp4"
        local.write_bytes(b"\x00" * 64)
        from PySide6.QtMultimedia import QMediaPlayer
        with patch.object(QMediaPlayer, "play"):
            page._on_download_ready(str(local), "عنوان تجريبي")
        self.app.processEvents()
        self.assertTrue(page._seekable)
        self.assertEqual(page._local_file, str(local))
        self.assertTrue(page.back_btn.isEnabled())
        self.assertIn("نسخة محلية", page.mode_label.text())
        page.stop_stream()
        self.app.processEvents()
        self.assertFalse(local.exists())
        self.assertEqual(page._local_file, "")

    def test_preset_applies_and_marks_custom(self):
        from core import desktop_presets

        page = self.window.import_page
        page.radio_high.setChecked(True)
        page.clips_spin.setValue(6)
        desktop_presets.save_preset("اختبار", page.settings())
        page.refresh_presets("اختبار")
        page.clips_spin.setValue(11)
        self.app.processEvents()
        self.assertEqual(page.preset_combo.currentData(), "")
        page.preset_combo.setCurrentIndex(page.preset_combo.findData("اختبار"))
        self.app.processEvents()
        self.assertEqual(page.clips_spin.value(), 6)
        page.clips_spin.setValue(4)
        self.app.processEvents()
        self.assertEqual(page.preset_combo.currentData(), "")  # back to "custom"


if __name__ == "__main__":
    unittest.main()
