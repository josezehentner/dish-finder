"""SQLite cache for place lookups, OSM results and crawled website text.

Crawled text is stored independent of the search terms, so searching the same
area for another dish only needs the database, not the network.
"""

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from dish_finder.crawler import Snapshot
from dish_finder.matching import Document

# Bump when the schema changes: old caches are then dropped and rebuilt.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS lookups (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,  -- JSON
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_key        TEXT PRIMARY KEY,
    pages           INTEGER NOT NULL,
    pdfs            INTEGER NOT NULL,
    unreadable_pdfs TEXT NOT NULL,  -- JSON list of URLs
    error           TEXT,           -- set if the crawl failed or was cut short
    crawled_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    site_key TEXT NOT NULL REFERENCES sites (site_key) ON DELETE CASCADE,
    position INTEGER NOT NULL,  -- keeps the crawl order
    url      TEXT NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('html', 'pdf')),
    text     TEXT NOT NULL,
    PRIMARY KEY (site_key, position)
);
"""


def site_key(websites: list[str]) -> str:
    """Places sharing the same website(s) share one cache entry."""
    return "\n".join(websites)


class Cache:
    """Use ':memory:' as path for a throwaway cache (e.g. --no-cache)."""

    def __init__(
        self, path: Path | str, max_age: float, error_max_age: float, clock: Callable[[], float] = time.time
    ) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.max_age = max_age
        self.error_max_age = error_max_age
        self._clock = clock
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        (version,) = self._db.execute("PRAGMA user_version").fetchone()
        if version != SCHEMA_VERSION:
            # It's only a cache: start over instead of migrating data.
            self._db.executescript(
                "DROP TABLE IF EXISTS documents; DROP TABLE IF EXISTS sites; DROP TABLE IF EXISTS lookups;"
            )
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def _fresh(self, stored_at: float, max_age: float) -> bool:
        return self._clock() - stored_at <= max_age

    # --- JSON lookups (geocoding, Overpass) ----------------------------------

    def get_json(self, key: str) -> Any | None:
        row = self._db.execute("SELECT value, fetched_at FROM lookups WHERE key = ?", (key,)).fetchone()
        if row is None or not self._fresh(row[1], self.max_age):
            return None
        return json.loads(row[0])

    def put_json(self, key: str, value: Any) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO lookups (key, value, fetched_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), self._clock()),
            )

    # --- Crawled websites ----------------------------------------------------

    def get_snapshot(self, key: str) -> Snapshot | None:
        row = self._db.execute(
            "SELECT pages, pdfs, unreadable_pdfs, error, crawled_at FROM sites WHERE site_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        pages, pdfs, unreadable_pdfs, error, crawled_at = row
        if not self._fresh(crawled_at, self.error_max_age if error else self.max_age):
            return None
        documents = [
            Document(url, kind, text)
            for url, kind, text in self._db.execute(
                "SELECT url, kind, text FROM documents WHERE site_key = ? ORDER BY position", (key,)
            )
        ]
        return Snapshot(documents, pages, pdfs, json.loads(unreadable_pdfs), error)

    def put_snapshot(self, key: str, snapshot: Snapshot) -> None:
        with self._db:  # one transaction: a site is stored completely or not at all
            self._db.execute("DELETE FROM sites WHERE site_key = ?", (key,))  # cascades to documents
            self._db.execute(
                "INSERT INTO sites (site_key, pages, pdfs, unreadable_pdfs, error, crawled_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    key,
                    snapshot.pages,
                    snapshot.pdfs,
                    json.dumps(snapshot.unreadable_pdfs),
                    snapshot.error,
                    self._clock(),
                ),
            )
            self._db.executemany(
                "INSERT INTO documents (site_key, position, url, kind, text) VALUES (?, ?, ?, ?, ?)",
                [(key, i, doc.url, doc.kind, doc.text) for i, doc in enumerate(snapshot.documents)],
            )
