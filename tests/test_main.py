import logging
import sys
from dataclasses import dataclass, field

import pytest

import config
import main
from crawler import Snapshot
from matching import Document, Hit
from osm import Restaurant
from tests.conftest import make_restaurant


def parse(monkeypatch, *argv: str):
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    return main.parse_args()


def hit_result(name: str = "Wirt") -> main.Result:
    hit = Hit("Tafelspitz", "https://wirt.at/karte.pdf", "pdf", 2, "wiener tafelspitz 24,90")
    return main.Result(make_restaurant("https://wirt.at", name=name), Snapshot(pages=1, pdfs=1), [hit])


class TestParseArgs:
    def test_several_terms_and_place(self, monkeypatch):
        args = parse(monkeypatch, "Club Mate", "Mate", "--near", "Wien Neubau")
        assert args.terms == ["Club Mate", "Mate"]
        assert args.near == "Wien Neubau"
        assert args.coords is None

    def test_defaults(self, monkeypatch):
        args = parse(monkeypatch, "Tafelspitz", "--near", "Wien")
        assert (args.radius, args.max_pages, args.concurrency) == (1000, 15, 8)
        assert not args.whole_word
        assert not args.verbose
        assert not args.debug
        assert not args.refresh
        assert not args.no_cache

    def test_coords(self, monkeypatch):
        assert parse(monkeypatch, "Mate", "--coords", "48.2", "16.35").coords == [48.2, 16.35]

    @pytest.mark.parametrize(
        "argv",
        [
            ("Mate",),  # no location
            ("Mate", "--near", "Wien", "--coords", "1", "2"),  # both locations
            ("--near", "Wien"),  # no search term
            ("Mate", "--near", "Wien", "--refresh", "--no-cache"),  # contradicting cache options
        ],
    )
    def test_invalid_arguments_exit(self, monkeypatch, argv):
        with pytest.raises(SystemExit):
            parse(monkeypatch, *argv)


class TestOutput:
    def test_print_hit(self, capsys):
        main.print_hit(hit_result())
        out = capsys.readouterr().out
        assert "✔ Wirt (restaurant)" in out
        assert "[pdf] https://wirt.at/karte.pdf  'Tafelspitz' ×2" in out
        assert "…wiener tafelspitz 24,90…" in out

    def test_print_miss_shows_details(self, capsys):
        snapshot = Snapshot(pages=2, pdfs=1, unreadable_pdfs=["x.pdf"], error="timeout")
        main.print_miss(main.Result(make_restaurant("https://wirt.at"), snapshot, []))
        out = capsys.readouterr().out
        assert "✘ Gasthaus Test — https://wirt.at (2 pages, 1 PDFs" in out
        assert "1 PDFs without text" in out
        assert "error: timeout" in out


@dataclass
class Fakes:
    """Stand-ins for the network parts; they record every call."""

    restaurants: list[Restaurant]
    pages: dict[str, Snapshot]  # website -> what crawling it returns
    geocoded: list[str] = field(default_factory=list)
    searched: list[tuple[float, float, int]] = field(default_factory=list)
    crawled: list[list[str]] = field(default_factory=list)  # names per crawl_all call
    crawl_options: dict[str, object] = field(default_factory=dict)


@pytest.fixture
def fakes(monkeypatch) -> Fakes:
    fakes = Fakes(
        restaurants=[
            make_restaurant("https://wirt.at", name="Wirt"),
            make_restaurant("https://x.at", name="Miss"),
            make_restaurant(name="No Website"),
        ],
        pages={
            "https://wirt.at": Snapshot(documents=[Document("https://wirt.at/karte", "html", "Tafelspitz")], pages=1),
            "https://x.at": Snapshot(documents=[Document("https://x.at", "html", "Schnitzel")], pages=1),
        },
    )

    def fake_geocode(place):
        fakes.geocoded.append(place)
        return 48.2, 16.35

    def fake_find(lat, lon, radius):
        fakes.searched.append((lat, lon, radius))
        return fakes.restaurants

    async def fake_crawl_all(places, **kwargs):
        fakes.crawled.append([p.name for p in places])
        fakes.crawl_options = kwargs
        snapshots = []
        for place in places:
            snapshot = fakes.pages[place.websites[0]]
            kwargs["on_done"](place, snapshot)
            snapshots.append(snapshot)
        return snapshots

    monkeypatch.setattr(main, "geocode", fake_geocode)
    monkeypatch.setattr(main, "find_restaurants", fake_find)
    monkeypatch.setattr(main, "crawl_all", fake_crawl_all)
    return fakes


class TestMain:
    def test_full_run_with_place_name(self, capsys, fakes):
        main.main(["Tafelspitz", "--near", "Wien", "--radius", "300"])

        assert fakes.geocoded == ["Wien"]
        assert fakes.searched == [(48.2, 16.35, 300)]
        assert fakes.crawled == [["Wirt", "Miss"]]  # places without website are not crawled
        assert fakes.crawl_options["max_pages"] == 15

        captured = capsys.readouterr()
        assert "1 of 2 crawled places mention Tafelspitz" in captured.out
        assert "✔ Wirt" in captured.out
        assert "[html] https://wirt.at/karte  'Tafelspitz' ×1" in captured.out
        assert "Miss" not in captured.out
        assert "3 places in 300 m, 2 with a website" in captured.err

    def test_coords_skip_geocoding(self, fakes):
        main.main(["Mate", "--coords", "1.5", "2.5"])
        assert fakes.geocoded == []
        assert fakes.searched == [(1.5, 2.5, 1000)]

    def test_verbose_lists_misses(self, capsys, fakes):
        main.main(["Tafelspitz", "--coords", "1", "2", "-v"])
        out = capsys.readouterr().out
        assert "No hits:" in out
        assert "✘ Miss — https://x.at (1 pages, 0 PDFs)" in out

    def test_whole_word_is_applied_to_search(self, capsys, fakes):
        fakes.pages["https://wirt.at"] = Snapshot(documents=[Document("https://wirt.at", "html", "Tomatensuppe")])
        main.main(["mate", "--coords", "1", "2", "--whole-word"])
        assert "0 of 2 crawled places" in capsys.readouterr().out

    def test_stops_when_no_place_has_a_website(self, fakes):
        fakes.restaurants[:] = [make_restaurant(name="Offline")]
        main.main(["Mate", "--coords", "1", "2"])
        assert fakes.crawled == []

    def test_places_sharing_a_website_are_crawled_once(self, capsys, fakes):
        fakes.restaurants.append(make_restaurant("https://wirt.at", name="Wirt Schanigarten"))

        main.main(["Tafelspitz", "--coords", "1", "2"])

        assert fakes.crawled == [["Wirt", "Miss"]]
        out = capsys.readouterr().out
        assert "2 of 3 crawled places mention Tafelspitz" in out
        assert "✔ Wirt Schanigarten" in out


class TestCaching:
    def test_second_run_needs_no_network(self, capsys, fakes):
        main.main(["Tafelspitz", "--near", "Wien"])
        capsys.readouterr()

        main.main(["Club Mate", "--near", "Wien"])  # different dish, same area

        assert fakes.geocoded == ["Wien"]
        assert len(fakes.searched) == 1
        assert len(fakes.crawled) == 1
        assert "💾 2 websites from cache, crawling 0" in capsys.readouterr().err

    def test_new_search_uses_cached_text(self, capsys, fakes):
        main.main(["Tafelspitz", "--coords", "1", "2"])
        capsys.readouterr()

        main.main(["Schnitzel", "--coords", "1", "2"])

        out = capsys.readouterr().out
        assert "1 of 2 crawled places mention Schnitzel" in out
        assert "✔ Miss" in out

    def test_place_names_are_cached_case_insensitively(self, fakes):
        main.main(["Mate", "--near", "Wien"])
        main.main(["Mate", "--near", " WIEN "])
        assert fakes.geocoded == ["Wien"]

    def test_different_radius_is_a_new_osm_search(self, fakes):
        main.main(["Mate", "--coords", "1", "2", "--radius", "500"])
        main.main(["Mate", "--coords", "1", "2", "--radius", "800"])
        assert fakes.searched == [(1.0, 2.0, 500), (1.0, 2.0, 800)]
        assert len(fakes.crawled) == 1  # the websites themselves are still cached

    def test_refresh_fetches_everything_again(self, fakes):
        main.main(["Mate", "--near", "Wien"])
        main.main(["Mate", "--near", "Wien", "--refresh"])
        assert len(fakes.geocoded) == len(fakes.searched) == len(fakes.crawled) == 2

    def test_refresh_updates_the_cache(self, capsys, fakes):
        main.main(["Mate", "--coords", "1", "2"])
        fakes.pages["https://x.at"] = Snapshot(documents=[Document("https://x.at", "html", "Neu: Club Mate")])
        main.main(["Mate", "--coords", "1", "2", "--refresh"])
        capsys.readouterr()

        main.main(["Club Mate", "--coords", "1", "2"])

        assert len(fakes.crawled) == 2
        assert "✔ Miss" in capsys.readouterr().out

    def test_no_cache_neither_reads_nor_writes(self, fakes, isolated_cache):
        main.main(["Mate", "--near", "Wien", "--no-cache"])
        main.main(["Mate", "--near", "Wien", "--no-cache"])
        assert len(fakes.crawled) == 2
        assert not isolated_cache.exists()

    def test_failed_crawls_are_cached_too(self, capsys, fakes):
        fakes.pages["https://x.at"] = Snapshot(error="ConnectError: down")

        main.main(["Mate", "--coords", "1", "2"])
        main.main(["Mate", "--coords", "1", "2", "-v"])

        assert fakes.crawled == [["Wirt", "Miss"]]
        assert "error: ConnectError: down" in capsys.readouterr().out

    def test_failed_crawls_are_retried_once_expired(self, monkeypatch, fakes):
        fakes.pages["https://x.at"] = Snapshot(error="ConnectError: down")
        monkeypatch.setattr(config, "CACHE_ERROR_MAX_AGE", -1)  # already expired when read

        main.main(["Mate", "--coords", "1", "2"])
        main.main(["Mate", "--coords", "1", "2"])

        assert fakes.crawled == [["Wirt", "Miss"], ["Miss"]]


class TestLogging:
    def test_normal_mode_shows_plain_info_messages(self):
        main.configure_logging(debug=False)
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert root.handlers[0].formatter._fmt == "%(message)s"  # type: ignore[union-attr]
        assert logging.getLogger("httpx").level == logging.ERROR

    def test_debug_mode_shows_details_and_third_party_warnings(self):
        main.configure_logging(debug=True)
        assert logging.getLogger().level == logging.DEBUG
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("pypdf").level == logging.WARNING

    def test_status_goes_to_stderr_results_to_stdout(self, capsys, fakes):
        main.main(["Tafelspitz", "--near", "Wien"])
        captured = capsys.readouterr()
        assert "📍 Wien" in captured.err
        assert "📍" not in captured.out
        assert "crawled places mention" in captured.out

    def test_debug_replaces_progress_line_with_log_messages(self, capsys, fakes):
        main.main(["Tafelspitz", "--coords", "1", "2", "--debug"])
        err = capsys.readouterr().err
        assert "🔎 crawled" not in err
        assert "Done 1/2: Wirt" in err

    def test_normal_mode_shows_progress_line(self, capsys, fakes):
        main.main(["Tafelspitz", "--coords", "1", "2"])
        assert "🔎 crawled 2/2" in capsys.readouterr().err
