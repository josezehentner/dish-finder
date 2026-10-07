import asyncio
import logging
from urllib.robotparser import RobotFileParser

import httpx
import pytest
from bs4 import BeautifulSoup

import config
import crawler
from crawler import (
    CrawlResult,
    _allowed,
    _crawl,
    _fetch,
    _host,
    _looks_like_pdf,
    _menu_links,
    _pdf_text,
    crawl_all,
)
from tests.conftest import build_pdf, html, make_restaurant, pdf

SITE = "https://wirt.at"


def crawl(web, *websites: str, terms=("Tafelspitz",), max_pages: int = 15, whole_word: bool = False) -> CrawlResult:
    """Run _crawl against a FakeWeb and return the result."""
    result = CrawlResult(make_restaurant(*websites))

    async def run():
        async with web.client() as client:
            await _crawl(client, result, list(terms), max_pages, whole_word)

    asyncio.run(run())
    return result


class TestUrlHelpers:
    @pytest.mark.parametrize(
        ("url", "host"),
        [
            ("https://www.Wirt.at/karte", "wirt.at"),
            ("http://wirt.at", "wirt.at"),
            ("https://cdn.wix.com/x", "cdn.wix.com"),
        ],
    )
    def test_host(self, url, host):
        assert _host(url) == host

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://a.at/karte.pdf", True),
            ("https://a.at/KARTE.PDF", True),
            ("https://a.at/karte.pdf?v=3", True),
            ("https://a.at/pdf/karte", False),
            ("https://a.at/karte.html", False),
        ],
    )
    def test_looks_like_pdf(self, url, expected):
        assert _looks_like_pdf(url) is expected


class TestMenuLinks:
    def links(self, body: str, base: str = f"{SITE}/") -> list[str]:
        return _menu_links(BeautifulSoup(body, "html.parser"), base)

    def test_keyword_in_url_or_link_text(self):
        body = '<a href="/speisekarte">x</a> <a href="/p/12">Getränke</a> <a href="/team">Team</a>'
        assert self.links(body) == [f"{SITE}/speisekarte", f"{SITE}/p/12"]

    def test_keyword_match_ignores_case(self):
        assert self.links('<a href="/x">MENÜ</a>') == [f"{SITE}/x"]

    def test_resolves_relative_links_and_drops_fragments(self):
        body = '<a href="karte#mittag">a</a>'
        assert self.links(body, base=f"{SITE}/de/") == [f"{SITE}/de/karte"]

    def test_skips_non_http_links(self):
        body = '<a href="mailto:menu@wirt.at">x</a> <a href="tel:123">Karte</a> <a href="javascript:menu()">y</a>'
        assert self.links(body) == []

    def test_skips_malformed_links(self):
        assert self.links('<a href="http://[broken/karte">Karte</a>') == []

    def test_pdfs_come_first(self):
        body = '<a href="/karte">a</a> <a href="/files/karte.pdf">b</a>'
        assert self.links(body) == [f"{SITE}/files/karte.pdf", f"{SITE}/karte"]


class TestPdfText:
    def test_extracts_text(self):
        assert "Tafelspitz" in _pdf_text(build_pdf("Wiener Tafelspitz 24,90"))

    def test_scanned_pdf_has_no_text(self):
        assert _pdf_text(build_pdf()).strip() == ""

    def test_broken_pdf_returns_empty_string(self):
        assert _pdf_text(b"%PDF-1.4 this is not really a pdf") == ""


class TestFetch:
    def fetch(self, web, url: str):
        async def run():
            async with web.client() as client:
                return await _fetch(client, url)

        return asyncio.run(run())

    def test_returns_final_url_content_type_and_body(self, fake_web):
        web = fake_web(
            {
                f"{SITE}/": httpx.Response(301, headers={"location": f"{SITE}/de"}),
                f"{SITE}/de": httpx.Response(200, content=b"hi", headers={"content-type": "Text/HTML; charset=utf-8"}),
            }
        )
        assert self.fetch(web, f"{SITE}/") == (f"{SITE}/de", "text/html; charset=utf-8", b"hi")

    def test_non_200_returns_none(self, fake_web):
        assert self.fetch(fake_web({}), f"{SITE}/missing") is None

    def test_too_large_returns_none(self, fake_web, monkeypatch):
        monkeypatch.setattr(config, "MAX_BYTES", 10)
        web = fake_web({f"{SITE}/big.pdf": pdf(b"x" * 11)})
        assert self.fetch(web, f"{SITE}/big.pdf") is None


class TestAllowed:
    def allowed(self, web, *urls: str) -> list[bool]:
        async def run():
            cache: dict[str, RobotFileParser] = {}
            async with web.client() as client:
                return [await _allowed(client, url, cache) for url in urls]

        return asyncio.run(run())

    def test_respects_disallow(self, fake_web):
        web = fake_web({f"{SITE}/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /intern/")})
        assert self.allowed(web, f"{SITE}/karte", f"{SITE}/intern/karte") == [True, False]

    def test_missing_robots_allows_everything(self, fake_web):
        assert self.allowed(fake_web({}), f"{SITE}/karte") == [True]

    def test_unreachable_robots_allows_everything(self, fake_web):
        web = fake_web({f"{SITE}/robots.txt": httpx.ConnectError("down")})
        assert self.allowed(web, f"{SITE}/karte") == [True]

    def test_robots_is_fetched_once_per_host(self, fake_web):
        web = fake_web({})
        self.allowed(web, f"{SITE}/a", f"{SITE}/b", "https://other.at/c")
        assert web.requested == [f"{SITE}/robots.txt", "https://other.at/robots.txt"]


class TestCrawl:
    def test_finds_hits_on_homepage_menu_page_and_cdn_pdf(self, fake_web):
        web = fake_web(
            {
                f"{SITE}/": html(
                    'Tafelspitz wie bei Oma! <a href="/speisekarte">Speisekarte</a>'
                    ' <a href="https://cdn.example.com/karte.pdf">PDF</a>'
                ),
                f"{SITE}/speisekarte": html("Wiener Tafelspitz 24,90"),
                "https://cdn.example.com/karte.pdf": pdf(build_pdf("Tafelspitz vom Rind")),
            }
        )

        result = crawl(web, f"{SITE}/")

        assert {(h.url, h.kind) for h in result.hits} == {
            (f"{SITE}/", "html"),
            (f"{SITE}/speisekarte", "html"),
            ("https://cdn.example.com/karte.pdf", "pdf"),
        }
        assert (result.pages, result.pdfs, result.error) == (2, 1, None)

    def test_does_not_follow_irrelevant_or_foreign_links(self, fake_web):
        web = fake_web(
            {
                f"{SITE}/": html('<a href="/impressum">Impressum</a> <a href="https://other.at/speisekarte">Karte</a>'),
            }
        )
        crawl(web, f"{SITE}/")
        assert f"{SITE}/impressum" not in web.requested
        assert "https://other.at/speisekarte" not in web.requested

    def test_ignores_text_in_scripts_styles_and_noscript(self, fake_web):
        web = fake_web(
            {
                f"{SITE}/": html(
                    "<script>var dish = 'Tafelspitz'</script><style>.tafelspitz {}</style>"
                    "<noscript>Tafelspitz needs JavaScript</noscript>Schnitzel"
                ),
            }
        )
        assert crawl(web, f"{SITE}/").hits == []

    def test_detects_pdf_by_content_without_pdf_extension(self, fake_web):
        web = fake_web({f"{SITE}/download?id=3": pdf(build_pdf("Tafelspitz"), content_type="application/octet-stream")})
        result = crawl(web, f"{SITE}/download?id=3")
        assert result.pdfs == 1
        assert result.hits[0].kind == "pdf"

    def test_records_pdfs_without_text(self, fake_web):
        web = fake_web({f"{SITE}/scan.pdf": pdf(build_pdf())})
        result = crawl(web, f"{SITE}/scan.pdf")
        assert result.unreadable_pdfs == [f"{SITE}/scan.pdf"]
        assert result.hits == []

    def test_stops_at_max_depth(self, fake_web):
        web = fake_web(
            {
                f"{SITE}/": html('<a href="/karte">Karte</a>'),
                f"{SITE}/karte": html('<a href="/karte/wein">Weinkarte</a>'),
                f"{SITE}/karte/wein": html('<a href="/karte/wein/rot">Rotweinkarte</a>'),
            }
        )
        crawl(web, f"{SITE}/")
        assert f"{SITE}/karte/wein" in web.requested
        assert f"{SITE}/karte/wein/rot" not in web.requested

    def test_stops_at_max_pages(self, fake_web):
        links = " ".join(f'<a href="/karte{i}">Karte</a>' for i in range(10))
        web = fake_web({f"{SITE}/": html(links), **{f"{SITE}/karte{i}": html(f"Seite {i}") for i in range(10)}})
        result = crawl(web, f"{SITE}/", max_pages=3)
        assert result.pages == 3

    def test_visits_each_url_once(self, fake_web):
        web = fake_web(
            {
                f"{SITE}/": html('<a href="/karte">Karte</a> <a href="/karte#top">Menü</a>'),
                f"{SITE}/karte": html('<a href="/">Speisen</a>'),
            }
        )
        crawl(web, f"{SITE}/")
        assert web.requested.count(f"{SITE}/karte") == 1
        assert web.requested.count(f"{SITE}/") == 1

    def test_respects_robots_txt(self, fake_web):
        web = fake_web(
            {
                f"{SITE}/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /karte"),
                f"{SITE}/": html('<a href="/karte">Karte</a>'),
            }
        )
        crawl(web, f"{SITE}/")
        assert f"{SITE}/karte" not in web.requested

    def test_unreachable_website_sets_error(self, fake_web):
        web = fake_web({f"{SITE}/": httpx.ConnectError("no such host")})
        result = crawl(web, f"{SITE}/")
        assert result.error == "ConnectError: no such host"

    def test_broken_subpage_is_skipped_silently(self, fake_web):
        web = fake_web(
            {
                f"{SITE}/": html('Tafelspitz <a href="/karte">Karte</a>'),
                f"{SITE}/karte": httpx.ReadTimeout("slow"),
            }
        )
        result = crawl(web, f"{SITE}/")
        assert result.error is None
        assert len(result.hits) == 1


class TestCrawlAll:
    def run(self, restaurants, **kwargs) -> list[CrawlResult]:
        options = {"max_pages": 5, "concurrency": 2, "whole_word": False} | kwargs
        return asyncio.run(crawl_all(restaurants, ["Tafelspitz"], **options))

    def test_returns_results_in_input_order_and_reports_progress(self, monkeypatch):
        async def fake_crawl(client, result, terms, max_pages, whole_word):
            result.pages = 1

        monkeypatch.setattr(crawler, "_crawl", fake_crawl)
        restaurants = [make_restaurant(SITE, name=f"R{i}") for i in range(3)]
        done: list[CrawlResult] = []

        results = self.run(restaurants, on_done=done.append)

        assert [r.restaurant.name for r in results] == ["R0", "R1", "R2"]
        assert sorted(r.restaurant.name for r in done) == ["R0", "R1", "R2"]

    def test_limits_concurrency(self, monkeypatch):
        running = peak = 0

        async def fake_crawl(*args):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

        monkeypatch.setattr(crawler, "_crawl", fake_crawl)
        self.run([make_restaurant(SITE)] * 6, concurrency=2)
        assert peak == 2

    def test_timeout_keeps_partial_results(self, monkeypatch):
        async def slow_crawl(client, result, *args):
            result.pages = 1
            await asyncio.sleep(10)

        monkeypatch.setattr(crawler, "_crawl", slow_crawl)
        monkeypatch.setattr(config, "RESTAURANT_TIMEOUT", 0.01)

        [result] = self.run([make_restaurant(SITE)])

        assert result.error == "timeout (partial results)"
        assert result.pages == 1

    def test_crash_of_one_site_does_not_stop_the_others(self, monkeypatch):
        async def fake_crawl(client, result, *args):
            if result.restaurant.name == "Broken":
                raise KeyError("oops")

        monkeypatch.setattr(crawler, "_crawl", fake_crawl)

        broken, ok = self.run([make_restaurant(SITE, name="Broken"), make_restaurant(SITE, name="Ok")])

        assert broken.error == "crashed: KeyError: 'oops'"
        assert ok.error is None


class TestUrlKey:
    @pytest.mark.parametrize(
        "variant",
        [
            "https://wirt.at/karte",
            "http://wirt.at/karte",
            "https://www.wirt.at/karte",
            "https://WIRT.at/karte/",
        ],
    )
    def test_equivalent_urls_share_a_key(self, variant):
        assert crawler._url_key(variant) == crawler._url_key("https://wirt.at/karte")

    def test_root_with_and_without_slash(self):
        assert crawler._url_key("https://wirt.at") == crawler._url_key("https://wirt.at/") == "wirt.at/"

    def test_query_and_path_case_matter(self):
        keys = {crawler._url_key(u) for u in ("https://wirt.at/k?id=1", "https://wirt.at/k?id=2", "https://wirt.at/K")}
        assert len(keys) == 3


class TestDuplicates:
    def test_url_variants_are_fetched_once(self, fake_web):
        web = fake_web(
            {
                f"{SITE}/": html(
                    '<a href="/karte">Karte</a> <a href="/karte/">Speisen</a> <a href="https://www.wirt.at/karte">Menü</a>'
                ),
                f"{SITE}/karte": html("Tafelspitz"),
            }
        )
        result = crawl(web, f"{SITE}/")
        karte_requests = [u for u in web.requested if "karte" in u]
        assert karte_requests == [f"{SITE}/karte"]
        assert len(result.hits) == 1

    def test_same_pdf_on_two_hosts_counts_once(self, fake_web):
        menu = build_pdf("Kaspressknoedel")
        web = fake_web(
            {
                f"{SITE}/": html(
                    '<a href="https://cdn-a.example/karte.pdf">PDF</a> <a href="https://cdn-b.example/karte.pdf">PDF</a>'
                ),
                "https://cdn-a.example/karte.pdf": pdf(menu),
                "https://cdn-b.example/karte.pdf": pdf(menu),
            }
        )
        result = crawl(web, f"{SITE}/", terms=("Kaspressknödel",))
        assert result.pdfs == 1
        assert [h.url for h in result.hits] == ["https://cdn-a.example/karte.pdf"]

    def test_redirect_to_already_seen_page_is_skipped(self, fake_web):
        web = fake_web(
            {
                f"{SITE}/": html('Tafelspitz <a href="/speisekarte-alt">Speisekarte</a>'),
                f"{SITE}/speisekarte-alt": httpx.Response(301, headers={"location": f"{SITE}/"}),
            }
        )
        result = crawl(web, f"{SITE}/")
        assert result.pages == 1
        assert len(result.hits) == 1

    def test_different_content_on_different_pages_is_kept(self, fake_web):
        web = fake_web(
            {
                f"{SITE}/": html('<a href="/karte">Karte</a> <a href="/getraenke">Getränke</a>'),
                f"{SITE}/karte": html("Tafelspitz"),
                f"{SITE}/getraenke": html("Tafelspitz-Spritzer"),
            }
        )
        assert crawl(web, f"{SITE}/").pages == 3


class TestDebugLogging:
    def test_logs_fetched_and_skipped_urls(self, fake_web, caplog):
        web = fake_web(
            {
                f"{SITE}/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /intern"),
                f"{SITE}/": html('<a href="/intern/karte">Karte</a> <a href="/karte.pdf">PDF</a>'),
                f"{SITE}/karte.pdf": httpx.ConnectError("reset"),
            }
        )
        with caplog.at_level(logging.DEBUG, logger="crawler"):
            crawl(web, f"{SITE}/")

        messages = caplog.messages
        assert f"HTML {SITE}/" in messages
        assert f"robots.txt forbids {SITE}/intern/karte" in messages
        assert f"Failed to fetch {SITE}/karte.pdf: reset" in messages

    def test_logs_crash_with_traceback(self, monkeypatch, caplog):
        async def broken_crawl(*args):
            raise KeyError("oops")

        monkeypatch.setattr(crawler, "_crawl", broken_crawl)
        with caplog.at_level(logging.DEBUG, logger="crawler"):
            asyncio.run(crawl_all([make_restaurant(SITE)], ["x"], max_pages=1, concurrency=1, whole_word=False))

        [record] = [r for r in caplog.records if "crashed" in r.message]
        assert record.exc_info is not None
