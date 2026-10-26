# -*- coding: utf-8 -*-
"""Deterministic caption corrections + Whisper hotwords.

Whisper mishears homophones (المذهب ← المزهر). Two independent levers:
  * hotwords  : bias the decoder toward the show's vocabulary before it runs.
  * glossary  : rewrite known wrong→right forms after transcription, so the
                burned captions, the exported transcript and the ranking all
                see the corrected text.
"""
from __future__ import annotations

import re

ARABIC = "\u0621-\u064a\u0660-\u0669"
BOUNDARY_BEFORE = rf"(?<![{ARABIC}A-Za-z0-9_])"
BOUNDARY_AFTER = rf"(?![{ARABIC}A-Za-z0-9_])"
# حروف تسبق الكلمة في العربية (و، ف، ب، ل، ك) وقد تتراكم مثل «وبالمزهر».
PREFIXES = "وفبلك"

MAX_PAIRS = 500
MAX_HOTWORDS_CHARS = 220
_SEPARATORS = ("=>", "→", "=")


class GlossaryError(ValueError):
    """خطأ في سطر من قاموس التصحيح، برقم السطر."""


def parse_glossary(text: str) -> list[tuple[str, str]]:
    """يحوّل نص المستخدم إلى أزواج (خطأ، صحيح). سطر لكل تصحيح.

    الصيغ المقبولة:  ``المزهر => المذهب``  أو  ``المزهر = المذهب``
    الأسطر الفارغة والأسطر التي تبدأ بـ``#`` أو ``//`` تُتجاهل.
    """
    pairs: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        for separator in _SEPARATORS:
            if separator in line:
                wrong, _, right = line.partition(separator)
                break
        else:
            raise GlossaryError(f"السطر {number}: استخدم الصيغة «الخطأ => الصحيح»")
        wrong, right = " ".join(wrong.split()), " ".join(right.split())
        if not wrong or not right:
            raise GlossaryError(f"السطر {number}: الطرفان مطلوبان")
        if wrong == right:
            raise GlossaryError(f"السطر {number}: الطرفان متطابقان")
        if len(wrong) > 60 or len(right) > 60:
            raise GlossaryError(f"السطر {number}: الكلمة طويلة جدًا (60 حرفًا كحد أقصى)")
        seen[wrong] = right
    for wrong, right in seen.items():
        pairs.append((wrong, right))
        if len(pairs) >= MAX_PAIRS:
            break
    return pairs


def hotwords_for(pairs, extra: str = "", limit: int = MAX_HOTWORDS_CHARS) -> str:
    """كلمات يتوقّعها التفريغ: أطراف التصحيح الصحيحة + كلمات المستخدم."""
    words: list[str] = []
    for _wrong, right in pairs:
        if right not in words:
            words.append(right)
    for chunk in re.split(r"[,،;\n]+", extra or ""):
        term = " ".join(chunk.split())
        if term and term not in words:
            words.append(term)
    kept: list[str] = []
    total = 0
    for term in words:
        if len(term.split()) > 5:
            continue
        extra_len = len(term) + (2 if kept else 0)
        if total + extra_len > limit:
            continue
        kept.append(term)
        total += extra_len
    return "، ".join(kept)


def rules(glossary_text: str, extra: str = ""):
    """(pairs, hotwords, error) — خطأ القاموس لا يمنع التشغيل."""
    try:
        pairs = parse_glossary(glossary_text)
    except GlossaryError as error:
        return [], hotwords_for([], extra), str(error)
    return pairs, hotwords_for(pairs, extra), ""


def apply(segments, pairs) -> dict[str, int]:
    """يصحّح نص المقاطع وكلماتها في مكانه. يعيد عدد التصحيحات لكل زوج."""
    counts: dict[str, int] = {}
    active = [(wrong, right,
               re.compile(BOUNDARY_BEFORE + rf"([{PREFIXES}]{{0,2}})" + re.escape(wrong) + BOUNDARY_AFTER))
              for wrong, right in pairs if wrong]
    if not active:
        return counts
    for segment in segments:
        text = getattr(segment, "text", "") or ""
        for wrong, right, pattern in active:
            text, hits = pattern.subn(lambda match, right=right: (match.group(1) or "") + right, text)
            if hits:
                counts[f"{wrong} => {right}"] = counts.get(f"{wrong} => {right}", 0) + hits
        segment.text = text
        for word in getattr(segment, "words", []) or []:
            token = getattr(word, "text", "") or ""
            for wrong, right, pattern in active:
                token, hits = pattern.subn(lambda match, right=right: (match.group(1) or "") + right, token)
                if hits:
                    key = f"{wrong} => {right}"
                    counts[key] = counts.get(key, 0) + hits
            word.text = token
    return counts


def total(counts: dict[str, int]) -> int:
    return sum(counts.values())


def summary(counts: dict[str, int], limit: int = 5) -> str:
    """سطر مختصر للسجل: أكثر التصحيحات تكرارًا."""
    if not counts:
        return "لا تصحيحات"
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    head = "، ".join(f"{key} ×{value}" for key, value in ordered[:limit])
    return f"{total(counts)} تصحيحًا: {head}"
