r"""Real-ffmpeg evidence for the lighting fix (Task B verification #2).

Synthesizes three clips on purpose - one deliberately dark, one
over-exposed with detail still recoverable, one well exposed - runs the
measured lighting correction over each through the real render path, and
reports mean luma (signalstats YAVG) before and after, plus chroma
(UAVG/VAVG) to show the correction is a lighting fix and not a colour
cast. It also calibrates core.lighting.apply_eq_model against the real
`eq` filter.

Run with the bundled ffmpeg on PATH:
    set PATH=...\static_ffmpeg\bin\win32;%PATH%
    python tests\test_lighting_ffmpeg.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import QuietLogger, REPO_ROOT  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

from config import Config  # noqa: E402
from core import clip_processor, lighting  # noqa: E402
from models.schemas import FinalClip  # noqa: E402
from utils.ffmpeg import check_ffmpeg_installed, get_ffmpeg_binaries  # noqa: E402

WIDTH, HEIGHT = 320, 240
FPS = 15
DURATION = 6.0


def synth_clip(path: str, source_filter: str) -> None:
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", source_filter,
        "-t", f"{DURATION}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-500:]
    print(f"  synthesized {Path(path).name} <- {source_filter}  (ffmpeg rc=0)")


def probe_output(path: str) -> dict:
    """Width/height/duration of a rendered file, via the bundled ffprobe."""
    import json

    _, ffprobe_bin = get_ffmpeg_binaries()
    result = subprocess.run(
        [
            ffprobe_bin, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", path,
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-500:]
    data = json.loads(result.stdout)
    return {
        "width": int(data["streams"][0]["width"]),
        "height": int(data["streams"][0]["height"]),
        "duration": float(data["format"]["duration"]),
    }


def render_flat(work: Path, luma: int, vf: str) -> tuple[float, float]:
    """Render a flat grey frame of `luma`, apply `vf`, and return
    (input YAVG, output YAVG) as measured by signalstats.
    """
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    src = str(work / f"flat_{luma}.mp4")
    if not os.path.exists(src):
        subprocess.run(
            [
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=black:s=160x120:r=10",
                "-vf", f"geq=r='{luma}':g='{luma}':b='{luma}'",
                "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", src,
            ],
            check=True, capture_output=True,
        )
    out = str(work / f"flat_{luma}_out.mp4")
    subprocess.run(
        [
            ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-i", src, "-vf", f"{vf},format=yuv420p",
            "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", out,
        ],
        check=True, capture_output=True,
    )
    before = lighting.measure_luma(src)
    after = lighting.measure_luma(out)
    return before.avg, after.avg


def calibrate_eq_model(work: Path, failures: list[str]) -> None:
    """Check core.lighting.apply_eq_model against the real eq filter on a
    set of flat luma levels. Flat frames are the honest case for the
    model: it predicts a single value's transform, not a distribution's.

    Gamma and contrast match the real filter to within ~1 luma.
    `brightness` is modelled to ~4 luma: eq's brightness is not a plain
    addition of `b * 255` code values in practice (measured deltas are a
    few luma smaller than that), and ffmpeg does not document the
    discrepancy. It is only ever used as a small, bounded top-up
    (MAX_BRIGHTNESS = 0.08), and the log's "after" value is always
    re-measured from the rendered file rather than predicted, so this
    modelling slack never decides the final exposure on its own.
    """
    print("\n[calibration] core.lighting.apply_eq_model vs real ffmpeg eq")
    combos = [
        (1.8, 1.0, 0.0, 1.5),
        (0.45, 1.0, 0.0, 1.5),
        (1.4, 1.1, 0.0, 1.5),
        (1.0, 1.0, 0.08, 4.0),
    ]
    for gamma, contrast, brightness, tolerance in combos:
        vf = f"eq=gamma={gamma}:contrast={contrast}:brightness={brightness}"
        worst = 0.0
        for luma in (32, 64, 96, 128, 160, 200, 232):
            before, after = render_flat(work, luma, vf)
            predicted = 255.0 * lighting.apply_eq_model(
                before / 255.0, gamma, contrast, brightness
            )
            worst = max(worst, abs(predicted - after))
        print(
            f"  {vf:52s} max |predicted - measured| = {worst:.2f} luma "
            f"(tolerance {tolerance})"
        )
        if worst > tolerance:
            failures.append(
                f"eq model mismatch for {vf}: {worst:.2f} luma off "
                f"(tolerance {tolerance})"
            )


def main() -> int:
    check_ffmpeg_installed()
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    print(f"ffmpeg: {ffmpeg_bin}")
    work = Path(tempfile.mkdtemp(prefix="lvc_lighting_"))
    failures: list[str] = []

    try:
        calibrate_eq_model(work, failures)

        # Deliberately dark (well below mid-luma), over-exposed but with
        # recoverable detail, and a plain test pattern that is already
        # well exposed.
        sources = {
            "dark": "color=black:s=320x240:r=15,geq=r='40+20*sin(X/30)':g='40+20*sin(X/30)':b='40+20*sin(X/30)'",
            "blown": "color=black:s=320x240:r=15,geq=r='205+25*sin(X/25)':g='205+25*sin(X/25)':b='205+25*sin(X/25)'",
            "good": "testsrc2=size=320x240:rate=15",
        }

        for name, filt in sources.items():
            src = str(work / f"{name}_src.mp4")
            synth_clip(src, filt)
            before = lighting.measure_luma(src)
            assert before is not None, f"could not measure {name}"

            cfg = Config(
                video_path=src,
                output_dir=str(work / "out"),
                num_clips=1,
                fix_lighting=True,
                lighting_strength=1.0,
            )
            clip = FinalClip(index=1, start=0.0, end=DURATION, score=90.0, reason="t", text="t")
            logger = QuietLogger()
            rendered = clip_processor.render_clips([clip], src, cfg, logger)
            assert rendered, f"render failed for {name}: {logger.warnings}"

            out = rendered[0].output_path
            after = lighting.measure_luma(out)
            assert after is not None
            info = probe_output(out)

            plan = lighting.plan_lighting(src, 0.0, DURATION, cfg, QuietLogger(), label=name)
            gamma = f"{plan.correction.gamma:.2f}" if plan.correction else "-"

            print(
                f"\n[{name}] samples={before.samples}\n"
                f"  filter     : {plan.filter_string}\n"
                f"  YAVG       : {before.avg:6.1f} -> {after.avg:6.1f}   "
                f"(target {cfg.lighting_target_luma:.0f})\n"
                f"  YMIN/YMAX  : {before.min:.0f}/{before.max:.0f} -> "
                f"{after.min:.0f}/{after.max:.0f}\n"
                f"  UAVG/VAVG  : {before.u_avg:.1f}/{before.v_avg:.1f} -> "
                f"{after.u_avg:.1f}/{after.v_avg:.1f}  (chroma shift)\n"
                f"  output     : {info['width']}x{info['height']}, "
                f"{info['duration']:.1f}s, gamma={gamma}"
            )

            delta = after.avg - before.avg
            if name == "dark":
                if delta < 20:
                    failures.append(f"dark clip only brightened by {delta:.1f}")
                if after.avg > 235:
                    failures.append(f"dark clip blown out (YAVG {after.avg:.1f})")
            elif name == "blown":
                if delta > -15:
                    failures.append(f"over-exposed clip only darkened by {delta:.1f}")
                if after.avg < 120:
                    failures.append(f"over-exposed clip over-darkened (YAVG {after.avg:.1f})")
            elif name == "good":
                if abs(delta) > lighting.NEUTRAL_BAND:
                    failures.append(
                        f"well-exposed clip was changed by {delta:.1f} (should be <="
                        f"{lighting.NEUTRAL_BAND})"
                    )
            if (info["width"], info["height"]) != (1080, 1920):
                failures.append(f"{name}: output is {info['width']}x{info['height']}")

        # off-by-default check: no fix_lighting => no filter, no luma change
        src = str(work / "dark_src.mp4")
        off_cfg = Config(video_path=src, output_dir=str(work / "out_off"), fix_lighting=False)
        plan_off = lighting.plan_lighting(src, 0.0, DURATION, off_cfg, QuietLogger(), label="off")
        print(f"\n[default/off] filter={plan_off.filter_string!r} note={plan_off.note!r}")
        if plan_off.active:
            failures.append("lighting fix was active with fix_lighting=False")

        print(f"\nffmpeg binaries used: {ffmpeg_bin}")
        if failures:
            print("\nFAILURES:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("\nAll lighting checks passed.")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
