"""
Data schemas used across the pipeline.

Keeping these in one place means every stage (transcriber, candidate
generator, clip selector, quality filter, clip processor) speaks the
same language and can be swapped out independently later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Word:
    """A single word with its own timestamps, used for short, punchy
    caption chunking rather than burning whole sentences at once."""

    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "text": self.text}

    @staticmethod
    def from_dict(d: dict) -> "Word":
        return Word(start=d["start"], end=d["end"], text=d["text"])


@dataclass
class TranscriptSegment:
    """A single timestamped chunk of speech, as produced by faster-whisper."""

    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
        }

    @staticmethod
    def from_dict(d: dict) -> "TranscriptSegment":
        return TranscriptSegment(
            start=d["start"],
            end=d["end"],
            text=d["text"],
            words=[Word.from_dict(w) for w in d.get("words", [])],
        )


@dataclass
class CandidateSegment:
    """A candidate window of the transcript that *might* become a clip."""

    start: float
    end: float
    text: str
    segment_indices: list[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "segment_indices": self.segment_indices,
        }

    @staticmethod
    def from_dict(d: dict) -> "CandidateSegment":
        return CandidateSegment(
            start=d["start"],
            end=d["end"],
            text=d["text"],
            segment_indices=d.get("segment_indices", []),
        )


@dataclass
class ScoredClip:
    """A candidate after the LLM has scored it."""

    start: float
    end: float
    score: float
    hook_score: float
    content_score: float
    story_score: float
    emotion_score: float
    standalone_score: float
    ending_score: float
    reason: str
    text: str = ""
    title: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "score": self.score,
            "hook_score": self.hook_score,
            "content_score": self.content_score,
            "story_score": self.story_score,
            "emotion_score": self.emotion_score,
            "standalone_score": self.standalone_score,
            "ending_score": self.ending_score,
            "reason": self.reason,
            "text": self.text,
            "title": self.title,
        }

    @staticmethod
    def from_dict(d: dict) -> "ScoredClip":
        return ScoredClip(
            start=float(d["start"]),
            end=float(d["end"]),
            score=float(d.get("score", 0)),
            hook_score=float(d.get("hook_score", 0)),
            content_score=float(d.get("content_score", 0)),
            story_score=float(d.get("story_score", 0)),
            emotion_score=float(d.get("emotion_score", 0)),
            standalone_score=float(d.get("standalone_score", 0)),
            ending_score=float(d.get("ending_score", 0)),
            reason=d.get("reason", ""),
            text=d.get("text", ""),
            title=d.get("title", ""),
        )


@dataclass
class FinalClip:
    """A clip after boundary/context cleanup, ready for rendering."""

    index: int
    start: float
    end: float
    score: float
    reason: str
    text: str
    title: str = ""
    output_path: Optional[str] = None

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "duration": self.duration,
            "score": self.score,
            "reason": self.reason,
            "text": self.text,
            "title": self.title,
            "output_path": self.output_path,
        }

    @staticmethod
    def from_dict(d: dict) -> "FinalClip":
        return FinalClip(
            index=int(d["index"]),
            start=float(d["start"]),
            end=float(d["end"]),
            score=float(d.get("score", 0)),
            reason=d.get("reason", ""),
            text=d.get("text", ""),
            title=d.get("title", ""),
            output_path=d.get("output_path"),
        )
