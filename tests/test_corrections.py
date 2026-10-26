import unittest

from utils.corrections import (GlossaryError, apply, hotwords_for, parse_glossary,
                               rules, summary, total)


class Segment:
    def __init__(self, text, words=None):
        self.text = text
        self.words = [Word(item) for item in (words or [])]


class Word:
    def __init__(self, text):
        self.text = text


class ParseGlossaryTests(unittest.TestCase):
    def test_accepts_both_separators_and_skips_noise(self):
        pairs = parse_glossary("""
        # تعليق
        // تعليق آخر

        المزهر => المذهب
        الوزن=الوزن الشعري
        """)
        self.assertEqual(pairs, [("المزهر", "المذهب"), ("الوزن", "الوزن الشعري")])

    def test_rejects_malformed_lines_with_line_number(self):
        with self.assertRaises(GlossaryError) as caught:
            parse_glossary("المزهر المذهب")
        self.assertIn("السطر 1", str(caught.exception))
        for bad in ("المزهر =>", "=> المذهب", "المزهر => المزهر"):
            with self.assertRaises(GlossaryError):
                parse_glossary(bad)

    def test_deduplicates_last_wins(self):
        pairs = parse_glossary("المزهر => المذهب\nالمزهر => المذهبُ")
        self.assertEqual(pairs, [("المزهر", "المذهبُ")])

    def test_empty_text_is_empty(self):
        self.assertEqual(parse_glossary(""), [])
        self.assertEqual(parse_glossary(None), [])


class HotwordsTests(unittest.TestCase):
    def test_uses_correct_side_and_extra(self):
        pairs = parse_glossary("المزهر => المذهب")
        words = hotwords_for(pairs, "ابن تيمية،  ابن القيم")
        self.assertIn("المذهب", words)
        self.assertIn("ابن تيمية", words)
        self.assertIn("ابن القيم", words)
        self.assertEqual(words.count("المذهب"), 1)

    def test_respects_length_budget(self):
        pairs = [("خطأ", "كلمة" * 40)]
        self.assertLessEqual(len(hotwords_for(pairs, limit=20)), 20)
        self.assertEqual(hotwords_for([], ""), "")


class RulesTests(unittest.TestCase):
    def test_valid_glossary_yields_pairs_and_hotwords(self):
        pairs, hotwords, error = rules("المزهر => المذهب", "ابن تيمية")
        self.assertEqual(pairs, [("المزهر", "المذهب")])
        self.assertEqual(error, "")
        self.assertIn("المذهب", hotwords)
        self.assertIn("ابن تيمية", hotwords)

    def test_invalid_glossary_does_not_block_transcription(self):
        pairs, hotwords, error = rules("سطر خاطئ بدون فاصل", "كلمة")
        self.assertEqual(pairs, [])
        self.assertIn("السطر 1", error)
        self.assertIn("كلمة", hotwords)


class ApplyTests(unittest.TestCase):
    def test_replaces_whole_words_only(self):
        pairs = parse_glossary("المزهر => المذهب")
        segments = [Segment("قال المزهر ثم المزهرة ثم المزهر.", ["المزهر", "ثم", "المزهرة"])]
        counts = apply(segments, pairs)
        self.assertEqual(segments[0].text, "قال المذهب ثم المزهرة ثم المذهب.")
        self.assertEqual([word.text for word in segments[0].words], ["المذهب", "ثم", "المزهرة"])
        self.assertEqual(total(counts), 3)
        self.assertIn("المزهر => المذهب", summary(counts))

    def test_handles_punctuation_and_latin_boundaries(self):
        pairs = parse_glossary("word => term")
        segments = [Segment("word, (word) xword word2")]
        counts = apply(segments, pairs)
        self.assertEqual(segments[0].text, "term, (term) xword word2")
        self.assertEqual(total(counts), 2)

    def test_multi_word_phrase(self):
        pairs = parse_glossary("أهل السنة => أهل السنة والجماعة")
        segments = [Segment("قال أهل السنة هنا، وأهل السنة كذلك")]
        apply(segments, pairs)
        self.assertEqual(segments[0].text, "قال أهل السنة والجماعة هنا، وأهل السنة والجماعة كذلك")

    def test_arabic_prefixes_are_preserved(self):
        pairs = parse_glossary("المزهر => المذهب")
        segments = [Segment("والمزهر وبالمزهر والمزهر")]
        apply(segments, pairs)
        self.assertEqual(segments[0].text, "والمذهب وبالمذهب والمذهب")

    def test_no_pairs_is_noop(self):
        segments = [Segment("نص")]
        self.assertEqual(apply(segments, []), {})
        self.assertEqual(segments[0].text, "نص")


if __name__ == "__main__":
    unittest.main()
