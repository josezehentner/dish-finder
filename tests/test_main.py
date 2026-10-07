import logging
import sys

import pytest

import main
from crawler import CrawlResult
from matching import Hit
from tests.conftest import make_restaurant


def parse(monkeypatch, *argv: str):
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    return main.parse_args()


def hit_result(name: str = "Wirt") -> CrawlResult:
    hit = Hit("Tafelspitz", "https://wirt.at/karte.pdf", "pdf", 2, "wiener tafelspitz 24,90")
    return CrawlResult(make_restaurant("https://wirt.at", name=name), hits=[hit], pages=1, pdfs=1)


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

    def test_coords(self, monkeypatch):
        assert parse(monkeypatch, "Mate", "--coords", "48.2", "16.35").coords == [48.2, 16.35]

    @pytest.mark.parametrize(
        "argv",
        [
            ("Mate",),  # no location
            ("Mate", "--near", "Wien", "--coords", "1", "2"),  # both locations
            ("--near", "Wien"),  # no search term
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
        result = CrawlResult(
            make_restaurant("https://wirt.at"), pages=2, pdfs=1, unreadable_pdfs=["x.pdf"], error="timeout"
        )
        main.print_miss(result)
        out = capsys.readouterr().out
        assert "✘ Gasthaus Test — https://wirt.at (2 pages, 1 PDFs" in out
        assert "1 PDFs without text" in out
        assert "error: timeout" in out


@pytest.fixture
def fakes(monkeypatch):
    """Replace geocoding, OSM and the crawler; returns what they were called with."""
    calls = {}
    restaurants = [make_restaurant("https://wirt.at", name="Wirt"), make_restaurant(name="No Website")]

    def fake_geocode(place):
        calls["geocode"] = place
        return 48.2, 16.35

    def fake_find(lat, lon, radius):
        calls["find"] = (lat, lon, radius)
        return restaurants

    async def fake_crawl_all(found, terms, **kwargs):
        calls["crawl"] = ([r.name for r in found], terms, kwargs)
        results = [hit_result(r.name) for r in found] + [CrawlResult(make_restaurant("https://x.at", name="Miss"))]
        for result in results:
            kwargs["on_done"](result)
        return results

    monkeypatch.setattr(main, "geocode", fake_geocode)
    monkeypatch.setattr(main, "find_restaurants", fake_find)
    monkeypatch.setattr(main, "crawl_all", fake_crawl_all)
    return calls, restaurants


class TestMain:
    def test_full_run_with_place_name(self, monkeypatch, capsys, fakes):
        calls, _ = fakes
        monkeypatch.setattr(sys, "argv", ["main.py", "Tafelspitz", "--near", "Wien", "--radius", "300"])

        main.main()

        assert calls["geocode"] == "Wien"
        assert calls["find"] == (48.2, 16.35, 300)
        crawled, terms, kwargs = calls["crawl"]
        assert crawled == ["Wirt"]  # places without website are not crawled
        assert terms == ["Tafelspitz"]
        assert kwargs["max_pages"] == 15

        captured = capsys.readouterr()
        assert "1 of 2 crawled places mention Tafelspitz" in captured.out
        assert "✔ Wirt" in captured.out
        assert "Miss" not in captured.out
        assert "2 places in 300 m, 1 with a website" in captured.err

    def test_coords_skip_geocoding(self, monkeypatch, fakes):
        calls, _ = fakes
        monkeypatch.setattr(sys, "argv", ["main.py", "Mate", "--coords", "1.5", "2.5"])

        main.main()

        assert "geocode" not in calls
        assert calls["find"] == (1.5, 2.5, 1000)

    def test_verbose_lists_misses(self, monkeypatch, capsys, fakes):
        monkeypatch.setattr(sys, "argv", ["main.py", "Mate", "--coords", "1", "2", "-v"])
        main.main()
        out = capsys.readouterr().out
        assert "No hits:" in out
        assert "✘ Miss" in out

    def test_stops_when_no_place_has_a_website(self, monkeypatch, fakes):
        calls, restaurants = fakes
        restaurants[:] = [make_restaurant(name="Offline")]
        monkeypatch.setattr(sys, "argv", ["main.py", "Mate", "--coords", "1", "2"])

        main.main()

        assert "crawl" not in calls


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

    def test_status_goes_to_stderr_results_to_stdout(self, monkeypatch, capsys, fakes):
        monkeypatch.setattr(sys, "argv", ["main.py", "Tafelspitz", "--near", "Wien"])
        main.main()
        captured = capsys.readouterr()
        assert "📍 Wien" in captured.err
        assert "📍" not in captured.out
        assert "crawled places mention" in captured.out

    def test_debug_replaces_progress_line_with_log_messages(self, capsys, fakes):
        main.main(["Tafelspitz", "--coords", "1", "2", "--debug"])
        err = capsys.readouterr().err
        assert "🔎 crawled" not in err
        assert "Done 1/1: Wirt" in err

    def test_normal_mode_shows_progress_line(self, capsys, fakes):
        main.main(["Tafelspitz", "--coords", "1", "2"])
        assert "🔎 crawled 1/1" in capsys.readouterr().err
