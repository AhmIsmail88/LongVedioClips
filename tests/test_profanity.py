"""Profanity masking tests: whole-word safety, punctuation, .ass output.

Run:
    python tests\\test_profanity.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO_ROOT  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

from core import captions  # noqa: E402
from models.schemas import FinalClip, TranscriptSegment, Word  # noqa: E402
from utils.sanitize import load_blocklist, mask_text  # noqa: E402


class MaskingTests(unittest.TestCase):
    def setUp(self):
        self.block = load_blocklist()

    def test_blocked_word_is_masked_in_context(self):
        out = mask_text("يا بز خليك محترم", self.block)
        self.assertNotIn("بز", out)
        self.assertIn("**", out)
        self.assertIn("محترم", out)

    def test_innocent_word_with_same_letters_is_untouched(self):
        self.assertEqual(mask_text("بزاف الناس", self.block), "بزاف الناس")

    def test_punctuation_is_preserved_while_letters_are_masked(self):
        self.assertEqual(mask_text("بز!", self.block), "**!")

    def test_english_word_is_case_insensitive(self):
        self.assertNotIn("Fuck", mask_text("Fuck this", self.block))

    def test_custom_words_file_extends_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = [Path(tmp), Path(tmp) / "empty"]
            roots[1].mkdir()
            (roots[0] / "profanity.txt").write_text(
                "# comment\nكلمةمخصصة\n", encoding="utf-8"
            )
            extended = load_blocklist(roots[0])
            plain = load_blocklist(roots[1])
        self.assertIn("كلمةمخصصة", extended)
        self.assertNotIn("كلمةمخصصة", plain)
        out = mask_text("هنا كلمةمخصصة الآن", extended)
        self.assertNotIn("كلمةمخصصة", out)
        self.assertIn("الآن", out)


class AssIntegrationTests(unittest.TestCase):
    def test_ass_file_contains_no_blocked_word(self):
        segs = [
            TranscriptSegment(
                start=0.0,
                end=3.0,
                text="مرحبا بز عالم",
                words=[
                    Word(start=0.0, end=1.0, text="مرحبا"),
                    Word(start=1.0, end=2.0, text="بز"),
                    Word(start=2.0, end=3.0, text="عالم"),
                ],
            )
        ]
        clip_obj = FinalClip(
            index=1, start=0.0, end=3.0, score=80.0,
            reason="", text="مرحبا بز عالم", title="عنوان بز جذاب",
        )
        path = captions.build_ass_file(
            clip_obj, segs, 1080, 1920,
            words_per_caption=5, burn_captions=True,
            add_title_overlay=True, title_duration_seconds=3.0,
        )
        self.assertIsNotNone(path)
        try:
            content = Path(path).read_text(encoding="utf-8")
            self.assertNotIn("بز", content)
            self.assertIn("*", content)
            self.assertIn("عنوان", content)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
