// Cloudflare Workers のエントリポイント。
//
// - Durable Object「Monitor」がアラームで intervalSeconds ごとに確認する（Cron の 1 分刻みより細かくできる）
// - 毎分の Cron は、アラームが止まっていないかを確かめて必要なら起動し直す見張り役
// - 状態は Durable Object のストレージに置く（強い整合性があり、1 インスタンスなので二重に動かない）
// - GET / で直近の確認結果と通知履歴を JSON で返す

import { DurableObject } from "cloudflare:workers";
import { BlockedError, fetchPickup, InventoryError, PICKUP_PATH, type StoreAvailability } from "./apple";
import userConfig from "../config";
import { ConfigError, resolveConfig, type Config } from "./config";
import { blockedRetryDelayMs, failureRetryDelayMs, runCycle, type HistoryEntry, type LastCheck, type Storage } from "./monitor";
import { ResendMailer, SlackNotifier } from "./notify";

export interface Env {
  MONITOR: DurableObjectNamespace<Monitor>;
  RESEND_API_KEY?: string;
  SLACK_WEBHOOK_URL?: string;
}

interface DoProbeResult {
  label: string;
  endpoint: string;
  store: string | null;
  partNumber: string | null;
  status: number | null;
  durationMs: number;
  valid: boolean;
  observed: number;
  egress: Record<string, string>;
  error?: string;
}

interface DoProbeRun {
  mode: "durable-object-probe";
  count: number;
  sameStatus: boolean;
  statuses: (number | null)[];
  probes: DoProbeResult[];
  executedAt: string;
  rateLimited: boolean;
  nextAllowedAt: string;
  note: string;
}

const PROBE_COOLDOWN_MS = 5 * 60_000;
const INVENTORY_DO_COUNT = 32;

interface RemotePickupSuccess {
  ok: true;
  status: 200;
  results: StoreAvailability[];
}

interface RemotePickupFailure {
  ok: false;
  status: number | null;
  error: string;
}

type RemotePickupResult = RemotePickupSuccess | RemotePickupFailure;

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
    const health = await this.ctx.storage.get<{ consecutiveBlocks?: number; consecutiveFailures?: number }>("health");
    // 間隔設定を変えたときも、直近の応答時刻から新しい待ち時間で予約し直す。
    const consecutiveBlocks = health?.consecutiveBlocks ?? 0;
    const consecutiveFailures = health?.consecutiveFailures ?? 0;
    if (alarm !== null) {
      const lastCheck = await this.ctx.storage.get<{ at?: string }>("lastCheck");
      const checkedAt = lastCheck?.at ? Date.parse(`${lastCheck.at.replace(" ", "T")}+09:00`) : NaN;
      if (Number.isFinite(checkedAt)) {
        const delayMs = consecutiveBlocks > 0
          ? blockedRetryDelayMs(consecutiveBlocks, INVENTORY_DO_COUNT)
          : failureRetryDelayMs(config.intervalSeconds, consecutiveFailures);
        const targetAlarm = checkedAt + delayMs;
        const adjustedAlarm = Math.max(Date.now() + 1000, targetAlarm);
        if (Math.abs(alarm - adjustedAlarm) > 30_000) {
          await this.ctx.storage.setAlarm(adjustedAlarm);
          console.log("次回確認を新しい間隔設定に合わせました");
          return "rescheduled";
        }
      }
    }
    // 不自然に先の時刻に予定されたアラームは立て直す。
    if (alarm === null || alarm > Date.now() + 21 * 60_000) {
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
    const savedDoIndex = (await this.ctx.storage.get<number>("inventoryDoIndex")) ?? 0;
    const inventoryDoIndex = ((savedDoIndex % INVENTORY_DO_COUNT) + INVENTORY_DO_COUNT) % INVENTORY_DO_COUNT;
    const inventoryDoLabel = `do-${inventoryDoIndex + 1}`;
    const inventoryDo = this.env.MONITOR.get(this.env.MONITOR.idFromName(`inventory-${inventoryDoIndex}`));
    try {
      const result = await runCycle({
        config,
        storage: new DurableStorage(this.ctx.storage),
        fetchFn: (input, init) => fetch(input, init),
        mailer: new ResendMailer(this.env.RESEND_API_KEY ?? "", config.mailFrom, config.mailTo),
        slack: this.env.SLACK_WEBHOOK_URL ? new SlackNotifier(this.env.SLACK_WEBHOOK_URL) : undefined,
        inventoryLabel: inventoryDoLabel,
        inventoryCount: INVENTORY_DO_COUNT,
        inventory: async (parts, store) => {
          try {
            const remote = await inventoryDo.pickup(parts, store, inventoryDoLabel);
            if (remote.ok) return remote.results;
            const message = `${inventoryDoLabel}: ${remote.error}`;
            if (remote.status === 541) throw new BlockedError(message, 541);
            throw new InventoryError(message, remote.status ?? undefined);
          } catch (error) {
            if (error instanceof InventoryError) throw error;
            throw new InventoryError(`${inventoryDoLabel}: 取得DO呼び出しに失敗しました: ${error instanceof Error ? error.message : String(error)}`);
          }
        },
        log: (line) => console.log(`[${inventoryDoLabel}] ${line}`),
      });
      delay = result.nextDelayMs;
      if (result.blocked) {
        const nextDoIndex = (inventoryDoIndex + 1) % INVENTORY_DO_COUNT;
        await this.ctx.storage.put("inventoryDoIndex", nextDoIndex);
        console.log(`HTTP 541 のため、次回は ${`do-${nextDoIndex + 1}`} に切り替えます（バックオフ ${Math.round(delay / 1000)} 秒）`);
      }
    } catch (e) {
      // 想定外のエラーでも監視は止めない
      console.error("確認中に想定外のエラー:", e instanceof Error ? e.stack : String(e));
    } finally {
      await this.ctx.storage.setAlarm(Math.max(started + delay, Date.now() + 1000));
    }
  }

  /** 指定された取得DOでApple在庫APIを呼び出す。Cookieは返さない。 */
  async pickup(parts: string[], store: string, doLabel: string): Promise<RemotePickupResult> {
    const started = Date.now();
    try {
      const results = await fetchPickup(
        (input, init) => fetch(input, init),
        parts,
        store,
      );
      console.log({ event: "inventory-do-result", do: doLabel, status: 200, durationMs: Date.now() - started });
      return { ok: true, status: 200, results };
    } catch (caught) {
      const status = caught instanceof InventoryError ? caught.status ?? null : null;
      const error = caught instanceof Error ? caught.message : String(caught);
      console.log({ event: "inventory-do-result", do: doLabel, status, durationMs: Date.now() - started });
      return { ok: false, status, error };
    }
  }

  /** この Durable Object が動いている拠点と、外部への通信に使われる IP（Cloudflare の trace で確認） */
  async egress(): Promise<Record<string, string>> {
    const trace = await (await fetch("https://www.cloudflare.com/cdn-cgi/trace")).text();
    return Object.fromEntries(
      trace
        .trim()
        .split("\n")
        .map((line) => line.split("="))
        .filter(([key]) => ["colo", "ip", "loc"].includes(key)),
    );
  }

  /** 本番の監視状態・通知・アラームを変更しない、比較専用の在庫照会。 */
  async probe(): Promise<Omit<DoProbeResult, "label">> {
    const started = Date.now();
    const { config, error: configError } = loadConfig();
    const store = config?.stores[0] ?? null;
    const partNumber = config?.variants[0]?.partNumber ?? null;
    if (!config || configError || !store || !partNumber) {
      return {
        endpoint: PICKUP_PATH,
        store,
        partNumber,
        status: null,
        durationMs: Date.now() - started,
        valid: false,
        observed: 0,
        egress: {},
        error: configError ?? "比較対象の店舗またはSKUが設定されていません",
      };
    }

    let status: number | null = null;
    let valid = false;
    let observed = 0;
    let probeError: string | undefined;
    try {
      const results = await fetchPickup(
        (input, init) => fetch(input, init),
        [partNumber],
        store,
      );
      status = 200;
      valid = true;
      observed = results.length;
    } catch (caught) {
      if (caught instanceof InventoryError) status = caught.status ?? null;
      probeError = caught instanceof Error ? caught.message : String(caught);
    }

    let egress: Record<string, string> = {};
    try {
      egress = await this.egress();
    } catch {
      // 出口情報の取得失敗は在庫APIの結果とは分けて扱う。
    }
    return {
      endpoint: PICKUP_PATH,
      store,
      partNumber,
      status,
      durationMs: Date.now() - started,
      valid,
      observed,
      egress,
      ...(probeError ? { error: probeError } : {}),
    };
  }

  /** 複数DO比較を5分に1回だけ実行し、直近結果を診断用に保存する。 */
  async runProbe(count: number): Promise<DoProbeRun> {
    const now = Date.now();
    const lastRunAt = await this.ctx.storage.get<number>("probeLastRunAt");
    const previous = await this.ctx.storage.get<DoProbeRun>("probeLastResult");
    if (previous && lastRunAt !== undefined && now - lastRunAt < PROBE_COOLDOWN_MS) {
      return {
        ...previous,
        rateLimited: true,
        nextAllowedAt: new Date(lastRunAt + PROBE_COOLDOWN_MS).toISOString(),
      };
    }

    await this.ctx.storage.put("probeLastRunAt", now);
    const runId = crypto.randomUUID();
    const probes = await Promise.all(
      Array.from({ length: count }, async (_, index): Promise<DoProbeResult> => {
        const label = `do-${index + 1}`;
        try {
          const stub = this.env.MONITOR.get(this.env.MONITOR.idFromName(`probe-${runId}-${index}`));
          return { label, ...(await stub.probe()) };
        } catch (caught) {
          return {
            label,
            endpoint: PICKUP_PATH,
            store: null,
            partNumber: null,
            status: null,
            durationMs: 0,
            valid: false,
            observed: 0,
            egress: {},
            error: caught instanceof Error ? caught.message : String(caught),
          };
        }
      }),
    );
    const statuses = probes.map((probe) => probe.status);
    const sameStatus = statuses.length > 0 && statuses.every((status) => status !== null && status === statuses[0]);
    const result: DoProbeRun = {
      mode: "durable-object-probe",
      count,
      sameStatus,
      statuses,
      probes,
      executedAt: new Date(now).toISOString(),
      rateLimited: false,
      nextAllowedAt: new Date(now + PROBE_COOLDOWN_MS).toISOString(),
      note: "本番Monitorの在庫状態・通知・アラームは変更せず、比較結果だけを保存しています。",
    };
    await this.ctx.storage.put("probeLastResult", result);
    return result;
  }

  async status(): Promise<Record<string, unknown>> {
    const { config, error } = loadConfig();
    const storage = this.ctx.storage;
    const alarm = await storage.getAlarm();
    const history = (await storage.get<HistoryEntry[]>("history")) ?? [];
    const savedLastCheck = await storage.get<LastCheck>("lastCheck");
    const lastCheck = savedLastCheck
      ? { ...savedLastCheck, httpStatus: savedLastCheck.httpStatus?.map(removeEndpoint) ?? [] }
      : undefined;
    const recentChecks = ((await storage.get<string[]>("recentChecks")) ?? []).slice().reverse().map(removeEndpoint);
    const counts: Record<string, number> = {};
    for (const entry of recentChecks) {
      const checks = entry.slice(entry.indexOf(" ") + 1).split(",");
      for (const check of checks) {
        const code = check.trim().split(/\s+/, 1)[0];
        if (code) counts[code] = (counts[code] ?? 0) + 1;
      }
    }
    return {
      state: error ? "設定エラー" : config?.enabled ? "監視中" : "停止中",
      // 例: "200 正常（14:09:14 の確認）" / "541 応答（14:09:14 の確認）"
      apple: lastCheck
        ? `${lastCheck.httpStatus?.join(",") ?? "-"} ${lastCheck.ok ? "正常" : lastCheck.httpStatus?.includes("541") ? "応答" : "異常"}（${lastCheck.at.slice(11)} の確認）`
        : "まだ確認していません",
      // 例: "直近 50 回: 200 が 48 回 / 541 が 2 回"
      recentSummary: recentChecks.length
        ? `直近 ${recentChecks.length} 回: ${Object.entries(counts).map(([code, n]) => `${code} が ${n} 回`).join(" / ")}`
        : "",
      recentChecks,
      configError: error,
      target: config?.variants.map((v) => v.displayName),
      stores: config?.stores.length,
      mailTo: config?.mailTo.map(maskAddress),
      resendApiKey: this.env.RESEND_API_KEY ? "設定済み" : "未設定（npx wrangler secret put RESEND_API_KEY）",
      slackWebhook: this.env.SLACK_WEBHOOK_URL ? "設定済み" : "未設定（npx wrangler secret put SLACK_WEBHOOK_URL）",
      nextCheckInSeconds: alarm === null ? null : Math.max(0, Math.round((alarm - Date.now()) / 1000)),
      lastCheck: lastCheck ?? null,
      health: (await storage.get("health")) ?? null,
      pendingEmails: ((await storage.get<unknown[]>("outbox")) ?? []).length,
      recentNotifications: history.slice(-10).reverse(),
    };
  }
}

function removeEndpoint(value: string): string {
  // 旧バージョンが保存した "200 /pickup-message [do-2]" も表示時に新形式へそろえる。
  return value.replace(/\s+\/pickup-message(?=\s|$)/g, "");
}

function maskAddress(address: string): string {
  const [local, domain] = address.split("@");
  return `${local.slice(0, 2)}***@${domain}`;
}

function monitor(env: Env): DurableObjectStub<Monitor> {
  return env.MONITOR.get(env.MONITOR.idFromName("default"));
}

function probeCoordinator(env: Env): DurableObjectStub<Monitor> {
  // 既存のdefault DOのコード更新状態に依存しない比較専用オブジェクト。
  return env.MONITOR.get(env.MONITOR.idFromName("probe-coordinator-20261018"));
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

async function probeDurableObjects(env: Env, url: URL): Promise<Response> {
  const count = Number(url.searchParams.get("probe") ?? "2");
  if (!Number.isInteger(count) || count < 2 || count > 3) {
    return jsonResponse({ error: "probe は 2 または 3 を指定してください" }, 400);
  }
  return jsonResponse(await probeCoordinator(env).runProbe(count));
}

export default {
  async scheduled(_controller, env) {
    const state = await monitor(env).ensureScheduled();
    if (state !== "running") console.log(`監視の状態: ${state}`);
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/probe" || url.searchParams.has("probe")) return probeDurableObjects(env, url);
    if (url.pathname !== "/") return new Response("Not found", { status: 404 });
    const stub = monitor(env);
    await stub.ensureScheduled(); // デプロイ直後に開けば、Cron を待たずに監視が始まる
    const body: Record<string, unknown> = await stub.status();
    if (url.searchParams.has("egress")) body.egress = await stub.egress(); // /?egress で通信元を表示（診断用）
    return jsonResponse(body);
  },
} satisfies ExportedHandler<Env>;
