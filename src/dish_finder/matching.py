"""Search extracted text for dish/drink names."""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from dish_finder import config

_GERMAN = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_WHITESPACE = re.compile(r"\s+")

Kind = Literal["html", "pdf"]


@dataclass
class Document:
    """Searchable text of one web page or PDF."""

    url: str
    kind: Kind
    text: str


@dataclass
class Hit:
    """First match of a term in a document, with context in the document's original spelling."""

    term: str
    url: str
    kind: Kind
    count: int
    before: str  # text before the match, starts with "…" if cut
    match: str  # the matched text as written in the document
    after: str  # text after the match, ends with "…" if cut

    @property
    def snippet(self) -> str:
        return f"{self.before}{self.match}{self.after}"


def _fold(char: str) -> str:
    """Normalize one character: lowercase, umlauts spelled out, accents dropped (may return 0-2 chars)."""
    folded = unicodedata.normalize("NFKD", char.casefold().translate(_GERMAN))
    return "".join(c for c in folded if not unicodedata.combining(c))


def _index(text: str) -> tuple[str, str, list[int]]:
    """Return (source, normalized, positions): positions[i] is the index in source
    that normalized[i] came from, so matches can be mapped back to the original text."""
    source = unicodedata.normalize("NFC", text)
    chars: list[str] = []
    positions: list[int] = []
    for i, char in enumerate(source):
        for c in _fold(char):
            if c.isspace():
                if chars and chars[-1] == " ":
                    continue  # collapse runs of whitespace
                c = " "
            chars.append(c)
            positions.append(i)
    return source, "".join(chars), positions


def normalize(text: str) -> str:
    """Lowercase, spell umlauts out ('Spätzle' == 'Spaetzle'), drop other accents, collapse whitespace."""
    return _index(text)[1]


def _context(source: str, start: int, end: int) -> tuple[str, str]:
    """Text around source[start:end], cut at word boundaries and marked with '…' where cut."""
    radius = config.SNIPPET_RADIUS
    before = _WHITESPACE.sub(" ", source[max(0, start - radius) : start])
    after = _WHITESPACE.sub(" ", source[end : end + radius])
    if start - radius > 0:
        _, _, before = before.partition(" ")  # drop the partial first word
        before = "…" + before
    if end + radius < len(source):
        after, _, _ = after.rpartition(" ")  # drop the partial last word
        after += "…"
    return before.lstrip(), after.rstrip()


def find_hits(text: str, terms: list[str], url: str, kind: Kind, whole_word: bool = False) -> list[Hit]:
    source, haystack, positions = _index(text)
    hits = []
    for term in terms:
        needle = re.escape(normalize(term).strip())
        pattern = rf"\b{needle}\b" if whole_word else needle
        matches = list(re.finditer(pattern, haystack))
        if not matches:
            continue
        first = matches[0]
        start, end = positions[first.start()], positions[first.end() - 1] + 1
        before, after = _context(source, start, end)
        match = _WHITESPACE.sub(" ", source[start:end])
        hits.append(Hit(term, url, kind, len(matches), before, match, after))
    return hits


def search(documents: Iterable[Document], terms: list[str], whole_word: bool = False) -> list[Hit]:
    return [hit for doc in documents for hit in find_hits(doc.text, terms, doc.url, doc.kind, whole_word)]
