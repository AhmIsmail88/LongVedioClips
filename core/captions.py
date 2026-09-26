"""
core/captions.py

Builds a per-clip .ass subtitle file used to burn short, punchy captions
(and, optionally, an intro title overlay) into an already-rendered clip.

.ass (Advanced SubStation Alpha) is used instead of plain .srt because it
supports styling (font, color, position, outline) directly in the
subtitle file, which is what gives the "bold white text, black outline,
bottom-center" look common on TikTok/Reels - .srt has no styling of its
own and would render in whatever default the player picks.

All timestamps here are relative to the clip's own start (0 = the first
frame of the rendered clip), since this runs against the already-cut
clip file, not the original long video.
"""

from __future__ import annotations

import os
import tempfile

from models.schemas import FinalClip, TranscriptSegment, Word


def _seconds_to_ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    centiseconds = int(round((secs - int(secs)) * 100))
    return f"{hours}:{minutes:02d}:{int(secs):02d}.{centiseconds:02d}"


def _escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("\n", "\\N")
        .replace("{", "\\{")
        .replace("}", "\\}")
    )


def _collect_words_in_range(
    segments: list[TranscriptSegment], start: float, end: float
) -> list[Word]:
    """Pull out every word (or, lacking word-level timestamps, whole
    segments treated as one unit) that falls inside [start, end].
    """
    words: list[Word] = []
    for seg in segments:
        if seg.end <= start or seg.start >= end:
            continue
        if seg.words:
            for w in seg.words:
                if w.end <= start or w.start >= end:
                    continue
                words.append(w)
        else:
            # No word-level timestamps available (e.g. an older transcript.json
            # from before this feature existed) - fall back to one caption
            # per whole segment rather than skipping captions entirely.
            words.append(
                Word(start=max(seg.start, start), end=min(seg.end, end), text=seg.text)
            )
    return words


def _group_words(
    words: list[Word], words_per_group: int
) -> list[tuple[float, float, str]]:
    """Chunk words into short on-screen groups instead of showing a full
    sentence at once - the standard punchy short-form caption style.
    """
    groups = []
    for i in range(0, len(words), max(1, words_per_group)):
        chunk = words[i : i + words_per_group]
        text = " ".join(w.text.strip() for w in chunk if w.text.strip())
        if not text:
            continue
        groups.append((chunk[0].start, chunk[-1].end, text))
    return groups


_ASS_HEADER_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,{caption_size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,40,40,{caption_margin_v},1
Style: Title,Arial,{title_size},&H0000D7FF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,8,40,40,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass_file(
    clip: FinalClip,
    segments: list[TranscriptSegment],
    output_width: int,
    output_height: int,
    words_per_caption: int,
    burn_captions: bool,
    add_title_overlay: bool,
    title_duration_seconds: float,
) -> str | None:
    """Build a temp .ass file for this clip and return its path, or None
    if there's nothing to burn (both features off, or no title text and
    captions off).

    Caller is responsible for deleting the returned temp file.
    """
    if not burn_captions and not (add_title_overlay and clip.title):
        return None

    caption_size = max(18, int(output_height * 0.045))
    title_size = max(24, int(output_height * 0.06))
    caption_margin_v = int(output_height * 0.12)

    lines = [
        _ASS_HEADER_TEMPLATE.format(
            width=output_width,
            height=output_height,
            caption_size=caption_size,
            title_size=title_size,
            caption_margin_v=caption_margin_v,
        )
    ]

    if add_title_overlay and clip.title:
        end_t = min(title_duration_seconds, max(clip.duration, 0.5))
        lines.append(
            f"Dialogue: 0,{_seconds_to_ass_time(0)},{_seconds_to_ass_time(end_t)},"
            f"Title,,0,0,0,,{_escape_ass_text(clip.title)}\n"
        )

    if burn_captions:
        words = _collect_words_in_range(segments, clip.start, clip.end)
        for w_start, w_end, text in _group_words(words, words_per_caption):
            rel_start = max(0.0, w_start - clip.start)
            rel_end = max(rel_start + 0.1, w_end - clip.start)
            lines.append(
                f"Dialogue: 0,{_seconds_to_ass_time(rel_start)},{_seconds_to_ass_time(rel_end)},"
                f"Caption,,0,0,0,,{_escape_ass_text(text)}\n"
            )

    if len(lines) == 1:
        # Header only - burn_captions was on but no words fell in range.
        return None

    fd, path = tempfile.mkstemp(suffix=".ass", prefix="captions_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return path
