import logging
from typing import Any

import httpx
import pytest

import config
import osm
from config import OVERPASS_URLS, USER_AGENT
from osm import Restaurant, _websites, find_restaurants, geocode


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
    monkeypatch.setattr("osm.time.sleep", sleeps.append)
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

        with caplog.at_level(logging.DEBUG, logger="osm"):
            osm._overpass("query")

        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == ["⏳ Overpass servers busy, retrying in 10 s…"]
        assert f"Overpass server {OVERPASS_URLS[0]} failed: down" in caplog.messages

    def test_geocode_logs_matched_place(self, monkeypatch, caplog):
        result = [{"lat": "48.2", "lon": "16.3", "display_name": "Westbahnhof, Wien"}]
        monkeypatch.setattr("httpx.get", FakeHttp(json_response("GET", config.NOMINATIM_URL, result)))

        with caplog.at_level(logging.DEBUG, logger="osm"):
            geocode("Westbahnhof")

        assert "Geocoded 'Westbahnhof' as Westbahnhof, Wien" in caplog.messages
