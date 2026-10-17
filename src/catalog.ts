// iPhone Duo の製品カタログと Apple Store（日本）の店舗一覧。
// 2026-10 時点の購入ページ（PRODUCT_SELECTION_BOOTSTRAP）と在庫 API の応答から抽出した値。

export const MODEL_NAME = "iPhone Duo";
export const CARRIER_NAME = "SIMフリー";
export const BUY_PAGE_URL = "https://www.apple.com/jp/shop/buy-iphone/iphone-duo";

export const COLORS = {
  starwhite: "スターホワイト",
  nightsky: "ナイトスカイ",
} as const;

export const CAPACITIES = {
  "256gb": "256GB",
  "512gb": "512GB",
  "1tb": "1TB",
  "2tb": "2TB",
} as const;

export type Color = keyof typeof COLORS;
export type Capacity = keyof typeof CAPACITIES;

const PART_NUMBERS: Record<Color, Record<Capacity, string>> = {
  starwhite: { "256gb": "MK244J/A", "512gb": "MK264J/A", "1tb": "MK284J/A", "2tb": "MK2A4J/A" },
  nightsky: { "256gb": "MK254J/A", "512gb": "MK274J/A", "1tb": "MK294J/A", "2tb": "MK2C4J/A" },
};

// 店舗番号 → 店舗名。在庫 API は searchNearby=true で近隣店舗もまとめて返すので、
// 地域ごとに並べておくと丸の内・名古屋栄・福岡の 3 回の照会で全店舗を確認できる。
export const STORES = {
  R718: "丸の内",
  R079: "銀座",
  R224: "表参道",
  R128: "新宿",
  R119: "渋谷",
  R710: "川崎",
  R005: "名古屋栄",
  R711: "京都",
  R768: "梅田",
  R091: "心斎橋",
  R048: "福岡",
} as const;

export type StoreNumber = keyof typeof STORES;

export interface Variant {
  color: Color;
  capacity: Capacity;
  partNumber: string;
  colorName: string;
  capacityName: string;
  displayName: string;
  productUrl: string;
}

export function makeVariant(color: Color, capacity: Capacity): Variant {
  const colorName = COLORS[color];
  const capacityName = CAPACITIES[capacity];
  const token = `7.6インチディスプレイ-${capacity}-${colorName}-simフリー`;
  return {
    color,
    capacity,
    partNumber: PART_NUMBERS[color][capacity],
    colorName,
    capacityName,
    displayName: `${MODEL_NAME} ${capacityName} ${colorName}（${CARRIER_NAME}）`,
    productUrl: `${BUY_PAGE_URL}/${encodeURIComponent(token)}`,
  };
}
