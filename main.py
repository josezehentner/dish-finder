"""dish-finder: which restaurants nearby mention a dish or drink on their website?

Example:
    uv run main.py Tafelspitz --near "Wien Neubau" --radius 1500
    uv run main.py "Club Mate" Mate --coords 48.2 16.35 --whole-word
"""

import argparse
import asyncio
import logging
import sys

import config
from crawler import CrawlResult, crawl_all
from osm import find_restaurants, geocode

log = logging.getLogger(__name__)

# Third-party loggers that are too chatty for the console.
NOISY_LOGGERS = ("httpx", "httpcore", "pypdf")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find restaurants whose website or PDF menu mentions a dish or drink.")
    parser.add_argument("terms", nargs="+", help="dish/drink names; a match on any of them counts")
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--near", help="place to search around, e.g. 'Wien Neubau'")
    location.add_argument("--coords", nargs=2, type=float, metavar=("LAT", "LON"))
    parser.add_argument(
        "--radius", type=int, default=config.DEFAULT_RADIUS, help="radius in meters (default: %(default)s)"
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=config.DEFAULT_MAX_PAGES,
        help="pages + PDFs per restaurant (default: %(default)s)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=config.DEFAULT_CONCURRENCY,
        help="restaurants crawled in parallel (default: %(default)s)",
    )
    parser.add_argument("--whole-word", action="store_true", help="'mate' won't match 'tomate'")
    parser.add_argument("-v", "--verbose", action="store_true", help="also list restaurants without hits")
    parser.add_argument("--debug", action="store_true", help="log every fetched URL and why pages were skipped")
    return parser.parse_args(argv)


def configure_logging(debug: bool) -> None:
    """Status messages go to stderr, so stdout only contains the results."""
    if debug:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", force=True)
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING if debug else logging.ERROR)


def print_hit(result: CrawlResult) -> None:
    r = result.restaurant
    print(f"\n✔ {r.name} ({r.amenity})")
    print(f"  {r.osm_url}")
    for hit in result.hits:
        print(f"  [{hit.kind}] {hit.url}  '{hit.term}' ×{hit.count}")
        print(f"      …{hit.snippet}…")


def print_miss(result: CrawlResult) -> None:
    r = result.restaurant
    details = f"{result.pages} pages, {result.pdfs} PDFs"
    if result.unreadable_pdfs:
        details += f", {len(result.unreadable_pdfs)} PDFs without text (scanned?)"
    if result.error:
        details += f", error: {result.error}"
    print(f"  ✘ {r.name} — {r.websites[0]} ({details})")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(args.debug)

    if args.near:
        lat, lon = geocode(args.near)
        log.info("📍 %s → %.5f, %.5f", args.near, lat, lon)
    else:
        lat, lon = args.coords

    restaurants = find_restaurants(lat, lon, args.radius)
    with_website = [r for r in restaurants if r.websites]
    log.info("🍽  %s places in %s m, %s with a website", len(restaurants), args.radius, len(with_website))
    if not with_website:
        return

    done = 0

    def on_done(result: CrawlResult) -> None:
        nonlocal done
        done += 1
        if args.debug:
            log.debug("Done %s/%s: %s", done, len(with_website), result.restaurant.name)
        else:
            # A single self-overwriting progress line; it would garble debug output.
            print(f"\r🔎 crawled {done}/{len(with_website)}", end="", file=sys.stderr, flush=True)

    results = asyncio.run(
        crawl_all(
            with_website,
            args.terms,
            max_pages=args.max_pages,
            concurrency=args.concurrency,
            whole_word=args.whole_word,
            on_done=on_done,
        )
    )
    if not args.debug:
        print(file=sys.stderr)  # end the progress line

    found = sorted((r for r in results if r.hits), key=lambda r: r.restaurant.name.casefold())
    print(f"\n{len(found)} of {len(results)} crawled places mention {' / '.join(args.terms)}")
    for result in found:
        print_hit(result)

    if args.verbose:
        print("\nNo hits:")
        for result in sorted((r for r in results if not r.hits), key=lambda r: r.restaurant.name.casefold()):
            print_miss(result)


if __name__ == "__main__":
    main()
