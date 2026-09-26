"""Deterministic progress-logging tests for core.transcriber.transcribe().

faster-whisper is never imported for real: a stub module is injected into
sys.modules, `_load_whisper_model` is replaced by a stub model, ffmpeg is
stubbed out (both the audio extraction and the duration probe) and
TemporaryDirectory is replaced by an in-memory path, so this test touches
neither the GPU, nor ffmpeg, nor the filesystem.

Run:
    .venv\\Scripts\\python.exe tests\\test_transcribe_progress.py
"""

from __future__ import annotations

import contextlib
import re
import sys
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import QuietLogger, REPO_ROOT  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

from core import transcriber  # noqa: E402
from models.schemas import TranscriptSegment, Word  # noqa: E402

PROGRESS_RE = re.compile(
    r"^Transcribing\.\.\. (?P<percent>\d+)% \((?P<done>\d+)s of (?P<total>\d+)s of audio\)$"
)
FALLBACK_RE = re.compile(r"^Transcribing\.\.\. (?P<count>\d+) segments processed")
DONE_RE = re.compile(r"^Transcribing\.\.\. done \((?P<count>\d+) segments processed\)$")


# --------------------------------------------------------------------------
# Stand-ins for faster-whisper's objects
# --------------------------------------------------------------------------


@dataclass
class _StubWord:
    start: float
    end: float
    word: str


@dataclass
class _StubSegment:
    start: float
    end: float
    text: str
    words: list = field(default_factory=list)


class _StubInfo:
    language = "en"
    language_probability = 0.98


class _StubModel:
    """Minimal stand-in for faster_whisper.WhisperModel."""

    def __init__(self, segments: list[_StubSegment]):
        self._segments = segments

    def transcribe(self, audio_path: str, **kwargs):
        return iter(self._segments), _StubInfo()


def _fake_faster_whisper() -> types.ModuleType:
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = _StubModel
    return module


@contextlib.contextmanager
def _stub_temporary_directory(*args, **kwargs):
    """Stand-in for tempfile.TemporaryDirectory - no real directory is made."""
    yield "C:/nonexistent/lvc_progress_stub"


class _Patcher:
    """Minimal save/restore patcher, in the style of the other tests here."""

    def __init__(self) -> None:
        self._saved: list[tuple[object, str, object]] = []

    def set(self, obj: object, name: str, value: object) -> None:
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        for obj, name, value in reversed(self._saved):
            setattr(obj, name, value)
        self._saved.clear()


def _fake_segments(
    count: int, step: float, textless: tuple[int, ...] = ()
) -> list[_StubSegment]:
    """`count` segments of `step` seconds each, back to back from t=0.

    `textless` gives 1-based indices whose segments carry no text, to prove
    progress still advances when segments are dropped from the transcript.
    """
    segments: list[_StubSegment] = []
    for i in range(1, count + 1):
        start = (i - 1) * step
        end = i * step
        text = "" if i in textless else f"segment {i}"
        words = [] if i in textless else [_StubWord(start, end, f"segment {i}")]
        segments.append(_StubSegment(start=start, end=end, text=text, words=words))
    return segments


def _expected_transcript(
    segments: list[_StubSegment],
) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(
            start=s.start,
            end=s.end,
            text=s.text.strip(),
            words=[
                Word(start=w.start, end=w.end, text=w.word.strip()) for w in s.words
            ],
        )
        for s in segments
        if s.text.strip()
    ]


def _progress_lines(logger: QuietLogger) -> list[str]:
    return [line for line in logger.lines if line.startswith("Transcribing... ")]


def _percent_of(line: str) -> int:
    match = PROGRESS_RE.match(line)
    assert match is not None, f"not a percent line: {line!r}"
    return int(match.group("percent"))


class TranscribeProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patch = _Patcher()
        self.logger = QuietLogger()
        self._had_real_module = "faster_whisper" in sys.modules
        self._real_module = sys.modules.get("faster_whisper")
        sys.modules["faster_whisper"] = _fake_faster_whisper()

    def tearDown(self) -> None:
        self.patch.restore()
        if self._had_real_module:
            sys.modules["faster_whisper"] = self._real_module
        else:
            sys.modules.pop("faster_whisper", None)

    def _prepare(self, segments, duration) -> None:
        self.patch.set(transcriber, "_load_whisper_model", lambda *a, **k: _StubModel(segments))
        self.patch.set(transcriber, "extract_audio", lambda *a, **k: None)
        self.patch.set(transcriber.tempfile, "TemporaryDirectory", _stub_temporary_directory)
        if isinstance(duration, BaseException):
            def _boom(path):
                raise duration

            self.patch.set(transcriber, "probe_duration", _boom)
        else:
            self.patch.set(transcriber, "probe_duration", lambda path: duration)

    def _run(self, segments, duration):
        self._prepare(segments, duration)
        return transcriber.transcribe(
            video_path="C:/nonexistent/video.mp4",
            model_size="tiny",
            language="en",
            device="cpu",  # keeps the CUDA probe (ctranslate2) out of the test
            compute_type_gpu="float16",
            compute_type_cpu="int8",
            logger=self.logger,
        )

    # -- known duration: percent lines -----------------------------------

    def test_percent_lines_are_throttled_and_monotonic(self):
        segments = _fake_segments(count=100, step=10.0, textless=(7, 13))
        returned = self._run(segments, duration=1000.0)

        progress = _progress_lines(self.logger)
        percents = [_percent_of(line) for line in progress]

        # One line per 5% of the processed timeline, at the exact boundaries.
        self.assertEqual(percents, list(range(5, 101, 5)))
        self.assertLessEqual(len(progress), 20, "throttle cap is one line per 5%")

        # Monotonic, never past 100, and never two lines inside one percent.
        for a, b in zip(percents, percents[1:]):
            self.assertGreater(b, a)
            self.assertGreaterEqual(b - a, 1)
        self.assertTrue(all(0 <= p <= 100 for p in percents))

        # Each line reports the segment end against the probed duration.
        self.assertEqual(progress[0], "Transcribing... 5% (50s of 1000s of audio)")
        self.assertEqual(progress[2], "Transcribing... 15% (150s of 1000s of audio)")
        self.assertEqual(progress[-1], "Transcribing... 100% (1000s of 1000s of audio)")
        self.assertEqual(sum(1 for p in percents if p == 100), 1)
        self.assertIn("100%", progress[-1])

        # A probe failure fallback must not appear at all here.
        joined = "\n".join(progress)
        self.assertNotIn("segments processed", joined)
        # Existing lines are untouched.
        self.assertIn("Detected language: en (p=0.98)", self.logger.lines)

        # The returned transcript is exactly what the old code produced.
        self.assertEqual(returned, _expected_transcript(segments))
        self.assertEqual(len(returned), 98)

    def test_irregular_vad_style_segments_stay_within_the_throttle(self):
        # VAD-filtered output is not evenly spaced: one segment can jump
        # most of the timeline. The throttle must still hold.
        ends = [0.4, 1.2, 4.9, 5.1, 9.8, 10.0, 33.0, 33.4, 71.0, 99.9, 100.0]
        segments = [
            _StubSegment(start=0.0 if i == 0 else ends[i - 1], end=end, text=f"s{i}")
            for i, end in enumerate(ends)
        ]
        self._run(segments, duration=100.0)

        percents = [_percent_of(line) for line in _progress_lines(self.logger)]
        self.assertEqual(percents, [5, 10, 33, 71, 99, 100])
        self.assertEqual(len(percents), len(set(percents)))
        self.assertLessEqual(len(percents), 20)
        for a, b in zip(percents, percents[1:]):
            self.assertGreater(b, a)

    def test_short_audio_emits_only_the_closing_line(self):
        segments = _fake_segments(count=2, step=1.0)
        returned = self._run(segments, duration=1000.0)
        progress = _progress_lines(self.logger)
        self.assertEqual(progress, ["Transcribing... 100% (1000s of 1000s of audio)"])
        self.assertEqual(len(returned), 2)

    def test_unparseable_segment_end_does_not_raise(self):
        segments = _fake_segments(count=20, step=10.0)
        segments[3].end = None  # type: ignore[assignment]
        returned = self._run(segments, duration=200.0)
        self.assertEqual(len(returned), 20)
        percents = [_percent_of(line) for line in _progress_lines(self.logger)]
        # 10s segments out of 200s = 5% each; the broken one contributes
        # nothing at all (its 20% tick is simply skipped), and the run still
        # finishes with its closing 100% line.
        expected = [p for p in range(5, 101, 5) if p != 20]
        self.assertEqual(percents, expected)

    def test_non_finite_segment_end_does_not_raise(self):
        for bad in (float("inf"), float("nan"), "not a number", 10 ** 400):
            with self.subTest(end=bad):
                self.logger = QuietLogger()
                segments = _fake_segments(count=10, step=10.0)
                segments[4].end = bad  # type: ignore[assignment]
                returned = self._run(segments, duration=100.0)
                self.assertEqual(len(returned), 10)
                percents = [_percent_of(line) for line in _progress_lines(self.logger)]
                self.assertEqual(percents, [10, 20, 30, 40, 60, 70, 80, 90, 100])

    # -- unknown duration: counter fallback ------------------------------

    def test_probe_exception_degrades_to_counter_messages(self):
        segments = _fake_segments(count=120, step=10.0, textless=(7, 13))
        returned = self._run(
            segments, duration=transcriber.PipelineError("no ffprobe here")
        )
        self.assertEqual(len(returned), 118)

        progress = _progress_lines(self.logger)
        self.assertEqual(
            progress,
            [
                "Transcribing... 50 segments processed (audio duration unknown)",
                "Transcribing... 100 segments processed (audio duration unknown)",
                "Transcribing... done (120 segments processed)",
            ],
        )
        self.assertFalse(any(PROGRESS_RE.match(line) for line in progress))
        self.assertTrue(DONE_RE.match(progress[-1]))

    def test_zero_duration_degrades_to_counter_messages(self):
        segments = _fake_segments(count=120, step=10.0)
        self._run(segments, duration=0.0)
        progress = _progress_lines(self.logger)
        self.assertEqual(len(progress), 3)
        self.assertEqual(progress[-1], "Transcribing... done (120 segments processed)")
        self.assertFalse(any(PROGRESS_RE.match(line) for line in progress))

    def test_probe_negative_and_nan_durations_degrade(self):
        for bad in (-5.0, float("nan"), float("inf")):
            with self.subTest(duration=bad):
                self.logger = QuietLogger()
                segments = _fake_segments(count=60, step=10.0)
                self._run(segments, duration=bad)
                progress = _progress_lines(self.logger)
                self.assertEqual(len(progress), 2)
                self.assertEqual(
                    progress[-1], "Transcribing... done (60 segments processed)"
                )


def _demo(duration: float | None = 3057.0, count: int = 100) -> None:
    """Print what a stubbed 51-minute run looks like through the real Logger.

        .venv\\Scripts\\python.exe tests\\test_transcribe_progress.py --demo
    """
    from utils.logger import Logger

    logger = Logger(verbose=True)
    segments = _fake_segments(count=count, step=(duration or 3057.0) / count)
    case = TranscribeProgressTests("runTest")
    case.setUp()
    try:
        print(f"--- duration probe -> {duration!r}, {count} stub segments ---")
        case.logger = logger
        case._run(
            segments,
            duration=duration if duration is not None else transcriber.PipelineError("ffprobe failed"),
        )
    finally:
        case.tearDown()


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
        print("\n--- duration probe unavailable ---")
        _demo(duration=None, count=120)
    else:
        unittest.main(verbosity=2)
