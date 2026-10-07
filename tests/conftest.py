"""Shared fixtures: fake HTTP servers, restaurants and generated PDFs."""

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from dish_finder import config
from dish_finder.osm import Restaurant

# A route answers with a fixed response, builds one from the request, or raises.
Route = httpx.Response | Callable[[httpx.Request], httpx.Response] | Exception


class FakeWeb:
    """In-memory 'internet' for httpx: unknown URLs answer 404, every request is recorded."""

    def __init__(self, routes: dict[str, Route]):
        self.routes = routes
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        route = self.routes.get(url, httpx.Response(404))
        if isinstance(route, Exception):
            raise route
        if callable(route):
            return route(request)
        return route

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), follow_redirects=True)


def html(body: str) -> httpx.Response:
    return httpx.Response(200, html=f"<html><body>{body}</body></html>")


def pdf(data: bytes, content_type: str = "application/pdf") -> httpx.Response:
    return httpx.Response(200, content=data, headers={"content-type": content_type})


def build_pdf(text: str = "") -> bytes:
    """Build a minimal one-page PDF; empty text gives a page without text, like a scan."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def make_restaurant(*websites: str, name: str = "Gasthaus Test") -> Restaurant:
    return Restaurant(
        name=name,
        amenity="restaurant",
        lat=48.2,
        lon=16.35,
        websites=list(websites),
        osm_url="https://www.openstreetmap.org/node/1",
    )


@pytest.fixture
def fake_web() -> Callable[[dict[str, Route]], FakeWeb]:
    return FakeWeb


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real cache in ~/.cache during tests."""
    path = tmp_path / "cache.sqlite3"
    monkeypatch.setattr(config, "CACHE_PATH", path)
    return path


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """main.configure_logging() changes global logger state; undo it after each test."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    noisy = {name: logging.getLogger(name).level for name in ("httpx", "httpcore", "pypdf")}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, noisy_level in noisy.items():
        logging.getLogger(name).setLevel(noisy_level)
