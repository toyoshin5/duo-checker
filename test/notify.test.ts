import { describe, expect, it } from "vitest";
import { formatJst, MailError, ResendMailer } from "../src/notify";

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
});
