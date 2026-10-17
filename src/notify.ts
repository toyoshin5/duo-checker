// 通知メールの文面と、Resend API での送信。

import { fetchText, type FetchFn, type StoreAvailability } from "./apple";
import { BUY_PAGE_URL, CARRIER_NAME, MODEL_NAME, type Variant } from "./catalog";

export interface Email {
  /** 冪等キー。再送しても同じメールが二重に届かないようにする */
  id: string;
  subject: string;
  text: string;
}

export class MailError extends Error {
  constructor(
    message: string,
    /** 再試行しても直らない（API キーや宛先の誤りなど） */
    readonly permanent: boolean,
  ) {
    super(message);
  }
}

export interface Mailer {
  send(email: Email): Promise<void>;
}

/** 日本時間の "2026-10-17 10:00:05" 形式（日本は夏時間がないので +9 時間で求める） */
export function formatJst(epochMs: number): string {
  return new Date(epochMs + 9 * 3600_000).toISOString().replace("T", " ").slice(0, 19);
}

export function restockEmail(
  items: StoreAvailability[],
  variants: Map<string, Variant>,
  checkedAt: number,
  test = false,
): Email {
  const first = items[0];
  const v0 = variants.get(first.partNumber)!;
  const otherStores = new Set(items.map((i) => i.storeNumber)).size - 1;
  let subject = `【在庫あり】${MODEL_NAME} ${v0.capacityName} ${v0.colorName} — Apple ${first.storeName}`;
  if (otherStores > 0) subject += ` ほか${otherStores}店舗`;
  if (test) subject = `[テスト] ${subject}`;

  const lines: string[] = [];
  if (test) lines.push("※ これは duo-checker のテスト送信です。実際の在庫ではありません。", "");
  lines.push(`${MODEL_NAME} の店頭受け取り在庫が見つかりました。`, "");
  for (const item of items) {
    const v = variants.get(item.partNumber)!;
    lines.push(
      `■ Apple ${item.storeName}（${item.storeNumber}）`,
      `  モデル　: ${MODEL_NAME}（${CARRIER_NAME}）`,
      `  容量　　: ${v.capacityName}`,
      `  カラー　: ${v.colorName}`,
      `  受け取り: ${item.pickupQuote || "—"}`,
      `  購入ページ: ${v.productUrl}`,
    );
    if (item.storeUrl) lines.push(`  店舗ページ: ${item.storeUrl}`);
    lines.push("");
  }
  lines.push(
    `在庫確認時刻: ${formatJst(checkedAt)}（日本時間）`,
    "",
    "在庫はすぐになくなることがあります。購入ページで「ストアでの受け取り」を選び、店舗を指定して注文してください。",
    `商品一覧: ${BUY_PAGE_URL}`,
    "",
    "-- duo-checker",
  );
  const key = items.map((i) => `${i.storeNumber}:${i.partNumber}`).join(",");
  return { id: `restock-${checkedAt}-${key}${test ? "-test" : ""}`, subject, text: lines.join("\n") };
}

export function alertEmail(id: string, subject: string, body: string): Email {
  return { id, subject, text: `${body}\n\n-- duo-checker` };
}

export class ResendMailer implements Mailer {
  constructor(
    private readonly apiKey: string,
    private readonly from: string,
    private readonly to: string[],
    private readonly fetchFn: FetchFn = (input, init) => fetch(input, init),
  ) {}

  async send(email: Email): Promise<void> {
    if (!this.apiKey) throw new MailError("RESEND_API_KEY が設定されていません", true);
    let response: { status: number; ok: boolean; body: string };
    try {
      response = await fetchText(
        this.fetchFn,
        "https://api.resend.com/emails",
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${this.apiKey}`,
            "Content-Type": "application/json",
            "Idempotency-Key": email.id.slice(0, 256),
          },
          body: JSON.stringify({ from: this.from, to: this.to, subject: email.subject, text: email.text }),
        },
        20_000,
      );
    } catch (error) {
      throw new MailError(`Resend に接続できません: ${error instanceof Error ? error.message : String(error)}`, false);
    }
    if (response.ok) return;
    const detail = response.body.slice(0, 300);
    const permanent = response.status >= 400 && response.status < 500 && response.status !== 429 && response.status !== 409;
    throw new MailError(`Resend がエラーを返しました (status=${response.status}): ${detail}`, permanent);
  }
}
