"""在庫 API（/jp/shop/fulfillment-messages）の URL 組み立てと応答の解析。"""

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlencode

from .catalog import CARRIER_PART

API_PATH = "/jp/shop/fulfillment-messages"
MAX_PARTS_PER_REQUEST = 3  # 購入ページの fulfillmentBootstrap.maxParamsPerURL


class InventoryError(Exception):
    """応答が想定した形式でない（ブロック・セッション切れ・仕様変更など）。"""


@dataclass(frozen=True)
class StoreAvailability:
    store_number: str
    store_name: str
    part_number: str
    available: bool
    pickup_display: str  # "available" / "unavailable" / "ineligible" など
    pickup_quote: str  # 例: "本日"、"現在ご利用いただけません"
    store_url: str

    @property
    def key(self) -> str:
        return f"{self.store_number}:{self.part_number}"


def build_query(parts: Iterable[str], store: str) -> str:
    params: list[tuple[str, str]] = [("fae", "true"), ("pl", "true"), ("mts.0", "regular"), ("cppart", CARRIER_PART)]
    params += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    params += [("searchNearby", "true"), ("store", store)]
    return f"{API_PATH}?{urlencode(params)}"


def parse_pickup_response(payload: dict, parts: Iterable[str]) -> list[StoreAvailability]:
    try:
        pickup = payload["body"]["content"]["pickupMessage"]
    except (KeyError, TypeError):
        raise InventoryError("応答に pickupMessage がありません") from None

    results: list[StoreAvailability] = []
    for store in pickup.get("stores", []):
        for part in parts:
            info = store.get("partsAvailability", {}).get(part)
            if info is None:
                continue
            display = info.get("pickupDisplay", "")
            results.append(
                StoreAvailability(
                    store_number=store["storeNumber"],
                    store_name=store.get("storeName", store["storeNumber"]),
                    part_number=part,
                    available=display == "available",
                    pickup_display=display,
                    pickup_quote=info.get("pickupSearchQuote", ""),
                    store_url=(store.get("reservationUrl") or "").replace("http://", "https://"),
                )
            )
    return results
