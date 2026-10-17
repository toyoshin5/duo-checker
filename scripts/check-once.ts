// この PC から在庫を 1 回だけ確認して表示する（メールは送らない・状態も保存しない）。
import userConfig from "../config";
import { resolveConfig } from "../src/config";
import { MemoryStorage, runCycle } from "../src/monitor";

const config = resolveConfig(userConfig);
const result = await runCycle({
  config,
  storage: new MemoryStorage(),
  fetchFn: (input, init) => fetch(input, init),
  mailer: { send: async () => {} },
  log: (line) => {
    if (line.startsWith("確認")) console.log(line);
  },
});
if (result.restocked.length) console.log("在庫あり:", result.restocked.map((r) => `Apple ${r.storeName} ${r.partNumber}`).join("、"));
process.exit(result.ok ? 0 : 1);
