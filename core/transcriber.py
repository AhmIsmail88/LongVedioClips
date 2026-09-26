"""
Step 2 - Transcription.

Wraps faster-whisper. Detects CUDA availability and falls back to CPU
automatically. Language defaults to auto-detect but can be forced
(Arabic and English are the initial priority languages per spec, but
nothing here hard-codes Arabic - any language faster-whisper supports
will work).
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from pathlib import Path

from models.schemas import TranscriptSegment, Word
from utils.ffmpeg import extract_audio, probe_duration
from utils.logger import Logger, PipelineError

# Set to True once a CUDA transcription attempt has failed in this
# process: a second CUDA attempt after a cublas/cudnn failure has
# been observed to hang instead of failing fast, so later videos
# skip CUDA entirely and go straight to CPU.
_cuda_transcribe_broken = False

# Progress cadence for the segment loop: faster-whisper's generator can
# run for many minutes without yielding a single line, which readers
# interpret as a hang. One logger.info line per ~5% of the audio keeps a
# 51-minute video to ~20 lines. When the audio duration is unknown we
# fall back to a counter, reported every this many segments.
_PROGRESS_PERCENT_STEP = 5.0
_PROGRESS_SEGMENT_STEP = 50


def _cuda_is_available() -> bool:
    """Best-effort CUDA availability check without requiring torch."""
    try:
        import ctranslate2  # faster-whisper's backend

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False

def _add_nvidia_dll_dirs() -> None:
    """Expose pip-installed NVIDIA CUDA libraries to ctranslate2 on Windows.

    The nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels place their DLLs
    under site-packages/nvidia/<lib>/bin, which is NOT on the default DLL
    search path. Without this, model loading succeeds but the first
    transcription fails with 'cublas64_12.dll is not found'.
    """
    candidates: list[Path] = []
    try:
        import nvidia

        candidates.append(Path(list(nvidia.__path__)[0]))
    except (ImportError, IndexError, AttributeError):
        pass
    candidates.append(Path(sys.prefix) / "Lib" / "site-packages" / "nvidia")
    found: list[str] = []
    for base in candidates:
        for sub in ("cublas", "cudnn"):
            d = base / sub / "bin"
            if d.is_dir():
                found.append(str(d))
                try:
                    os.add_dll_directory(str(d))
                except (OSError, AttributeError):
                    pass
    if found:
        # ctranslate2 loads CUDA DLLs with plain LoadLibrary, which searches
        # PATH but not user-added DLL directories - prepending PATH is the
        # reliable cross-setup way to make cublas/cudnn loadable.
        os.environ["PATH"] = os.pathsep.join(found + [os.environ.get("PATH", "")])
        # Belt and suspenders: ctranslate2's bare-name library lookup can
        # still bypass PATH/user dirs depending on its search flags. Loading
        # every CUDA DLL once by absolute path registers it in the process
        # module table, after which any later bare-name load resolves to it.
        import ctypes

        for d_str in found:
            for dll in sorted(Path(d_str).glob("*.dll")):
                try:
                    ctypes.WinDLL(str(dll))
                except OSError:
                    pass  # a DLL with unmet deps stays skipped; best effort

def _find_cached_model_dir(model_size: str) -> str | None:
    """Locate a fully downloaded faster-whisper model in the local HF cache
    with zero network and zero hub 'completeness' logic: huggingface_hub v1
    refuses local_files_only when auxiliary files (README, .gitattributes)
    are missing even though the model itself is fully usable.
    """
    if os.environ.get("HF_HUB_CACHE"):
        hub = Path(os.environ["HF_HUB_CACHE"])
    elif os.environ.get("HF_HOME"):
        hub = Path(os.environ["HF_HOME"]) / "hub"
    else:
        hub = Path.home() / ".cache" / "huggingface" / "hub"
    snapshots = hub / f"models--Systran--faster-whisper-{model_size}" / "snapshots"
    if not snapshots.is_dir():
        return None
    required = ("model.bin", "config.json")
    for snap in sorted(snapshots.iterdir(), reverse=True):
        if snap.is_dir() and all((snap / f).exists() for f in required):
            return str(snap)
    return None


def _load_whisper_model(
    model_size: str,
    device: str,
    compute_type: str,
    compute_type_cpu: str,
    logger: Logger,
):
    """Load the faster-whisper model, preferring a fully cached local copy.

    snapshot_download(local_files_only=True) resolves the model entirely
    from the local HuggingFace cache with zero network I/O, so a cached
    model loads instantly even on a flaky or hub-blocked network instead
    of hanging on a revision check. If the model isn't cached (first
    run), we fall back to the normal path, which downloads it.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise PipelineError(
            "faster-whisper is not installed.",
            hint="Install it with: pip install faster-whisper",
        ) from e

    _add_nvidia_dll_dirs()

    cached = _find_cached_model_dir(model_size)
    if cached:
        logger.info(f"Using locally cached Whisper model: {cached}")
        return WhisperModel(cached, device=device, compute_type=compute_type)

    repo_id = f"Systran/faster-whisper-{model_size}"
    try:
        from huggingface_hub import snapshot_download

        local_path = snapshot_download(repo_id=repo_id, local_files_only=True)
        logger.info(f"Using locally cached Whisper model: {repo_id}")
        return WhisperModel(local_path, device=device, compute_type=compute_type)
    except Exception:
        # Not cached yet (or unknown layout) - use the normal download path.
        pass

    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as e:
        if device == "cuda":
            logger.warn(f"CUDA model load failed ({e}). Retrying on CPU.")
            try:
                model = WhisperModel(model_size, device="cpu", compute_type=compute_type_cpu)
            except Exception as e2:
                raise PipelineError(
                    f"Failed to load Whisper model '{model_size}'.",
                    hint=str(e2),
                ) from e2
        else:
            raise PipelineError(
                f"Failed to load Whisper model '{model_size}'.",
                hint=str(e),
            ) from e
    return model


def _finite_seconds(value: object) -> float:
    """`float(value)`, or 0.0 for anything unusable (None, NaN, inf, junk).

    Only used by the progress reporting, which must never raise no matter
    what a segment's timestamp holds.
    """
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return seconds if math.isfinite(seconds) else 0.0


def _audio_seconds_for_progress(video_path: str) -> float:
    """Best-effort duration of the source video, in seconds, for progress only.

    Returns 0.0 whenever the probe is unusable (missing ffprobe, an
    unreadable container, a zero/negative/NaN duration): progress reporting
    must never be able to break transcription. The caller degrades to a
    segment-counter message in that case.
    """
    try:
        total = float(probe_duration(video_path))
    except Exception:
        return 0.0
    return total if math.isfinite(total) and total > 0 else 0.0


def transcribe(
    video_path: str,
    model_size: str,
    language: str | None,
    device: str,
    compute_type_gpu: str,
    compute_type_cpu: str,
    logger: Logger,
) -> list[TranscriptSegment]:
    """Transcribe a video's audio track into timestamped segments."""

    global _cuda_transcribe_broken

    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise PipelineError(
            "faster-whisper is not installed.",
            hint="Install it with: pip install faster-whisper",
        ) from e

    resolved_device = device
    compute_type = compute_type_cpu
    if device == "auto":
        if _cuda_is_available():
            resolved_device = "cuda"
            compute_type = compute_type_gpu
        else:
            resolved_device = "cpu"
            compute_type = compute_type_cpu
    elif device == "cuda":
        if not _cuda_is_available():
            logger.warn("CUDA requested but not available. Falling back to CPU.")
            resolved_device = "cpu"
            compute_type = compute_type_cpu
        else:
            compute_type = compute_type_gpu
    else:
        resolved_device = "cpu"
        compute_type = compute_type_cpu

    if _cuda_transcribe_broken and resolved_device == "cuda":
        logger.warn("CUDA transcription already failed earlier in this run - using CPU for this video.")
        resolved_device = "cpu"
        compute_type = compute_type_cpu

    logger.info(f"Whisper device={resolved_device}, compute_type={compute_type}")

    model = _load_whisper_model(
        model_size, resolved_device, compute_type, compute_type_cpu, logger
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        wav_path = os.path.join(tmp_dir, "audio.wav")
        logger.info("Extracting audio track...")
        extract_audio(video_path, wav_path)

        logger.info("Running transcription (this can take a while)...")
        try:
            segments_iter, info = model.transcribe(
                wav_path,
                language=language,
                vad_filter=True,
                word_timestamps=True,
            )
        except Exception as e:
            if resolved_device != "cuda":
                raise PipelineError("Transcription failed.", hint=str(e)) from e
            _cuda_transcribe_broken = True
            logger.warn(f"CUDA transcription failed ({e}). Retrying on CPU.")
            model = _load_whisper_model(
                model_size, "cpu", compute_type_cpu, compute_type_cpu, logger
            )
            try:
                segments_iter, info = model.transcribe(
                    wav_path,
                    language=language,
                    vad_filter=True,
                    word_timestamps=True,
                )
            except Exception as e2:
                raise PipelineError("Transcription failed.", hint=str(e2)) from e2

        logger.info(
            f"Detected language: {info.language} (p={info.language_probability:.2f})"
        )

        # Probed once, outside the loop, purely for progress reporting.
        total_seconds = _audio_seconds_for_progress(video_path)
        next_percent = _PROGRESS_PERCENT_STEP
        last_percent = 0.0
        segment_count = 0

        segments: list[TranscriptSegment] = []
        for seg in segments_iter:
            text = seg.text.strip()
            if text:
                words = [
                    Word(start=w.start, end=w.end, text=w.word.strip())
                    for w in (seg.words or [])
                    if w.word.strip()
                ]
                segments.append(
                    TranscriptSegment(start=seg.start, end=seg.end, text=text, words=words)
                )

            # Progress on the processed timeline (segment end vs audio
            # duration), deliberately independent of whether this segment
            # carried text, and never allowed to raise.
            segment_count += 1
            if total_seconds > 0:
                end_seconds = _finite_seconds(seg.end)
                percent = min(100.0, max(0.0, end_seconds / total_seconds * 100.0))
                if percent >= next_percent:
                    # The truncated percent (not a rounded one) keeps the
                    # printed values strictly increasing.
                    logger.info(
                        f"Transcribing... {int(percent)}% "
                        f"({int(end_seconds)}s of {int(total_seconds)}s of audio)"
                    )
                    last_percent = percent
                    next_percent = (
                        math.floor(percent / _PROGRESS_PERCENT_STEP) + 1
                    ) * _PROGRESS_PERCENT_STEP
            elif segment_count % _PROGRESS_SEGMENT_STEP == 0:
                logger.info(
                    f"Transcribing... {segment_count} segments processed "
                    "(audio duration unknown)"
                )

    if not segments:
        raise PipelineError(
            "Transcription produced no text.",
            hint="The video may be silent, music-only, or in an unsupported language.",
        )

    # Closing line, so the log never ends mid-percentage.
    if total_seconds > 0:
        if last_percent < 100.0:
            logger.info(
                f"Transcribing... 100% "
                f"({int(total_seconds)}s of {int(total_seconds)}s of audio)"
            )
    else:
        logger.info(f"Transcribing... done ({segment_count} segments processed)")

    return segments


def save_transcript(segments: list[TranscriptSegment], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([s.to_dict() for s in segments], f, ensure_ascii=False, indent=2)


def load_transcript(path: str) -> list[TranscriptSegment]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [TranscriptSegment.from_dict(d) for d in data]
