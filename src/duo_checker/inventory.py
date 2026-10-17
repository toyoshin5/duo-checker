"""Apple Store の在庫 API と、応答を共通形式へ変換する取得層。"""

from __future__ import annotations

import gzip
import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from .catalog import BUY_PAGE_URL, CARRIER_PART, COLORS, PART_NUMBERS

BASE_URL = "https://www.apple.com"
FULFILLMENT_API_PATH = "/jp/shop/fulfillment-messages"
PICKUP_API_PATH = "/jp/shop/retail/pickup-message"
# 旧コードからの公開名を残す。
API_PATH = FULFILLMENT_API_PATH
# 購入ページの fulfillmentBootstrap.maxParamsPerURL に合わせる。両 API とも
# 複数 SKU を受け付けるが、仕様変更時に一度の失敗を小さくするため上限を共有する。
MAX_PARTS_PER_REQUEST = 3

SAFARI_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15"
)


class InventoryError(Exception):
    """在庫を確定できない応答または通信エラー。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(InventoryError):
    """Apple Shield のレート制限と考えられる HTTP 541。"""

    def __init__(self, message: str = "在庫 API の応答が異常です (status=541)") -> None:
        super().__init__(message, status_code=541)


class TargetStoreMissingError(InventoryError):
    """200 応答だが要求した店舗または SKU が含まれていない。"""


class TransportError(InventoryError):
    """HTTPクライアント自体の失敗。"""


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: str
    elapsed_seconds: float
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass
class RequestMetrics:
    """診断とログに使う、1リクエスト分の結果。"""

    api_mode: str
    transport: str
    status_code: int | None = None
    elapsed_seconds: float | None = None
    store_count: int = 0
    sku_status: dict[str, str] = field(default_factory=dict)
    error: str = ""
    success: bool = False


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
    """既存の fulfillment-messages 用クエリを組み立てる。"""

    params: list[tuple[str, str]] = [
        ("fae", "true"),
        ("pl", "true"),
        ("mts.0", "regular"),
        ("cppart", CARRIER_PART),
    ]
    params += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    params += [("searchNearby", "true"), ("store", store)]
    return f"{FULFILLMENT_API_PATH}?{urlencode(params)}"


def build_pickup_query(parts: Iterable[str], store: str) -> str:
    """新しい /retail/pickup-message 用クエリを組み立てる。

    `store` と `searchNearby=true` の組み合わせは、指定店舗を含む近隣地域を返す。
    実測では丸の内・名古屋栄・福岡の3回で日本の11店舗を網羅できた。
    """

    params: list[tuple[str, str]] = [
        ("pl", "true"),
        ("purchaseOption", "fullPrice"),
        ("mts.0", "regular"),
    ]
    params += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    params += [("searchNearby", "true"), ("store", store)]
    return f"{PICKUP_API_PATH}?{urlencode(params)}"


def _product_referer(part: str) -> str:
    """SKUに対応する購入ページを Referer にする。未知のSKUは製品トップに戻す。"""

    for (color, capacity), part_number in PART_NUMBERS.items():
        if part_number == part:
            token = f"7.6インチディスプレイ-{capacity}-{COLORS[color]}-simフリー"
            return f"{BUY_PAGE_URL}/{quote(token)}"
    return BUY_PAGE_URL


def browser_headers(part: str, user_agent: str = SAFARI_USER_AGENT) -> dict[str, str]:
    """Apple Store の同一オリジン fetch に近い共通ヘッダー。"""

    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": _product_referer(part),
        "Origin": "https://www.apple.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "X-Requested-With": "XMLHttpRequest",
        "x-skip-redirect": "true",
        "User-Agent": user_agent,
    }


class InventoryTransport(Protocol):
    name: str

    def get(self, path: str, *, referer_part: str) -> HttpResponse: ...


class StandardHttpTransport:
    """標準ライブラリだけで行う HTTP 通信。主に pickup-message 用。"""

    name = "http"

    def __init__(self, timeout_seconds: float = 25) -> None:
        self.timeout_seconds = timeout_seconds

    def get(self, path: str, *, referer_part: str) -> HttpResponse:
        request = Request(
            urljoin(BASE_URL, path),
            headers={"Accept-Encoding": "identity", **browser_headers(referer_part)},
            method="GET",
        )
        started = time.monotonic()
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                headers = dict(response.headers.items())
                status = int(response.status)
        except HTTPError as error:
            raw = error.read()
            headers = dict(error.headers.items()) if error.headers else {}
            status = int(error.code)
        except (URLError, TimeoutError, OSError) as error:
            raise TransportError(f"通常HTTPの通信に失敗しました: {error}") from None
        if headers.get("Content-Encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError as error:
                raise TransportError(f"HTTP応答のgzip展開に失敗しました: {error}") from None
        return HttpResponse(
            status_code=status,
            body=raw.decode("utf-8", errors="replace"),
            elapsed_seconds=time.monotonic() - started,
            headers=headers,
        )


class CurlCffiTransport:
    """curl-impersonate の Chrome TLS/HTTP2 フィンガープリントを使う通信。"""

    name = "curl_cffi"

    def __init__(self, timeout_seconds: float = 25, impersonate: str = "chrome") -> None:
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError:
            raise TransportError("curl_cffi がインストールされていません。`uv sync` を実行してください") from None
        self._requests = cffi_requests
        self.timeout_seconds = timeout_seconds
        self.impersonate = impersonate
        self.session = cffi_requests.Session()

    def _headers(self, part: str) -> dict[str, str]:
        headers = browser_headers(part)
        # 固定ターゲットを指定した場合は、TLSプロファイルとUAを一致させる。
        # `chrome`（バージョンなし）は curl_cffi が最新対応プロファイルのUAを設定する。
        match = re.fullmatch(r"chrome(\d+)", self.impersonate)
        if match:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{match.group(1)}.0.0.0 Safari/537.36"
            )
        else:
            headers.pop("User-Agent", None)
        return headers

    def get(self, path: str, *, referer_part: str) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self.session.get(
                urljoin(BASE_URL, path),
                headers=self._headers(referer_part),
                impersonate=self.impersonate,
                timeout=self.timeout_seconds,
            )
        except Exception as error:  # curl_cffi has several exception classes per backend
            raise TransportError(f"curl_cffiの通信に失敗しました: {error}") from None
        return HttpResponse(
            status_code=int(response.status_code),
            body=response.text,
            elapsed_seconds=time.monotonic() - started,
            headers=dict(response.headers.items()),
        )


def _stores(payload: object, api_mode: str, *, allow_missing: bool = False) -> list[dict]:
    if not isinstance(payload, dict):
        raise InventoryError("在庫 API の JSON がオブジェクトではありません")
    _check_payload_status(payload)
    try:
        if api_mode == "fulfillment":
            pickup_message = payload["body"]["content"]["pickupMessage"]
            if allow_missing and isinstance(pickup_message, dict) and "stores" not in pickup_message:
                return []
            stores = pickup_message["stores"]
        elif api_mode == "pickup":
            body = payload["body"]
            if allow_missing and isinstance(body, dict) and "stores" not in body:
                return []
            stores = body["stores"]
        else:
            raise InventoryError(f"未知の API モードです: {api_mode}")
    except (KeyError, TypeError):
        raise InventoryError(f"{api_mode} API の応答に店舗一覧がありません") from None
    if not isinstance(stores, list):
        raise InventoryError(f"{api_mode} API の店舗一覧が配列ではありません")
    return [store for store in stores if isinstance(store, dict)]


def _check_payload_status(payload: dict) -> None:
    head = payload.get("head")
    if not isinstance(head, dict) or "status" not in head:
        return
    status = str(head["status"])
    if status == "541":
        raise RateLimitError()
    if status != "200":
        raise InventoryError(
            f"在庫 API の JSON status が異常です: {status}",
            status_code=int(status) if status.isdigit() else None,
        )


def _parse_stores(payload: object, parts: Iterable[str], api_mode: str) -> list[StoreAvailability]:
    requested_parts = list(dict.fromkeys(parts))
    results: list[StoreAvailability] = []
    for store in _stores(payload, api_mode, allow_missing=True):
        store_number = store.get("storeNumber")
        if not isinstance(store_number, str) or not store_number:
            continue
        availability = store.get("partsAvailability")
        if not isinstance(availability, dict):
            continue
        for part in requested_parts:
            info = availability.get(part)
            if not isinstance(info, dict):
                continue
            display = info.get("pickupDisplay")
            if not isinstance(display, str) or not display:
                # pickupDisplay がない値を「unavailable」と解釈しない。
                continue
            results.append(
                StoreAvailability(
                    store_number=store_number,
                    store_name=store.get("storeName", store_number),
                    part_number=part,
                    available=display.lower() == "available",
                    pickup_display=display,
                    pickup_quote=info.get("pickupSearchQuote", ""),
                    store_url=(store.get("reservationUrl") or "").replace("http://", "https://"),
                )
            )
    return results


def parse_fulfillment_response(payload: object, parts: Iterable[str]) -> list[StoreAvailability]:
    """fulfillment-messages の body.content.pickupMessage を解析する。"""

    return _parse_stores(payload, parts, "fulfillment")


def parse_retail_pickup_response(payload: object, parts: Iterable[str]) -> list[StoreAvailability]:
    """retail/pickup-message の body.stores を解析する。"""

    return _parse_stores(payload, parts, "pickup")


def parse_pickup_response(payload: object, parts: Iterable[str]) -> list[StoreAvailability]:
    """後方互換用。従来の fulfillment 応答パーサー名を維持する。"""

    return parse_fulfillment_response(payload, parts)


def _decode_response(response: HttpResponse, api_mode: str) -> object:
    if response.status_code == 541:
        raise RateLimitError(
            f"{api_mode} API の応答が異常です (status=541, body={response.body[:120]!r})"
        )
    if response.status_code != 200:
        raise InventoryError(
            f"{api_mode} API の HTTP 応答が異常です (status={response.status_code}, body={response.body[:120]!r})",
            status_code=response.status_code,
        )
    if not response.body.lstrip().startswith("{"):
        raise InventoryError(f"{api_mode} API の応答が JSON ではありません (body={response.body[:120]!r})")
    try:
        payload = json.loads(response.body)
    except json.JSONDecodeError as error:
        raise InventoryError(f"{api_mode} API の応答が不正な JSON です: {error}") from None
    if not isinstance(payload, dict):
        raise InventoryError(f"{api_mode} API の JSON がオブジェクトではありません")
    _check_payload_status(payload)
    return payload


def _validate_target(observations: list[StoreAvailability], parts: list[str], store: str) -> None:
    found = {item.part_number for item in observations if item.store_number == store}
    missing = [part for part in parts if part not in found]
    if missing:
        if not found:
            raise TargetStoreMissingError(f"応答に対象店舗 {store} が含まれていません")
        raise TargetStoreMissingError(f"対象店舗 {store} に SKU {', '.join(missing)} が含まれていません")


class ApiInventorySource:
    """1つの API と通信方式を組み合わせた在庫ソース。"""

    def __init__(self, api_mode: str, transport: InventoryTransport) -> None:
        if api_mode not in ("pickup", "fulfillment"):
            raise ValueError(f"未知の API モードです: {api_mode}")
        self.api_mode = api_mode
        self.transport = transport
        self.last_metrics: RequestMetrics | None = None

    @property
    def name(self) -> str:
        return f"{self.api_mode}/{self.transport.name}"

    def fetch(self, parts: list[str], store: str) -> list[StoreAvailability]:
        requested_parts = list(dict.fromkeys(parts))
        if not requested_parts:
            raise InventoryError("在庫取得する SKU がありません")
        path = (
            build_pickup_query(requested_parts, store)
            if self.api_mode == "pickup"
            else build_query(requested_parts, store)
        )
        metrics = RequestMetrics(
            api_mode=self.api_mode,
            transport=self.transport.name,
            sku_status={part: "not_checked" for part in requested_parts},
        )
        self.last_metrics = metrics
        try:
            response = self.transport.get(path, referer_part=requested_parts[0])
            metrics.status_code = response.status_code
            metrics.elapsed_seconds = response.elapsed_seconds
            payload = _decode_response(response, self.api_mode)
            stores = _stores(payload, self.api_mode)
            metrics.store_count = len({s.get("storeNumber") for s in stores if s.get("storeNumber")})
            parser = parse_retail_pickup_response if self.api_mode == "pickup" else parse_fulfillment_response
            observations = parser(payload, requested_parts)
            for part in requested_parts:
                metrics.sku_status[part] = (
                    "ok"
                    if any(item.store_number == store and item.part_number == part for item in observations)
                    else "missing"
                )
            _validate_target(observations, requested_parts, store)
        except InventoryError as error:
            metrics.error = str(error)
            raise
        metrics.success = True
        return observations
