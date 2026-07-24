"""
Matches FanDuel's player names (from the salary CSV) to nflverse's
player names (from weekly stats). There's no shared ID between the
two sources — nflverse's players.csv tracks gsis/pfr/espn/otc/pff IDs
but nothing FanDuel-specific — so this has to work on names, made as
reliable as reasonably possible:

  1. normalize both sides (strip periods/apostrophes/suffixes, lowercase)
  2. exact match on the normalized form
  3. fall back to fuzzy matching (difflib) above a similarity cutoff
  4. anything below the cutoff is reported as unmatched rather than
     guessed — a wrong silent match is worse than a visible miss
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Literal

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PUNCTUATION_RE = re.compile(r"[.'\-]")
_WHITESPACE_RE = re.compile(r"\s+")

MatchQuality = Literal["exact", "fuzzy", "unmatched"]


def normalize_name(raw: str) -> str:
    """'D.K. Metcalf Jr.' -> 'dk metcalf'; 'Odell Beckham III' -> 'odell beckham'"""
    text = _PUNCTUATION_RE.sub("", raw.lower())
    words = [w for w in _WHITESPACE_RE.split(text.strip()) if w and w not in _SUFFIXES]
    return " ".join(words)


@dataclass(frozen=True)
class NameMatch:
    canonical_name: str | None  # the nflverse name to key stats lookups by
    quality: MatchQuality


class PlayerNameMatcher:
    """Build once per season from every known nflverse player name,
    then resolve each FanDuel CSV name against it."""

    FUZZY_CUTOFF = 0.85

    def __init__(self, known_names: list[str]) -> None:
        self._by_normalized: dict[str, str] = {}
        for name in known_names:
            self._by_normalized.setdefault(normalize_name(name), name)
        self._normalized_pool = list(self._by_normalized.keys())

    def match(self, raw_name: str) -> NameMatch:
        normalized = normalize_name(raw_name)

        exact = self._by_normalized.get(normalized)
        if exact:
            return NameMatch(canonical_name=exact, quality="exact")

        close = difflib.get_close_matches(normalized, self._normalized_pool, n=1, cutoff=self.FUZZY_CUTOFF)
        if close:
            return NameMatch(canonical_name=self._by_normalized[close[0]], quality="fuzzy")

        return NameMatch(canonical_name=None, quality="unmatched")
