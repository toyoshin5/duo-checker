// config.ts を検証して内容を表示する。`npm run deploy` の前にも自動で実行される。
import userConfig from "../config";
import { ConfigError, resolveConfig } from "../src/config";

try {
  const config = resolveConfig(userConfig);
  console.log("config.ts の設定:");
  console.log(`  監視対象: ${config.variants.map((v) => v.displayName).join("、")}`);
  console.log(`  店舗　　: ${config.stores.length} 店舗（${config.stores.join(", ")}）`);
  console.log(`  通知先　: ${config.mailTo.join(", ")}（送信元 ${config.mailFrom}）`);
  console.log(`  確認間隔: ${config.intervalSeconds} 秒${config.enabled ? "" : "（enabled: false のため停止中）"}`);
} catch (error) {
  if (error instanceof ConfigError) {
    console.error(error.message);
    process.exit(1);
  }
  throw error;
}
