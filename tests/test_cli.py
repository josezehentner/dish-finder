import logging
import sys
from dataclasses import dataclass, field

import pytest

from dish_finder import cli, config
from dish_finder.crawler import Snapshot
from dish_finder.matching import Document
from dish_finder.osm import Restaurant
from tests.conftest import make_restaurant


def parse(monkeypatch, *argv: str):
    monkeypatch.setattr(sys, "argv", ["cli.py", *argv])
    return cli.parse_args()


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

    monkeypatch.setattr(cli, "geocode", fake_geocode)
    monkeypatch.setattr(cli, "find_restaurants", fake_find)
    monkeypatch.setattr(cli, "crawl_all", fake_crawl_all)
    return fakes


class TestMain:
    def test_full_run_with_place_name(self, capsys, fakes):
        cli.main(["Tafelspitz", "--near", "Wien", "--radius", "300"])

        assert fakes.geocoded == ["Wien"]
        assert fakes.searched == [(48.2, 16.35, 300)]
        assert fakes.crawled == [["Wirt", "Miss"]]  # places without website are not crawled
        assert fakes.crawl_options["max_pages"] == 15

        captured = capsys.readouterr()
        assert "Tafelspitz found at 1 of 2 places" in captured.out
        assert "⏺ Wirt · restaurant" in captured.out
        assert "     HTML  https://wirt.at/karte\n           Tafelspitz\n" in captured.out
        assert "Miss" not in captured.out
        assert "3 places in 300 m, 2 with a website" in captured.err

    def test_coords_skip_geocoding(self, fakes):
        cli.main(["Mate", "--coords", "1.5", "2.5"])
        assert fakes.geocoded == []
        assert fakes.searched == [(1.5, 2.5, 1000)]

    def test_verbose_lists_misses(self, capsys, fakes):
        cli.main(["Tafelspitz", "--coords", "48.2", "16.35", "-v"])
        out = capsys.readouterr().out
        assert "No match (1)" in out
        assert "  · Miss · restaurant · 0 m  https://x.at  1 page, 0 PDFs" in out

    def test_whole_word_is_applied_to_search(self, capsys, fakes):
        fakes.pages["https://wirt.at"] = Snapshot(documents=[Document("https://wirt.at", "html", "Tomatensuppe")])
        cli.main(["mate", "--coords", "1", "2", "--whole-word"])
        assert "mate not found (2 places searched)" in capsys.readouterr().out

    def test_stops_when_no_place_has_a_website(self, fakes):
        fakes.restaurants[:] = [make_restaurant(name="Offline")]
        cli.main(["Mate", "--coords", "1", "2"])
        assert fakes.crawled == []

    def test_places_sharing_a_website_are_crawled_once(self, capsys, fakes):
        fakes.restaurants.append(make_restaurant("https://wirt.at", name="Wirt Schanigarten"))

        cli.main(["Tafelspitz", "--coords", "1", "2"])

        assert fakes.crawled == [["Wirt", "Miss"]]
        out = capsys.readouterr().out
        assert "Tafelspitz found at 2 of 3 places" in out
        assert "⏺ Wirt Schanigarten" in out


class TestCaching:
    def test_second_run_needs_no_network(self, capsys, fakes):
        cli.main(["Tafelspitz", "--near", "Wien"])
        capsys.readouterr()

        cli.main(["Club Mate", "--near", "Wien"])  # different dish, same area

        assert fakes.geocoded == ["Wien"]
        assert len(fakes.searched) == 1
        assert len(fakes.crawled) == 1
        assert "💾 2 websites from cache, crawling 0" in capsys.readouterr().err

    def test_new_search_uses_cached_text(self, capsys, fakes):
        cli.main(["Tafelspitz", "--coords", "1", "2"])
        capsys.readouterr()

        cli.main(["Schnitzel", "--coords", "1", "2"])

        out = capsys.readouterr().out
        assert "Schnitzel found at 1 of 2 places" in out
        assert "⏺ Miss" in out

    def test_place_names_are_cached_case_insensitively(self, fakes):
        cli.main(["Mate", "--near", "Wien"])
        cli.main(["Mate", "--near", " WIEN "])
        assert fakes.geocoded == ["Wien"]

    def test_different_radius_is_a_new_osm_search(self, fakes):
        cli.main(["Mate", "--coords", "1", "2", "--radius", "500"])
        cli.main(["Mate", "--coords", "1", "2", "--radius", "800"])
        assert fakes.searched == [(1.0, 2.0, 500), (1.0, 2.0, 800)]
        assert len(fakes.crawled) == 1  # the websites themselves are still cached

    def test_refresh_fetches_everything_again(self, fakes):
        cli.main(["Mate", "--near", "Wien"])
        cli.main(["Mate", "--near", "Wien", "--refresh"])
        assert len(fakes.geocoded) == len(fakes.searched) == len(fakes.crawled) == 2

    def test_refresh_updates_the_cache(self, capsys, fakes):
        cli.main(["Mate", "--coords", "1", "2"])
        fakes.pages["https://x.at"] = Snapshot(documents=[Document("https://x.at", "html", "Neu: Club Mate")])
        cli.main(["Mate", "--coords", "1", "2", "--refresh"])
        capsys.readouterr()

        cli.main(["Club Mate", "--coords", "1", "2"])

        assert len(fakes.crawled) == 2
        assert "⏺ Miss" in capsys.readouterr().out

    def test_no_cache_neither_reads_nor_writes(self, fakes, isolated_cache):
        cli.main(["Mate", "--near", "Wien", "--no-cache"])
        cli.main(["Mate", "--near", "Wien", "--no-cache"])
        assert len(fakes.crawled) == 2
        assert not isolated_cache.exists()

    def test_failed_crawls_are_cached_too(self, capsys, fakes):
        fakes.pages["https://x.at"] = Snapshot(error="ConnectError: down")

        cli.main(["Mate", "--coords", "1", "2"])
        cli.main(["Mate", "--coords", "1", "2", "-v"])

        assert fakes.crawled == [["Wirt", "Miss"]]
        assert "0 pages, 0 PDFs  ConnectError: down" in capsys.readouterr().out

    def test_failed_crawls_are_retried_once_expired(self, monkeypatch, fakes):
        fakes.pages["https://x.at"] = Snapshot(error="ConnectError: down")
        monkeypatch.setattr(config, "CACHE_ERROR_MAX_AGE", -1)  # already expired when read

        cli.main(["Mate", "--coords", "1", "2"])
        cli.main(["Mate", "--coords", "1", "2"])

        assert fakes.crawled == [["Wirt", "Miss"], ["Miss"]]


class TestLogging:
    def test_normal_mode_shows_plain_info_messages(self):
        cli.configure_logging(debug=False)
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert root.handlers[0].formatter._fmt == "%(message)s"  # type: ignore[union-attr]
        assert logging.getLogger("httpx").level == logging.ERROR

    def test_debug_mode_shows_details_and_third_party_warnings(self):
        cli.configure_logging(debug=True)
        assert logging.getLogger().level == logging.DEBUG
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("pypdf").level == logging.WARNING

    def test_status_goes_to_stderr_results_to_stdout(self, capsys, fakes):
        cli.main(["Tafelspitz", "--near", "Wien"])
        captured = capsys.readouterr()
        assert "📍 Wien" in captured.err
        assert "📍" not in captured.out
        assert "Tafelspitz found at" in captured.out

    def test_debug_replaces_progress_line_with_log_messages(self, capsys, fakes):
        cli.main(["Tafelspitz", "--coords", "1", "2", "--debug"])
        err = capsys.readouterr().err
        assert "🔎" not in err
        assert "Done 1/2: Wirt" in err

    def test_terminal_shows_progress_line_and_erases_it(self, monkeypatch, capsys, fakes):
        monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
        cli.main(["Tafelspitz", "--coords", "1", "2"])
        err = capsys.readouterr().err
        assert "\r🔎 crawling 1/2\r🔎 crawling 2/2\r\033[K" in err

    def test_no_progress_line_when_stderr_is_not_a_terminal(self, capsys, fakes):
        cli.main(["Tafelspitz", "--coords", "1", "2"])
        assert "🔎" not in capsys.readouterr().err

    def test_report_links_google_maps_first(self, capsys, fakes):
        cli.main(["Tafelspitz", "--coords", "1", "2"])
        links = [line.split()[-1] for line in capsys.readouterr().out.splitlines() if "https://" in line]
        assert links[:3] == [
            "https://www.google.com/maps/search/?api=1&query=48.2,16.35",
            "https://www.openstreetmap.org/node/1",
            "https://wirt.at",
        ]

    def test_results_are_plain_text_when_piped(self, capsys, fakes):
        cli.main(["Tafelspitz", "--coords", "1", "2"])
        assert "\033[" not in capsys.readouterr().out


def place_north(name: str, meters: float, website: bool = True) -> Restaurant:
    """A place `meters` north of the search center (48.2, 16.35)."""
    lat = 48.2 + meters / 111_195
    return Restaurant(name, "restaurant", lat, 16.35, [f"https://{name.lower()}.at"] if website else [], "")


class TestLimit:
    @pytest.fixture
    def spread(self, fakes: Fakes) -> Fakes:
        """Four places with websites at 300 m, 100 m, 400 m, 200 m plus one without website at 50 m."""
        fakes.restaurants[:] = [
            place_north("D", 300),
            place_north("A", 100),
            place_north("Offline", 50, website=False),
            place_north("E", 400),
            place_north("B", 200),
        ]
        for name in "ABDE":
            url = f"https://{name.lower()}.at"
            fakes.pages[url] = Snapshot(documents=[Document(url, "html", "Mate")])
        return fakes

    def test_parses_limit(self, monkeypatch):
        assert parse(monkeypatch, "Mate", "--near", "Wien", "--limit", "5").limit == 5

    def test_no_limit_by_default(self, monkeypatch):
        assert parse(monkeypatch, "Mate", "--near", "Wien").limit is None

    @pytest.mark.parametrize("value", ["0", "-3", "abc", "2.5"])
    def test_rejects_invalid_limits(self, monkeypatch, capsys, value):
        with pytest.raises(SystemExit):
            parse(monkeypatch, "Mate", "--near", "Wien", "--limit", value)
        assert "--limit" in capsys.readouterr().err

    def test_crawls_only_the_nearest(self, spread):
        cli.main(["Mate", "--coords", "48.2", "16.35", "--limit", "2"])
        assert spread.crawled == [["A", "B"]]

    def test_places_without_website_do_not_use_up_the_limit(self, spread):
        cli.main(["Mate", "--coords", "48.2", "16.35", "--limit", "1"])
        assert spread.crawled == [["A"]]

    def test_results_only_include_limited_places(self, capsys, spread):
        cli.main(["Mate", "--coords", "48.2", "16.35", "--limit", "3"])
        out = capsys.readouterr().out
        assert "Mate found at 3 of 3 places" in out
        assert "⏺ E" not in out

    def test_logs_how_far_the_limited_search_reaches(self, capsys, spread):
        cli.main(["Mate", "--coords", "48.2", "16.35", "--limit", "2"])
        assert "📏 limited to the 2 nearest (up to 200 m away)" in capsys.readouterr().err

    def test_limit_at_or_above_count_changes_nothing(self, capsys, spread):
        cli.main(["Mate", "--coords", "48.2", "16.35", "--limit", "4"])
        assert spread.crawled == [["D", "A", "E", "B"]]  # original order, nothing dropped
        assert "📏" not in capsys.readouterr().err

    def test_raising_the_limit_later_only_crawls_the_new_places(self, spread):
        cli.main(["Mate", "--coords", "48.2", "16.35", "--limit", "2"])
        cli.main(["Mate", "--coords", "48.2", "16.35", "--limit", "3"])
        assert spread.crawled == [["A", "B"], ["D"]]
