r"""Real render smoke test through core.clip_processor.render_clips.

Uses the bundled ffmpeg on PATH (no Ollama, no whisper, no cv2), and
proves four things end to end:

  1. the default center-crop path renders,
  2. --fix-lighting renders (and re-measures the result),
  3. lighting + captions/title overlay together render (the overlay is a
     second ffmpeg pass on top of the lit base clip),
  4. every output is 1080x1920.

It records every ffmpeg subprocess return code so the codes can be
reported, not just asserted.

Run:
    set PATH=...\static_ffmpeg\bin\win32;%PATH%
    python tests\test_render_smoke.py
"""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import QuietLogger, REPO_ROOT  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

from config import Config  # noqa: E402
from core import clip_processor  # noqa: E402
from models.schemas import FinalClip, TranscriptSegment, Word  # noqa: E402
from utils import ffmpeg as ffmpeg_utils  # noqa: E402
from utils.ffmpeg import check_ffmpeg_installed, get_ffmpeg_binaries  # noqa: E402

DURATION = 8.0
CLIP_DURATION = 5.0

_recorded: list[dict] = []
_real_run = subprocess.run


def recording_run(cmd, *args, **kwargs):
    """Wrap subprocess.run to capture every ffmpeg/ffprobe invocation."""
    result = _real_run(cmd, *args, **kwargs)
    if isinstance(cmd, (list, tuple)) and cmd:
        _recorded.append(
            {
                "tool": Path(str(cmd[0])).name,
                "returncode": result.returncode,
                "vf": _extract_vf(cmd),
                "cmd": " ".join(str(c) for c in cmd),
                "out": str(cmd[-1]),
            }
        )
    return result


def is_expected_nonzero(record: dict) -> bool:
    """The NVENC availability probe legitimately fails on a machine with
    no NVIDIA GPU - that is how nvenc_available() decides to use the CPU
    encoder, not a render error.
    """
    return "h264_nvenc" in record["cmd"] and record["vf"] == ""


def _extract_vf(cmd) -> str:
    cmd = [str(c) for c in cmd]
    if "-vf" in cmd:
        return cmd[cmd.index("-vf") + 1]
    return ""


def synth_source(path: str) -> None:
    """A 640x360 dark-ish talking-head stand-in with an audio track."""
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=15:duration={DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={DURATION}",
        "-vf", "eq=brightness=-0.28",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", path,
    ]
    result = _real_run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-500:]


def probe(path: str) -> dict:
    _, ffprobe_bin = get_ffmpeg_binaries()
    result = _real_run(
        [
            ffprobe_bin, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name:format=duration",
            "-of", "json", path,
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-500:]
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "codec": stream["codec_name"],
        "duration": float(data["format"]["duration"]),
    }


def make_segments() -> list[TranscriptSegment]:
    words = []
    for i in range(10):
        words.append(Word(start=i * 0.5, end=i * 0.5 + 0.45, text=f"كلمة{i}"))
    return [TranscriptSegment(start=0.0, end=DURATION, text="نص تجريبي للاختبار", words=words)]


def run_case(name: str, source: str, work: Path, cfg: Config, segments=None) -> dict:
    clip = FinalClip(
        index=1, start=0.0, end=CLIP_DURATION, score=88.5, reason="smoke",
        text="نص تجريبي", title="عنوان تجريبي",
    )
    logger = QuietLogger()
    rendered = clip_processor.render_clips([clip], source, cfg, logger, segments)
    if not rendered:
        raise AssertionError(f"[{name}] nothing rendered; warnings={logger.warnings}")
    out = rendered[0].output_path
    info = probe(out)
    print(f"\n[{name}]")
    print(f"  output     : {out}")
    print(f"  resolution : {info['width']}x{info['height']} ({info['codec']}), "
          f"{info['duration']:.2f}s")
    for line in logger.lines:
        if "Lighting" in line:
            print(f"  log        : {line}")
    return {"out": out, "info": info, "logger": logger, "clip": rendered[0]}


def main() -> int:
    check_ffmpeg_installed()
    ffmpeg_bin, _ = get_ffmpeg_binaries()
    print(f"ffmpeg: {ffmpeg_bin}")

    work = Path(tempfile.mkdtemp(prefix="lvc_smoke_"))
    failures: list[str] = []
    ffmpeg_utils.subprocess.run = recording_run
    try:
        source = str(work / "source.mp4")
        synth_source(source)
        segments = make_segments()

        cases = {
            "default (no lighting)": Config(
                video_path=source, output_dir=str(work / "out_default"),
                fix_lighting=False, normalize_audio=True,
            ),
            "fix-lighting": Config(
                video_path=source, output_dir=str(work / "out_lit"),
                fix_lighting=True, normalize_audio=True,
            ),
            "fix-lighting + captions + title": Config(
                video_path=source, output_dir=str(work / "out_lit_caps"),
                fix_lighting=True, burn_captions=True, add_title_overlay=True,
                normalize_audio=True,
            ),
        }

        results = {}
        for name, cfg in cases.items():
            segs = segments if cfg.burn_captions else None
            results[name] = run_case(name, source, work, cfg, segs)
            info = results[name]["info"]
            if (info["width"], info["height"]) != (1080, 1920):
                failures.append(
                    f"[{name}] output is {info['width']}x{info['height']}, expected 1080x1920"
                )

        # The lighting pass must actually be in the chain when enabled and
        # absent when disabled.
        lit_vf = [r["vf"] for r in _recorded if "eq=gamma" in r["vf"]]
        if not lit_vf:
            failures.append("no render used an eq=gamma (lighting) filter")
        if len(lit_vf) < 2:
            failures.append(
                f"only {len(lit_vf)} render(s) applied lighting; expected >=2 "
                f"(lighting case + lighting/captions case)"
            )
        print("\n  lighting filters applied:")
        for vf in lit_vf:
            print(f"    {vf}")

        # Captions case must have run a second (ass overlay) pass.
        ass_passes = [r for r in _recorded if "ass=" in r["vf"]]
        if not ass_passes:
            failures.append("captions/title overlay pass did not run")
        else:
            print(f"\n  ass overlay passes: {len(ass_passes)} "
                  f"(returncode {ass_passes[0]['returncode']})")

        # A lit render should be brighter than the unlit one on the same
        # deliberately-dark source (the real proof lighting does something).
        from core import lighting

        lit = lighting.measure_luma(results["fix-lighting"]["out"])
        unlit = lighting.measure_luma(results["default (no lighting)"]["out"])
        dark = lighting.measure_luma(source, 0.0, CLIP_DURATION)
        print(
            f"\n  luma: source {dark.avg:.1f} -> unlit render {unlit.avg:.1f} "
            f"-> lit render {lit.avg:.1f}"
        )
        if lit.avg <= unlit.avg + 5:
            failures.append(
                f"lit render ({lit.avg:.1f}) is not meaningfully brighter than the "
                f"unlit one ({unlit.avg:.1f})"
            )

        # Smart reframe shares the same filter-chain builder and the same
        # clip_processor call site; it cannot run here (no cv2/torch), so
        # at least pin the interface that keeps the two paths aligned.
        from core.smart_reframe import render_vertical_clip_smart

        sig = inspect.signature(render_vertical_clip_smart)
        if "extra_video_filter" not in sig.parameters:
            failures.append("render_vertical_clip_smart lacks extra_video_filter")
        if "extra_video_filter" not in inspect.signature(
            ffmpeg_utils.render_vertical_clip
        ).parameters:
            failures.append("render_vertical_clip lacks extra_video_filter")

        # The CLI's --render-from path (analysis.json + transcript.json)
        # must honour --fix-lighting too.
        import app as app_module
        from models.schemas import FinalClip as _FinalClip

        analysis_dir = work / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        (analysis_dir / "transcript.json").write_text(
            json.dumps([s.to_dict() for s in segments], ensure_ascii=False),
            encoding="utf-8",
        )
        analysis_path = analysis_dir / "analysis.json"
        analysis_path.write_text(
            json.dumps(
                [
                    _FinalClip(
                        index=1, start=0.0, end=CLIP_DURATION, score=77.0,
                        reason="hand-edited", text="نص", title="عنوان",
                    ).to_dict()
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        render_cfg = Config(
            video_path=source,
            output_dir=str(work / "out_cli"),
            fix_lighting=True,
            burn_captions=True,
        )
        cli_logger = QuietLogger()
        cli_rendered = app_module.render_from_analysis(
            str(analysis_path), render_cfg, cli_logger
        )
        if not cli_rendered:
            failures.append(f"--render-from produced no clips: {cli_logger.warnings}")
        else:
            cli_info = probe(cli_rendered[0].output_path)
            print("\n[cli --render-from --fix-lighting --captions]")
            print(f"  output     : {cli_rendered[0].output_path}")
            print(f"  resolution : {cli_info['width']}x{cli_info['height']} "
                  f"({cli_info['codec']}), {cli_info['duration']:.2f}s")
            for line in cli_logger.lines:
                if "Lighting" in line:
                    print(f"  log        : {line}")
            if (cli_info["width"], cli_info["height"]) != (1080, 1920):
                failures.append(
                    f"--render-from output is {cli_info['width']}x{cli_info['height']}"
                )

        print("\n  ffmpeg/ffprobe return codes (this run):")
        for r in _recorded:
            note = "  (expected: NVENC probe, no GPU)" if is_expected_nonzero(r) else ""
            print(f"    rc={r['returncode']}  {r['tool']}  vf={r['vf'][:60]!r}{note}")
        bad = [r for r in _recorded if r["returncode"] != 0 and not is_expected_nonzero(r)]
        if bad:
            failures.append(f"{len(bad)} ffmpeg call(s) returned non-zero")

        if failures:
            print("\nFAILURES:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("\nAll render smoke checks passed.")
        return 0
    finally:
        ffmpeg_utils.subprocess.run = _real_run
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
