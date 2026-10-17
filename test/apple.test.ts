import { describe, expect, it } from "vitest";
import real from "./fixtures/pickup-message-R718.json";
import mixed from "./fixtures/pickup-message-mixed.json";
import { AppleSession, BAG_URL, BlockedError, buildPickupUrl, fetchPickup, fetchText, InventoryError, parsePickupResponse, type FetchFn } from "../src/apple";

const respond = (body: unknown, status = 200) => async () =>
  new Response(typeof body === "string" ? body : JSON.stringify(body), { status });

describe("pickup-message の解析", () => {
  it("実際の応答（丸の内周辺 6 店舗）を読める", () => {
    const result = parsePickupResponse(real, ["MK244J/A", "MK254J/A"]);
    expect(result.map((r) => r.storeNumber)).toEqual(["R718", "R718", "R079", "R079", "R224", "R224", "R128", "R128", "R119", "R119", "R710", "R710"]);
    expect(result[0]).toMatchObject({ storeName: "丸の内", available: false, pickupQuote: "現在ご利用いただけません", storeUrl: "https://www.apple.com/jp/retail/marunouchi" });
  });

  it("在庫ありを判定できる", () => {
    const result = parsePickupResponse(mixed, ["MK244J/A", "MK254J/A"]);
    expect(result.find((r) => r.storeNumber === "R718" && r.partNumber === "MK254J/A")).toMatchObject({ available: true, pickupQuote: "本日" });
    expect(result.filter((r) => r.available)).toHaveLength(1);
  });

  it("形式が違う応答はエラーにする", () => {
    for (const bad of [{}, { body: {} }, [], null, "x"]) expect(() => parsePickupResponse(bad, ["MK244J/A"])).toThrow(InventoryError);
  });
});

describe("在庫 API の呼び出し", () => {
  it("URL を組み立てる", () => {
    const url = new URL(buildPickupUrl(["MK244J/A", "MK254J/A"], "R718"));
    expect(url.origin + url.pathname).toBe("https://www.apple.com/jp/shop/retail/pickup-message");
    expect(url.searchParams.get("parts.0")).toBe("MK244J/A");
    expect(url.searchParams.get("parts.1")).toBe("MK254J/A");
    expect(url.searchParams.get("store")).toBe("R718");
    expect(url.searchParams.get("searchNearby")).toBe("true");
  });

  it("541 は在庫取得の失敗として扱う", async () => {
    await expect(fetchPickup(respond("<html>Page Not Found</html>", 541), ["MK244J/A"], "R718")).rejects.toBeInstanceOf(BlockedError);
  });

  it("最初にバッグを取得し、同じSessionのCookieを在庫APIへ送る", async () => {
    const pickupUrl = buildPickupUrl(["MK244J/A"], "R718");
    const calls: { url: string; cookie: string | null }[] = [];
    const fetchFn: FetchFn = async (input, init) => {
      calls.push({ url: input, cookie: new Headers(init?.headers).get("Cookie") });
      if (input === BAG_URL) {
        return new Response("<html>bag</html>", {
          status: 200,
          headers: { "Set-Cookie": "dssid2=warm; Path=/; Secure" },
        });
      }
      expect(input).toBe(pickupUrl);
      return new Response(JSON.stringify(real), { status: 200, headers: { "content-type": "application/json" } });
    };

    await fetchPickup(fetchFn, ["MK244J/A"], "R718", new AppleSession());

    expect(calls.map((call) => call.url)).toEqual([BAG_URL, pickupUrl]);
    expect(calls[0].cookie).toBeNull();
    expect(calls[1].cookie).toBe("dssid2=warm");
  });

  it("バッグが2xxでも在庫APIに適用できるCookieがなければウォームアップ失敗とする", async () => {
    let bagCalls = 0;
    const session = new AppleSession();
    const fetchFn: FetchFn = async (input) => {
      if (input === BAG_URL) {
        bagCalls += 1;
        return new Response("<html>bag</html>", {
          status: 200,
          headers: { "Set-Cookie": "bag_only=unused; Path=/jp/shop/bag; Secure" },
        });
      }
      return new Response(JSON.stringify(real));
    };
    const pickupUrl = buildPickupUrl(["MK244J/A"], "R718");

    await expect(session.ensureWarm(fetchFn, pickupUrl)).resolves.toBe(false);
    await expect(session.ensureWarm(fetchFn, pickupUrl)).resolves.toBe(false);
    expect(session.cookieNamesFor(pickupUrl)).toEqual([]);
    expect(bagCalls).toBe(1);
    await expect(fetchPickup(fetchFn, ["MK244J/A"], "R718", session)).resolves.toHaveLength(6);
    expect(bagCalls).toBe(1);
  });

  it("並行するウォームアップは1回にまとめ、失敗の再試行は60秒以上空ける", async () => {
    let now = 0;
    let bagCalls = 0;
    let successful = false;
    const session = new AppleSession(() => now);
    const pickupUrl = buildPickupUrl(["MK244J/A"], "R718");
    const fetchFn: FetchFn = async (input) => {
      if (input !== BAG_URL) throw new Error("unexpected request");
      bagCalls += 1;
      return new Response("<html>bag</html>", {
        status: 200,
        headers: successful ? { "Set-Cookie": "dssid2=warm; Path=/; Secure" } : undefined,
      });
    };

    const first = session.ensureWarm(fetchFn, pickupUrl);
    const second = session.ensureWarm(fetchFn, pickupUrl);
    await expect(Promise.all([first, second])).resolves.toEqual([false, false]);
    expect(bagCalls).toBe(1);

    now = 59_999;
    await expect(session.ensureWarm(fetchFn, pickupUrl)).resolves.toBe(false);
    expect(bagCalls).toBe(1);
    successful = true;
    now = 60_000;
    await expect(session.ensureWarm(fetchFn, pickupUrl)).resolves.toBe(true);
    expect(bagCalls).toBe(2);
  });

  it("541時はバッグを取り直して1回だけ再試行し、再度541ならSessionを破棄する", async () => {
    const pickupUrl = buildPickupUrl(["MK244J/A"], "R718");
    const calls: { url: string; cookie: string | null }[] = [];
    let bagNumber = 0;
    const fetchFn: FetchFn = async (input, init) => {
      calls.push({ url: input, cookie: new Headers(init?.headers).get("Cookie") });
      if (input === BAG_URL) {
        bagNumber += 1;
        return new Response("<html>bag</html>", {
          status: 200,
          headers: { "Set-Cookie": `dssid2=warm${bagNumber}; Path=/; Secure` },
        });
      }
      return new Response("<html>blocked</html>", { status: 541 });
    };
    const session = new AppleSession();

    await expect(fetchPickup(fetchFn, ["MK244J/A"], "R718", session)).rejects.toBeInstanceOf(BlockedError);

    expect(calls.map((call) => call.url)).toEqual([BAG_URL, pickupUrl, BAG_URL, pickupUrl]);
    expect(calls.filter((call) => call.url === pickupUrl).map((call) => call.cookie)).toEqual(["dssid2=warm1", "dssid2=warm2"]);
    expect(session.cookieHeaderFor(pickupUrl)).toBe("");
  });

  it("JSON でない応答・ほかの HTTP エラー・通信エラーは在庫なしとみなさない", async () => {
    await expect(fetchPickup(respond("<!doctype html>"), ["MK244J/A"], "R718")).rejects.toThrow("JSON ではありません");
    await expect(fetchPickup(respond("oops", 503), ["MK244J/A"], "R718")).rejects.toThrow("status=503");
    await expect(fetchPickup(async () => { throw new TypeError("network down"); }, ["MK244J/A"], "R718")).rejects.toThrow("network down");
  });

  it("指定した店舗が応答に含まれていなければエラーにする（ほかの店舗だけで在庫なしと判断しない）", async () => {
    await expect(fetchPickup(respond(real), ["MK244J/A"], "R005")).rejects.toThrow("R005");
  });
});

describe("タイムアウト", () => {
  it("応答がなければ打ち切ってエラーにする", async () => {
    const hang: FetchFn = (_url, init) =>
      new Promise((_resolve, reject) => init?.signal?.addEventListener("abort", () => reject(init.signal!.reason)));
    await expect(fetchText(hang, "https://example.invalid", {}, 50)).rejects.toThrow("応答がありません");
  });
});
