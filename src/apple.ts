// Apple Store の在庫 API（/jp/shop/retail/pickup-message）の呼び出しと解析。
//
// fulfillment-messages は自動化クライアントやデータセンターからのアクセスに HTTP 541 を返すが、
// pickup-message は Cloudflare Workers から通常の fetch で取得できる（2026-10 に実測）。

export const API_ORIGIN = "https://www.apple.com";
export const PICKUP_PATH = "/jp/shop/retail/pickup-message";
/** 購入ページの fulfillmentBootstrap.maxParamsPerURL に合わせる */
export const MAX_PARTS_PER_REQUEST = 3;
const TIMEOUT_MS = 20_000;

export interface StoreAvailability {
  storeNumber: string;
  storeName: string;
  partNumber: string;
  available: boolean;
  /** "available" / "unavailable" / "ineligible" など */
  pickupDisplay: string;
  /** 例: "本日"、"現在ご利用いただけません" */
  pickupQuote: string;
  storeUrl: string;
}

export class InventoryError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}

/** HTTP 541（Apple のボット対策によるブロック） */
export class BlockedError extends InventoryError {}

export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

/**
 * タイムアウト付きで fetch し、本文まで読む。
 * AbortSignal.timeout() のタイマーは処理が終わっても残り、Workers ではその間イベントが終わらず
 * 稼働時間（Durable Object の課金対象）が延びるため、終わったら必ず解除する。
 */
export async function fetchText(fetchFn: FetchFn, url: string, init: RequestInit, timeoutMs: number): Promise<{ status: number; ok: boolean; body: string }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error(`${timeoutMs / 1000} 秒以内に応答がありません`)), timeoutMs);
  try {
    const response = await fetchFn(url, { ...init, signal: controller.signal });
    return { status: response.status, ok: response.ok, body: await response.text() };
  } finally {
    clearTimeout(timer);
  }
}

export function buildPickupUrl(parts: string[], store: string): string {
  const params = new URLSearchParams({ pl: "true", purchaseOption: "fullPrice", "mts.0": "regular" });
  parts.forEach((p, i) => params.append(`parts.${i}`, p));
  params.append("searchNearby", "true");
  params.append("store", store);
  return `${API_ORIGIN}${PICKUP_PATH}?${params}`;
}

export function parsePickupResponse(payload: unknown, parts: string[]): StoreAvailability[] {
  const stores = (payload as { body?: { stores?: unknown } })?.body?.stores;
  if (!Array.isArray(stores)) throw new InventoryError("応答に body.stores がありません");
  const results: StoreAvailability[] = [];
  for (const store of stores) {
    for (const part of parts) {
      const info = store?.partsAvailability?.[part];
      if (!info || typeof store.storeNumber !== "string") continue;
      const display = String(info.pickupDisplay ?? "");
      results.push({
        storeNumber: store.storeNumber,
        storeName: String(store.storeName ?? store.storeNumber),
        partNumber: part,
        available: display === "available",
        pickupDisplay: display,
        pickupQuote: String(info.pickupSearchQuote ?? ""),
        storeUrl: String(store.reservationUrl ?? "").replace(/^http:\/\//, "https://"),
      });
    }
  }
  return results;
}

/** 指定店舗（と近隣店舗）の在庫を取得する。指定店舗の情報がなければエラーにする。 */
export async function fetchPickup(fetchFn: FetchFn, parts: string[], store: string): Promise<StoreAvailability[]> {
  let response: { status: number; body: string };
  try {
    response = await fetchText(
      fetchFn,
      buildPickupUrl(parts, store),
      {
        headers: {
          Accept: "application/json",
          "Accept-Language": "ja-JP,ja;q=0.9",
          Referer: "https://www.apple.com/jp/shop/buy-iphone/iphone-duo",
        },
      },
      TIMEOUT_MS,
    );
  } catch (error) {
    throw new InventoryError(`在庫 API に接続できません: ${error instanceof Error ? error.message : String(error)}`);
  }
  const body = response.body;
  if (response.status === 541) throw new BlockedError("在庫 API にブロックされました (status=541)", 541);
  if (response.status !== 200) throw new InventoryError(`在庫 API の応答が異常です (status=${response.status})`, response.status);
  let payload: unknown;
  try {
    payload = JSON.parse(body);
  } catch {
    throw new InventoryError(`在庫 API の応答が JSON ではありません: ${body.slice(0, 80)}`);
  }
  const results = parsePickupResponse(payload, parts);
  const found = new Set(results.filter((r) => r.storeNumber === store).map((r) => r.partNumber));
  const missing = parts.filter((p) => !found.has(p));
  if (missing.length) throw new InventoryError(`応答に店舗 ${store} の ${missing.join(", ")} が含まれていません`);
  return results;
}
