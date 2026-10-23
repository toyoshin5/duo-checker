import { describe, expect, it } from "vitest";
import { formatJst, MailError, ResendMailer, restockSlackMessage, SlackError, SlackNotifier } from "../src/notify";
import { makeVariant } from "../src/catalog";

describe("Resend での送信", () => {
  it("API キー・冪等キー・宛先を付けて送る", async () => {
    const requests: { url: string; init: RequestInit }[] = [];
    const mailer = new ResendMailer("re_test", "duo-checker <onboarding@resend.dev>", ["me@example.org"], async (url, init) => {
      requests.push({ url, init: init! });
      return Response.json({ id: "abc" });
    });
    await mailer.send({ id: "restock-1", subject: "件名", text: "本文" });
    expect(requests[0].url).toBe("https://api.resend.com/emails");
    const headers = requests[0].init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer re_test");
    expect(headers["Idempotency-Key"]).toBe("restock-1");
    expect(JSON.parse(requests[0].init.body as string)).toEqual({ from: "duo-checker <onboarding@resend.dev>", to: ["me@example.org"], subject: "件名", text: "本文" });
  });

  it("API キーや宛先の誤りは再試行しても直らないエラー、混雑やサーバーエラーは一時的なエラーにする", async () => {
    const send = (status: number) =>
      new ResendMailer("re_test", "a@b.c", ["d@e.f"], async () => new Response("{}", { status })).send({ id: "x", subject: "s", text: "t" });
    for (const [status, permanent] of [[401, true], [403, true], [422, true], [429, false], [500, false]] as const) {
      await expect(send(status)).rejects.toMatchObject({ permanent });
    }
    await expect(new ResendMailer("", "a@b.c", ["d@e.f"]).send({ id: "x", subject: "s", text: "t" })).rejects.toBeInstanceOf(MailError);
  });

  it("日本時間で時刻を書く", () => {
    expect(formatJst(Date.UTC(2026, 9, 16, 15, 30, 5))).toBe("2026-10-17 00:30:05");
  });

  it("Slack Incoming Webhookに在庫メッセージを送る", async () => {
    const requests: { url: string; init: RequestInit }[] = [];
    const notifier = new SlackNotifier("https://hooks.slack.test/services/T/B/S", async (url, init) => {
      requests.push({ url, init: init! });
      return new Response("ok", { status: 200 });
    });
    await notifier.send({ id: "restock-1", subject: "【在庫あり】iPhone Duo", text: "丸の内 R718" });

    expect(requests[0].url).toBe("https://hooks.slack.test/services/T/B/S");
    expect(new Headers(requests[0].init.headers).get("content-type")).toBe("application/json");
    expect(JSON.parse(requests[0].init.body as string)).toEqual({ text: "*【在庫あり】iPhone Duo*\n\n丸の内 R718" });
  });

  it("Slackの在庫通知は機種・確認時刻・購入ページだけに短縮する", () => {
    const variant = makeVariant("starwhite", "256gb");
    const message = restockSlackMessage(
      [{
        storeNumber: "R718",
        storeName: "丸の内",
        partNumber: variant.partNumber,
        available: true,
        pickupDisplay: "available",
        pickupQuote: "本日",
        storeUrl: "https://www.apple.com/jp/retail/marunouchi",
      }],
      new Map([[variant.partNumber, variant]]),
      Date.UTC(2026, 9, 16, 15, 30, 5),
    );
    expect(message.subject).toBe("【在庫あり】丸の内、iPhone Duo 256GB スターホワイト（SIMフリー）、");
    expect(message.text).toBe(`00:30:05\n購入ページ: ${variant.productUrl}`);
  });

  it("2店舗以上の在庫は複数店舗用の見出しにする", () => {
    const variant = makeVariant("starwhite", "256gb");
    const item = (storeNumber: string, storeName: string) => ({
      storeNumber,
      storeName,
      partNumber: variant.partNumber,
      available: true,
      pickupDisplay: "available" as const,
      pickupQuote: "本日",
      storeUrl: `https://www.apple.com/jp/retail/${storeNumber}`,
    });
    const message = restockSlackMessage(
      [item("R718", "丸の内"), item("R079", "銀座")],
      new Map([[variant.partNumber, variant]]),
      Date.UTC(2026, 9, 16, 15, 30, 5),
    );

    expect(message.subject).toBe("【🔔複数店舗在庫あり】丸の内、銀座、iPhone Duo 256GB スターホワイト（SIMフリー）、");
    expect(message.text).toBe(`00:30:05\n購入ページ: ${variant.productUrl}`);
  });

  it("Slackでは「ラベル: URL」の行をリンク表記にする", async () => {
    let body = "";
    const notifier = new SlackNotifier("https://hooks.slack.test/services/T/B/S", async (_url, init) => {
      body = init!.body as string;
      return new Response("ok", { status: 200 });
    });
    await notifier.send({ id: "x", subject: "s", text: "・Apple 丸の内（R718） 受け取り: 本日\nバッグ: https://www.apple.com/jp/shop/bag" });
    expect(JSON.parse(body).text).toBe("*s*\n\n・Apple 丸の内（R718） 受け取り: 本日\n<https://www.apple.com/jp/shop/bag|バッグ>");
  });

  it("SlackのエラーはURLや応答本文を含めずに通知失敗として扱う", async () => {
    await expect(
      new SlackNotifier("https://hooks.slack.test/services/T/SECRET", async () => new Response("secret response", { status: 500 }))
        .send({ id: "x", subject: "s", text: "t" }),
    ).rejects.toEqual(new SlackError("Slack Webhook がエラーを返しました (status=500)"));
  });
});
