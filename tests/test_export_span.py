# -*- coding: utf-8 -*-
"""حدود التوقيت: التقريب في الواجهة لا يجوز أن يمنع التصدير."""

import unittest

from core.desktop_pipeline import TIME_EPSILON, clamped_span, clips_from_rows, span_ok

DURATION = 8427.77542


def _snapshot(clips=None, duration=DURATION):
    return {
        "duration": duration,
        "final_clips": clips if clips is not None else [
            {"index": 94, "start": 8338.118234680851, "end": DURATION,
             "score": 0.0, "reason": "Complete source coverage", "text": "",
             "title": "", "output_path": None},
            {"index": 1, "start": 0.0, "end": 89.66,
             "score": 0.5, "reason": "r", "text": "t", "title": "", "output_path": None},
        ],
    }


def _row(index, start, end, selected=True, title="", focus="largest"):
    return {"index": index, "start": start, "end": end,
            "selected": selected, "title": title, "focus": focus}


class SpanTests(unittest.TestCase):
    def test_spinbox_rounding_is_accepted(self):
        # 8427.77542 يُعرض في QDoubleSpinBox كـ 8427.78
        self.assertTrue(span_ok(8338.118234680851, 8427.78, DURATION))

    def test_far_beyond_duration_is_rejected(self):
        self.assertFalse(span_ok(0.0, DURATION + 0.125, DURATION))
        self.assertFalse(span_ok(0.0, DURATION + TIME_EPSILON + 0.001, DURATION))

    def test_basic_bounds(self):
        self.assertTrue(span_ok(0.0, 10.0, 10.0))
        self.assertFalse(span_ok(10.0, 10.0, 10.0))
        self.assertFalse(span_ok(11.0, 12.0, 10.0))
        self.assertFalse(span_ok(-1.0, 5.0, 10.0))

    def test_clamp_keeps_end_inside_video(self):
        start, end = clamped_span(8338.118234680851, 8427.78, DURATION)
        self.assertAlmostEqual(start, 8338.118234680851)
        self.assertEqual(end, DURATION)

    def test_clamp_handles_negative_start(self):
        self.assertEqual(clamped_span(-3.0, 5.0, 10.0), (0.0, 5.0))


class ClipsFromRowsTests(unittest.TestCase):
    def test_selected_row_ending_past_duration_is_clamped(self):
        clips, titles, focus = clips_from_rows(
            _snapshot(), [_row(94, 8338.118234680851, 8427.78, title="آخر مقطع", focus="center")])
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].end, DURATION)
        self.assertEqual(titles, {"94": "آخر مقطع"})
        self.assertEqual(focus, {"94": "center"})

    def test_unselected_rows_are_skipped(self):
        clips, titles, _ = clips_from_rows(_snapshot(), [
            _row(94, 8338.118234680851, 8427.78, selected=False),
            _row(1, 0.0, 89.66, title="أول مقطع"),
        ])
        self.assertEqual([c.index for c in clips], [1])
        self.assertEqual(titles, {"1": "أول مقطع"})

    def test_unknown_index_raises(self):
        with self.assertRaises(ValueError):
            clips_from_rows(_snapshot(), [_row(999, 0.0, 5.0)])

    def test_reversed_span_raises(self):
        with self.assertRaises(ValueError):
            clips_from_rows(_snapshot(), [_row(1, 50.0, 10.0)])

    def test_nothing_selected_raises(self):
        with self.assertRaises(ValueError):
            clips_from_rows(_snapshot(), [_row(1, 0.0, 89.66, selected=False)])


if __name__ == "__main__":
    unittest.main()
