"""
Automatic lighting correction ("إصلاح الإضاءة") for exported clips.

This is a *measured* correction, not a fixed magic constant:

1. `measure_luma` reads the clip's real luma statistics straight from the
   source with ffmpeg's `signalstats` filter, seeked to the clip range and
   sampled at ~1 frame per second. No extra dependency, and cheap (a few
   frames of decode per clip, versus a full re-encode).
2. `compute_correction` derives an `eq` filter (gamma, plus a bounded
   contrast lift for flat footage) that moves the measured average luma
   towards a sensible mid-luma target.
3. The correction is clamped, and skipped entirely when the clip is
   already near the target - so a well-exposed clip is left alone and a
   very dark clip is brightened without being blown out.

The resulting filter string is handed to whichever render path is in use
(center crop or smart reframe) so both apply exactly the same correction;
see utils/ffmpeg.combine_filters.
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass

from config import Config
from utils.ffmpeg import get_ffmpeg_binaries
from utils.logger import Logger

# Reference luma scale for signalstats output (0-255).
_LUMA_MAX = 255.0

# Corrections are clamped to these bounds regardless of how bad the
# measurement is: beyond them a near-black clip just becomes amplified
# noise, and a near-white clip would be pushed below a believable
# exposure (there is no detail left to recover).
MAX_GAMMA = 1.8
MIN_GAMMA = 0.45
MAX_CONTRAST = 1.2
MAX_BRIGHTNESS = 0.08

# How close to the target counts as "already fine" (in luma units).
NEUTRAL_BAND = 6.0
# Only genuinely flat footage gets a contrast lift, and only when it is
# dark (see compute_correction).
FLAT_SPAN = 0.25
# Above this max luma the clip is close enough to clipping that adding
# brightness would only burn highlights, so it is skipped.
BRIGHT_CEILING = 230.0

_LUMA_KEY_RE = re.compile(r"lavfi\.signalstats\.(YAVG|YMIN|YMAX|UAVG|VAVG)=([\d.]+)")


@dataclass
class LumaStats:
    """Average/min/max luma of the sampled frames."""

    avg: float
    min: float
    max: float
    samples: int
    u_avg: float = 0.0
    v_avg: float = 0.0


@dataclass
class LightingCorrection:
    """A measured, bounded correction to apply to one clip."""

    gamma: float
    contrast: float
    brightness: float
    measured_avg: float
    predicted_avg: float
    target_avg: float
    reason: str

    def filter_string(self) -> str:
        return (
            f"eq=gamma={self.gamma:.4f}"
            f":contrast={self.contrast:.4f}"
            f":brightness={self.brightness:.4f}"
        )


@dataclass
class LightingPlan:
    """What the lighting fix decided for one clip."""

    filter_string: str | None
    source_stats: LumaStats | None
    correction: LightingCorrection | None
    note: str

    @property
    def active(self) -> bool:
        return self.filter_string is not None


def measure_luma(
    video_path: str,
    start: float = 0.0,
    end: float | None = None,
    max_samples: int = 60,
) -> LumaStats | None:
    """Measure average/min/max luma of `[start, end)` in `video_path`.

    Uses `signalstats` + `metadata=print`, which emits the per-frame
    statistics to ffmpeg's log - so no temp files, no path escaping, and
    the source is seeked (`-ss` before `-i`) rather than decoded from the
    beginning. Returns None if ffmpeg fails or produced no frames.
    """
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    duration = None
    if end is not None:
        duration = max(0.05, end - start)

    # ~1 sample per second, but never more than max_samples frames across
    # the clip (long clips get a coarser, still representative sample).
    fps = 1.0
    if duration and duration > max_samples:
        fps = max(0.02, max_samples / duration)

    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "info"]
    if start:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", video_path]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vf", f"fps={fps},signalstats,metadata=print", "-an", "-f", "null", "-"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None

    # metadata=print writes one line per key per frame, in this order:
    # YMIN, YLOW, YAVG, YHIGH, YMAX, ... UMIN, ..., UAVG, ..., VAVG ...
    frames: list[dict[str, float]] = []
    current: dict[str, float] = {}
    for line in result.stderr.splitlines():
        match = _LUMA_KEY_RE.search(line)
        if not match:
            continue
        key, value = match.group(1), float(match.group(2))
        if key == "YMIN" and current:
            frames.append(current)
            current = {}
        current[key] = value
    if current:
        frames.append(current)
    if not frames:
        return None

    def mean(key: str) -> float:
        vals = [f[key] for f in frames if key in f]
        return sum(vals) / len(vals) if vals else 0.0

    return LumaStats(
        avg=mean("YAVG"),
        min=mean("YMIN"),
        max=mean("YMAX"),
        samples=len(frames),
        u_avg=mean("UAVG"),
        v_avg=mean("VAVG"),
    )


def apply_eq_model(
    value_norm: float, gamma: float, contrast: float, brightness: float
) -> float:
    """ffmpeg's `eq` transfer function for one normalised luma value.

    Verified against the real filter (tests/test_lighting_ffmpeg.py): eq
    applies contrast + brightness FIRST, clamps to [0, 1], and only then
    applies the gamma power. Getting that order wrong is why a naive
    "gamma towards the target" model over/under-shoots badly on flat
    footage (a 1.25 contrast on a bright clip raises it into clipping
    before gamma ever runs).
    """
    v = contrast * (value_norm - 0.5) + 0.5 + brightness
    v = min(1.0, max(0.0, v))
    if v <= 0.0:
        return 0.0
    return v ** (1.0 / gamma)


def compute_correction(
    stats: LumaStats,
    target_luma: float = 118.0,
    strength: float = 1.0,
) -> LightingCorrection | None:
    """Derive a bounded correction that moves `stats.avg` towards
    `target_luma`. Returns None when the clip is already close enough (or
    has no recoverable signal), so well-exposed footage is untouched.

    `strength` scales how far the correction deviates from "do nothing":
    0 = off, 1 = the full measured correction, >1 = more aggressive (still
    clamped to MAX_GAMMA / MAX_CONTRAST / MAX_BRIGHTNESS).
    """
    if strength <= 0:
        return None

    target = min(_LUMA_MAX - 2.0, max(2.0, target_luma))

    if stats.samples <= 0:
        return None
    if abs(stats.avg - target) <= NEUTRAL_BAND:
        return None
    if stats.avg <= 1.0:
        # Effectively black: there is no detail to recover, and lifting it
        # would only amplify noise/banding. Leave it alone.
        return None

    measured = stats.avg / _LUMA_MAX
    target_norm = target / _LUMA_MAX
    brightening = measured < target_norm

    # Contrast: only for genuinely flat, dark footage, where the range is
    # too small for a gamma lift alone to look right. Kept mild because eq
    # applies it before gamma, so it costs some of the brightening.
    span = max(0.0, (stats.max - stats.min) / _LUMA_MAX)
    contrast = 1.0
    if brightening and span < FLAT_SPAN:
        contrast = min(
            MAX_CONTRAST, 1.0 + (FLAT_SPAN - span) * 0.6 * strength
        )

    def solve_gamma(br: float) -> float:
        inner = min(1.0, max(1e-4, contrast * (measured - 0.5) + 0.5 + br))
        raw = math.log(inner) / math.log(target_norm)
        # Strength moves the deviation from 1.0, so strength=0 is a no-op
        # and strength=1 is exactly the measured correction.
        return min(MAX_GAMMA, max(MIN_GAMMA, 1.0 + (raw - 1.0) * strength))

    gamma = solve_gamma(0.0)
    predicted = _LUMA_MAX * apply_eq_model(measured, gamma, contrast, 0.0)

    # If the clamp stopped gamma short of the target (very dark clips),
    # make up the rest with a small, bounded brightness lift - but only
    # while the clip has headroom left, so highlights never burn out.
    brightness = 0.0
    if brightening and predicted < target - 2.0 and stats.max < BRIGHT_CEILING:
        gap = target - predicted
        brightness = min(MAX_BRIGHTNESS, max(0.0, gap / _LUMA_MAX * 0.5 * strength))
        gamma = solve_gamma(brightness)
        predicted = _LUMA_MAX * apply_eq_model(measured, gamma, contrast, brightness)

    if abs(gamma - 1.0) < 1e-3 and abs(contrast - 1.0) < 1e-3 and brightness <= 1e-4:
        return None

    reason = "dark" if brightening else "over-bright"
    return LightingCorrection(
        gamma=gamma,
        contrast=contrast,
        brightness=brightness,
        measured_avg=stats.avg,
        predicted_avg=predicted,
        target_avg=target,
        reason=reason,
    )


def plan_lighting(
    video_path: str,
    start: float,
    end: float,
    config: Config,
    logger: Logger,
    label: str = "",
) -> LightingPlan:
    """Measure one clip and decide its correction.

    Never raises: if the measurement fails (odd codec, ffmpeg hiccup) the
    clip is rendered unmodified with a warning, because a lighting
    nicety must not sink the whole render.
    """
    if not getattr(config, "fix_lighting", False):
        return LightingPlan(None, None, None, "disabled")

    prefix = f"Lighting {label}: " if label else "Lighting: "
    try:
        stats = measure_luma(video_path, start, end)
    except Exception as e:  # noqa: BLE001 - never fail a render over this
        logger.warn(f"{prefix}could not measure luma ({e}); leaving the clip as-is.")
        return LightingPlan(None, None, None, "measure failed")

    if stats is None:
        logger.warn(f"{prefix}could not measure luma; leaving the clip as-is.")
        return LightingPlan(None, None, None, "measure failed")

    correction = compute_correction(
        stats,
        target_luma=getattr(config, "lighting_target_luma", 118.0),
        strength=getattr(config, "lighting_strength", 1.0),
    )
    if correction is None:
        logger.info(
            f"{prefix}measured YAVG {stats.avg:.1f} (YMIN {stats.min:.0f}, "
            f"YMAX {stats.max:.0f}) - already well exposed, no change."
        )
        return LightingPlan(None, stats, None, "already well exposed")

    logger.info(
        f"{prefix}measured YAVG {stats.avg:.1f} (YMIN {stats.min:.0f}, "
        f"YMAX {stats.max:.0f}) - {correction.reason}: gamma "
        f"{correction.gamma:.2f}, contrast {correction.contrast:.2f}, "
        f"brightness {correction.brightness:+.3f} -> predicted YAVG "
        f"{correction.predicted_avg:.1f} (target {correction.target_avg:.0f})."
    )
    return LightingPlan(correction.filter_string(), stats, correction, "corrected")


def verify_lighting(
    plan: LightingPlan,
    output_path: str,
    logger: Logger,
    label: str = "",
) -> LumaStats | None:
    """Re-measure the *rendered* file and log measured before -> after.

    This is the honest check that the filter did what it predicted; it
    costs one extra (short) decode per clip and only runs when a
    correction was actually applied.
    """
    if not plan.active:
        return None
    after = measure_luma(output_path)
    if after is None:
        logger.warn(
            f"Lighting: could not verify the rendered luma of {output_path}."
        )
        return None
    before_avg = plan.source_stats.avg if plan.source_stats else float("nan")
    logger.info(
        f"Lighting result{' ' + label if label else ''}: measured YAVG "
        f"{before_avg:.1f} -> {after.avg:.1f} (target "
        f"{plan.correction.target_avg:.0f})."
    )
    return after
