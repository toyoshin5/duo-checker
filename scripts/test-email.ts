// Resend 経由でテストメールを 1 通送る。RESEND_API_KEY は .dev.vars（または環境変数）から読む。
import userConfig from "../config";
import { makeVariant } from "../src/catalog";
import { resolveConfig } from "../src/config";
import { ResendMailer, restockEmail } from "../src/notify";

const config = resolveConfig(userConfig);
const apiKey = process.env.RESEND_API_KEY;
if (!apiKey || apiKey.startsWith("re_xxx")) {
  console.error("RESEND_API_KEY がありません。.dev.vars.example を .dev.vars にコピーして API キーを書いてください");
  process.exit(1);
}
const variant = config.variants[0] ?? makeVariant("starwhite", "256gb");
const email = restockEmail(
  [{ storeNumber: "R718", storeName: "丸の内", partNumber: variant.partNumber, available: true, pickupDisplay: "available", pickupQuote: "本日", storeUrl: "https://www.apple.com/jp/retail/marunouchi" }],
  new Map([[variant.partNumber, variant]]),
  Date.now(),
  true,
);
console.log(`テストメールを ${config.mailTo.join(", ")} に送信します（送信元 ${config.mailFrom}）…`);
try {
  await new ResendMailer(apiKey, config.mailFrom, config.mailTo).send(email);
  console.log("送信しました。届いているか確認してください（迷惑メールフォルダも）。");
} catch (error) {
  console.error(`送信に失敗しました: ${error instanceof Error ? error.message : error}`);
  process.exit(1);
}
