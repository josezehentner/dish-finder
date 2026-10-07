"""Render search results as a clean, README-like terminal report.

Kaspressknödel found at 2 of 40 places

⏺ Gasthaus Steindl · restaurant · 120 m
  ⎿  Google Maps  https://www.google.com/maps/search/?api=1&query=48.2141,16.3453
     OSM          https://www.openstreetmap.org/node/1853444670
     Website      https://dassteindl.at

     PDF   https://dassteindl.at/…/Speisen-2026.pdf
           …Leberknödelsuppe € 5,90 Kaspressknödel in Rindsuppe € 8,90…
"""

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TextIO

from dish_finder.crawler import Snapshot
from dish_finder.matching import Hit
from dish_finder.osm import Restaurant, google_maps_url

INDENT = "     "  # aligns with the text after "  ⎿  "
LABEL_WIDTH = 13  # "Google Maps" + two spaces
KIND_WIDTH = 6  # "HTML" / "PDF" column


@dataclass
class Result:
    restaurant: Restaurant
    snapshot: Snapshot
    hits: list[Hit]
    distance: float  # meters from the search center


@dataclass(frozen=True)
class Style:
    """ANSI colors for terminals; plain text otherwise (pipes, files, NO_COLOR)."""

    color: bool = False

    @classmethod
    def for_stream(cls, stream: TextIO) -> "Style":
        wanted = "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
        return cls(color=wanted and stream.isatty())

    def _sgr(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def bold(self, text: str) -> str:
        return self._sgr("1", text)

    def dim(self, text: str) -> str:
        return self._sgr("2", text)

    def green(self, text: str) -> str:
        return self._sgr("32", text)

    def red(self, text: str) -> str:
        return self._sgr("31", text)

    def highlight(self, text: str) -> str:
        return self._sgr("1;33", text)  # bold yellow


def format_distance(meters: float) -> str:
    return f"{round(meters)} m" if meters < 1000 else f"{meters / 1000:.1f} km"


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _group_by_url(hits: Iterable[Hit]) -> dict[str, list[Hit]]:
    groups: dict[str, list[Hit]] = {}
    for hit in hits:
        groups.setdefault(hit.url, []).append(hit)
    return groups


def _details(result: Result) -> str:
    return f"· {result.restaurant.amenity.replace('_', ' ')} · {format_distance(result.distance)}"


def render_hit(result: Result, style: Style) -> str:
    r = result.restaurant
    lines = [f"{style.green('⏺')} {style.bold(r.name)} {style.dim(_details(result))}"]

    links = [("Google Maps", google_maps_url(r.lat, r.lon)), ("OSM", r.osm_url), ("Website", r.websites[0])]
    for i, (label, url) in enumerate(links):
        lead = "  ⎿  " if i == 0 else INDENT
        lines.append(f"{lead}{style.dim(label.ljust(LABEL_WIDTH))}{url}")

    for url, hits in _group_by_url(result.hits).items():
        lines.append("")
        lines.append(f"{INDENT}{style.dim(hits[0].kind.upper().ljust(KIND_WIDTH))}{url}")
        for hit in hits:
            count = style.dim(f"  ×{hit.count}") if hit.count > 1 else ""
            lines.append(f"{INDENT}{' ' * KIND_WIDTH}{hit.before}{style.highlight(hit.match)}{hit.after}{count}")
    return "\n".join(lines)


def render_miss(result: Result, style: Style) -> str:
    snap = result.snapshot
    details = [plural(snap.pages, "page"), plural(snap.pdfs, "PDF")]
    if snap.unreadable_pdfs:
        details.append(f"{plural(len(snap.unreadable_pdfs), 'scanned PDF')} without text")
    r = result.restaurant
    line = f"  {style.dim('·')} {r.name} {style.dim(_details(result))}  {style.dim(r.websites[0])}"
    line += f"  {style.dim(', '.join(details))}"
    if snap.error:
        line += f"  {style.red(snap.error)}"
    return line


def render_report(results: list[Result], terms: list[str], verbose: bool, style: Style) -> str:
    """Matches nearest first; with verbose also the places without a match."""
    by_distance = sorted(results, key=lambda r: r.distance)
    found = [r for r in by_distance if r.hits]
    searched = " / ".join(terms)

    if found:
        lines = [f"{style.bold(searched)} found at {len(found)} of {plural(len(results), 'place')}"]
    else:
        lines = [f"{style.bold(searched)} not found ({plural(len(results), 'place')} searched)"]
    for result in found:
        lines += ["", render_hit(result, style)]

    misses = [r for r in by_distance if not r.hits]
    if verbose and misses:
        lines += ["", style.bold(f"No match ({len(misses)})")]
        lines += [render_miss(result, style) for result in misses]
    return "\n".join(lines)
