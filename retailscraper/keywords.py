"""Loading a custom offer vocabulary from a file.

The built-in offer detection in `promotions.py` knows retail wording -- "25%
off", "buy 2 get 1", "free gift with purchase". A different vertical uses
different language: a betting site advertises "free spins", "no deposit" and
"5x wager", none of which the retail rules would recognise as an offer.

A keyword file replaces that vocabulary. Replaces, not extends: if you supply
your own list, only your list is used, so you can see exactly what you are
matching without the built-in rules quietly widening it.

Two formats are accepted.

Plain text, one pattern per line, `#` for comments:

    # black-friday.txt
    black friday
    cyber monday
    doorbuster
    [0-9]+% off

Or JSON, when you want to name the set:

    {
      "name": "black-friday",
      "keywords": ["black friday", "cyber monday", "[0-9]+% off"]
    }

Each keyword is a regular expression, so "bet .* get" and "[0-9]+x wager"
work as written. Patterns are matched case-insensitively, and a pattern made
only of ordinary words is wrapped in word boundaries so "bonus" does not
match "bonuses" the way a bare substring search would.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

# Characters that mean the author is writing a regex rather than a phrase.
# A pattern containing any of these is used verbatim; a plain phrase gets
# word boundaries added so it matches whole words.
_REGEX_CHARS = set(r"[](){}*+?|^$\.")


class KeywordFileError(ValueError):
    """A keyword file could not be read, or holds an invalid pattern.

    Raised rather than skipped: a typo in a keyword file would silently
    scrape nothing, and a run that finds no offers because of a broken
    pattern looks exactly like a run against a site with no offers.
    """


@dataclass(frozen=True)
class KeywordSet:
    """A named list of offer patterns, compiled into one matcher."""

    name: str
    keywords: List[str]
    pattern: re.Pattern

    def matches(self, text: str) -> bool:
        """True when the text contains any keyword in this set."""
        return bool(text) and bool(self.pattern.search(text))

    def __len__(self) -> int:
        return len(self.keywords)


def _as_pattern(keyword: str) -> str:
    """Turn one keyword into a regex fragment.

    A plain phrase gets word boundaries; anything with regex syntax in it is
    trusted as written, so "bet .* get" and "[0-9]+x wager" behave as the
    author intended.

    A boundary is only added on a side that actually starts or ends with a
    word character. `\\b` asserts a word/non-word transition, so "20%" wrapped
    as `\\b20%\\b` can never match: the trailing boundary needs a word
    character straight after the "%", and real copy reads "20% off". The
    pattern still compiles, so validation cannot catch it -- it would just
    silently match nothing, which looks exactly like a site running no
    promotions.
    """
    keyword = keyword.strip()
    if any(char in _REGEX_CHARS for char in keyword):
        return keyword

    escaped = re.escape(keyword)
    prefix = r"\b" if keyword[:1].isalnum() or keyword[:1] == "_" else ""
    suffix = r"\b" if keyword[-1:].isalnum() or keyword[-1:] == "_" else ""
    return f"{prefix}{escaped}{suffix}"


def compile_keywords(keywords: Sequence[str], name: str = "custom") -> KeywordSet:
    """Compile a list of keywords into a KeywordSet.

    Every pattern is validated individually so the error names the keyword
    that is broken, rather than reporting a failure in one long combined
    expression that nobody can read.
    """
    cleaned = [k.strip() for k in keywords if k and k.strip()]
    if not cleaned:
        raise KeywordFileError(f"keyword set {name!r} is empty")

    fragments = []
    for keyword in cleaned:
        fragment = _as_pattern(keyword)
        try:
            re.compile(fragment)
        except re.error as exc:
            raise KeywordFileError(
                f"invalid keyword {keyword!r} in {name!r}: {exc}"
            ) from exc
        fragments.append(fragment)

    return KeywordSet(
        name=name,
        keywords=cleaned,
        pattern=re.compile("|".join(fragments), re.IGNORECASE),
    )


def load_keywords(path: Path) -> KeywordSet:
    """Read a keyword file. Accepts the JSON or the plain-text form."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KeywordFileError(f"could not read keyword file {path}: {exc}") from exc

    stripped = raw.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return _load_json(raw, path)
    return _load_text(raw, path)


def _load_json(raw: str, path: Path) -> KeywordSet:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise KeywordFileError(f"{path} is not valid JSON: {exc}") from exc

    if isinstance(data, list):
        return compile_keywords(data, name=path.stem)

    if not isinstance(data, dict):
        raise KeywordFileError(
            f"{path} must hold a list of keywords or an object with a "
            f"'keywords' list, not {type(data).__name__}"
        )

    keywords = data.get("keywords")
    if not isinstance(keywords, list):
        raise KeywordFileError(f"{path} has no 'keywords' list")

    return compile_keywords(keywords, name=str(data.get("name") or path.stem))


def _load_text(raw: str, path: Path) -> KeywordSet:
    keywords = []
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            keywords.append(line)
    return compile_keywords(keywords, name=path.stem)


def describe(keyword_set: Optional[KeywordSet]) -> str:
    """One line describing which vocabulary is in force, for the run header."""
    if keyword_set is None:
        return "built-in retail offer rules"
    preview = ", ".join(keyword_set.keywords[:4])
    if len(keyword_set) > 4:
        preview += f", +{len(keyword_set) - 4} more"
    return f"{keyword_set.name} ({len(keyword_set)} keywords: {preview})"
