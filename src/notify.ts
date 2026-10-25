// 通知メール・Slackの文面と、各サービスへの送信。

import { fetchText, type FetchFn, type StoreAvailability } from "./apple";
import { BAG_URL, MODEL_NAME, type Variant } from "./catalog";

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

export interface SlackSender {
  send(message: Email): Promise<void>;
}

export class SlackError extends Error {}

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
  let subject = `【在庫あり】${first.storeName}${otherStores > 0 ? `ほか${otherStores}店舗` : ""} ${MODEL_NAME} ${v0.capacityName} ${v0.colorName}`;
  if (test) subject = `[テスト] ${subject}`;

  const lines: string[] = [];
  if (test) lines.push("※ テスト送信です。実際の在庫ではありません。", "");
  lines.push(`店頭受け取りの在庫があります（${formatJst(checkedAt)} 確認）`);
  // モデルごとに店舗をまとめ、購入ページはモデルごとに 1 回だけ載せる
  for (const partNumber of new Set(items.map((i) => i.partNumber))) {
    const v = variants.get(partNumber)!;
    lines.push("", `■ ${v.displayName}`);
    for (const item of items.filter((i) => i.partNumber === partNumber)) {
      lines.push(`・Apple ${item.storeName}（${item.storeNumber}） 受け取り: ${item.pickupQuote || "—"}`);
    }
    lines.push(`購入ページ: ${v.productUrl}`);
  }
  lines.push("", `バッグ: ${BAG_URL}`);
  const key = items.map((i) => `${i.storeNumber}:${i.partNumber}`).join(",");
  return { id: `restock-${checkedAt}-${key}${test ? "-test" : ""}`, subject, text: lines.join("\n") };
}

export function restockSlackMessage(
  items: StoreAvailability[],
  variants: Map<string, Variant>,
  checkedAt: number,
): Email {
  const inStockVariants = [...new Set(items.map((item) => item.partNumber))].map((partNumber) => variants.get(partNumber)!);
  const first = inStockVariants[0];
  const storeCount = new Set(items.map((item) => item.storeNumber)).size;
  const subjectPrefix = storeCount >= 2 ? "【🔔複数店舗在庫あり】" : "【在庫あり】";
  const storeNames = [...new Map(items.map((item) => [item.storeNumber, item.storeName])).values()];
  const pickupQuote = [...new Set(items.map((item) => item.pickupQuote).filter(Boolean))].join("、");
  const text = [formatJst(checkedAt).slice(11), ...inStockVariants.map((variant) => `購入ページ: ${variant.productUrl}`)].join("\n");
  const key = items.map((item) => `${item.storeNumber}:${item.partNumber}`).join(",");
  return {
    id: `restock-${checkedAt}-${key}`,
    subject: subjectPrefix + storeNames.join("、") + "、" + (pickupQuote ? pickupQuote + "、" : "") + first.displayName + "、",
    text,
  };
}

export function alertEmail(id: string, subject: string, body: string): Email {
  return { id, subject, text: body };
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

/** Slack Incoming Webhookへの通知。Webhook URLはログやステータスに出さない。 */
export class SlackNotifier implements SlackSender {
  constructor(
    private readonly webhookUrl: string,
    private readonly fetchFn: FetchFn = (input, init) => fetch(input, init),
  ) {}

  async send(message: Email): Promise<void> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error("Slackへの送信がタイムアウトしました")), 10_000);
    try {
      let response: Response;
      try {
        response = await this.fetchFn(this.webhookUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // 「ラベル: URL」の行は Slack のリンク表記にして、長い URL を本文に出さない
          body: JSON.stringify({ text: `*${message.subject}*\n\n${message.text.replace(/^(.+?): (https:\/\/\S+)$/gm, "<$2|$1>")}` }),
          signal: controller.signal,
        });
      } catch (error) {
        throw new SlackError(`Slackに接続できません: ${error instanceof Error ? error.message : String(error)}`);
      }
      // 応答本文は記録・エラーメッセージに含めない（Webhook URLも秘密情報のため）。
      await response.text();
      if (!response.ok) throw new SlackError(`Slack Webhook がエラーを返しました (status=${response.status})`);
    } finally {
      clearTimeout(timer);
    }
  }
}
