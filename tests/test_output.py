import io

import pytest

from dish_finder.crawler import Snapshot
from dish_finder.matching import Hit
from dish_finder.output import Result, Style, format_distance, plural, render_hit, render_miss, render_report
from tests.conftest import make_restaurant

PLAIN = Style(color=False)
COLOR = Style(color=True)


def hit(url: str = "https://wirt.at/karte.pdf", kind="pdf", term="Tafelspitz", count=1) -> Hit:
    return Hit(term, url, kind, count, "…Wiener ", term, " mit Kren…")


def result(name="Wirt", distance=120.0, hits=None, amenity="restaurant", **snapshot) -> Result:
    place = make_restaurant("https://wirt.at", name=name)
    place.amenity = amenity
    return Result(place, Snapshot(**snapshot), hits if hits is not None else [hit()], distance)


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestStyle:
    def test_plain_style_leaves_text_alone(self):
        assert PLAIN.bold("x") == PLAIN.dim("x") == PLAIN.highlight("x") == PLAIN.red("x") == "x"

    def test_color_style_wraps_text_in_ansi_codes(self):
        assert COLOR.bold("x") == "\033[1mx\033[0m"
        assert COLOR.highlight("x") == "\033[1;33mx\033[0m"

    def test_color_on_a_terminal(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")
        assert Style.for_stream(TTY()).color

    def test_no_color_when_piped(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert not Style.for_stream(io.StringIO()).color

    @pytest.mark.parametrize(("variable", "value"), [("NO_COLOR", "1"), ("TERM", "dumb")])
    def test_environment_can_turn_color_off(self, monkeypatch, variable, value):
        monkeypatch.setenv(variable, value)
        assert not Style.for_stream(TTY()).color


class TestHelpers:
    @pytest.mark.parametrize(
        ("meters", "text"), [(0, "0 m"), (41.4, "41 m"), (999.4, "999 m"), (1000, "1.0 km"), (2549, "2.5 km")]
    )
    def test_format_distance(self, meters, text):
        assert format_distance(meters) == text

    @pytest.mark.parametrize(("count", "text"), [(0, "0 pages"), (1, "1 page"), (2, "2 pages")])
    def test_plural(self, count, text):
        assert plural(count, "page") == text


class TestRenderHit:
    def test_layout(self):
        assert render_hit(result(), PLAIN).splitlines() == [
            "⏺ Wirt · restaurant · 120 m",
            "  ⎿  Google Maps  https://www.google.com/maps/search/?api=1&query=48.2,16.35",
            "     OSM          https://www.openstreetmap.org/node/1",
            "     Website      https://wirt.at",
            "",
            "     PDF   https://wirt.at/karte.pdf",
            "           …Wiener Tafelspitz mit Kren…",
        ]

    def test_links_in_order_google_maps_osm_website(self):
        lines = render_hit(result(), PLAIN).splitlines()
        assert [line.split()[-2] for line in lines[1:4]] == ["Maps", "OSM", "Website"]

    def test_matches_on_the_same_page_are_grouped(self):
        hits = [
            hit("https://wirt.at/", "html", "Tafelspitz"),
            hit("https://wirt.at/karte.pdf", "pdf", "Tafelspitz"),
            hit("https://wirt.at/", "html", "Kren"),
        ]
        lines = render_hit(result(hits=hits), PLAIN).splitlines()

        assert [line.strip() for line in lines if "https://wirt.at/" in line and "Website" not in line] == [
            "HTML  https://wirt.at/",
            "PDF   https://wirt.at/karte.pdf",
        ]
        assert sum("…Wiener" in line for line in lines) == 3

    def test_count_only_shown_for_repeated_matches(self):
        assert "×" not in render_hit(result(hits=[hit(count=1)]), PLAIN)
        assert render_hit(result(hits=[hit(count=4)]), PLAIN).endswith("mit Kren…  ×4")

    def test_amenity_underscores_become_spaces(self):
        assert "· fast food ·" in render_hit(result(amenity="fast_food"), PLAIN)

    def test_match_is_highlighted_in_color(self):
        assert "…Wiener \033[1;33mTafelspitz\033[0m mit Kren…" in render_hit(result(), COLOR)


class TestRenderMiss:
    def test_one_line_with_details(self):
        line = render_miss(result(hits=[], pages=2, pdfs=1), PLAIN)
        assert line == "  · Wirt · restaurant · 120 m  https://wirt.at  2 pages, 1 PDF"

    def test_scanned_pdfs_and_error(self):
        line = render_miss(result(hits=[], pdfs=1, unreadable_pdfs=["x.pdf"], error="ConnectError: down"), PLAIN)
        assert line.endswith("0 pages, 1 PDF, 1 scanned PDF without text  ConnectError: down")

    def test_error_is_red(self):
        assert "\033[31mConnectError: down\033[0m" in render_miss(result(hits=[], error="ConnectError: down"), COLOR)


class TestRenderReport:
    def test_summary_and_matches_nearest_first(self):
        results = [result("Far", 400), result("Miss", 50, hits=[]), result("Near", 100)]
        report = render_report(results, ["Tafelspitz"], verbose=False, style=PLAIN)

        lines = report.splitlines()
        assert lines[0] == "Tafelspitz found at 2 of 3 places"
        assert [line for line in lines if line.startswith("⏺")] == [
            "⏺ Near · restaurant · 100 m",
            "⏺ Far · restaurant · 400 m",
        ]
        assert "Miss" not in report

    def test_several_terms(self):
        report = render_report([result()], ["Club Mate", "Mate"], verbose=False, style=PLAIN)
        assert report.startswith("Club Mate / Mate found at 1 of 1 place")

    def test_nothing_found(self):
        report = render_report([result(hits=[])], ["Tafelspitz"], verbose=False, style=PLAIN)
        assert report == "Tafelspitz not found (1 place searched)"

    def test_verbose_lists_misses_nearest_first(self):
        results = [result("B", 300, hits=[]), result("Hit", 10), result("A", 200, hits=[])]
        lines = render_report(results, ["Tafelspitz"], verbose=True, style=PLAIN).splitlines()

        start = lines.index("No match (2)")
        assert [line.split(" · ")[1] for line in lines[start + 1 :]] == ["A", "B"]

    def test_verbose_without_misses_has_no_section(self):
        assert "No match" not in render_report([result()], ["Tafelspitz"], verbose=True, style=PLAIN)

    def test_search_terms_are_bold(self):
        assert render_report([result()], ["Tafelspitz"], False, COLOR).startswith("\033[1mTafelspitz\033[0m found")
