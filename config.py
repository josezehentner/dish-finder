"""All tunable settings in one place."""

from pathlib import Path

# --- HTTP ------------------------------------------------------------------
# OSM services require an identifying User-Agent; websites see it too.
USER_AGENT = "dish-finder/0.1 (personal hobby project)"
HTTP_TIMEOUT = 15  # seconds per website request

# --- OpenStreetMap -----------------------------------------------------------
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_TIMEOUT = 30
# Public Overpass servers are often overloaded, so try mirrors in order.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
OVERPASS_ATTEMPTS = 3  # rounds over all mirrors
OVERPASS_RETRY_DELAY = 10  # seconds, multiplied by the round number
OVERPASS_HTTP_TIMEOUT = 60
# A small server-side limit makes busy servers more likely to accept the query.
OVERPASS_QUERY_TIMEOUT = 25

AMENITIES = ("restaurant", "cafe", "bar", "pub", "biergarten", "fast_food")
# Menu-specific tags first, so the crawler starts with the most promising URL.
WEBSITE_TAGS = ("website:menu", "menu:url", "website", "contact:website", "url")

# --- Crawler -----------------------------------------------------------------
# Links whose URL or text contains one of these are followed.
MENU_KEYWORDS = (
    "karte", "menu", "menü", "speise", "getränk", "essen", "food", "drink",
    "wein", "wine", "cocktail", "bier", "beer", "lunch", "mittag", "carta", "pdf",
)  # fmt: skip
MAX_DEPTH = 2  # homepage -> menu page -> PDF linked from the menu page
MAX_BYTES = 15 * 1024 * 1024
RESTAURANT_TIMEOUT = 90  # seconds per restaurant, partial results are kept

# --- Cache -------------------------------------------------------------------
# Crawled website text, place lookups and OSM results are stored here, so a
# search for another dish in the same area needs no network at all.
CACHE_PATH = Path.home() / ".cache" / "dish-finder" / "cache.sqlite3"
CACHE_MAX_AGE = 14 * 24 * 3600  # seconds; older entries are fetched again
# Failed crawls (dead domain, timeout, …) are retried sooner, but not on every run.
CACHE_ERROR_MAX_AGE = 24 * 3600

# --- Matching ----------------------------------------------------------------
SNIPPET_RADIUS = 60  # characters shown around a hit

# --- CLI defaults ------------------------------------------------------------
DEFAULT_RADIUS = 1000
DEFAULT_MAX_PAGES = 15
DEFAULT_CONCURRENCY = 8
