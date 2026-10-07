"""dish-finder: which restaurants nearby mention a dish or drink on their website?

Example:
    uv run dish-finder Tafelspitz --near "Wien Neubau" --radius 1500
    uv run dish-finder "Club Mate" Mate --coords 48.2 16.35 --whole-word
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import asdict

from dish_finder import config
from dish_finder.cache import Cache, site_key
from dish_finder.crawler import Snapshot, crawl_all
from dish_finder.matching import search
from dish_finder.osm import Restaurant, distance_m, find_restaurants, geocode, nearest
from dish_finder.output import Result, Style, render_report

log = logging.getLogger(__name__)

# Third-party loggers that are too chatty for the console.
NOISY_LOGGERS = ("httpx", "httpcore", "pypdf")


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dish-finder", description="Find restaurants whose website or PDF menu mentions a dish or drink."
    )
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
    parser.add_argument(
        "--limit",
        type=positive_int,
        metavar="N",
        help="only search the N nearest places with a website (default: all in the radius)",
    )
    parser.add_argument("--whole-word", action="store_true", help="'mate' won't match 'tomate'")
    parser.add_argument("-v", "--verbose", action="store_true", help="also list restaurants without hits")
    parser.add_argument("--debug", action="store_true", help="log every fetched URL and why pages were skipped")
    cache = parser.add_mutually_exclusive_group()
    cache.add_argument("--refresh", action="store_true", help="ignore cached data, fetch everything again")
    cache.add_argument("--no-cache", action="store_true", help="neither read nor write the cache")
    return parser.parse_args(argv)


def configure_logging(debug: bool) -> None:
    """Status messages go to stderr, so stdout only contains the results."""
    if debug:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", force=True)
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING if debug else logging.ERROR)


def cached_geocode(cache: Cache, place: str) -> tuple[float, float]:
    key = f"geocode:{place.strip().casefold()}"
    if (cached := cache.get_json(key)) is not None:
        lat, lon = cached
    else:
        lat, lon = geocode(place)
        cache.put_json(key, [lat, lon])
    return lat, lon


def cached_restaurants(cache: Cache, lat: float, lon: float, radius: int) -> list[Restaurant]:
    key = f"overpass:{lat:.5f},{lon:.5f},{radius}"
    if (cached := cache.get_json(key)) is not None:
        log.debug("Places for %s from cache", key)
        return [Restaurant(**r) for r in cached]
    restaurants = find_restaurants(lat, lon, radius)
    cache.put_json(key, [asdict(r) for r in restaurants])
    return restaurants


def collect_snapshots(cache: Cache, places: list[Restaurant], args: argparse.Namespace) -> dict[str, Snapshot]:
    """Snapshots per website: from the cache where possible, crawled otherwise."""
    snapshots: dict[str, Snapshot] = {}
    to_crawl: dict[str, Restaurant] = {}  # one crawl per website, even if several places share it
    for place in places:
        key = site_key(place.websites)
        if key in snapshots or key in to_crawl:
            continue
        if (cached := cache.get_snapshot(key)) is not None:
            snapshots[key] = cached
        else:
            to_crawl[key] = place
    log.info("💾 %s websites from cache, crawling %s", len(snapshots), len(to_crawl))
    if not to_crawl:
        return snapshots

    done = 0
    # A self-overwriting progress line: only on a real terminal, and never mixed into debug logs.
    show_progress = not args.debug and sys.stderr.isatty()

    def on_done(place: Restaurant, snapshot: Snapshot) -> None:
        nonlocal done
        done += 1
        key = site_key(place.websites)
        snapshots[key] = snapshot
        cache.put_snapshot(key, snapshot)  # failed crawls expire sooner, see config.CACHE_ERROR_MAX_AGE
        if args.debug:
            log.debug("Done %s/%s: %s", done, len(to_crawl), place.name)
        elif show_progress:
            print(f"\r🔎 crawling {done}/{len(to_crawl)}", end="", file=sys.stderr, flush=True)

    asyncio.run(
        crawl_all(list(to_crawl.values()), max_pages=args.max_pages, concurrency=args.concurrency, on_done=on_done)
    )
    if show_progress:
        print("\r\033[K", end="", file=sys.stderr, flush=True)  # erase the progress line
    return snapshots


def run(args: argparse.Namespace, cache: Cache) -> None:
    if args.near:
        lat, lon = cached_geocode(cache, args.near)
        log.info("📍 %s → %.5f, %.5f", args.near, lat, lon)
    else:
        lat, lon = args.coords

    restaurants = cached_restaurants(cache, lat, lon, args.radius)
    with_website = [r for r in restaurants if r.websites]
    log.info("🍽  %s places in %s m, %s with a website", len(restaurants), args.radius, len(with_website))
    if args.limit is not None and args.limit < len(with_website):
        with_website = nearest(with_website, lat, lon, args.limit)
        farthest = round(distance_m(lat, lon, with_website[-1].lat, with_website[-1].lon))
        log.info("📏 limited to the %s nearest (up to %s m away)", args.limit, farthest)
    if not with_website:
        return

    snapshots = collect_snapshots(cache, with_website, args)
    results = []
    for place in with_website:
        snapshot = snapshots[site_key(place.websites)]
        hits = search(snapshot.documents, args.terms, args.whole_word)
        results.append(Result(place, snapshot, hits, distance_m(lat, lon, place.lat, place.lon)))

    print()
    print(render_report(results, args.terms, args.verbose, Style.for_stream(sys.stdout)))


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(args.debug)
    path = ":memory:" if args.no_cache else config.CACHE_PATH
    # --refresh: only entries written during this run count as fresh.
    max_age, error_max_age = (0, 0) if args.refresh else (config.CACHE_MAX_AGE, config.CACHE_ERROR_MAX_AGE)
    with Cache(path, max_age=max_age, error_max_age=error_max_age) as cache:
        run(args, cache)


if __name__ == "__main__":
    main()
