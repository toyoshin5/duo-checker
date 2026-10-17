// 在庫を「なし → あり → あり → なし → あり」と模擬的に変化させ、本番と同じ処理で通知を確認する。
// 期待される結果: 2 回目（復活）と 5 回目（再入荷）の 2 通だけメールが届く。
import userConfig from "../config";

import { STORES } from "../src/catalog";
import { resolveConfig } from "../src/config";
import { MemoryStorage, runCycle } from "../src/monitor";
import { ResendMailer } from "../src/notify";

const config = resolveConfig(userConfig);
const apiKey = process.env.RESEND_API_KEY;
if (!apiKey || apiKey.startsWith("re_xxx")) {
  console.error("RESEND_API_KEY がありません。.dev.vars.example を .dev.vars にコピーして API キーを書いてください");
  process.exit(1);
}
const sequence = [false, true, true, false, true];
const target = config.stores[0];
let step = 0;

// 在庫 API の代わりに、指定どおりの在庫状況を返す
const fakeFetch = async (input: string): Promise<Response> => {
  const url = new URL(input);
  const parts = [...url.searchParams.entries()].filter(([k]) => k.startsWith("parts.")).map(([, v]) => v);
  const stores = config.stores.map((s) => ({
    storeNumber: s,
    storeName: STORES[s],
    reservationUrl: "",
    partsAvailability: Object.fromEntries(
      parts.map((p) => {
        const available = sequence[step] && s === target;
        return [p, { pickupDisplay: available ? "available" : "unavailable", pickupSearchQuote: available ? "本日" : "現在ご利用いただけません" }];
      }),
    ),
  }));
  return Response.json({ head: { status: "200" }, body: { stores } });
};

const label = (v: boolean) => (v ? "在庫あり" : "在庫なし");
console.log(`Apple ${STORES[target]} の在庫を ${sequence.map(label).join(" → ")} と変化させ、${config.mailTo.join(", ")} への通知を確認します。\n`);
const storage = new MemoryStorage();
const mailer = new ResendMailer(apiKey, config.mailFrom, config.mailTo);
let total = 0;
for (step = 0; step < sequence.length; step++) {
  const result = await runCycle({ config, storage, fetchFn: fakeFetch, mailer, test: true, log: () => {} });
  total += result.sent;
  const failed = ((await storage.get<unknown[]>("outbox")) ?? []).length;
  console.log(`  ${step + 1} 回目: ${label(sequence[step])} → 通知${result.restocked.length ? "あり" : "なし"}（送信 ${result.sent} 通${failed ? `、送信失敗 ${failed} 通` : ""}）`);
}
console.log(`\n合計 ${total} 通送信しました（期待値 2 通）。受信箱を確認してください。`);
if (total !== 2) {
  const history = (await storage.get<{ error?: string }[]>("history")) ?? [];
  const lastError = history.findLast((h) => h.error)?.error;
  if (lastError) console.error(`直近のエラー: ${lastError}`);
  process.exit(1);
}
