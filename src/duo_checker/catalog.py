"""iPhone Duo の製品カタログと Apple Store（日本）の店舗一覧。

2026-10-16 時点の購入ページ（window.PRODUCT_SELECTION_BOOTSTRAP）と在庫 API の応答から抽出した値。
製品番号はキャリアに依存しない。キャリアは SIMフリー固定で、在庫 API の `cppart` に渡す。
"""

from dataclasses import dataclass
from urllib.parse import quote

MODEL_NAME = "iPhone Duo"
BUY_PAGE_URL = "https://www.apple.com/jp/shop/buy-iphone/iphone-duo"
CARRIER_PART = "UNLOCKED_JP"
CARRIER_NAME = "SIMフリー"

COLORS = {
    "starwhite": "スターホワイト",
    "nightsky": "ナイトスカイ",
}

CAPACITIES = {
    "256gb": "256GB",
    "512gb": "512GB",
    "1tb": "1TB",
    "2tb": "2TB",
}

PART_NUMBERS = {
    ("starwhite", "256gb"): "MK244J/A",
    ("nightsky", "256gb"): "MK254J/A",
    ("starwhite", "512gb"): "MK264J/A",
    ("nightsky", "512gb"): "MK274J/A",
    ("starwhite", "1tb"): "MK284J/A",
    ("nightsky", "1tb"): "MK294J/A",
    ("starwhite", "2tb"): "MK2A4J/A",
    ("nightsky", "2tb"): "MK2C4J/A",
}

# 店舗番号 → 店舗名。在庫 API は searchNearby=true で近隣店舗もまとめて返すため、
# 地域ごとに並べておくと 1 回の照会でまとめて取得できる。
STORES = {
    "R718": "丸の内",
    "R079": "銀座",
    "R224": "表参道",
    "R128": "新宿",
    "R119": "渋谷",
    "R710": "川崎",
    "R005": "名古屋栄",
    "R711": "京都",
    "R768": "梅田",
    "R091": "心斎橋",
    "R048": "福岡",
}


@dataclass(frozen=True)
class Variant:
    color: str
    capacity: str

    def __post_init__(self) -> None:
        if self.color not in COLORS:
            raise ValueError(f"未知のカラー: {self.color!r}（{' / '.join(COLORS)}）")
        if self.capacity not in CAPACITIES:
            raise ValueError(f"未知の容量: {self.capacity!r}（{' / '.join(CAPACITIES)}）")

    @property
    def part_number(self) -> str:
        return PART_NUMBERS[(self.color, self.capacity)]

    @property
    def color_name(self) -> str:
        return COLORS[self.color]

    @property
    def capacity_name(self) -> str:
        return CAPACITIES[self.capacity]

    @property
    def display_name(self) -> str:
        return f"{MODEL_NAME} {self.capacity_name} {self.color_name}（{CARRIER_NAME}）"

    @property
    def product_url(self) -> str:
        token = f"7.6インチディスプレイ-{self.capacity}-{self.color_name}-simフリー"
        return f"{BUY_PAGE_URL}/{quote(token)}"
