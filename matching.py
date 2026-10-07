"""Search extracted text for dish/drink names."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

import config

_GERMAN = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

Kind = Literal["html", "pdf"]


@dataclass
class Hit:
    term: str
    url: str
    kind: Kind
    count: int
    snippet: str


def normalize(text: str) -> str:
    """Lowercase, spell umlauts out ('Spätzle' == 'Spaetzle'), drop other accents."""
    text = unicodedata.normalize("NFC", text).casefold().translate(_GERMAN)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text)


def find_hits(text: str, terms: list[str], url: str, kind: Kind, whole_word: bool = False) -> list[Hit]:
    haystack = normalize(text)
    radius = config.SNIPPET_RADIUS
    hits = []
    for term in terms:
        needle = re.escape(normalize(term).strip())
        pattern = rf"\b{needle}\b" if whole_word else needle
        matches = list(re.finditer(pattern, haystack))
        if not matches:
            continue
        first = matches[0]
        snippet = haystack[max(0, first.start() - radius) : first.end() + radius]
        hits.append(Hit(term, url, kind, len(matches), snippet.strip()))
    return hits
