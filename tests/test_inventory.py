import json
from pathlib import Path

import pytest

from duo_checker.catalog import PART_NUMBERS, Variant
from duo_checker.inventory import InventoryError, build_query, parse_pickup_response

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


@pytest.mark.parametrize("payload", [{}, {"body": {}}, {"head": {"status": "541"}}, []])
def test_parse_broken_response(payload) -> None:
    with pytest.raises(InventoryError):
        parse_pickup_response(payload, ["MK244J/A"])


def test_build_query() -> None:
    q = build_query(["MK244J/A", "MK254J/A"], "R005")
    assert q.startswith("/jp/shop/fulfillment-messages?")
    assert "parts.0=MK244J%2FA" in q and "parts.1=MK254J%2FA" in q
    assert "cppart=UNLOCKED_JP" in q and "store=R005" in q and "searchNearby=true" in q


def test_catalog_variants() -> None:
    v = Variant("starwhite", "256gb")
    assert v.part_number == "MK244J/A"
    assert v.display_name == "iPhone Duo 256GB スターホワイト（SIMフリー）"
    assert v.product_url.startswith("https://www.apple.com/jp/shop/buy-iphone/iphone-duo/7.6")
    assert len(PART_NUMBERS) == 8
    with pytest.raises(ValueError):
        Variant("gold", "256gb")
