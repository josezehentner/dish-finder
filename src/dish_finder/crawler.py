"""Crawl restaurant websites for menu pages and PDFs and extract their text."""

import asyncio
import hashlib
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from dish_finder import config
from dish_finder.matching import Document
from dish_finder.osm import Restaurant

log = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """Everything found on one website, independent of what is searched for."""

    documents: list[Document] = field(default_factory=list)
    pages: int = 0
    pdfs: int = 0
    unreadable_pdfs: list[str] = field(default_factory=list)  # scanned/image-only
    error: str | None = None


async def crawl_all(
    restaurants: list[Restaurant],
    *,
    max_pages: int,
    concurrency: int,
    on_done: Callable[[Restaurant, Snapshot], None] = lambda *_: None,
) -> list[Snapshot]:
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        headers={"User-Agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT, follow_redirects=True
    ) as client:

        async def run(restaurant: Restaurant) -> Snapshot:
            snapshot = Snapshot()
            async with semaphore:
                try:
                    await asyncio.wait_for(
                        _crawl(client, snapshot, restaurant.websites, max_pages), config.RESTAURANT_TIMEOUT
                    )
                except TimeoutError:
                    snapshot.error = "timeout (partial results)"
                except Exception as exc:  # one weird website must not kill the whole run
                    log.debug("Crawling %s crashed", restaurant.name, exc_info=True)
                    snapshot.error = f"crashed: {type(exc).__name__}: {exc}"
            on_done(restaurant, snapshot)
            return snapshot

        return await asyncio.gather(*(run(r) for r in restaurants))


async def _crawl(client: httpx.AsyncClient, snapshot: Snapshot, seeds: list[str], max_pages: int) -> None:
    site_hosts = {_host(url) for url in seeds}
    queue = deque((url, 0) for url in seeds)
    seen_urls = {_url_key(url) for url in seeds}
    # The same page/PDF is often reachable under several URLs (redirects, mirrors).
    seen_content: set[bytes] = set()
    robots: dict[str, RobotFileParser] = {}

    while queue and snapshot.pages + snapshot.pdfs < max_pages:
        url, depth = queue.popleft()
        if not await _allowed(client, url, robots):
            log.debug("robots.txt forbids %s", url)
            continue
        try:
            fetched = await _fetch(client, url)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            log.debug("Failed to fetch %s: %s", url, exc)
            if depth == 0:
                snapshot.error = f"{type(exc).__name__}: {exc}"
            continue
        if fetched is None:
            continue
        final_url, content_type, body = fetched

        digest = hashlib.sha256(body).digest()
        if digest in seen_content:
            log.debug("Skipping %s: same content as an earlier page", final_url)
            continue
        seen_content.add(digest)

        if "pdf" in content_type or body.startswith(b"%PDF"):
            log.debug("PDF  %s", final_url)
            snapshot.pdfs += 1
            text = _pdf_text(body)
            if not text.strip():
                snapshot.unreadable_pdfs.append(final_url)
                continue
            snapshot.documents.append(Document(final_url, "pdf", text))

        elif "html" in content_type:
            log.debug("HTML %s", final_url)
            snapshot.pages += 1
            soup = BeautifulSoup(body, "html.parser")
            links = _menu_links(soup, final_url) if depth < config.MAX_DEPTH else []
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            snapshot.documents.append(Document(final_url, "html", soup.get_text(" ")))

            for link in links:
                key = _url_key(link)
                if key in seen_urls:
                    continue
                # Stay on the restaurant's site, but PDFs often live on a CDN.
                if _host(link) not in site_hosts and not _looks_like_pdf(link):
                    continue
                seen_urls.add(key)
                queue.append((link, depth + 1))


async def _fetch(client: httpx.AsyncClient, url: str) -> tuple[str, str, bytes] | None:
    async with client.stream("GET", url) as resp:
        if resp.status_code != 200:
            log.debug("HTTP %s for %s", resp.status_code, url)
            return None
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > config.MAX_BYTES:
                log.debug("Skipping %s: larger than %s bytes", url, config.MAX_BYTES)
                return None
            chunks.append(chunk)
        return str(resp.url), resp.headers.get("content-type", "").lower(), b"".join(chunks)


async def _allowed(client: httpx.AsyncClient, url: str, cache: dict[str, RobotFileParser]) -> bool:
    parts = urlparse(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in cache:
        parser = RobotFileParser()
        try:
            resp = await client.get(root + "/robots.txt")
            parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except (httpx.HTTPError, httpx.InvalidURL):
            parser.parse([])
        cache[root] = parser
    return cache[root].can_fetch(config.USER_AGENT, url)


def _menu_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    links = []
    for a in soup.select("a[href]"):
        try:
            url, _ = urldefrag(urljoin(base_url, str(a["href"])))
        except ValueError:  # malformed href, e.g. "http://[broken"
            continue
        if not url.startswith(("http://", "https://")):
            continue  # mailto:, tel:, javascript:
        haystack = f"{url} {a.get_text(' ')}".casefold()
        if any(keyword in haystack for keyword in config.MENU_KEYWORDS):
            links.append(url)
    # PDFs first: they are usually the actual menu.
    return sorted(links, key=lambda link: not _looks_like_pdf(link))


def _pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:  # pypdf raises many different errors on broken files
        return ""


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _url_key(url: str) -> str:
    """Identity of a URL for 'already visited' checks: ignores http/https, www. and a trailing slash."""
    parts = urlparse(url)
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{_host(url)}{path}{query}"


def _looks_like_pdf(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")
