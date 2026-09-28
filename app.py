#!/usr/bin/env python3
"""
Long Video -> TikTok Clips
Local AI video clipping pipeline - MVP CLI entrypoint.

Usage:
    python app.py "video.mp4"
    python app.py --batch "C:/path/to/videos"
    python app.py "video.mp4" --clips 10 --language ar --model medium
    python app.py "video.mp4" --yes   # skip the confirmation prompt (scripts/automation)
    python app.py "video.mp4" --captions --title-overlay   # burn captions + a title
    python app.py "video.mp4" --render-from output/video/analysis.json  # re-export after hand-editing clip times

If --clips is omitted, the tool suggests a clip count based on the
video's length and asks you to confirm or adjust it before starting -
so a long video doesn't silently get under-covered by a small fixed
default.

Everything runs locally: transcription via faster-whisper, ranking via a
local Qwen model through Ollama, and rendering via FFmpeg. Nothing is
uploaded anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from config import (
    Config,
    DEFAULT_MAX_OVERLAP_RATIO,
    PLATFORM_PRESETS,
    SUPPORTED_VIDEO_EXTENSIONS,
    apply_band_plan,
    plan_clip_band,
    suggest_num_clips,
)
from core import candidate_generator, clip_processor, clip_selector, quality_filter, transcriber
from utils.ffmpeg import check_ffmpeg_installed, probe_duration
from utils.resume import (
    band_fingerprint,
    reuse_if_fresh,
    video_identity,
    whisper_fingerprint,
    write_meta,
)
from utils.logger import Logger, PipelineError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Turn a long video into short, vertical clips - entirely locally."
    )
    parser.add_argument(
        "video",
        nargs="?",
        default=None,
        help="Path to the input video file (or omit and use --batch for a folder).",
    )
    parser.add_argument(
        "--batch",
        default=None,
        metavar="FOLDER",
        help="Process every supported video in FOLDER one by one (error-isolated).",
    )
    parser.add_argument(
        "--platform",
        default=None,
        choices=sorted(PLATFORM_PRESETS),
        help=(
            "Apply a platform preset for clip length (tiktok/reels/shorts). "
            "Explicit --min-duration/--max-duration values always win."
        ),
    )
    parser.add_argument(
        "--clips",
        type=int,
        default=None,
        help="Number of clips to produce. If omitted, suggested automatically based on video length.",
    )
    parser.add_argument("--language", default=None, help="Force a language code (e.g. ar, en). Default: auto-detect.")
    parser.add_argument("--model", default="medium", help="faster-whisper model size (default: medium).")
    parser.add_argument("--output", default="output", help="Output directory (default: output/).")
    parser.add_argument(
        "--min-duration",
        type=float,
        default=None,
        help="Preferred minimum clip duration in seconds (default: 20, or the --platform preset).",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=None,
        help="Preferred maximum clip duration in seconds (default: 60, or the --platform preset).",
    )
    parser.add_argument(
        "--split-evenly",
        action="store_true",
        help=(
            "Derive the clip length from the clip count instead of the "
            "duration band: each clip gets video_duration / --clips "
            "seconds, so N clips tile the whole video."
        ),
    )
    parser.add_argument(
        "--no-audio-normalize",
        action="store_true",
        help="Disable loudness normalization (loudnorm to -16 LUFS) on rendered clips.",
    )
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"], help="Transcription device.")
    parser.add_argument("--ollama-model", default="qwen3.5:9b", help="Ollama model name for ranking.")
    parser.add_argument("--ollama-host", default="http://localhost:11434", help="Ollama server URL.")
    parser.add_argument(
        "--smart-reframe",
        action="store_true",
        help=(
            "Track faces/speakers instead of a fixed center crop (slower; "
            "requires: pip install -r requirements-smart-reframe.txt)."
        ),
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Don't ask for confirmation before running (e.g. for scripts/automation).",
    )
    parser.add_argument(
        "--captions",
        action="store_true",
        help="Burn short, auto-timed captions onto each clip.",
    )
    parser.add_argument(
        "--title-overlay",
        action="store_true",
        help="Burn an AI-generated catchy title onto the first few seconds of each clip.",
    )
    parser.add_argument(
        "--fix-lighting",
        action="store_true",
        help=(
            "Measure each clip's brightness and automatically correct dark or "
            "blown-out clips towards a normal exposure (applies to both the "
            "default crop and --smart-reframe). Off by default."
        ),
    )
    parser.add_argument(
        "--lighting-strength",
        type=float,
        default=1.0,
        metavar="X",
        help=(
            "How strongly --fix-lighting corrects (0 = off, 1 = default, "
            "up to 2 = more aggressive; still clamped so clips never blow out)."
        ),
    )
    parser.add_argument(
        "--render-from",
        default=None,
        metavar="ANALYSIS_JSON",
        help=(
            "Skip transcription/ranking and render directly from a "
            "(possibly hand-edited) analysis.json - lets you tweak clip "
            "start/end times yourself before exporting."
        ),
    )
    return parser.parse_args()


def validate_input(video_path: str) -> None:
    path = Path(video_path)
    if not path.exists():
        raise PipelineError(
            f"Video file not found: {video_path}",
            hint="Check the path and try again.",
        )
    if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        raise PipelineError(
            f"Unsupported video format: {path.suffix}",
            hint=f"Supported formats: {', '.join(sorted(SUPPORTED_VIDEO_EXTENSIONS))}",
        )


def requested_max_duration(args: argparse.Namespace) -> float:
    """The max clip duration the user's flags ask for, before any
    adaptive shrinking - explicit --max-duration wins over --platform,
    which wins over the 60s default.
    """
    if args.max_duration is not None:
        return float(args.max_duration)
    if args.platform:
        return float(PLATFORM_PRESETS[args.platform]["max_duration"])
    return 60.0


def feasibility_note(
    video_duration: float,
    num_clips: int,
    max_duration: float,
    max_overlap_ratio: float = DEFAULT_MAX_OVERLAP_RATIO,
    split_evenly: bool = False,
) -> str | None:
    """Plainly state the source's physical limit when the request exceeds
    it, instead of letting the run silently produce fewer clips.

    Returns a human-readable sentence, or None if the request fits.
    """
    plan = plan_clip_band(
        video_duration,
        num_clips,
        0.0,
        max_duration,
        max_overlap_ratio,
        split_evenly=split_evenly,
    )
    if plan.max_feasible_clips >= num_clips:
        return None
    shrink = ""
    if plan.max_duration < max_duration - 1e-9:
        shrink = (
            f" Even after shrinking the clip length to "
            f"{plan.max_duration:.0f}s (= {video_duration:.0f}s / {num_clips})"
        )
    return (
        f"This video is only {video_duration:.0f}s long.{shrink}, at most "
        f"{plan.max_feasible_clips} non-overlapping clip(s) fit - "
        f"{num_clips} cannot be produced. Pick a smaller number of clips or a "
        f"longer video; the run will report exactly what it managed."
    )


def resolve_num_clips(args: argparse.Namespace, logger: Logger, batch: bool = False) -> int:
    """If the user passed --clips explicitly, respect it as-is - no nagging.

    Otherwise, probe the video's duration, suggest a clip count that
    scales with it, tell the user plainly what the tool is about to do
    and how much of the video that covers, and let them either accept it
    (Enter), type a different number, or cancel to adjust settings
    themselves. --yes skips this prompt for scripts/automation, and batch
    mode skips it too (a folder must run unattended).
    """
    if args.clips is not None:
        return args.clips

    check_ffmpeg_installed()
    video_duration = probe_duration(args.video)
    suggested = suggest_num_clips(video_duration)
    if batch:
        return suggested
    minutes = video_duration / 60
    covered_minutes = (suggested * args.max_duration) / 60

    print(
        f"This video is about {minutes:.0f} minutes long. You didn't set "
        f"--clips, so by default this tool will create {suggested} clips "
        f"(covering up to ~{covered_minutes:.0f} of the {minutes:.0f} minutes "
        f"- the rest of the video won't be used)."
    )

    if args.yes:
        return suggested

    answer = input(
        "Press Enter to continue with this, type a different number of "
        "clips, or 'n' to cancel and adjust settings yourself: "
    ).strip()

    if answer.lower() in ("n", "no"):
        print("Cancelled. Re-run with --clips <N> (and/or --max-duration) to set it yourself.")
        raise SystemExit(0)

    if answer:
        try:
            requested = int(answer)
        except ValueError:
            logger.warn(f"'{answer}' isn't a valid number - using the suggested {suggested}.")
            return suggested
        note = feasibility_note(
            video_duration,
            requested,
            requested_max_duration(args),
        )
        if note:
            print(f"NOTE: {note}")
        return requested

    return suggested


def build_config(args: argparse.Namespace, num_clips: int) -> Config:
    min_duration = args.min_duration
    max_duration = args.max_duration
    if args.platform:
        preset = PLATFORM_PRESETS[args.platform]
        if min_duration is None:
            min_duration = preset["min_duration"]
        if max_duration is None:
            max_duration = preset["max_duration"]
    return Config(
        video_path=args.video,
        output_dir=args.output,
        whisper_model=args.model,
        language=args.language,
        device=args.device,
        min_duration=20.0 if min_duration is None else min_duration,
        max_duration=60.0 if max_duration is None else max_duration,
        num_clips=num_clips,
        ollama_model=args.ollama_model,
        ollama_host=args.ollama_host,
        use_smart_reframe=args.smart_reframe,
        burn_captions=args.captions,
        add_title_overlay=args.title_overlay,
        normalize_audio=not args.no_audio_normalize,
        split_evenly=args.split_evenly,
        fix_lighting=args.fix_lighting,
        lighting_strength=args.lighting_strength,
    )


def _load_candidates(path):
    """Candidate list from a previous run's candidates.json."""
    from models.schemas import CandidateSegment

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [CandidateSegment.from_dict(d) for d in data]


def _reuse_artifact(path, fingerprint, config, logger, loader, label):
    """Load an artifact from an earlier run, or None to recompute it.

    Reuse requires the sidecar metadata to match this video and these
    settings, so a stale artifact (different file, different model, a
    different clip band) is never silently reused.
    """
    if not reuse_if_fresh(path, fingerprint, config):
        return None
    try:
        artifact = loader()
    except Exception as e:  # noqa: BLE001
        logger.warn(f"Could not reuse the saved {label} ({e}); recomputing it.")
        return None
    if not artifact:
        return None
    logger.info(f"Resume: reusing the saved {label} - skipping that stage.")
    return artifact


def analyze(
    config: Config, logger: Logger, stats_out: dict | None = None
) -> tuple[list, list, float]:
    """Stages 1-5: transcribe, generate candidates, rank with the LLM,
    and quality-filter down to the final clip list - stops short of
    rendering, so a caller (like the GUI) can let the user review/adjust
    clip boundaries before spending time on the render.

    Returns (final_clips, transcript_segments, video_duration).

    `stats_out`, if given, receives per-stage counts including why fewer
    clips than requested came out (quality_filter.describe_shortfall
    turns them into a readable report) - the GUI uses this to show the
    reason above the review list.
    """
    os.makedirs(config.project_output_dir(), exist_ok=True)

    logger.stage(1, "Loading video...")
    with logger.timed("Load & validate video"):
        check_ffmpeg_installed()
        video_duration = probe_duration(config.video_path)
        logger.info(f"Video duration: {video_duration / 60:.1f} minutes")

    # Decide the clip-length band up front: on a source too short to hold
    # `num_clips` clips of the requested length, the band shrinks so the
    # requested count can actually fit (and is reported as impossible when
    # even the floor doesn't fit).
    plan = plan_clip_band(
        video_duration,
        config.num_clips,
        config.min_duration,
        config.max_duration,
        config.max_overlap_ratio,
        split_evenly=config.split_evenly,
    )
    for note in plan.notes:
        logger.info(note)
    logger.info(
        (f"Adjusted clip band: {plan.summary()}" if plan.adapted else plan.summary())
    )
    if not plan.feasible:
        logger.warn(
            f"This {video_duration:.0f}s video can hold at most "
            f"{plan.max_feasible_clips} non-overlapping clip(s) of "
            f"{plan.min_duration:.0f}-{plan.max_duration:.0f}s, so the requested "
            f"{config.num_clips} cannot all be produced."
        )
    effective_config = apply_band_plan(config, plan)

    logger.stage(2, "Extracting/transcribing audio...")
    transcript_path = config.transcript_path()
    segments = _reuse_artifact(
        transcript_path,
        whisper_fingerprint(config),
        config,
        logger,
        lambda: transcriber.load_transcript(str(transcript_path)),
        "transcript",
    )
    if segments is None:
        with logger.timed("Transcription"):
            segments = transcriber.transcribe(
                video_path=config.video_path,
                model_size=config.whisper_model,
                language=config.language,
                device=config.device,
                compute_type_gpu=config.compute_type_gpu,
                compute_type_cpu=config.compute_type_cpu,
                logger=logger,
            )
            transcriber.save_transcript(segments, str(transcript_path))
            write_meta(
                transcript_path,
                fingerprint=whisper_fingerprint(config),
                **video_identity(config.video_path),
            )
            logger.info(
                f"Transcript saved: {transcript_path} ({len(segments)} segments)"
            )

    stats = stats_out if stats_out is not None else {}
    stats["max_feasible_clips"] = plan.max_feasible_clips
    stats["band_adapted"] = plan.adapted
    stats["requested"] = effective_config.num_clips

    logger.stage(3, "Generating candidates...")
    candidates_path = config.candidates_path()
    candidates = _reuse_artifact(
        candidates_path,
        band_fingerprint(effective_config),
        effective_config,
        logger,
        lambda: _load_candidates(candidates_path),
        "candidate list",
    )
    if candidates is None:
        with logger.timed("Candidate generation"):
            candidates = candidate_generator.generate_candidates(
                segments,
                window_sizes=effective_config.candidate_window_sizes,
                stride=effective_config.candidate_stride,
                min_duration=effective_config.min_duration,
                max_duration=effective_config.max_duration,
                max_candidates=effective_config.max_candidates,
            )
            if not candidates:
                raise PipelineError(
                    "No candidate segments could be generated from the transcript.",
                    hint="The video may be too short, or contain too little speech.",
                )
            logger.info(f"Generated {len(candidates)} candidate segments")
            with open(candidates_path, "w", encoding="utf-8") as f:
                json.dump(
                    [c.to_dict() for c in candidates], f,
                    ensure_ascii=False, indent=2,
                )
            write_meta(
                candidates_path,
                fingerprint=band_fingerprint(effective_config),
                **video_identity(config.video_path),
            )

    logger.stage(4, "Ranking candidates with Qwen...")
    with logger.timed("LLM ranking"):
        scored = clip_selector.score_candidates(
            candidates, effective_config, logger, stats_out=stats
        )
        if not scored:
            raise PipelineError(
                "The LLM did not return any usable scored clips.",
                hint=(
                    "Make sure Ollama is running and the model is pulled: "
                    f"ollama pull {effective_config.ollama_model}"
                ),
            )
        scored = clip_selector.apply_weights(scored, effective_config)
        logger.info(f"Scored {len(scored)} candidates")

    logger.stage(5, "Selecting and filtering clips...")
    with logger.timed("Quality filtering & selection"):
        final_clips = quality_filter.select_final_clips(
            scored, effective_config, video_duration, segments, logger, stats_out=stats
        )
        if not final_clips:
            raise PipelineError(
                "No suitable clips found after quality filtering.",
                hint="Try lowering --min-duration or checking the transcript quality.",
            )
        if len(final_clips) < effective_config.num_clips:
            # One consolidated, actionable block - the per-clip reasons
            # above are detail; this is the summary that says what to fix.
            print("", flush=True)
            for line in quality_filter.describe_shortfall(stats):
                logger.warn(line)
        with open(config.analysis_path(), "w", encoding="utf-8") as f:
            json.dump([c.to_dict() for c in final_clips], f, ensure_ascii=False, indent=2)

    return final_clips, segments, video_duration


def run(config: Config, logger: Logger) -> list:
    """Full pipeline: analyze() followed immediately by rendering - what
    the CLI uses by default. The GUI instead calls analyze() and
    core.clip_processor.render_clips() separately, with a review step in
    between.
    """
    final_clips, segments, _ = analyze(config, logger)

    with logger.timed("Rendering"):
        rendered = clip_processor.render_clips(
            final_clips, config.video_path, config, logger, segments
        )
        if not rendered:
            raise PipelineError("Rendering failed for all selected clips.")

    return rendered


def render_from_analysis(analysis_path: str, config: Config, logger: Logger) -> list:
    """Skip transcription/candidates/LLM ranking entirely and render
    straight from a (possibly hand-edited) analysis.json - lets you
    tweak clip start/end times yourself and re-export without waiting
    through the whole pipeline again.
    """
    from models.schemas import FinalClip

    with open(analysis_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    final_clips = [FinalClip.from_dict(d) for d in data]
    if not final_clips:
        raise PipelineError(f"'{analysis_path}' contains no clips.")

    segments = []
    transcript_path = Path(analysis_path).parent / "transcript.json"
    if transcript_path.exists():
        segments = transcriber.load_transcript(str(transcript_path))
    elif config.burn_captions or config.add_title_overlay:
        logger.warn(
            f"No transcript.json found next to '{analysis_path}' - "
            "captions/title overlay will be skipped."
        )

    logger.stage(1, f"Loaded {len(final_clips)} clip(s) from {analysis_path}")
    with logger.timed("Rendering"):
        rendered = clip_processor.render_clips(
            final_clips, config.video_path, config, logger, segments
        )
        if not rendered:
            raise PipelineError("Rendering failed for all clips.")

    return rendered


def resolve_video_targets(args: argparse.Namespace) -> list[str]:
    """Figure out which video(s) to process: a single positional path, or
    every supported video inside --batch FOLDER. The two modes are
    mutually exclusive, and missing/empty inputs fail with clean errors.
    """
    if args.batch and args.video:
        raise PipelineError(
            "Specify either a video file or --batch FOLDER, not both.",
            hint="Run one command per mode.",
        )
    if args.batch and args.render_from:
        raise PipelineError(
            "--batch cannot be combined with --render-from.",
            hint="--render-from works with a single video only.",
        )
    if args.batch:
        folder = Path(args.batch)
        if not folder.is_dir():
            raise PipelineError(
                f"Batch folder not found: {args.batch}",
                hint="Check the path and try again.",
            )
        targets = sorted(
            str(p)
            for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
        )
        if not targets:
            raise PipelineError(
                f"No supported video files found in '{args.batch}'.",
                hint=f"Supported formats: {', '.join(sorted(SUPPORTED_VIDEO_EXTENSIONS))}",
            )
        return targets
    if not args.video:
        raise PipelineError(
            "No video specified.",
            hint=(
                'Pass a video file: python app.py "video.mp4" - or a whole '
                'folder: python app.py --batch "C:\\videos"'
            ),
        )
    return [args.video]


def run_batch(args: argparse.Namespace, logger: Logger) -> int:
    """Process every video in args.batch one by one, isolating failures:
    one broken video never stops the rest. Prints a per-video summary and
    returns 0 only if every video succeeded.
    """
    targets = resolve_video_targets(args)
    logger.info(f"Batch mode: {len(targets)} video(s) to process.")
    results: list[tuple[str, bool, int, str | None, float]] = []
    batch_start = time.perf_counter()
    for i, video in enumerate(targets, 1):
        logger.info(f"[batch {i}/{len(targets)}] Processing: {video}")
        video_start = time.perf_counter()
        try:
            validate_input(video)
            video_args = argparse.Namespace(**{**vars(args), "video": video})
            num_clips = resolve_num_clips(video_args, logger, batch=True)
            config = build_config(video_args, num_clips)
            rendered = run(config, logger)
            elapsed = time.perf_counter() - video_start
            results.append((video, True, len(rendered), None, elapsed))
            logger.info(f"Done: {len(rendered)} clip(s) -> {config.project_output_dir()}")
        except PipelineError as e:
            elapsed = time.perf_counter() - video_start
            results.append((video, False, 0, e.message, elapsed))
            logger.error(f"FAILED {Path(video).name}: {e.message}")
            if e.hint:
                logger.info(f"Hint: {e.hint}")
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 - batch must survive anything
            elapsed = time.perf_counter() - video_start
            results.append((video, False, 0, f"Unexpected error: {e}", elapsed))
            logger.error(f"FAILED {Path(video).name}: unexpected error: {e}")

    total_elapsed = time.perf_counter() - batch_start
    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 60)
    print("BATCH SUMMARY")
    print("=" * 60)
    for video, success, n, err, elapsed in results:
        status = "OK    " if success else "FAILED"
        print(f"  [{status}] {Path(video).name} - {n} clip(s) - {elapsed:.1f}s")
        if err:
            print(f"          {err}")
    print(f"\n  {len(results) - len(failed)} succeeded, {len(failed)} failed, total {total_elapsed:.1f}s")
    return 0 if not failed else 1


def main() -> int:
    args = parse_args()
    logger = Logger(total_stages=5)

    try:
        if args.batch:
            return run_batch(args, logger)

        validate_input(resolve_video_targets(args)[0])

        if args.render_from:
            config = build_config(args, num_clips=0)  # num_clips unused on this path
            start = time.perf_counter()
            rendered = render_from_analysis(args.render_from, config, logger)
        else:
            num_clips = resolve_num_clips(args, logger)
            config = build_config(args, num_clips)
            start = time.perf_counter()
            rendered = run(config, logger)
        elapsed = time.perf_counter() - start

        requested = getattr(config, "num_clips", 0)
        shortfall = (
            f" (only {len(rendered)} of the {requested} you asked for - see the "
            f"report above)"
            if requested and len(rendered) < requested
            else ""
        )
        print(f"\nDone in {elapsed:.1f}s. {len(rendered)} clip(s) created{shortfall}:\n")
        for clip in rendered:
            print(f"  {clip.output_path}  ({clip.duration:.1f}s, score {clip.score:.1f})")
            print(f"    \"{clip.text[:100]}{'...' if len(clip.text) > 100 else ''}\"")
        print(f"\nOutput folder: {config.project_output_dir()}")
        logger.summary()
        return 0

    except PipelineError as e:
        logger.error(e.message)
        if e.hint:
            print(f"HINT: {e.hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001 - last-resort safety net for the CLI
        logger.error(f"Unexpected error: {e}")
        print(
            "HINT: This looks like a bug rather than a normal usage error. "
            "Please check the traceback below.",
            file=sys.stderr,
        )
        raise


if __name__ == "__main__":
    sys.exit(main())
