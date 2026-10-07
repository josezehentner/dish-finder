"""Find restaurants and their websites via OpenStreetMap (Nominatim + Overpass)."""

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from dish_finder import config

log = logging.getLogger(__name__)

EARTH_RADIUS_M = 6_371_000


@dataclass
class Restaurant:
    name: str
    amenity: str
    lat: float
    lon: float
    websites: list[str]
    osm_url: str


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters (haversine formula)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def nearest(restaurants: list[Restaurant], lat: float, lon: float, limit: int | None = None) -> list[Restaurant]:
    """Sort by distance from (lat, lon), nearest first, and keep at most `limit`."""
    by_distance = sorted(restaurants, key=lambda r: distance_m(lat, lon, r.lat, r.lon))
    return by_distance if limit is None else by_distance[:limit]


def geocode(place: str) -> tuple[float, float]:
    """Turn a place name like 'Wien Neubau' into (lat, lon)."""
    resp = httpx.get(
        config.NOMINATIM_URL,
        params={"q": place, "format": "json", "limit": 1},
        headers={"User-Agent": config.USER_AGENT},
        timeout=config.NOMINATIM_TIMEOUT,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Place not found: {place!r}")
    log.debug("Geocoded %r as %s", place, results[0].get("display_name"))
    return float(results[0]["lat"]), float(results[0]["lon"])


def find_restaurants(lat: float, lon: float, radius_m: int) -> list[Restaurant]:
    amenity_regex = "|".join(config.AMENITIES)
    query = f"""
        [out:json][timeout:{config.OVERPASS_QUERY_TIMEOUT}];
        nwr["amenity"~"^({amenity_regex})$"](around:{radius_m},{lat},{lon});
        out center tags;
    """
    restaurants = []
    for element in _overpass(query):
        tags = element.get("tags", {})
        # Ways/relations carry their coordinates in "center", nodes directly.
        point = element.get("center", element)
        restaurants.append(
            Restaurant(
                name=tags.get("name", "<unnamed>"),
                amenity=tags["amenity"],
                lat=point["lat"],
                lon=point["lon"],
                websites=_websites(tags),
                osm_url=f"https://www.openstreetmap.org/{element['type']}/{element['id']}",
            )
        )
    return restaurants


def _overpass(query: str) -> list[dict[str, Any]]:
    # Busy servers answer 504/429/500 one moment and 200 the next, so retry
    # all mirrors a few times with growing pauses before giving up.
    errors = []
    for attempt in range(config.OVERPASS_ATTEMPTS):
        if attempt:
            wait = config.OVERPASS_RETRY_DELAY * attempt
            log.warning("⏳ Overpass servers busy, retrying in %s s…", wait)
            time.sleep(wait)
        for url in config.OVERPASS_URLS:
            try:
                resp = httpx.post(
                    url,
                    data={"data": query},
                    headers={"User-Agent": config.USER_AGENT},
                    timeout=config.OVERPASS_HTTP_TIMEOUT,
                )
                resp.raise_for_status()
                elements: list[dict[str, Any]] = resp.json()["elements"]
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("Overpass server %s failed: %s", url, exc)
                errors.append(f"{url}: {exc}")
            else:
                return elements
    raise RuntimeError("All Overpass servers failed:\n" + "\n".join(errors))


def _websites(tags: dict[str, str]) -> list[str]:
    urls = []
    for tag in config.WEBSITE_TAGS:
        # Tags may hold several values separated by ";".
        for raw in tags.get(tag, "").split(";"):
            url = raw.strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            if url not in urls:
                urls.append(url)
    return urls
