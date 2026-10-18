import { describe, expect, it } from "vitest";
import { type FetchFn } from "../src/apple";
import { STORES } from "../src/catalog";
import { resolveConfig, type UserConfig } from "../src/config";
import { MemoryStorage, runCycle, type CycleDeps, type HistoryEntry } from "../src/monitor";
import { MailError, type Email, type Mailer, type SlackSender } from "../src/notify";

// 在庫 API の代わり。実際の応答と同じく、指定店舗を含む地域の店舗をまとめて返す
const REGIONS = [["R718", "R079", "R224", "R128", "R119", "R710"], ["R005", "R711", "R768", "R091"], ["R048"]];

class FakeApple {
  available = new Set<string>();
  failing = new Map<string, number>(); // 店舗 → HTTP ステータス
  calls: string[] = [];

  fetch: FetchFn = async (input) => {
    const url = new URL(input);
    const store = url.searchParams.get("store")!;
    this.calls.push(store);
    const status = this.failing.get(store);
    if (status) return new Response("<html>blocked</html>", { status });
    const parts = [...url.searchParams].filter(([k]) => k.startsWith("parts.")).map(([, v]) => v);
    const region = REGIONS.find((r) => r.includes(store))!;
    const stores = region.map((s) => ({
      storeNumber: s,
      storeName: STORES[s as keyof typeof STORES],
      reservationUrl: `http://www.apple.com/jp/retail/${s}`,
      partsAvailability: Object.fromEntries(
        parts.map((p) => [p, this.available.has(s)
          ? { pickupDisplay: "available", pickupSearchQuote: "本日" }
          : { pickupDisplay: "unavailable", pickupSearchQuote: "現在ご利用いただけません" }]),
      ),
    }));
    return Response.json({ head: { status: "200" }, body: { stores } });
  };
}

class FakeMailer implements Mailer {
  sent: Email[] = [];
  failNext = 0;
  async send(email: Email) {
    if (this.failNext > 0) {
      this.failNext -= 1;
      throw new MailError("temporary failure", false);
    }
    this.sent.push(email);
  }
}

class FakeSlack implements SlackSender {
  sent: Email[] = [];
  async send(message: Email) {
    this.sent.push(message);
  }
}

function setup(overrides: Partial<UserConfig> = {}) {
  const config = resolveConfig({ colors: ["starwhite"], capacities: ["256gb"], stores: "all", mailTo: ["me@example.org"], ...overrides });
  const apple = new FakeApple();
  const mailer = new FakeMailer();
  const slack = new FakeSlack();
  const storage = new MemoryStorage();
  const clock = { t: Date.UTC(2026, 9, 17, 1, 0, 0) }; // 10:00 JST
  const deps: CycleDeps = { config, storage, fetchFn: apple.fetch, mailer, slack, now: () => clock.t, log: () => {}, sleep: async () => {} };
  const cycle = async () => {
    const result = await runCycle(deps);
    clock.t += 30_000;
    return result;
  };
  return { apple, mailer, slack, storage, clock, deps, cycle };
}

describe("通知の判定", () => {
  it("復活で通知し、在庫ありが続く間は通知せず、売り切れ後の再入荷でまた通知する", async () => {
    const { apple, mailer, cycle } = setup();
    await cycle();
    expect(mailer.sent).toHaveLength(0);
    apple.available.add("R005");
    await cycle();
    expect(mailer.sent).toHaveLength(1);
    await cycle();
    await cycle();
    expect(mailer.sent).toHaveLength(1);
    apple.available.delete("R005");
    await cycle();
    apple.available.add("R005");
    await cycle();
    expect(mailer.sent).toHaveLength(2);
    apple.available.add("R718");
    await cycle();
    expect(mailer.sent).toHaveLength(3);
    expect(mailer.sent[2].subject).toContain("丸の内");
  });

  it("同じ確認で複数店舗が復活したら 1 通にまとめる", async () => {
    const { apple, mailer, cycle } = setup();
    apple.available.add("R718");
    apple.available.add("R048");
    await cycle();
    expect(mailer.sent).toHaveLength(1);
    expect(mailer.sent[0].subject).toBe("【在庫あり】iPhone Duo 256GB スターホワイト — Apple 丸の内 ほか1店舗");
    expect(mailer.sent[0].text).toContain("Apple 福岡（R048）");
  });

  it("メール本文に店舗・モデル・容量・カラー・確認時刻・購入ページを含める", async () => {
    const { apple, mailer, cycle } = setup();
    apple.available.add("R005");
    await cycle();
    const text = mailer.sent[0].text;
    for (const s of ["Apple 名古屋栄（R005）", "iPhone Duo（SIMフリー）", "256GB", "スターホワイト", "受け取り: 本日", "2026-10-17 10:00:00（日本時間）", "https://www.apple.com/jp/shop/buy-iphone/iphone-duo/7.6", "https://www.apple.com/jp/retail/R005"]) {
      expect(text).toContain(s);
    }
  });

  it("取得に失敗しても状態を在庫なしに戻さない（エラー明けに重複通知しない）", async () => {
    const { apple, mailer, cycle } = setup();
    apple.available.add("R005");
    await cycle();
    apple.failing.set("R718", 541);
    expect((await cycle()).ok).toBe(false);
    apple.failing.clear();
    await cycle();
    expect(mailer.sent).toHaveLength(1);
  });
});

describe("在庫の取得", () => {
  it("近隣店舗をまとめて取得し、11 店舗を 3 回の照会で確認する", async () => {
    const { apple, cycle } = setup();
    const result = await cycle();
    expect(apple.calls).toEqual(["R718", "R005", "R048"]);
    expect(result.ok).toBe(true);
    expect(result.observations).toHaveLength(11);
  });

  it("モデルが 4 つ以上なら 3 つずつに分けて照会する", async () => {
    const { apple, cycle } = setup({ colors: ["starwhite", "nightsky"], capacities: ["256gb", "512gb"], stores: ["R718"] });
    const result = await cycle();
    expect(apple.calls).toEqual(["R718", "R718"]);
    expect(result.observations).toHaveLength(4);
  });

  it("HTTP ステータスを記録し、直近の推移を残す", async () => {
    const { apple, storage, cycle } = setup({ stores: ["R718"] });
    await cycle();
    apple.failing.set("R718", 541);
    await cycle();
    expect((await storage.get<{ httpStatus: string[] }>("lastCheck"))!.httpStatus).toEqual(["541"]);
    expect(await storage.get<string[]>("recentChecks")).toEqual([
      "10:00:00 200",
      "10:00:30 541",
    ]);
  });

  it("取得DOの識別子を直近ログに残す", async () => {
    const { deps, storage, cycle } = setup({ stores: ["R718"] });
    deps.inventoryLabel = "do-3";
    deps.inventory = async (parts, store) => parts.map((partNumber) => ({
      storeNumber: store,
      storeName: STORES[store as keyof typeof STORES],
      partNumber,
      available: false,
      pickupDisplay: "unavailable",
      pickupQuote: "現在ご利用いただけません",
      storeUrl: `https://www.apple.com/jp/retail/${store}`,
    }));
    await cycle();
    expect((await storage.get<{ httpStatus: string[] }>("lastCheck"))!.httpStatus).toEqual(["200 [do-3]"]);
    expect(await storage.get<string[]>("recentChecks")).toEqual(["10:00:00 200 [do-3]"]);
  });

  it("541応答があればそのサイクルの残りの照会をやめる", async () => {
    const { apple, cycle } = setup();
    apple.failing.set("R718", 541);
    const result = await cycle();
    expect(apple.calls).toEqual(["R718"]);
    expect(result.ok).toBe(false);
    expect(result.errors[0]).toContain("541");
  });

  it("通信エラーが続くと確認間隔を延ばし、成功すると戻す", async () => {
    const { apple, cycle } = setup();
    apple.failing.set("R718", 503);
    const delays = [];
    for (let i = 0; i < 6; i++) delays.push((await cycle()).nextDelayMs / 1000);
    expect(delays).toEqual([30, 60, 120, 240, 480, 600]);
    apple.failing.clear();
    expect((await cycle()).nextDelayMs).toBe(30_000);
  });

  it("連続する541は30秒、1分、2分から延ばし、最大10分にする", async () => {
    const { apple, cycle } = setup({ intervalSeconds: 120 });
    apple.failing.set("R718", 541);
    const delays = [];
    for (let i = 0; i < 10; i++) delays.push((await cycle()).nextDelayMs / 1000);
    expect(delays).toEqual([30, 60, 120, 240, 480, 600, 600, 600, 600, 600]);
    apple.failing.clear();
    expect((await cycle()).nextDelayMs).toBe(120_000);
    apple.failing.set("R718", 541);
    expect((await cycle()).nextDelayMs).toBe(30_000);
  });
});

describe("Slack通知の判定", () => {
  it("在庫復活時だけ通知し、在庫なし・継続中・取得失敗では通知しない", async () => {
    const { apple, slack, cycle } = setup();
    await cycle();
    expect(slack.sent).toHaveLength(0);

    apple.available.add("R005");
    await cycle();
    expect(slack.sent).toHaveLength(1);
    expect(slack.sent[0].subject).toContain("在庫あり");

    await cycle();
    expect(slack.sent).toHaveLength(1);
    apple.failing.set("R718", 541);
    await cycle();
    expect(slack.sent).toHaveLength(1);
  });
});

describe("メール送信の確実性", () => {
  it("送信に失敗したら次の確認で再送する（在庫ありが続いていても）", async () => {
    const { apple, mailer, storage, cycle } = setup();
    mailer.failNext = 3; // 1 サイクル目の 3 回の試行はすべて失敗
    apple.available.add("R005");
    await cycle();
    expect(mailer.sent).toHaveLength(0);
    expect(await storage.get<unknown[]>("outbox")).toHaveLength(1);
    await cycle();
    expect(mailer.sent).toHaveLength(1);
    expect(await storage.get<unknown[]>("outbox")).toHaveLength(0);
    const history = (await storage.get<HistoryEntry[]>("history"))!;
    expect(history.map((h) => h.status)).toEqual(["failed", "sent"]);
  });

  it("1 サイクル内の再試行で送れれば、そのサイクルで届く", async () => {
    const { apple, mailer, cycle } = setup();
    mailer.failNext = 2;
    apple.available.add("R005");
    expect((await cycle()).sent).toBe(1);
  });

  it("取得できない状態が 10 分続いたら警告し、復旧したら知らせる", async () => {
    const { apple, mailer, clock, cycle } = setup();
    apple.failing.set("R718", 541);
    for (let i = 0; i < 20; i++) await cycle(); // 30 秒 × 20 = 10 分（最後の回で 9 分 30 秒経過）
    expect(mailer.sent).toHaveLength(0);
    clock.t += 30_000;
    await cycle();
    await cycle();
    expect(mailer.sent.map((m) => m.subject)).toEqual(["【duo-checker】在庫を取得できていません"]);
    expect(mailer.sent[0].text).toContain("541");
    apple.failing.clear();
    await cycle();
    expect(mailer.sent.at(-1)!.subject).toBe("【duo-checker】在庫の監視が復旧しました");
    expect(mailer.sent).toHaveLength(2);
  });
});
