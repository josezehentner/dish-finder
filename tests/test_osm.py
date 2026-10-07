import logging
import math
from typing import Any

import httpx
import pytest

from dish_finder import config, osm
from dish_finder.config import OVERPASS_URLS, USER_AGENT
from dish_finder.osm import Restaurant, _websites, find_restaurants, geocode


def json_response(method: str, url: str, data: object, status: int = 200) -> httpx.Response:
    # raise_for_status() needs the request attached to the response.
    return httpx.Response(status, json=data, request=httpx.Request(method, url))


class FakeHttp:
    """Stands in for httpx.get/httpx.post: replays prepared responses and records calls."""

    def __init__(self, *responses: httpx.Response | Exception):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def no_sleep(monkeypatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr("dish_finder.osm.time.sleep", sleeps.append)
    return sleeps


class TestWebsites:
    def test_menu_tags_come_first(self):
        tags = {"website": "https://a.at", "website:menu": "https://a.at/karte"}
        assert _websites(tags) == ["https://a.at/karte", "https://a.at"]

    def test_adds_missing_scheme(self):
        assert _websites({"website": "www.a.at"}) == ["https://www.a.at"]

    def test_keeps_http_scheme(self):
        assert _websites({"website": "http://a.at"}) == ["http://a.at"]

    def test_splits_multiple_values(self):
        assert _websites({"website": "https://a.at; https://b.at"}) == ["https://a.at", "https://b.at"]

    def test_removes_duplicates(self):
        assert _websites({"website": "https://a.at", "contact:website": "https://a.at"}) == ["https://a.at"]

    def test_no_website_tags(self):
        assert _websites({"name": "Bar", "website": " ; "}) == []


class TestGeocode:
    def test_returns_coordinates_of_first_result(self, monkeypatch):
        fake = FakeHttp(
            json_response("GET", config.NOMINATIM_URL, [{"lat": "48.2", "lon": "16.3"}, {"lat": "0", "lon": "0"}])
        )
        monkeypatch.setattr("httpx.get", fake)

        assert geocode("Wien Westbahnhof") == (48.2, 16.3)
        url, kwargs = fake.calls[0]
        assert url == config.NOMINATIM_URL
        assert kwargs["params"]["q"] == "Wien Westbahnhof"
        assert kwargs["headers"]["User-Agent"] == USER_AGENT

    def test_unknown_place_raises(self, monkeypatch):
        monkeypatch.setattr("httpx.get", FakeHttp(json_response("GET", config.NOMINATIM_URL, [])))

        with pytest.raises(ValueError, match="Place not found"):
            geocode("Nirgendwo")

    def test_http_error_propagates(self, monkeypatch):
        monkeypatch.setattr("httpx.get", FakeHttp(json_response("GET", config.NOMINATIM_URL, {}, status=503)))

        with pytest.raises(httpx.HTTPStatusError):
            geocode("Wien")


class TestFindRestaurants:
    def test_parses_nodes_and_ways(self, monkeypatch, no_sleep):
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 48.1,
                "lon": 16.1,
                "tags": {"amenity": "restaurant", "name": "Wirt", "website": "wirt.at"},
            },
            {
                "type": "way",
                "id": 2,
                "center": {"lat": 48.2, "lon": 16.2},
                "tags": {"amenity": "bar"},
            },
        ]
        fake = FakeHttp(json_response("POST", OVERPASS_URLS[0], {"elements": elements}))
        monkeypatch.setattr("httpx.post", fake)

        restaurants = find_restaurants(48.0, 16.0, 500)

        assert restaurants == [
            Restaurant("Wirt", "restaurant", 48.1, 16.1, ["https://wirt.at"], "https://www.openstreetmap.org/node/1"),
            Restaurant("<unnamed>", "bar", 48.2, 16.2, [], "https://www.openstreetmap.org/way/2"),
        ]
        query = fake.calls[0][1]["data"]["data"]
        assert "around:500,48.0,16.0" in query
        assert all(amenity in query for amenity in config.AMENITIES)


class TestOverpassFallback:
    def test_falls_back_to_next_mirror(self, monkeypatch, no_sleep):
        fake = FakeHttp(
            json_response("POST", OVERPASS_URLS[0], {}, status=504),
            json_response("POST", OVERPASS_URLS[1], {"elements": [{"id": 7}]}),
        )
        monkeypatch.setattr("httpx.post", fake)

        assert osm._overpass("query") == [{"id": 7}]
        assert [url for url, _ in fake.calls] == list(OVERPASS_URLS[:2])
        assert no_sleep == []

    def test_retries_all_mirrors_with_growing_pauses(self, monkeypatch, no_sleep):
        mirrors = len(OVERPASS_URLS)
        failures = [httpx.ConnectError("down")] * (mirrors * (config.OVERPASS_ATTEMPTS - 1))
        success = json_response("POST", OVERPASS_URLS[0], {"elements": []})
        monkeypatch.setattr("httpx.post", FakeHttp(*failures, success))

        assert osm._overpass("query") == []
        assert no_sleep == [10, 20]

    def test_gives_up_after_all_attempts(self, monkeypatch, no_sleep):
        total = len(OVERPASS_URLS) * config.OVERPASS_ATTEMPTS
        monkeypatch.setattr("httpx.post", FakeHttp(*[httpx.ConnectError("down")] * total))

        with pytest.raises(RuntimeError, match="All Overpass servers failed"):
            osm._overpass("query")

    def test_invalid_json_counts_as_failure(self, monkeypatch, no_sleep):
        broken = httpx.Response(200, text="<html>busy</html>", request=httpx.Request("POST", OVERPASS_URLS[0]))
        good = json_response("POST", OVERPASS_URLS[1], {"elements": [{"id": 1}]})
        monkeypatch.setattr("httpx.post", FakeHttp(broken, good))

        assert osm._overpass("query") == [{"id": 1}]


class TestOsmLogging:
    def test_retry_is_logged_as_warning(self, monkeypatch, no_sleep, caplog):
        failures = [httpx.ConnectError("down")] * len(OVERPASS_URLS)
        success = json_response("POST", OVERPASS_URLS[0], {"elements": []})
        monkeypatch.setattr("httpx.post", FakeHttp(*failures, success))

        with caplog.at_level(logging.DEBUG, logger="dish_finder.osm"):
            osm._overpass("query")

        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == ["⏳  Overpass servers busy, retrying in 10 s…"]
        assert f"Overpass server {OVERPASS_URLS[0]} failed: down" in caplog.messages

    def test_geocode_logs_matched_place(self, monkeypatch, caplog):
        result = [{"lat": "48.2", "lon": "16.3", "display_name": "Westbahnhof, Wien"}]
        monkeypatch.setattr("httpx.get", FakeHttp(json_response("GET", config.NOMINATIM_URL, result)))

        with caplog.at_level(logging.DEBUG, logger="dish_finder.osm"):
            geocode("Westbahnhof")

        assert "Geocoded 'Westbahnhof' as Westbahnhof, Wien" in caplog.messages


METERS_PER_DEGREE = 2 * math.pi * osm.EARTH_RADIUS_M / 360  # ≈ 111 195 m along a meridian


def place_at(name: str, lat: float, lon: float = 16.35) -> Restaurant:
    return Restaurant(name, "restaurant", lat, lon, [f"https://{name.lower()}.at"], "")


class TestDistance:
    def test_same_point_is_zero(self):
        assert osm.distance_m(48.2, 16.35, 48.2, 16.35) == 0

    def test_one_degree_of_latitude(self):
        assert osm.distance_m(48.0, 16.0, 49.0, 16.0) == pytest.approx(METERS_PER_DEGREE)

    def test_longitude_degrees_shrink_towards_the_poles(self):
        at_equator = osm.distance_m(0, 0, 0, 1)
        at_60_north = osm.distance_m(60, 0, 60, 1)
        assert at_equator == pytest.approx(METERS_PER_DEGREE)
        assert at_60_north == pytest.approx(METERS_PER_DEGREE / 2, rel=1e-3)  # cos(60°) = 0.5

    def test_symmetric(self):
        assert osm.distance_m(48.2, 16.37, 48.19, 16.33) == osm.distance_m(48.19, 16.33, 48.2, 16.37)

    def test_vienna_stephansplatz_to_westbahnhof(self):
        # Roughly 2.9 km as the crow flies.
        assert osm.distance_m(48.20849, 16.37208, 48.19661, 16.33797) == pytest.approx(2900, abs=100)


PLACES = (place_at("Far", 48.21), place_at("Near", 48.201), place_at("Middle", 48.205))


class TestNearest:
    def test_sorts_nearest_first(self):
        assert [r.name for r in osm.nearest(list(PLACES), 48.2, 16.35)] == ["Near", "Middle", "Far"]

    def test_limit_keeps_the_nearest(self):
        assert [r.name for r in osm.nearest(list(PLACES), 48.2, 16.35, limit=2)] == ["Near", "Middle"]

    def test_limit_larger_than_list(self):
        assert len(osm.nearest(list(PLACES), 48.2, 16.35, limit=10)) == 3

    def test_distance_counts_in_every_direction(self):
        south = place_at("South", 48.1995)  # ~56 m south
        east = place_at("East", 48.2, lon=16.352)  # ~148 m east
        assert [r.name for r in osm.nearest([east, *PLACES, south], 48.2, 16.35, limit=2)] == ["South", "Near"]

    def test_does_not_modify_the_input(self):
        places = list(PLACES)
        osm.nearest(places, 48.2, 16.35, limit=1)
        assert places == list(PLACES)

    def test_empty(self):
        assert osm.nearest([], 48.2, 16.35, limit=3) == []


class TestGoogleMapsUrl:
    def test_pin_at_coordinates(self):
        assert osm.google_maps_url(48.2108014, 16.3479407) == (
            "https://www.google.com/maps/search/?api=1&query=48.2108014,16.3479407"
        )

    def test_rounds_to_seven_decimals(self):
        # 7 decimals ≈ 1 cm, the precision OSM stores
        assert (
            osm.google_maps_url(48.123456789, -16.0)
            == "https://www.google.com/maps/search/?api=1&query=48.1234568,-16.0"
        )
