// Cloudflare Workers のエントリポイント。
//
// - Durable Object「Monitor」がアラームで intervalSeconds ごとに確認する（Cron の 1 分刻みより細かくできる）
// - 毎分の Cron は、アラームが止まっていないかを確かめて必要なら起動し直す見張り役
// - 状態は Durable Object のストレージに置く（強い整合性があり、1 インスタンスなので二重に動かない）
// - GET / で直近の確認結果と通知履歴を JSON で返す

import { DurableObject } from "cloudflare:workers";
import userConfig from "../config";
import { ConfigError, resolveConfig, type Config } from "./config";
import { runCycle, type HistoryEntry, type LastCheck, type Storage } from "./monitor";
import { ResendMailer } from "./notify";

export interface Env {
  MONITOR: DurableObjectNamespace<Monitor>;
  RESEND_API_KEY?: string;
}

class DurableStorage implements Storage {
  constructor(private readonly storage: DurableObjectStorage) {}
  get<T>(key: string): Promise<T | undefined> {
    return this.storage.get<T>(key);
  }
  put<T>(key: string, value: T): Promise<void> {
    return this.storage.put(key, value);
  }
}

function loadConfig(): { config?: Config; error?: string } {
  try {
    return { config: resolveConfig(userConfig) };
  } catch (error) {
    if (error instanceof ConfigError) return { error: error.message };
    throw error;
  }
}

export class Monitor extends DurableObject<Env> {
  /** アラームが動いていなければ開始する。Cron から毎分呼ばれる */
  async ensureScheduled(): Promise<string> {
    const { config, error } = loadConfig();
    if (!config || !config.enabled) {
      await this.ctx.storage.deleteAlarm();
      if (error) console.error(error);
      return error ? "config-error" : "disabled";
    }
    const alarm = await this.ctx.storage.getAlarm();
    // 最長のバックオフ（10 分）より先に予定されているのはおかしいので立て直す
    if (alarm === null || alarm > Date.now() + 11 * 60_000) {
      // 止まっていた間を「失敗が続いた時間」に数えないよう、異常の記録をリセットしてから始める
      await this.ctx.storage.delete("health");
      await this.ctx.storage.setAlarm(Date.now());
      console.log("監視を開始しました");
      return "started";
    }
    return "running";
  }

  async alarm(): Promise<void> {
    const started = Date.now();
    const { config, error } = loadConfig();
    if (!config || !config.enabled) {
      if (error) console.error(error);
      return; // 次のアラームは設定しない（Cron の ensureScheduled が設定を見て再開する）
    }
    let delay = config.intervalSeconds * 1000;
    try {
      const result = await runCycle({
        config,
        storage: new DurableStorage(this.ctx.storage),
        fetchFn: (input, init) => fetch(input, init),
        mailer: new ResendMailer(this.env.RESEND_API_KEY ?? "", config.mailFrom, config.mailTo),
      });
      delay = result.nextDelayMs;
    } catch (e) {
      // 想定外のエラーでも監視は止めない
      console.error("確認中に想定外のエラー:", e instanceof Error ? e.stack : String(e));
    } finally {
      await this.ctx.storage.setAlarm(Math.max(started + delay, Date.now() + 1000));
    }
  }

  async status(): Promise<Record<string, unknown>> {
    const { config, error } = loadConfig();
    const storage = this.ctx.storage;
    const alarm = await storage.getAlarm();
    const history = (await storage.get<HistoryEntry[]>("history")) ?? [];
    return {
      state: error ? "設定エラー" : config?.enabled ? "監視中" : "停止中",
      configError: error,
      target: config?.variants.map((v) => v.displayName),
      stores: config?.stores.length,
      mailTo: config?.mailTo.map(maskAddress),
      resendApiKey: this.env.RESEND_API_KEY ? "設定済み" : "未設定（npx wrangler secret put RESEND_API_KEY）",
      nextCheckInSeconds: alarm === null ? null : Math.max(0, Math.round((alarm - Date.now()) / 1000)),
      lastCheck: (await storage.get<LastCheck>("lastCheck")) ?? null,
      health: (await storage.get("health")) ?? null,
      pendingEmails: ((await storage.get<unknown[]>("outbox")) ?? []).length,
      recentNotifications: history.slice(-10).reverse(),
    };
  }
}

function maskAddress(address: string): string {
  const [local, domain] = address.split("@");
  return `${local.slice(0, 2)}***@${domain}`;
}

function monitor(env: Env): DurableObjectStub<Monitor> {
  return env.MONITOR.get(env.MONITOR.idFromName("default"));
}

export default {
  async scheduled(_controller, env) {
    const state = await monitor(env).ensureScheduled();
    if (state !== "running") console.log(`監視の状態: ${state}`);
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/") return new Response("Not found", { status: 404 });
    const stub = monitor(env);
    await stub.ensureScheduled(); // デプロイ直後に開けば、Cron を待たずに監視が始まる
    return new Response(JSON.stringify(await stub.status(), null, 2), {
      headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
    });
  },
} satisfies ExportedHandler<Env>;
