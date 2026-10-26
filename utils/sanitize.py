"""Profanity masking for on-screen text (burned captions + titles).

Transcripts occasionally contain a vulgar word - actually spoken or a
mishearing - and burned captions are on-screen text, so listed words are
masked before the .ass file is written. Matching is whole-word and
diacritic/punctuation-insensitive, so innocent words are never touched.

The blocklist comes from `profanity.txt` in the project root (one word per
line, `#` starts a comment), plus a small built-in default set.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]

_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
_NON_LETTERS = re.compile(r"[^\w\u0600-\u06ff]+", re.UNICODE)

# Small on purpose: guarantees the mechanism works out of the box and seeds
# profanity.txt - the user's own list carries the rest.
DEFAULT_WORDS = (
    # Arabic
    "بز", "بزاز", "تيز", "عرص", "شرموط", "متناك", "قحبة", "خول", "زب", "كس",
    # Explicit/sexual vocabulary is never allowed in generated/on-screen text.
    "إباحي", "إباحية", "اباحي", "اباحية", "بورنو", "جنس", "جنسي", "جنسية",
    "نكاح", "نيك", "ينيك", "نيكة", "كسها", "كسك", "زبي", "زبك", "زبها",
    "مص", "اغتصاب", "اغتصب", "عاري", "عارية", "عراة", "سكس", "شرموطة",
    "حلمات", "حلمة", "قضيب", "مهبل", "مني", "استمناء", "عاهرة",
    # English
    "porn", "porno", "pornography", "pornographic", "sex", "sexual", "sexually",
    "erotic", "erotica", "nude", "nudes", "nudity", "xxx", "orgasm", "blowjob",
    "handjob", "masturbation", "masturbate", "penis", "vagina", "pussy", "dick",
    "cock", "cum", "semen", "anal", "rape", "rapist", "sexting", "tits", "boobs",
    "fuck", "shit", "bitch", "bastard", "whore",
)


def _norm(word: str) -> str:
    """Lower-cased, diacritic-free, punctuation-free core of a word."""
    cleaned = _DIACRITICS.sub("", word)
    cleaned = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f]", "", cleaned)
    cleaned = _NON_LETTERS.sub("", cleaned)
    return cleaned.casefold()


def load_blocklist(root: Path | None = None) -> frozenset[str]:
    """The active blocklist: built-ins + profanity.txt at the project root."""
    words = {_norm(w) for w in DEFAULT_WORDS}
    path = (root or _PACKAGE_ROOT) / "profanity.txt"
    if path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                core = _norm(line)
                if core:
                    words.add(core)
        except OSError:
            pass
    return frozenset(w for w in words if w)


def mask_text(text: str, blocklist: frozenset[str]) -> str:
    """Mask every blocked word in `text`, one '*' per character.

    The word keeps its slot (punctuation and caption timing untouched);
    only its letters are replaced, e.g. "بز!" -> "**!".
    """
    if not text or not blocklist:
        return text
    out: list[str] = []
    for token in re.split(r"(\s+)", text):
        if not token or token.isspace():
            out.append(token)
            continue
        if _blocked_token(token, blocklist):
            out.append(re.sub(r"\w", "*", token, flags=re.UNICODE))
        else:
            out.append(re.sub(r"[^\W_]+", lambda match:
                "*" * len(match.group()) if _blocked_token(match.group(), blocklist)
                else match.group(), token, flags=re.UNICODE))
    return "".join(out)


def _canonical(word):
    return _norm(word).translate(str.maketrans({"أ":"ا", "إ":"ا", "آ":"ا", "ى":"ي", "ة":"ه"}))


@lru_cache(maxsize=8)
def _blocked_pattern(blocklist):
    words={_canonical(word) for word in blocklist}
    arabic=[re.escape(word) for word in words if re.fullmatch(r"[\u0600-\u06ff]+",word)]
    other=[re.escape(word) for word in words if not re.fullmatch(r"[\u0600-\u06ff]+",word)]
    patterns=[]
    if arabic:
        patterns.append(r"(?:و|ف|ب|ل|ال|وال|فال|بال|لل)?(?:"+'|'.join(arabic)+r")(?:ه|ها|هم|ك|كم|نا|ي|يه|ات)?")
    if other:
        patterns.append(r"(?:"+'|'.join(other)+r")(?:s|ing|ed)?")
    return re.compile('|'.join(patterns) or r"(?!)")

@lru_cache(maxsize=8)
def _blocked_pattern(blocklist):
    words={_canonical(word) for word in blocklist}
    arabic=[re.escape(word) for word in words if re.fullmatch(r"[\u0600-\u06ff]+",word)]
    other=[re.escape(word) for word in words if not re.fullmatch(r"[\u0600-\u06ff]+",word)]
    patterns=[]
    if arabic:
        patterns.append(r"(?:و|ف|ب|ل|ال|وال|فال|بال|لل)?(?:"+'|'.join(arabic)+r")(?:ه|ها|هم|ك|كم|نا|ي|يه|ات)?")
    if other:
        patterns.append(r"(?:"+'|'.join(other)+r")(?:s|ing|ed)?")
    return re.compile('|'.join(patterns) or r"(?!)")

def _blocked_token(token, blocklist):
    return bool(_blocked_pattern(frozenset(blocklist)).fullmatch(_canonical(token)))


def contains_blocked_text(text):
    blocklist = load_blocklist()
    # Check punctuation-separated words as well as deliberately joined tokens.
    return any(_blocked_token(token, blocklist)
               for token in re.split(r"\s+|[,،;؛!?؟/]", text or "") if token)


def is_placeholder_title(text):
    return bool(re.fullmatch(r"\s*(?:clip|part|segment|video|مقطع|كليب|جزء)[ _#:-]*[0-9٠-٩]+\s*", text or "", re.I))
