import unittest

from core.desktop_review import crop_box


class CropBoxTests(unittest.TestCase):
    def test_full_frame_is_nine_by_sixteen(self):
        left, top, width, height = crop_box(1920, 1080)
        self.assertEqual(height, 1080)
        self.assertEqual(width, 608)
        self.assertEqual(left, 656)
        self.assertEqual(top, 0)

    def test_zoom_halves_the_box_around_the_center(self):
        left, top, wide, tall = crop_box(1920, 1080, 0.5, 2.0)
        self.assertEqual((wide, tall), (304, 540))
        self.assertEqual((left, top), (808, 270))

    def test_center_moves_and_clamps(self):
        self.assertEqual(crop_box(1920, 1080, 0.0)[0], 0)
        self.assertEqual(crop_box(1920, 1080, 1.0)[0], 1920 - 608)
        self.assertEqual(crop_box(1920, 1080, 2.5)[0], 656)
        self.assertEqual(crop_box(1920, 1080, 1.0, 3.0)[0], 1920 - 203)

    def test_wide_source_is_capped_by_width(self):
        left, top, wide, tall = crop_box(400, 1000)
        self.assertEqual((wide, tall), (400, 711))
        self.assertEqual(top, 144)
        self.assertEqual(left, 0)

    def test_invalid_size_is_rejected(self):
        with self.assertRaises(ValueError):
            crop_box(0, 1080)
