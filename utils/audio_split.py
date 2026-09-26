"""
Split an audio file (or the audio track of a video) into parts.

Backs the GUI's «تقسيم الصوت» section - the audio sibling of the clip
pipeline's "split the video into N parts" mode. Planning (which seconds
belong to which part) is pure and testable without ffmpeg; the cutting
re-encodes each part through the same ffmpeg wrapper the rest of the
pipeline uses (utils.ffmpeg), so binary resolution and error reporting stay
in one place.

Output format is decided at runtime, not assumed: MP3 via libmp3lame when
the ffmpeg build actually has it, AAC/m4a otherwise. static-ffmpeg's
bundled build and a system build can differ, the same way nvenc_available()
in utils.ffmpeg probes the video encoders.
"""

from __future__ import annotations

import io
import math
import subprocess
import zipfile
from functools import lru_cache
from pathlib import Path

from utils.ffmpeg import check_ffmpeg_installed, get_ffmpeg_binaries, probe_duration
from utils.logger import PipelineError

# Below this a "part" stops being useful audio, so a request that would
# produce one is refused up front - the audio counterpart of
# MIN_ADAPTIVE_CLIP_SECONDS in config.py.
MIN_PART_SECONDS = 3.0

DEFAULT_AUDIO_BITRATE = "192k"


def _encoder_available(needle: str) -> bool:
    """Whether the *selected* ffmpeg binary lists an encoder matching `needle`."""
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return needle in result.stdout


@lru_cache(maxsize=1)
def mp3_available() -> bool:
    """True when this ffmpeg build can encode MP3 (libmp3lame)."""
    return _encoder_available("libmp3lame")


@lru_cache(maxsize=1)
def aac_available() -> bool:
    """True when this ffmpeg build can encode AAC.

    Matched with surrounding spaces so `aac_mf` / `aac_at` variants alone
    do not count as the native encoder.
    """
    return _encoder_available(" aac ")


def default_out_format() -> str:
    """The best widely-playable audio format this ffmpeg build supports."""
    if mp3_available():
        return "mp3"
    if aac_available():
        return "m4a"
    raise PipelineError(
        "This ffmpeg build cannot encode MP3 or AAC audio.",
        hint=(
            "Install a complete FFmpeg build "
            "(winget install ffmpeg, or: pip install static-ffmpeg)."
        ),
    )


def plan_parts(
    duration: float,
    *,
    num_parts: int | None = None,
    part_seconds: float | None = None,
    min_part_seconds: float = MIN_PART_SECONDS,
) -> list[tuple[float, float]]:
    """Plan the [start, end) windows that cover [0, duration].

    Exactly one of the two modes must be given:

    * ``num_parts``: equal shares of ``duration / num_parts`` - the audio
      equivalent of the clip pipeline's split-evenly mode.
    * ``part_seconds``: consecutive windows of at most that length. A final
      fragment shorter than ``min_part_seconds`` is folded into the part
      before it rather than shipping a sub-3-second stub.

    Raises ``ValueError`` with a user-presentable message when the request
    cannot be honoured (no usable duration, bad numbers, or a share below
    ``min_part_seconds``).
    """
    try:
        total = float(duration)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid duration: {duration!r}.") from None
    if not math.isfinite(total) or total <= 0:
        raise ValueError(f"The source has no usable duration ({total:.1f}s).")
    if (num_parts is None) == (part_seconds is None):
        raise ValueError("Choose either a part count or a part length, not both.")

    if num_parts is not None:
        try:
            count = int(num_parts)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid part count: {num_parts!r}.") from None
        if count < 1:
            raise ValueError("The part count must be at least 1.")
        share = total / count
        if share < min_part_seconds:
            max_feasible = max(1, int(total // min_part_seconds))
            raise ValueError(
                f"{count} parts would make each part {share:.1f}s, which is "
                f"below the {min_part_seconds:.0f}s minimum. This "
                f"{total:.0f}s file can hold at most {max_feasible} part(s)."
            )
        return [(total * i / count, total * (i + 1) / count) for i in range(count)]

    try:
        seconds = float(part_seconds)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid part duration: {part_seconds!r}.") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("The part duration must be a positive number of seconds.")
    if seconds < min_part_seconds:
        raise ValueError(
            f"A part length of {seconds:.1f}s is below the "
            f"{min_part_seconds:.0f}s minimum."
        )

    count = math.ceil(total / seconds)
    parts = [(i * seconds, min((i + 1) * seconds, total)) for i in range(count)]
    # Fold a too-short tail into the previous part instead of shipping it.
    if len(parts) > 1 and (parts[-1][1] - parts[-1][0]) < min_part_seconds:
        parts[-2:] = [(parts[-2][0], total)]
    return parts


def split_audio(
    source_path: str | Path,
    output_dir: str | Path,
    *,
    num_parts: int | None = None,
    part_seconds: float | None = None,
    out_format: str | None = None,
    logger=None,
) -> list[Path]:
    """Cut the audio of ``source_path`` into the planned parts.

    A video source is fine: ``-vn`` drops the picture and only the audio
    track is split. Parts are written as ``<stem>_part_NN.<format>`` inside
    ``output_dir`` and returned in order.
    """
    check_ffmpeg_installed()
    out_format = (out_format or default_out_format()).lstrip(".")

    duration = probe_duration(str(source_path))
    try:
        parts = plan_parts(
            duration, num_parts=num_parts, part_seconds=part_seconds
        )
    except ValueError as e:
        raise PipelineError(str(e)) from e

    source = Path(source_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin, _ = get_ffmpeg_binaries()
    if out_format == "mp3":
        codec_args = ["-c:a", "libmp3lame", "-b:a", DEFAULT_AUDIO_BITRATE]
    else:
        codec_args = ["-c:a", "aac", "-b:a", DEFAULT_AUDIO_BITRATE]

    written: list[Path] = []
    total = len(parts)
    for index, (start, end) in enumerate(parts, start=1):
        out_path = out_dir / f"{source.stem}_part_{index:02d}.{out_format}"
        result = subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(source),
                "-t",
                f"{end - start:.3f}",
                "-vn",
                *codec_args,
                str(out_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PipelineError(
                f"FFmpeg failed while writing part {index}/{total} "
                f"('{out_path.name}').",
                hint=result.stderr.strip()[-800:] if result.stderr else None,
            )
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise PipelineError(
                f"FFmpeg reported success but part {index}/{total} is missing "
                "or empty.",
                hint=str(out_path),
            )
        written.append(out_path)
        if logger is not None:
            logger.info(f"Part {index}/{total} written: {out_path.name}")

    return written


def parts_zip_bytes(paths: list[Path]) -> bytes:
    """Every part as one in-memory .zip, with bare file names as entries."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, arcname=Path(path).name)
    return buffer.getvalue()
