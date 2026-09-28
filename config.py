"""
Central configuration for the pipeline.

Nothing here is hard-coded into the logic of other modules — every stage
reads its settings from a Config object so behavior can be tuned (or
overridden from the CLI) without touching pipeline code.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path


SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}

# Audio-only sources. The transcription path never cared about the
# container - ffmpeg extracts (or simply re-encodes) the audio track either
# way - so the "extract text" flow in the GUI accepts plain audio too and
# never has to think about video at all.
SUPPORTED_AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".wav",
    ".aac",
    ".ogg",
    ".flac",
    ".opus",
    ".wma",
}

# Anything that can be transcribed: a video (audio track pulled out by
# ffmpeg) or an audio file. Used by the GUI's text-extraction uploader;
# the clip pipeline keeps using SUPPORTED_VIDEO_EXTENSIONS alone.
SUPPORTED_MEDIA_EXTENSIONS = SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_AUDIO_EXTENSIONS

# Floor for the adaptive clip-length band (see plan_clip_band): when a
# source is too short to hold the requested number of clips at the
# requested length, the band shrinks - but never below this, because
# below it a "clip" stops being a usable short-form video. If even this
# floor doesn't fit, the request is reported as infeasible rather than
# silently under-produced.
MIN_ADAPTIVE_CLIP_SECONDS = 8.0

# Default maximum time overlap between two selected clips, as a
# fraction of the shorter clip. 0.30 was too lax in practice: on a
# real 51-minute run it let two clips share 17% and 25% of their
# length, so the very same sentences were exported twice. 0.15 still
# leaves room on short sources (a 60s video can still hold 5 clips of
# ~12s) while rejecting the duplicated-content cases that showed up.
DEFAULT_MAX_OVERLAP_RATIO = 0.15

# Ceiling for the "split the video into N parts" mode
# (Config.split_evenly): the even share of a very long video can be a
# very long clip, and beyond this it stops being a clip at all.
ABSOLUTE_MAX_CLIP_SECONDS = 1800.0

# Preset clip-length bands per target platform. Applied when the user
# selects --platform and does not explicitly set --min-duration/
# --max-duration (explicit user values always win).
PLATFORM_PRESETS: dict[str, dict[str, float]] = {
    "tiktok": {"min_duration": 15.0, "max_duration": 60.0},
    "reels": {"min_duration": 15.0, "max_duration": 90.0},
    "shorts": {"min_duration": 20.0, "max_duration": 60.0},
}


def suggest_num_clips(duration_seconds: float) -> int:
    """Suggest a sensible clip count based on video length.

    A fixed default (e.g. always 5) badly under-covers a long video: on a
    59-minute podcast, 5 clips at up to 60s each only touches ~5% of the
    content. This scales roughly 1 clip per 4 minutes of video, clamped
    to a sane range so very short or very long videos don't get an
    absurd number of clips either way.
    """
    duration_minutes = max(duration_seconds, 0) / 60.0
    suggested = round(duration_minutes / 4)
    return max(5, min(25, suggested))


def max_nonoverlapping_clips(
    video_duration: float,
    clip_duration: float,
    max_overlap_ratio: float = DEFAULT_MAX_OVERLAP_RATIO,
) -> int:
    """How many clips of (at most) `clip_duration` seconds can coexist in a
    `video_duration`-second source under the overlap rule used by
    quality_filter.remove_overlaps.

    Two clips may share at most `max_overlap_ratio` of the shorter one, so
    their starts must be at least `clip_duration * (1 - max_overlap_ratio)`
    apart. That stride is what bounds the count - it is the physical
    capacity of the source, independent of how good any clip's content is.
    """
    if video_duration <= 0 or clip_duration <= 0:
        return 0
    if video_duration < clip_duration:
        return 0
    stride = clip_duration * max(0.05, 1.0 - max_overlap_ratio)
    return int((video_duration - clip_duration) // stride) + 1


@dataclass
class ClipBandPlan:
    """The clip-length band actually used for a run, plus why it differs
    from what the user asked for - so callers can report it instead of
    quietly producing fewer clips than requested.
    """

    video_duration: float
    num_clips: int
    requested_min_duration: float
    requested_max_duration: float
    min_duration: float
    max_duration: float
    adapted: bool
    feasible: bool
    max_feasible_clips: int
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.num_clips} clip(s) requested from a {self.video_duration:.0f}s "
            f"source; using a {self.min_duration:.1f}-{self.max_duration:.1f}s band "
            f"(requested {self.requested_min_duration:.0f}-"
            f"{self.requested_max_duration:.0f}s); source can hold at most "
            f"{self.max_feasible_clips} non-overlapping clip(s) at this length."
        )


def plan_clip_band(
    video_duration: float,
    num_clips: int,
    min_duration: float,
    max_duration: float,
    max_overlap_ratio: float = DEFAULT_MAX_OVERLAP_RATIO,
    floor: float = MIN_ADAPTIVE_CLIP_SECONDS,
    split_evenly: bool = False,
) -> ClipBandPlan:
    """Decide the effective [min, max] clip-length band for a run.

    The problem this solves: `num_clips x max_duration` can be far larger
    than the source. On a 56s video, "5 clips of up to 60s" is impossible
    - every selected clip spans most of the source, they all overlap each
    other, and the overlap rule throws almost all of them away, so the
    user asks for 5 and gets 1-2.

    So: if the source can't hold `num_clips` clips at `max_duration`, the
    effective max shrinks towards `video_duration / num_clips` - exactly
    the length at which N clips tile the source end to end with no overlap
    at all. The band is never widened beyond what the user asked for, and
    never shrunk below `floor`.
    """
    num_clips = max(1, int(num_clips))
    notes: list[str] = []

    eff_max = float(max_duration)
    eff_min = float(min_duration)

    if split_evenly and video_duration > 0:
        # The user picked a clip count and nothing else: the clip length
        # is this video's share per clip, so N clips tile the whole
        # video. Callers that keep the requested min/max act as the
        # floor and the (generous) ceiling for that share.
        share = video_duration / num_clips
        # The even split may ask for LONGER clips (the share per clip),
        # but never longer than the caller's own maximum: an explicit
        # "shorts: max 60s" must not turn into 10-minute clips just
        # because five clips could tile the whole video.
        capped = max(floor, float(max_duration))
        eff_max = min(ABSOLUTE_MAX_CLIP_SECONDS, max(floor, share), capped)
        eff_min = max(floor, min(float(min_duration), eff_max))
        if eff_max >= share:
            # Uncapped: keep the coverage floor so N clips still
            # spread across the video instead of clustering.
            eff_min = max(eff_min, min(eff_max, eff_max * 0.6))
        notes.append(
            f"Even split: {num_clips} clip(s) across {video_duration:.0f}s of "
            f"video gives {share:.1f}s per clip - using a "
            f"{eff_min:.1f}-{eff_max:.1f}s band."
        )

    capacity = max_nonoverlapping_clips(video_duration, eff_max, max_overlap_ratio)
    share_per_clip = video_duration / num_clips if num_clips > 0 else eff_max
    # Shrink the band in two cases, both of which make the clip length
    # follow the requested clip count:
    #   * the request cannot physically fit (`capacity`), or
    #   * the requested clip length is longer than this video's share per
    #     clip, i.e. the clips would each cover more than their share.
    if (
        video_duration > 0
        and eff_max > floor
        and not split_evenly
        and (capacity < num_clips or share_per_clip < eff_max)
    ):
        target = share_per_clip
        new_max = min(float(max_duration), max(floor, target))
        if new_max < eff_max - 1e-9:
            if capacity < num_clips:
                notes.append(
                    f"{num_clips} clips of up to {max_duration:.0f}s need "
                    f"{num_clips * max_duration:.0f}s of source, but the video is only "
                    f"{video_duration:.0f}s long. Shrinking the max clip length to "
                    f"{new_max:.1f}s (= {video_duration:.0f}s / {num_clips} clips) so "
                    f"{num_clips} non-overlapping clips can actually fit."
                )
            else:
                notes.append(
                    f"{num_clips} clips of up to {max_duration:.0f}s would each cover "
                    f"more than this video's share per clip ({share_per_clip:.0f}s). "
                    f"Shrinking the max clip length to {new_max:.1f}s "
                    f"(= {video_duration:.0f}s / {num_clips} clips)."
                )
            eff_max = new_max

    if eff_min > eff_max:
        notes.append(
            f"Lowering the min clip length from {eff_min:.1f}s to {eff_max:.1f}s "
            f"so it stays inside the shrunk band."
        )
        eff_min = eff_max

    # Keep a usable (non-degenerate) band when the shrink made min and max
    # meet, while still respecting the floor and never inverting the band.
    if eff_max - eff_min < 1e-9 and eff_max > floor:
        widened_min = min(eff_max, max(floor, eff_max * 0.6))
        if abs(widened_min - eff_min) > 1e-9:
            notes.append(
                f"Widening the min clip length from {eff_min:.1f}s to "
                f"{widened_min:.1f}s so the shrunk band isn't a single fixed length."
            )
            eff_min = widened_min

    capacity = max_nonoverlapping_clips(video_duration, eff_max, max_overlap_ratio)
    feasible = capacity >= num_clips
    if not feasible:
        notes.append(
            f"Source is only {video_duration:.0f}s: at most {capacity} "
            f"non-overlapping clip(s) of {eff_min:.0f}-{eff_max:.0f}s fit, so "
            f"{num_clips} clip(s) cannot be produced."
        )

    return ClipBandPlan(
        video_duration=video_duration,
        num_clips=num_clips,
        requested_min_duration=float(min_duration),
        requested_max_duration=float(max_duration),
        min_duration=eff_min,
        max_duration=eff_max,
        adapted=bool(notes),
        feasible=feasible,
        max_feasible_clips=capacity,
        notes=notes,
    )


@dataclass
class ScoringWeights:
    """Configurable weights for LLM clip scoring. Must sum to 1.0."""

    hook: float = 0.30
    content: float = 0.20
    story: float = 0.20
    emotion: float = 0.15
    standalone: float = 0.10
    ending: float = 0.05

    def weighted_score(self, s) -> float:
        return (
            s.hook_score * self.hook
            + s.content_score * self.content
            + s.story_score * self.story
            + s.emotion_score * self.emotion
            + s.standalone_score * self.standalone
            + s.ending_score * self.ending
        )


@dataclass
class Config:
    # Input / output
    video_path: str = ""
    output_dir: str = "output"

    # Transcription
    whisper_model: str = "medium"
    language: str | None = None  # None => auto-detect
    device: str = "auto"  # "auto" | "cuda" | "cpu"
    compute_type_gpu: str = "float16"
    compute_type_cpu: str = "int8"

    # Candidate generation
    min_duration: float = 20.0
    max_duration: float = 60.0
    candidate_window_sizes: tuple[int, ...] = (3, 5, 7)  # sentences per window
    candidate_stride: int = 2  # sentence step between window starts

    # Candidate pre-filtering: the raw sliding-window generator can
    # produce tens of thousands of windows on a long video, and ranking
    # all of them with the LLM would take days. Keep only the best N by
    # a cheap local heuristic, spread across the whole timeline.
    max_candidates: int = 600

    # LLM ranking
    ollama_model: str = "qwen3.5:9b"
    llm_batch_size: int = 16
    # Ollama's default context window silently truncates long prompts:
    # the system instructions and most candidates get dropped, and the
    # model then returns unusable (truncated) JSON. Measured: a 13-16
    # candidate Arabic batch needs ~4700-6400 tokens while the default
    # context processed only ~2050. Sized so a full batch plus its JSON
    # response fits comfortably.
    llm_num_ctx: int = 8192
    # Hard cap on generated tokens per request.
    llm_num_predict: int = 4096
    ollama_host: str = "http://localhost:11434"
    scoring_weights: ScoringWeights = field(default_factory=ScoringWeights)
    llm_timeout_seconds: int = 120
    llm_max_retries: int = 2

    # Resume: reuse the transcript / candidate list / scores / already
    # rendered clips produced by an EARLIER run of the same video with
    # the same settings, so an interrupted analysis does not start over
    # (transcription ~10 min and LLM ranking ~30 min are far too
    # expensive to lose). Artifacts carry a sidecar .meta.json with the
    # inputs they were made from; any mismatch recomputes that stage.
    resume: bool = True

    # Quality filtering / selection
    num_clips: int = 5
    # Maximum time overlap between two selected clips, as a fraction of
    # the shorter one. Two clips sharing more than this are treated as
    # covering the same content, and the lower-scored one is dropped.
    max_overlap_ratio: float = DEFAULT_MAX_OVERLAP_RATIO
    # Second, content-level duplicate gate: if two clips share this many
    # of the shorter one's 8-word sequences, the lower-scored is dropped
    # even when their spans barely overlap - catches the same content
    # retold later in a long video. 1.0 disables it.
    max_text_similarity: float = 0.5
    min_score_threshold: float = 40.0

    # Boundary snapping (see quality_filter.snap_to_speech_boundary).
    # Clips are moved onto the spoken "run" they land inside using
    # word-level timestamps, because the punctuation-based logic never
    # fires on dialectal Arabic transcripts (faster-whisper emits no
    # punctuation for them). `pause_threshold_seconds` is how much
    # silence separates two runs; `boundary_snap_max_seconds` is how
    # far a boundary may travel to reach one.
    pause_threshold_seconds: float = 0.45
    boundary_snap_max_seconds: float = 2.5

    # Context expansion
    context_expand_max_seconds: float = 3.0

    # "Split the video into N parts": the clip length is derived from
    # the requested count (video_duration / num_clips) and the length
    # controls above only act as the floor/ceiling for that share, so
    # the user picks the count and nothing else. Off by default on the
    # CLI (short-form behaviour is unchanged); the GUI turns it on.
    split_evenly: bool = False

    # Video export
    output_width: int = 1080
    output_height: int = 1920
    # Opt-in face/speaker-tracking crop instead of a fixed center crop.
    # Needs extra dependencies (see requirements-smart-reframe.txt) and is
    # noticeably slower - see core/smart_reframe.py.
    use_smart_reframe: bool = False

    # Burned-in captions / intro title overlay (core/captions.py). Both
    # require an extra ffmpeg re-encode pass per clip after the main
    # render (a few extra seconds per clip, not minutes).
    burn_captions: bool = False
    add_title_overlay: bool = False
    words_per_caption: int = 5
    title_duration_seconds: float = 4.0
    # Normalize each clip's loudness to a -16 LUFS integrated target
    # (pure ffmpeg loudnorm, no extra dependency) so clips rendered from
    # different sources sound consistently loud on short-form platforms.
    normalize_audio: bool = True

    # Automatic lighting correction ("إصلاح الإضاءة"). Off by default.
    # The correction is *measured* per clip (ffmpeg signalstats on the
    # source, seeked to the clip range - see core/lighting.py), not a
    # fixed magic constant, so an already well-exposed clip is left
    # essentially untouched.
    fix_lighting: bool = False
    # 0.0 = no correction, 1.0 = the full bounded correction, values above
    # 1.0 push further towards the target (still clamped to safe limits).
    lighting_strength: float = 1.0
    # Mid-luma target the measured clip average is moved towards, on the
    # 0-255 scale. ~128 is a neutral middle grey; 118 keeps a little more
    # headroom so highlights don't clip.
    lighting_target_luma: float = 118.0

    video_codec_gpu: str = "h264_nvenc"
    video_codec_cpu: str = "libx264"
    audio_codec: str = "aac"

    def project_name(self) -> str:
        return Path(self.video_path).stem

    def candidate_stride_for(self, plan: ClipBandPlan) -> int:
        """Sentence stride to use when generating candidate windows.

        When the band had to shrink for a short source, halve the stride:
        candidate generation is cheap there, and the extra window start
        positions are exactly what lets N clips be placed without
        overlapping each other (the old stride left gaps the size of a
        whole clip, so no 5-clip layout existed).
        """
        if not plan.adapted:
            return self.candidate_stride
        return max(1, self.candidate_stride // 2)

    def project_output_dir(self) -> Path:
        return Path(self.output_dir) / self.project_name()

    def clips_dir(self) -> Path:
        return self.project_output_dir() / "clips"

    def transcript_path(self) -> Path:
        return self.project_output_dir() / "transcript.json"

    def candidates_path(self) -> Path:
        return self.project_output_dir() / "candidates.json"

    def analysis_path(self) -> Path:
        return self.project_output_dir() / "analysis.json"


def apply_band_plan(config: Config, plan: ClipBandPlan) -> Config:
    """Return a copy of `config` carrying the planned duration band and
    the matching candidate stride.

    Every stage that cares about clip length (candidate generation,
    context expansion, overlap selection) reads it from Config, so one
    substitution keeps them consistent - and the original user-requested
    values stay visible in `plan` for reporting.
    """
    if (
        plan.min_duration == config.min_duration
        and plan.max_duration == config.max_duration
        and config.candidate_stride_for(plan) == config.candidate_stride
    ):
        return config
    return replace(
        config,
        min_duration=plan.min_duration,
        max_duration=plan.max_duration,
        candidate_stride=config.candidate_stride_for(plan),
    )
