import sqlite3

import pytest

from dish_finder import cache as cache_module
from dish_finder.cache import Cache, site_key
from dish_finder.crawler import Snapshot
from dish_finder.matching import Document

DAY = 24 * 3600


class Clock:
    """Controllable time source, so expiry can be tested without waiting."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def cache(tmp_path, clock):
    with Cache(tmp_path / "cache.sqlite3", max_age=7 * DAY, error_max_age=DAY, clock=clock) as c:
        yield c


def snapshot() -> Snapshot:
    return Snapshot(
        documents=[
            Document("https://wirt.at/", "html", "Willkommen"),
            Document("https://wirt.at/karte.pdf", "pdf", "Tafelspitz 24,90"),
        ],
        pages=1,
        pdfs=2,
        unreadable_pdfs=["https://wirt.at/scan.pdf"],
    )


class TestSiteKey:
    def test_joins_websites_in_order(self):
        assert site_key(["https://a.at", "https://b.at"]) == "https://a.at\nhttps://b.at"
        assert site_key(["https://a.at", "https://b.at"]) != site_key(["https://b.at", "https://a.at"])


class TestJsonLookups:
    def test_roundtrip(self, cache):
        cache.put_json("geocode:wien", [48.2, 16.37])
        assert cache.get_json("geocode:wien") == [48.2, 16.37]

    def test_missing_key(self, cache):
        assert cache.get_json("geocode:nirgendwo") is None

    def test_overwrites_existing_value(self, cache):
        cache.put_json("k", 1)
        cache.put_json("k", 2)
        assert cache.get_json("k") == 2

    def test_falsy_values_are_cached_too(self, cache):
        cache.put_json("overpass:empty", [])
        assert cache.get_json("overpass:empty") == []

    def test_expires_after_max_age(self, cache, clock):
        cache.put_json("k", "v")
        clock.now += 7 * DAY
        assert cache.get_json("k") == "v"
        clock.now += 1
        assert cache.get_json("k") is None


class TestSnapshots:
    def test_roundtrip_keeps_documents_in_order(self, cache):
        cache.put_snapshot("https://wirt.at", snapshot())
        assert cache.get_snapshot("https://wirt.at") == snapshot()

    def test_missing_site(self, cache):
        assert cache.get_snapshot("https://unknown.at") is None

    def test_empty_snapshot(self, cache):
        cache.put_snapshot("https://empty.at", Snapshot())
        assert cache.get_snapshot("https://empty.at") == Snapshot()

    def test_replacing_a_site_removes_its_old_documents(self, cache):
        cache.put_snapshot("https://wirt.at", snapshot())
        cache.put_snapshot("https://wirt.at", Snapshot(documents=[Document("https://wirt.at/neu", "html", "Neu")]))

        assert cache.get_snapshot("https://wirt.at").documents == [Document("https://wirt.at/neu", "html", "Neu")]
        (count,) = cache._db.execute("SELECT COUNT(*) FROM documents").fetchone()
        assert count == 1

    def test_expires_after_max_age(self, cache, clock):
        cache.put_snapshot("https://wirt.at", snapshot())
        clock.now += 8 * DAY
        assert cache.get_snapshot("https://wirt.at") is None

    def test_max_age_zero_ignores_older_entries(self, tmp_path, clock):
        path = tmp_path / "cache.sqlite3"
        with Cache(path, max_age=7 * DAY, error_max_age=DAY, clock=clock) as c:
            c.put_snapshot("https://wirt.at", snapshot())
        clock.now += 1
        with Cache(path, max_age=0, error_max_age=0, clock=clock) as refreshing:
            assert refreshing.get_snapshot("https://wirt.at") is None

    def test_failed_crawl_roundtrip(self, cache):
        failed = Snapshot(error="ConnectError: down")
        cache.put_snapshot("https://dead.at", failed)
        assert cache.get_snapshot("https://dead.at") == failed

    def test_failed_crawls_expire_sooner(self, cache, clock):
        cache.put_snapshot("https://dead.at", Snapshot(error="ConnectTimeout"))
        cache.put_snapshot("https://wirt.at", snapshot())

        clock.now += DAY + 1

        assert cache.get_snapshot("https://dead.at") is None
        assert cache.get_snapshot("https://wirt.at") == snapshot()

    def test_failed_write_leaves_old_entry_intact(self, cache):
        cache.put_snapshot("https://wirt.at", snapshot())
        broken = Snapshot(documents=[Document("https://wirt.at/x", "image", "?")])  # type: ignore[arg-type]

        with pytest.raises(sqlite3.IntegrityError):
            cache.put_snapshot("https://wirt.at", broken)

        assert cache.get_snapshot("https://wirt.at") == snapshot()


class TestPersistence:
    def test_survives_reopening(self, tmp_path):
        path = tmp_path / "cache.sqlite3"
        with Cache(path, max_age=DAY, error_max_age=DAY) as c:
            c.put_json("k", {"a": 1})
            c.put_snapshot("https://wirt.at", snapshot())
        with Cache(path, max_age=DAY, error_max_age=DAY) as c:
            assert c.get_json("k") == {"a": 1}
            assert c.get_snapshot("https://wirt.at") == snapshot()

    def test_creates_missing_directories(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "cache.sqlite3"
        with Cache(path, max_age=DAY, error_max_age=DAY):
            pass
        assert path.exists()

    def test_in_memory_cache_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with Cache(":memory:", max_age=DAY, error_max_age=DAY) as c:
            c.put_json("k", 1)
            assert c.get_json("k") == 1
        assert list(tmp_path.iterdir()) == []

    def test_upgrades_cache_from_older_schema(self, tmp_path):
        path = tmp_path / "cache.sqlite3"
        old = sqlite3.connect(path)  # what schema version 1 looked like
        old.executescript(
            "CREATE TABLE sites (site_key TEXT PRIMARY KEY, pages INTEGER, pdfs INTEGER,"
            " unreadable_pdfs TEXT, crawled_at REAL); PRAGMA user_version = 1;"
        )
        old.close()

        with Cache(path, max_age=DAY, error_max_age=DAY) as c:
            c.put_snapshot("https://dead.at", Snapshot(error="down"))
            assert c.get_snapshot("https://dead.at") == Snapshot(error="down")

    def test_schema_change_resets_the_cache(self, tmp_path, monkeypatch):
        path = tmp_path / "cache.sqlite3"
        with Cache(path, max_age=DAY, error_max_age=DAY) as c:
            c.put_json("k", 1)

        monkeypatch.setattr(cache_module, "SCHEMA_VERSION", cache_module.SCHEMA_VERSION + 1)
        with Cache(path, max_age=DAY, error_max_age=DAY) as c:
            assert c.get_json("k") is None
            c.put_json("k", 2)  # tables were recreated and work
            assert c.get_json("k") == 2
