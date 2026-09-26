"""
Transcript -> text / .docx export.

A .docx file is just a ZIP holding a few XML parts, so the whole thing can
be written with the standard library (zipfile + hand-built
WordprocessingML) instead of adding python-docx as a dependency.

Only the three parts Word actually requires are emitted:

    [Content_Types].xml          what each part is
    _rels/.rels                  "the main document lives at word/document.xml"
    word/document.xml            the body itself

Two details matter for the output to be *usable*, not merely valid XML:

* Element order inside w:pPr / w:rPr / w:body is fixed by the OOXML
  schema. Word accepts nothing that is out of order and answers with a
  "Word found unreadable content" repair prompt, so the order below is
  deliberate - do not shuffle it.
* Arabic is right-to-left: every paragraph carries <w:bidi/> and every run
  <w:rtl/>, and the section itself is bidi, otherwise Word renders the
  script correctly but lays the paragraph out left-to-right.
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from xml.sax.saxutils import escape

# WordprocessingML namespace used by every element below.
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# Main-document content type, as referenced by [Content_Types].xml and by
# the GUI's download button.
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

# Characters that are illegal in XML 1.0 (C0 controls other than tab/LF/CR).
# Whisper output should never contain them, but a stray one would make the
# whole document unopenable, so they are dropped instead.
_INVALID_XML_CHARS = re.compile(
    "[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels"'
    ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/word/document.xml" ContentType="{DOCX_MIME}.main+xml"/>'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships'
    ' xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1"'
    ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
    ' Target="word/document.xml"/>'
    "</Relationships>"
)


def _safe_seconds(value: object) -> float:
    """`float(value)` clamped to >= 0, or 0.0 for anything unusable.

    Timestamp formatting must never raise: the transcript is the data the
    user asked for, and one odd segment must not cost them the whole file.
    """
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(seconds):
        return 0.0
    return max(0.0, seconds)


def format_timestamp(seconds: object) -> str:
    """Format seconds as MM:SS, or HH:MM:SS once the hour is reached."""
    total = int(_safe_seconds(seconds))  # truncate the fraction of a second
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _xml_text(value: object) -> str:
    """Escape a string for use inside a <w:t> element."""
    return escape(_INVALID_XML_CHARS.sub("", str(value if value is not None else "")))


def _segment_lines(segments, include_timestamps: bool):
    """Yield (timestamp_or_None, text) for every non-empty segment.

    Accepts anything with `.start` / `.text` attributes (the pipeline's
    TranscriptSegment), so this module never imports a schema.
    """
    for segment in segments or []:
        text = getattr(segment, "text", "") or ""
        text = text.strip()
        if not text:
            continue
        if include_timestamps:
            yield format_timestamp(getattr(segment, "start", 0.0)), text
        else:
            yield None, text


def segments_to_text(segments, include_timestamps: bool = False) -> str:
    """The transcript as plain text, one segment per line.

    With `include_timestamps=True` each line is prefixed with the
    segment's start time, e.g. ``[01:23] النص``.
    """
    lines: list[str] = []
    for stamp, text in _segment_lines(segments, include_timestamps):
        lines.append(f"[{stamp}] {text}" if stamp else text)
    return "\n".join(lines)


def _run(text: str, *, bold: bool = False, size_half_points: int = 24) -> str:
    """One RTL text run.

    w:rPr child order is schema-fixed: w:b, then w:sz/w:szCs, then w:rtl.
    """
    rpr = ["<w:rPr>"]
    if bold:
        rpr.append("<w:b/>")
    rpr.append(f'<w:sz w:val="{size_half_points}"/>')
    rpr.append(f'<w:szCs w:val="{size_half_points}"/>')
    rpr.append("<w:rtl/>")
    rpr.append("</w:rPr>")
    return (
        f"<w:r>{''.join(rpr)}"
        f'<w:t xml:space="preserve">{_xml_text(text)}</w:t></w:r>'
    )


def _paragraph(text: str, *, bold: bool = False, size_half_points: int = 24) -> str:
    """One right-to-left paragraph.

    w:pPr child order is schema-fixed as well: w:bidi, then w:jc, and the
    paragraph-mark run properties (w:rPr) last.
    """
    return (
        "<w:p><w:pPr>"
        "<w:bidi/>"
        '<w:jc w:val="right"/>'
        "<w:rPr><w:rtl/></w:rPr>"
        "</w:pPr>"
        f"{_run(text, bold=bold, size_half_points=size_half_points)}"
        "</w:p>"
    )


def _document_xml(paragraphs: list[str]) -> str:
    """The complete word/document.xml for the given paragraph fragments."""
    # A document with an empty body is legal but some readers dislike it,
    # so an empty paragraph stands in when there is nothing to write.
    if not paragraphs:
        paragraphs = [_paragraph("")]
    # w:sectPr is the last child of w:body; w:bidi here makes the whole
    # section right-to-left, not just the individual paragraphs.
    section = "<w:sectPr><w:bidi/></w:sectPr>"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W_NS}"><w:body>'
        f"{''.join(paragraphs)}{section}"
        "</w:body></w:document>"
    )


def segments_to_docx_bytes(
    segments, title: str = "", include_timestamps: bool = False
) -> bytes:
    """Build a Word (.docx) document from transcript segments.

    `title` is written as a bold heading paragraph and may be empty.
    RTL/bidi markers are always emitted, so Arabic (and any other
    right-to-left script) is laid out correctly. An empty `segments`
    sequence yields a valid, openable document containing just the title
    (or nothing at all) rather than an error.
    """
    paragraphs: list[str] = []
    if title and title.strip():
        paragraphs.append(_paragraph(title.strip(), bold=True, size_half_points=32))
    for stamp, text in _segment_lines(segments, include_timestamps):
        paragraphs.append(_paragraph(f"[{stamp}] {text}" if stamp else text))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # [Content_Types].xml goes in first: some readers expect it to be
        # the first entry in the archive.
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        archive.writestr("_rels/.rels", _RELS_XML)
        archive.writestr("word/document.xml", _document_xml(paragraphs))
    return buffer.getvalue()
