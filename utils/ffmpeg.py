"""
Thin wrapper around the ffmpeg / ffprobe CLI tools.

Isolating this here means clip_processor.py doesn't need to know how to
detect hardware encoders or probe video metadata directly - and it means
we have one place to fix if ffmpeg's CLI behavior needs special-casing on
Windows vs Linux/macOS.

Binary resolution: prefers the `static-ffmpeg` package (bundled ffmpeg +
ffprobe executables, downloaded automatically on first use - no manual
install, no PATH setup, no version-mismatch headaches). Falls back to
whatever `ffmpeg`/`ffprobe` are already on the system PATH if
static-ffmpeg isn't installed or its one-time download fails (e.g. no
internet, restrictive firewall) - so this doesn't regress for anyone who
already has FFmpeg installed system-wide.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import lru_cache

from utils.logger import PipelineError

# EBU R128 loudness normalization to a -16 LUFS integrated target with a
# -1.5 dBTP ceiling - the standard 'consistent spoken-word' preset for
# short-form platforms. Applied on the first audio re-encode per clip.
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"


def combine_filters(*filters: str | None) -> str | None:
    """Join video filter fragments into one -vf chain, skipping the ones
    that are None or empty.

    Every render path builds its filter chain through this one function
    (center crop, smart reframe, the lighting fix), so an optional filter
    like the lighting correction cannot silently apply on one path and
    not the other.
    """
    parts = [f.strip().strip(",") for f in filters if f and f.strip()]
    return ",".join(parts) if parts else None


@lru_cache(maxsize=1)
def get_ffmpeg_binaries() -> tuple[str, str]:
    """Return (ffmpeg_path, ffprobe_path) to use for every subprocess call
    in this module (and in core/smart_reframe.py).
    """
    try:
        from static_ffmpeg import run as static_ffmpeg_run

        return static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
    except Exception:
        # static-ffmpeg not installed, or its download failed - fall back
        # to whatever's on PATH. check_ffmpeg_installed() gives a clear
        # error later if neither is available.
        return "ffmpeg", "ffprobe"


def check_ffmpeg_installed() -> None:
    ffmpeg_bin, ffprobe_bin = get_ffmpeg_binaries()
    have_ffmpeg = shutil.which(ffmpeg_bin) is not None or os.path.isfile(ffmpeg_bin)
    have_ffprobe = shutil.which(ffprobe_bin) is not None or os.path.isfile(ffprobe_bin)
    if not have_ffmpeg or not have_ffprobe:
        raise PipelineError(
            "FFmpeg was not found on this system.",
            hint=(
                "Either install `static-ffmpeg` (pip install static-ffmpeg - "
                "bundles ffmpeg/ffprobe automatically, no PATH setup needed), "
                "or install FFmpeg yourself and make sure it's on your PATH.\n"
                "  Windows: winget install ffmpeg\n"
                "  Linux:   sudo apt install ffmpeg\n"
                "  macOS:   brew install ffmpeg\n"
                "Then verify with: ffmpeg -version"
            ),
        )


def probe_duration(video_path: str) -> float:
    """Return the duration of a video file in seconds."""
    ffmpeg_bin, ffprobe_bin = get_ffmpeg_binaries()
    try:
        result = subprocess.run(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                video_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise PipelineError(
            f"Could not read video metadata for '{video_path}'.",
            hint="Make sure the file is a valid, non-corrupted video file.",
        ) from e
    except FileNotFoundError as e:
        raise PipelineError(
            "ffprobe was not found on this system.",
            hint="Install FFmpeg and ensure ffprobe is on your PATH, or `pip install static-ffmpeg`.",
        ) from e

    try:
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        raise PipelineError(
            f"Could not parse duration for '{video_path}'."
        ) from e


@lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """Detect whether ffmpeg was built with h264_nvenc support AND a usable
    NVIDIA GPU is actually present. This does a lightweight real-encode test
    rather than trusting `ffmpeg -encoders` alone, since a build can list
    nvenc without a working driver/GPU behind it.
    """
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    try:
        list_result = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False

    if "h264_nvenc" not in list_result.stdout:
        return False

    # Quick real test: encode one black frame to null output.
    test = subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x64:d=0.1",
            "-c:v",
            "h264_nvenc",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    return test.returncode == 0


def extract_audio(video_path: str, output_wav_path: str) -> None:
    """Extract mono 16kHz WAV audio, the format faster-whisper expects."""
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    try:
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                video_path,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                output_wav_path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise PipelineError(
            "Failed to extract audio from the video.",
            hint=e.stderr.strip() if e.stderr else None,
        ) from e


def trim_clip(video_path: str, output_path: str, start: float, end: float) -> None:
    """Cut [start, end] from video_path with no cropping or scaling.

    Used by the smart-reframe pipeline, which needs an isolated clip file
    to run scene detection / face tracking on before doing its own
    frame-by-frame crop - unlike render_vertical_clip, this keeps the
    original resolution and aspect ratio untouched.
    """
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    duration = end - start
    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        video_path,
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-c:a",
        "aac",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise PipelineError(
            f"FFmpeg failed while trimming clip '{output_path}'.",
            hint=result.stderr.strip()[-800:] if result.stderr else None,
        )


def burn_subtitles(
    input_path: str,
    output_path: str,
    ass_path: str,
    use_nvenc: bool,
    video_codec_gpu: str,
    video_codec_cpu: str,
    audio_codec: str,
) -> None:
    """Burn a .ass subtitle file (captions and/or title overlay) into an
    already-rendered clip. Runs as a second pass after the main crop/
    render step, re-encoding video but copying audio through untouched.
    """
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    codec = video_codec_gpu if use_nvenc else video_codec_cpu

    # ffmpeg's filter-graph parser treats ':' as an option separator, so a
    # Windows path like C:\Users\...\captions.ass needs its colon (and
    # backslashes) escaped when passed inside the `ass` filter argument.
    escaped_path = ass_path.replace("\\", "/").replace(":", "\\:")

    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_path,
        "-vf",
        f"ass='{escaped_path}'",
        "-c:v",
        codec,
        # Audio was already encoded (and optionally loudness-normalized)
        # in the first render pass - copy it through untouched.
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if use_nvenc:
            fallback_cmd = list(cmd)
            idx = fallback_cmd.index(codec)
            fallback_cmd[idx] = video_codec_cpu
            fallback_result = subprocess.run(fallback_cmd, capture_output=True, text=True)
            if fallback_result.returncode == 0:
                return
            stderr = fallback_result.stderr
        else:
            stderr = result.stderr
        raise PipelineError(
            f"Failed to burn captions/title into '{output_path}'.",
            hint=stderr.strip()[-800:] if stderr else None,
        )


def render_vertical_clip(
    video_path: str,
    output_path: str,
    start: float,
    end: float,
    width: int,
    height: int,
    use_nvenc: bool,
    video_codec_gpu: str,
    video_codec_cpu: str,
    audio_codec: str,
    normalize_audio: bool = False,
    extra_video_filter: str | None = None,
) -> None:
    """Cut [start, end] from video_path and export as a center-cropped
    vertical clip at `width`x`height`.

    `extra_video_filter` is appended to the crop/scale chain - this is how
    the lighting fix is applied (see core/lighting.py), so it lands in the
    same encode as the crop instead of costing an extra pass.
    """
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    duration = end - start
    codec = video_codec_gpu if use_nvenc else video_codec_cpu

    # Center crop to 9:16 then scale to the target resolution.
    crop_and_scale = (
        f"crop='if(gt(ih*{width}/{height},iw),iw,ih*{width}/{height})'"
        f":'if(gt(ih*{width}/{height},iw),iw*{height}/{width},ih)',"
        f"scale={width}:{height}"
    )
    vf = combine_filters(crop_and_scale, extra_video_filter)

    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        video_path,
        "-t",
        f"{duration:.3f}",
        "-vf",
        vf,
        "-c:v",
        codec,
        "-c:a",
        audio_codec,
        "-movflags",
        "+faststart",
        output_path,
    ]
    if normalize_audio:
        # Insert as output options right before the output path.
        cmd[cmd.index(output_path):cmd.index(output_path)] = ["-af", LOUDNORM_FILTER]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if use_nvenc:
            # Fall back to CPU encoding once before giving up.
            fallback_cmd = list(cmd)
            nvenc_idx = fallback_cmd.index(codec)
            fallback_cmd[nvenc_idx] = video_codec_cpu
            fallback_result = subprocess.run(
                fallback_cmd, capture_output=True, text=True
            )
            if fallback_result.returncode == 0:
                return
            stderr = fallback_result.stderr
        else:
            stderr = result.stderr
        raise PipelineError(
            f"FFmpeg failed while rendering clip '{output_path}'.",
            hint=stderr.strip()[-800:] if stderr else None,
        )
