import json
from pathlib import Path

import pytest

from duo_checker.catalog import PART_NUMBERS, Variant
from duo_checker.inventory import (
    ApiInventorySource,
    HttpResponse,
    InventoryError,
    RateLimitError,
    TargetStoreMissingError,
    TransportError,
    build_pickup_query,
    build_query,
    parse_pickup_response,
    parse_retail_pickup_response,
)
from duo_checker.monitor import AutoInventorySource, RateLimitedInventorySource

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_real_response_unavailable() -> None:
    result = parse_pickup_response(load("pickup_R005.json"), ["MK244J/A"])
    assert [r.store_number for r in result] == ["R005", "R711", "R768", "R091"]
    first = result[0]
    assert first.store_name == "名古屋栄"
    assert first.available is False
    assert first.pickup_quote == "現在ご利用いただけません"
    assert first.store_url == "https://www.apple.com/jp/retail/nagoyasakae"


def test_parse_available() -> None:
    payload = load("pickup_R005.json")
    info = payload["body"]["content"]["pickupMessage"]["stores"][0]["partsAvailability"]["MK244J/A"]
    info["pickupDisplay"] = "available"
    info["pickupSearchQuote"] = "本日"
    result = parse_pickup_response(payload, ["MK244J/A"])
    assert result[0].available is True
    assert result[0].pickup_quote == "本日"
    assert all(not r.available for r in result[1:])


def test_parse_no_nearby_stores() -> None:
    assert parse_pickup_response(load("pickup_no_stores.json"), ["MK244J/A"]) == []


def test_parse_retail_pickup_response() -> None:
    result = parse_retail_pickup_response(load("pickup_message_R718.json"), ["MK244J/A", "MK254J/A"])
    assert [(r.store_number, r.part_number) for r in result] == [
        ("R718", "MK244J/A"),
        ("R718", "MK254J/A"),
        ("R079", "MK244J/A"),
        ("R079", "MK254J/A"),
    ]
    assert result[0].store_url == "https://www.apple.com/jp/retail/marunouchi"
    assert result[1].available is True
    assert result[1].pickup_quote == "本日"


@pytest.mark.parametrize("payload", [{}, {"body": {}}, {"head": {"status": "541"}}, []])
def test_parse_broken_response(payload) -> None:
    with pytest.raises(InventoryError):
        parse_pickup_response(payload, ["MK244J/A"])


def test_build_query() -> None:
    q = build_query(["MK244J/A", "MK254J/A"], "R005")
    assert q.startswith("/jp/shop/fulfillment-messages?")
    assert "parts.0=MK244J%2FA" in q and "parts.1=MK254J%2FA" in q
    assert "cppart=UNLOCKED_JP" in q and "store=R005" in q and "searchNearby=true" in q


def test_build_pickup_query() -> None:
    q = build_pickup_query(["MK244J/A", "MK254J/A"], "R718")
    assert q.startswith("/jp/shop/retail/pickup-message?")
    assert "parts.0=MK244J%2FA" in q and "parts.1=MK254J%2FA" in q
    assert "purchaseOption=fullPrice" in q and "store=R718" in q and "searchNearby=true" in q


class FakeTransport:
    name = "fake"

    def __init__(self, payload=None, status=200, body=None):
        self.payload = payload
        self.status = status
        self.body = body
        self.calls = []

    def get(self, path, *, referer_part):
        self.calls.append((path, referer_part))
        if self.body is not None:
            raw = self.body
        else:
            raw = json.dumps(self.payload, ensure_ascii=False)
        return HttpResponse(self.status, raw, 0.25)


def test_api_source_validates_target_store_and_records_metrics() -> None:
    transport = FakeTransport(load("pickup_message_R718.json"))
    source = ApiInventorySource("pickup", transport)
    result = source.fetch(["MK244J/A", "MK254J/A"], "R718")
    assert len(result) == 4
    assert source.last_metrics is not None
    assert source.last_metrics.store_count == 2
    assert source.last_metrics.sku_status == {"MK244J/A": "ok", "MK254J/A": "ok"}
    assert source.last_metrics.success is True


def test_api_source_does_not_accept_wrong_store_as_no_stock() -> None:
    payload = load("pickup_message_R718.json")
    payload["body"]["stores"] = [payload["body"]["stores"][1]]
    source = ApiInventorySource("pickup", FakeTransport(payload))
    with pytest.raises(TargetStoreMissingError, match="R718"):
        source.fetch(["MK244J/A"], "R718")
    assert source.last_metrics is not None
    assert source.last_metrics.success is False
    assert source.last_metrics.sku_status["MK244J/A"] == "missing"


def test_api_source_541_is_not_valid_inventory() -> None:
    source = ApiInventorySource("fulfillment", FakeTransport(status=541, body="<html>blocked</html>"))
    with pytest.raises(RateLimitError, match="541"):
        source.fetch(["MK244J/A"], "R718")
    assert source.last_metrics is not None
    assert source.last_metrics.status_code == 541
    assert source.last_metrics.success is False


def test_api_source_invalid_json_is_not_valid_inventory() -> None:
    source = ApiInventorySource("pickup", FakeTransport(status=200, body="<!doctype html>"))
    with pytest.raises(InventoryError, match="JSON"):
        source.fetch(["MK244J/A"], "R718")


def test_api_source_timeout_is_not_valid_inventory() -> None:
    class TimeoutTransport:
        name = "timeout"

        def get(self, path, *, referer_part):
            raise TransportError("request timed out")

    source = ApiInventorySource("pickup", TimeoutTransport())
    with pytest.raises(TransportError, match="timed out"):
        source.fetch(["MK244J/A"], "R718")
    assert source.last_metrics is not None
    assert source.last_metrics.success is False


class FakeSource:
    def __init__(self, name, result=None, error=None):
        self.name = name
        self.result = result or []
        self.error = error
        self.calls = 0

    def fetch(self, parts, store):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_auto_promotes_successful_fallback() -> None:
    item = parse_retail_pickup_response(load("pickup_message_R718.json"), ["MK244J/A"])[0]
    first = FakeSource("pickup/http", error=InventoryError("bad json"))
    second = FakeSource("fulfillment/safari", result=[item])
    source = AutoInventorySource([first, second])
    assert source.fetch(["MK244J/A"], "R718") == [item]
    assert (first.calls, second.calls) == (1, 1)
    assert source.fetch(["MK244J/A"], "R718") == [item]
    assert (first.calls, second.calls) == (1, 2)


def test_auto_does_not_switch_after_541() -> None:
    first = FakeSource("pickup/http", error=RateLimitError())
    second = FakeSource("fulfillment/safari", result=[])
    source = AutoInventorySource([first, second])
    with pytest.raises(RateLimitError):
        source.fetch(["MK244J/A"], "R718")
    assert first.calls == 1
    assert second.calls == 0


def test_541_cooldown_skips_network_until_expired() -> None:
    now = [0.0]
    underlying = FakeSource("pickup/http", error=RateLimitError())
    source = RateLimitedInventorySource(underlying, cooldown_seconds=30, monotonic=lambda: now[0])
    with pytest.raises(RateLimitError):
        source.fetch(["MK244J/A"], "R718")
    with pytest.raises(RateLimitError, match="冷却中"):
        source.fetch(["MK244J/A"], "R718")
    assert underlying.calls == 1
    now[0] = 30
    with pytest.raises(RateLimitError):
        source.fetch(["MK244J/A"], "R718")
    assert underlying.calls == 2


def test_catalog_variants() -> None:
    v = Variant("starwhite", "256gb")
    assert v.part_number == "MK244J/A"
    assert v.display_name == "iPhone Duo 256GB スターホワイト（SIMフリー）"
    assert v.product_url.startswith("https://www.apple.com/jp/shop/buy-iphone/iphone-duo/7.6")
    assert len(PART_NUMBERS) == 8
    with pytest.raises(ValueError):
        Variant("gold", "256gb")
