"""Structural tests for utils.docx_export (text + .docx from segments).

No dependency beyond the standard library: the .docx is opened again with
zipfile + xml.etree and checked the way a reader would see it, since there
is no Word/LibreOffice automation available here to open it for real.

Run:
    .venv\\Scripts\\python.exe tests\\test_docx_export.py
"""

from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from models.schemas import TranscriptSegment, Word  # noqa: E402
from utils.docx_export import (  # noqa: E402
    DOCX_MIME,
    W_NS,
    format_timestamp,
    segments_to_docx_bytes,
    segments_to_text,
)

ARABIC_LINE = "أهلاً بيكم في الحلقة الجديدة، النهاردة هنتكلم عن المشروع ده."
ENGLISH_LINE = "and this part is in English."
MARKUP_LINE = 'رموز لازم تطلع زي ما هي: <w:t> & "quotes" </w:t>'

SEGMENTS = [
    TranscriptSegment(
        start=0.0, end=4.2, text=ARABIC_LINE, words=[Word(0.0, 1.0, "أهلاً")]
    ),
    TranscriptSegment(start=83.0, end=90.5, text=ENGLISH_LINE, words=[]),
    TranscriptSegment(start=3661.0, end=3670.0, text=MARKUP_LINE, words=[]),
    # A textless segment must simply be skipped, not crash or leave a blank
    # line behind.
    TranscriptSegment(start=3700.0, end=3705.0, text="   ", words=[]),
]


def _read_parts(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _document_text(data: bytes) -> str:
    """All <w:t> text of word/document.xml, as a reader would collect it."""
    root = ElementTree.fromstring(_read_parts(data)["word/document.xml"])
    return "\n".join(
        node.text or "" for node in root.iter(f"{{{W_NS}}}t")
    )


def _local_names(data: bytes) -> list[str]:
    root = ElementTree.fromstring(_read_parts(data)["word/document.xml"])
    return [
        node.tag.split("}", 1)[1] if "}" in node.tag else node.tag
        for node in root.iter()
    ]


class DocxPackageTests(unittest.TestCase):
    """The .docx as a package: is everything Word needs actually in there."""

    def setUp(self) -> None:
        self.data = segments_to_docx_bytes(
            SEGMENTS, title="Transcript", include_timestamps=False
        )

    def test_is_a_valid_zip_with_every_required_part(self):
        # testzip() returns None when every entry's CRC checks out.
        with zipfile.ZipFile(io.BytesIO(self.data)) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
        for part in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
            self.assertIn(part, names)
        # Some readers expect the content-type table to be the first entry.
        self.assertEqual(names[0], "[Content_Types].xml")

    def test_every_part_parses_as_xml(self):
        parts = _read_parts(self.data)
        for part in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
            with self.subTest(part=part):
                self.assertIsNotNone(ElementTree.fromstring(parts[part]))

    def test_root_elements_and_namespaces_are_the_expected_ones(self):
        parts = _read_parts(self.data)
        types = ElementTree.fromstring(parts["[Content_Types].xml"])
        self.assertTrue(types.tag.endswith("}Types"))
        rels = ElementTree.fromstring(parts["_rels/.rels"])
        self.assertTrue(rels.tag.endswith("}Relationships"))
        document = ElementTree.fromstring(parts["word/document.xml"])
        self.assertEqual(document.tag, f"{{{W_NS}}}document")

    def test_content_types_declare_the_main_document(self):
        types = _read_parts(self.data)["[Content_Types].xml"].decode("utf-8")
        self.assertIn("/word/document.xml", types)
        self.assertIn(f'ContentType="{DOCX_MIME}.main+xml"', types)

    def test_relationship_points_at_the_document(self):
        rels = _read_parts(self.data)["_rels/.rels"].decode("utf-8")
        self.assertIn('Target="word/document.xml"', rels)
        self.assertIn("officeDocument", rels)

    def test_the_expected_text_is_inside_the_document(self):
        text = _document_text(self.data)
        for line in (ARABIC_LINE, ENGLISH_LINE, MARKUP_LINE):
            with self.subTest(line=line):
                self.assertIn(line, text)

    def test_markup_in_the_transcript_is_escaped_not_interpreted(self):
        # The <w:t> & "quotes" sample above must survive as literal text:
        # if it had been dropped into the XML raw, the part would either
        # fail to parse or lose content.
        text = _document_text(self.data)
        self.assertIn('<w:t> & "quotes" </w:t>', text)

    def test_rtl_and_bidi_markers_are_present(self):
        names = _local_names(self.data)
        self.assertIn("rtl", names, "runs need w:rtl for RTL text")
        self.assertIn("bidi", names, "paragraphs need w:bidi for RTL layout")
        # Both live on more than one element: per paragraph AND on the
        # section / paragraph-mark properties, so the direction is not
        # purely run-level.
        self.assertGreaterEqual(names.count("bidi"), 2)
        self.assertGreaterEqual(names.count("rtl"), 2)
        raw = _read_parts(self.data)["word/document.xml"].decode("utf-8")
        self.assertIn("<w:bidi/>", raw)
        self.assertIn("<w:rtl/>", raw)
        self.assertIn("<w:sectPr><w:bidi/></w:sectPr>", raw)

    def test_the_title_is_included_and_bold(self):
        text = _document_text(self.data)
        self.assertIn("Transcript", text)
        raw = _read_parts(self.data)["word/document.xml"].decode("utf-8")
        self.assertIn("<w:b/>", raw)

    def test_element_order_follows_the_schema(self):
        # Word refuses out-of-order children inside these property elements
        # ("unreadable content" repair prompt), so pin the order here.
        raw = _read_parts(self.data)["word/document.xml"].decode("utf-8")
        self.assertIn("<w:bidi/><w:jc w:val=\"right\"/>", raw)
        # Scoped to a single run: w:rPr order is w:b, w:sz/w:szCs, then w:rtl.
        run_start = raw.index("<w:r>")
        run_rpr = raw[run_start : raw.index("</w:rPr>", run_start)]
        self.assertIn("<w:b/>", run_rpr)
        self.assertLess(run_rpr.index("<w:b/>"), run_rpr.index("<w:sz "))
        self.assertLess(run_rpr.index("<w:sz "), run_rpr.index("<w:rtl/>"))
        # w:sectPr must be the last thing in the body.
        body_end = raw.index("</w:body>")
        self.assertGreater(raw.index("<w:sectPr>"), raw.index("<w:p>"))
        self.assertLess(raw.index("<w:sectPr>"), body_end)


class DocxTimestampTests(unittest.TestCase):
    def test_timestamps_appear_only_when_requested(self):
        plain = _document_text(
            segments_to_docx_bytes(SEGMENTS, title="", include_timestamps=False)
        )
        stamped = _document_text(
            segments_to_docx_bytes(SEGMENTS, title="", include_timestamps=True)
        )
        self.assertNotIn("[00:00]", plain)
        self.assertNotIn("[01:23]", plain)
        self.assertIn("[00:00] " + ARABIC_LINE, stamped)
        self.assertIn("[01:23] " + ENGLISH_LINE, stamped)
        # Over an hour the third segment gets an HH:MM:SS stamp.
        self.assertIn("[01:01:01] " + MARKUP_LINE, stamped)
        # The text itself is never altered.
        self.assertIn(ARABIC_LINE, plain)

    def test_arabic_renders_as_a_single_run_and_paragraph(self):
        # One segment = one paragraph + one run, plus the empty stand-in
        # paragraph when there is nothing to write.
        data = segments_to_docx_bytes(
            [SEGMENTS[0]], title="", include_timestamps=False
        )
        names = _local_names(data)
        self.assertEqual(names.count("p"), 1)
        self.assertEqual(names.count("r"), 1)


class TextExportTests(unittest.TestCase):
    def test_plain_text_is_one_line_per_segment(self):
        self.assertEqual(
            segments_to_text(SEGMENTS),
            "\n".join([ARABIC_LINE, ENGLISH_LINE, MARKUP_LINE]),
        )

    def test_text_with_timestamps(self):
        self.assertEqual(
            segments_to_text(SEGMENTS, include_timestamps=True),
            "\n".join(
                [
                    f"[00:00] {ARABIC_LINE}",
                    f"[01:23] {ENGLISH_LINE}",
                    f"[01:01:01] {MARKUP_LINE}",
                ]
            ),
        )

    def test_timestamp_formatting(self):
        self.assertEqual(format_timestamp(0), "00:00")
        self.assertEqual(format_timestamp(59.9), "00:59")
        self.assertEqual(format_timestamp(60), "01:00")
        self.assertEqual(format_timestamp(3661), "01:01:01")
        # Junk must degrade to 00:00 rather than raise.
        for bad in (None, "junk", float("nan"), float("inf"), -5.0):
            with self.subTest(value=bad):
                self.assertEqual(format_timestamp(bad), "00:00")


class EmptyAndDegenerateInputTests(unittest.TestCase):
    def test_empty_segments_do_not_raise(self):
        self.assertEqual(segments_to_text([]), "")
        self.assertEqual(segments_to_text([], include_timestamps=True), "")

        data = segments_to_docx_bytes([], title="", include_timestamps=False)
        parts = _read_parts(data)
        self.assertIn("word/document.xml", parts)
        self.assertIsNotNone(ElementTree.fromstring(parts["word/document.xml"]))
        # An empty body would be legal but unwelcome; a lone empty
        # paragraph stands in instead.
        self.assertEqual(_document_text(data), "")

    def test_title_only_document_is_still_valid(self):
        data = segments_to_docx_bytes([], title="عنوان فقط")
        self.assertEqual(_document_text(data), "عنوان فقط")
        self.assertEqual(_local_names(data).count("p"), 1)

    def test_whitespace_only_segments_are_skipped(self):
        blank = [TranscriptSegment(start=0.0, end=1.0, text=" \n\t ", words=[])]
        self.assertEqual(segments_to_text(blank), "")
        self.assertEqual(_document_text(segments_to_docx_bytes(blank)), "")

    def test_segments_without_a_start_attribute_do_not_raise(self):
        class Bare:
            text = "no timestamps here"

        self.assertEqual(segments_to_text([Bare()]), "no timestamps here")
        self.assertIn(
            "[00:00] no timestamps here",
            _document_text(
                segments_to_docx_bytes([Bare()], title="", include_timestamps=True)
            ),
        )

    def test_control_characters_are_stripped_from_the_docx_only(self):
        dirty = [TranscriptSegment(start=0.0, end=1.0, text="ok\x0bbad\x1fend")]
        # The .txt export is a faithful dump of the transcript...
        self.assertEqual(segments_to_text(dirty), "ok\x0bbad\x1fend")
        # ...while the XML part cannot carry C0 controls at all: without
        # the strip, document.xml would fail to parse.
        self.assertEqual(
            _document_text(segments_to_docx_bytes(dirty, title="")), "okbadend"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
