// Apple Store の在庫 API（/jp/shop/retail/pickup-message）の呼び出しと解析。
//
// 541 の発生条件や応答を生成したレイヤーは公開情報だけでは特定できない。
// 541 のときはバッグページを再取得して Cookie を更新し、在庫 API を一度だけ再試行する。

export const API_ORIGIN = "https://www.apple.com";
export const PICKUP_PATH = "/jp/shop/retail/pickup-message";
export const BAG_URL = `${API_ORIGIN}/jp/shop/bag`;
export const BUY_PAGE_URL = `${API_ORIGIN}/jp/shop/buy-iphone/iphone-duo`;
/** 購入ページの fulfillmentBootstrap.maxParamsPerURL に合わせる */
export const MAX_PARTS_PER_REQUEST = 3;
const TIMEOUT_MS = 20_000;
const WARM_RETRY_INTERVAL_MS = 60_000;

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

/** HTTP 541（在庫を取得できない非標準応答。原因はこの応答だけでは特定できない） */
export class BlockedError extends InventoryError {}

export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

interface AppleCookie {
  value: string;
  domain: string;
  hostOnly: boolean;
  path: string;
  secure: boolean;
  expiresAt: number | null;
}

/** Apple の Set-Cookie を同じ監視セッションの在庫 APIへ引き継ぐ。値はログに出さない。 */
export class AppleSession {
  private readonly cookies = new Map<string, AppleCookie>();
  private warmed = false;
  private warmAttemptAt: number | null = null;
  private warmPromise: Promise<boolean> | null = null;

  constructor(private readonly now: () => number = Date.now) {}

  reset(): void {
    this.cookies.clear();
    this.warmed = false;
    this.warmAttemptAt = null;
  }

  cookieHeaderFor(url: string): string {
    const matching = this.matchingCookies(url);
    return matching.map(([key, cookie]) => `${key.slice(key.lastIndexOf("\0") + 1)}=${cookie.value}`).join("; ");
  }

  /** 指定URLに実際に適用できるCookie名だけを返す。値は返さない。 */
  cookieNamesFor(url: string): string[] {
    return this.matchingCookies(url).map(([key]) => key.slice(key.lastIndexOf("\0") + 1));
  }

  /** バッグの応答が在庫APIに使えるCookieを取得できたか確認してから成功扱いにする。 */
  async ensureWarm(fetchFn: FetchFn, pickupUrl: string): Promise<boolean> {
    if (this.warmed) {
      if (this.cookieNamesFor(pickupUrl).length > 0) return true;
      this.warmed = false;
    }
    // 同じセッションを並行利用しても、同じウォームアップを待つ。
    if (this.warmPromise) return this.warmPromise;
    const now = this.now();
    if (this.warmAttemptAt !== null && now - this.warmAttemptAt < WARM_RETRY_INTERVAL_MS) return false;

    this.warmAttemptAt = now;
    this.warmPromise = this.warmOnce(fetchFn, pickupUrl).finally(() => {
      this.warmPromise = null;
    });
    return this.warmPromise;
  }

  private async warmOnce(fetchFn: FetchFn, pickupUrl: string): Promise<boolean> {
    try {
      const response = await fetchText(
        fetchFn,
        BAG_URL,
        withCookies(
          { headers: { Accept: "text/html", "Accept-Language": "ja-JP,ja;q=0.9", Referer: BUY_PAGE_URL } },
          this,
          BAG_URL,
        ),
        TIMEOUT_MS,
      );
      this.absorb(response.setCookies, BAG_URL);
      const cookieNames = this.cookieNamesFor(pickupUrl);
      const warmed = response.status >= 200 && response.status < 300 && cookieNames.length > 0;
      this.warmed = warmed;
      console.log({ event: "apple-cookie-warm", status: response.status, warmed, cookieNames });
      return warmed;
    } catch {
      console.log({ event: "apple-cookie-warm", status: "error", warmed: false });
      return false;
    }
  }

  private matchingCookies(url: string): [string, AppleCookie][] {
    const target = new URL(url);
    return [...this.cookies.entries()]
      .filter(([, cookie]) => {
        if (cookie.expiresAt !== null && cookie.expiresAt <= this.now()) return false;
        const domainMatches = cookie.hostOnly
          ? target.hostname === cookie.domain
          : target.hostname === cookie.domain || target.hostname.endsWith(`.${cookie.domain}`);
        const pathMatches = target.pathname === cookie.path
          || target.pathname.startsWith(cookie.path.endsWith("/") ? cookie.path : `${cookie.path}/`);
        return domainMatches && pathMatches && (!cookie.secure || target.protocol === "https:");
      })
      .sort((a, b) => b[1].path.length - a[1].path.length);
  }

  absorb(setCookies: string[], responseUrl: string): void {
    const source = new URL(responseUrl);
    for (const setCookie of setCookies) {
      const pair = setCookie.split(";", 1)[0].trim();
      const separator = pair.indexOf("=");
      if (separator <= 0) continue;

      const name = pair.slice(0, separator).trim();
      const value = pair.slice(separator + 1).trim();
      const attributes = setCookie.split(";").slice(1).map((attribute) => attribute.trim());
      const domainAttribute = attributes.find((attribute) => attribute.toLowerCase().startsWith("domain="));
      const domain = domainAttribute ? domainAttribute.slice("domain=".length).replace(/^\./, "").toLowerCase() : source.hostname;
      const hostOnly = domainAttribute === undefined;
      if (source.hostname !== domain && !source.hostname.endsWith(`.${domain}`)) continue;

      const pathAttribute = attributes.find((attribute) => attribute.toLowerCase().startsWith("path="));
      const path = pathAttribute?.slice("path=".length).startsWith("/")
        ? pathAttribute.slice("path=".length)
        : defaultCookiePath(source.pathname);
      const secure = attributes.some((attribute) => attribute.toLowerCase() === "secure");
      const maxAge = attributes.find((attribute) => attribute.toLowerCase().startsWith("max-age="));
      const expires = attributes.find((attribute) => attribute.toLowerCase().startsWith("expires="));
      const maxAgeSeconds = maxAge === undefined ? undefined : Number(maxAge.slice("max-age=".length));
      const expiresAt = maxAgeSeconds !== undefined
        ? this.now() + maxAgeSeconds * 1000
        : expires === undefined ? null : Date.parse(expires.slice("expires=".length));
      const key = `${domain}\0${path}\0${name}`;

      if (expiresAt !== null && expiresAt <= this.now()) this.cookies.delete(key);
      else this.cookies.set(key, { value, domain, hostOnly, path, secure, expiresAt: Number.isNaN(expiresAt) ? null : expiresAt });
    }
  }
}

function defaultCookiePath(pathname: string): string {
  const lastSlash = pathname.lastIndexOf("/");
  return lastSlash <= 0 ? "/" : pathname.slice(0, lastSlash);
}

function withCookies(init: RequestInit, session: AppleSession, url: string): RequestInit {
  const headers = new Headers(init.headers);
  const cookieHeader = session.cookieHeaderFor(url);
  if (cookieHeader) headers.set("Cookie", cookieHeader);
  return { ...init, headers };
}

/**
 * タイムアウト付きで fetch し、本文まで読む。
 * AbortSignal.timeout() のタイマーは処理が終わっても残り、Workers ではその間イベントが終わらず
 * 稼働時間（Durable Object の課金対象）が延びるため、終わったら必ず解除する。
 */
export async function fetchText(fetchFn: FetchFn, url: string, init: RequestInit, timeoutMs: number): Promise<{ status: number; ok: boolean; body: string; setCookies: string[] }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error(`${timeoutMs / 1000} 秒以内に応答がありません`)), timeoutMs);
  const startedAt = Date.now();
  try {
    const response = await fetchFn(url, { ...init, signal: controller.signal });
    const body = await response.text();
    console.log({
      event: "apple-http",
      endpoint: new URL(url).pathname,
      status: response.status,
      durationMs: Date.now() - startedAt,
      responseChars: body.length,
      contentType: response.headers.get("content-type"),
      date: response.headers.get("date"),
      retryAfter: response.headers.get("retry-after"),
      server: response.headers.get("server"),
      via: response.headers.get("via"),
      cfRay: response.headers.get("cf-ray"),
    });
    return { status: response.status, ok: response.ok, body, setCookies: response.headers.getSetCookie() };
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
export async function fetchPickup(fetchFn: FetchFn, parts: string[], store: string, session = new AppleSession()): Promise<StoreAvailability[]> {
  const url = buildPickupUrl(parts, store);
  const apiInit: RequestInit = {
    headers: {
      Accept: "application/json",
      "Accept-Language": "ja-JP,ja;q=0.9",
      Referer: BUY_PAGE_URL,
    },
  };
  // 補助リクエストが失敗しても在庫確認は続ける。成功時だけ同じSessionのCookieをAPIへ送る。
  await session.ensureWarm(fetchFn, url);
  let response: { status: number; body: string };
  try {
    const initial = await fetchText(fetchFn, url, withCookies(apiInit, session, url), TIMEOUT_MS);
    session.absorb(initial.setCookies, url);
    response = initial;
  } catch (error) {
    throw new InventoryError(`在庫 API に接続できません: ${error instanceof Error ? error.message : String(error)}`);
  }

  if (response.status === 541) {
    // 既存セッションや541応答のCookieを引き継がず、バッグから取り直して既存の1回だけ再試行する。
    session.reset();
    await session.ensureWarm(fetchFn, url);

    try {
      const retry = await fetchText(fetchFn, url, withCookies(apiInit, session, url), TIMEOUT_MS);
      session.absorb(retry.setCookies, url);
      response = retry;
    } catch (error) {
      throw new InventoryError(`HTTP 541 後の在庫 API 再試行に接続できません: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  const body = response.body;
  if (response.status === 541) {
    session.reset();
    throw new BlockedError("Cookie を更新して再試行しましたが、在庫 API が HTTP 541 を返しました", 541);
  }
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
