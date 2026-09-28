"""
Step 6 - Video Processing / Rendering.

Cuts and exports final clips as vertical (9:16) videos using FFmpeg,
with NVENC hardware acceleration when available and a transparent CPU
fallback otherwise. The crop strategy (currently: reliable center crop)
is isolated in utils/ffmpeg.render_vertical_clip so it can be swapped
for Smart Auto-Reframe later without touching this module's interface.

If burn_captions or add_title_overlay is enabled, a second ffmpeg pass
(core/captions.py + utils/ffmpeg.burn_subtitles) runs after the main
render to overlay them - kept as a separate pass so it works uniformly
regardless of which crop strategy produced the base clip.

If fix_lighting is enabled, the clip's brightness is measured from the
source and a bounded correction is folded into the main render's filter
chain (core/lighting.py) - one encode, and identical on both crop paths.
"""

from __future__ import annotations

import os

from config import Config
from core import lighting
from models.schemas import FinalClip, TranscriptSegment
from utils.ffmpeg import burn_subtitles, nvenc_available, render_vertical_clip
from utils.resume import read_meta, write_meta
from utils.logger import Logger


def render_clips(
    clips: list[FinalClip],
    video_path: str,
    config: Config,
    logger: Logger,
    segments: list[TranscriptSegment] | None = None,
) -> list[FinalClip]:
    if not clips:
        return []

    clips_dir = config.clips_dir()
    os.makedirs(clips_dir, exist_ok=True)

    wants_overlay = config.burn_captions or config.add_title_overlay
    if wants_overlay and not segments:
        logger.warn(
            "Captions/title overlay requested but no transcript is available - "
            "skipping the overlay for this run."
        )
        wants_overlay = False

    use_nvenc = False
    if config.use_smart_reframe:
        logger.info(
            "Smart reframe enabled - tracking faces/speakers instead of a "
            "fixed center crop (slower, frame-by-frame)."
        )
    else:
        use_nvenc = nvenc_available()
        if use_nvenc:
            logger.info("NVIDIA NVENC encoder detected - using GPU encoding.")
        else:
            logger.info(
                "NVIDIA NVENC encoder is not available. Falling back to CPU encoding."
            )

    if config.fix_lighting:
        logger.info(
            "Lighting fix enabled - measuring each clip's brightness and "
            "correcting exposure where needed (strength "
            f"{config.lighting_strength:.2f}, target YAVG "
            f"{config.lighting_target_luma:.0f})."
        )

    rendered: list[FinalClip] = []
    for clip in clips:
        filename = f"clip_{clip.index:02d}.mp4"
        output_path = str(clips_dir / filename)
        render_target = str(clips_dir / f"_raw_{filename}") if wants_overlay else output_path

        # Final guarantee against mid-word cuts: whatever produced
        # these boundaries (analysis or a hand edit in the GUI), land
        # them on word edges using the transcript's word timestamps.
        if segments:
            from core.quality_filter import _word_points, snap_to_word_edges

            _edges = _word_points(segments)
            if snap_to_word_edges(clip, _edges):
                logger.info(
                    f"{filename}: boundaries adjusted to complete words "
                    f"-> {clip.start:.1f}s-{clip.end:.1f}s"
                )

        # Resume: reuse a clip an earlier (interrupted) run already
        # finished - but only when its exact span matches, so edited
        # boundaries always render again.
        _already = False
        try:
            _already = os.path.exists(output_path) and os.path.getsize(output_path) > 0
        except OSError:
            _already = False
        if _already:
            _meta = read_meta(output_path)
            if (
                _meta.get("start") == round(clip.start, 3)
                and _meta.get("end") == round(clip.end, 3)
            ):
                logger.info(f"Resume: {filename} already rendered - skipping.")
                clip.output_path = output_path
                rendered.append(clip)
                continue

        logger.info(
            f"Rendering {filename}: {clip.start:.1f}s-{clip.end:.1f}s "
            f"(score {clip.score:.1f})"
        )
        try:
            # Measured lighting correction for THIS clip, shared by both
            # render paths below (it becomes part of their filter chain,
            # so --captions/--title-overlay - which run as a separate
            # pass afterwards - compose with it unchanged).
            light_plan = lighting.plan_lighting(
                video_path, clip.start, clip.end, config, logger, label=filename
            )
            extra_filter = light_plan.filter_string

            if config.use_smart_reframe:
                from core.smart_reframe import render_vertical_clip_smart

                render_vertical_clip_smart(
                    video_path=video_path,
                    output_path=render_target,
                    start=clip.start,
                    end=clip.end,
                    width=config.output_width,
                    height=config.output_height,
                    audio_codec=config.audio_codec,
                    normalize_audio=config.normalize_audio,
                    logger=logger,
                    extra_video_filter=extra_filter,
                )
            else:
                render_vertical_clip(
                    video_path=video_path,
                    output_path=render_target,
                    start=clip.start,
                    end=clip.end,
                    width=config.output_width,
                    height=config.output_height,
                    use_nvenc=use_nvenc,
                    video_codec_gpu=config.video_codec_gpu,
                    video_codec_cpu=config.video_codec_cpu,
                    audio_codec=config.audio_codec,
                    normalize_audio=config.normalize_audio,
                    extra_video_filter=extra_filter,
                )

            if light_plan.active:
                lighting.verify_lighting(light_plan, render_target, logger, label=filename)

            if wants_overlay:
                _burn_overlay(clip, render_target, output_path, segments, config, use_nvenc, logger)

            clip.output_path = output_path
            rendered.append(clip)
            write_meta(
                output_path,
                start=round(clip.start, 3),
                end=round(clip.end, 3),
            )
        except Exception as e:
            logger.warn(f"Failed to render {filename}: {e}")
            continue

    return rendered


def _burn_overlay(
    clip: FinalClip,
    render_target: str,
    output_path: str,
    segments: list[TranscriptSegment],
    config: Config,
    use_nvenc: bool,
    logger: Logger,
) -> None:
    from core.captions import build_ass_file

    ass_path = build_ass_file(
        clip=clip,
        segments=segments,
        output_width=config.output_width,
        output_height=config.output_height,
        words_per_caption=config.words_per_caption,
        burn_captions=config.burn_captions,
        add_title_overlay=config.add_title_overlay,
        title_duration_seconds=config.title_duration_seconds,
    )

    try:
        if ass_path is None:
            # Nothing to burn (e.g. captions on but no words fell in range) -
            # just use the plain render as the final output.
            os.replace(render_target, output_path)
            return

        logger.info(f"Burning captions/title into {os.path.basename(output_path)}...")
        burn_subtitles(
            input_path=render_target,
            output_path=output_path,
            ass_path=ass_path,
            use_nvenc=use_nvenc,
            video_codec_gpu=config.video_codec_gpu,
            video_codec_cpu=config.video_codec_cpu,
            audio_codec=config.audio_codec,
        )
    finally:
        if ass_path and os.path.exists(ass_path):
            os.remove(ass_path)
        if os.path.exists(render_target) and render_target != output_path:
            os.remove(render_target)
