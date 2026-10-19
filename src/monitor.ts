// 1 回分の確認処理: 在庫取得 → 状態遷移の判定 → 通知（送信失敗は次回再送）→ 異常継続の警告。
// 保存先（Durable Object のストレージ）や HTTP・メールは引数で受け取り、テストやローカル実行でも同じ処理を使う。

import { BlockedError, fetchPickup, InventoryError, MAX_PARTS_PER_REQUEST, type FetchFn, type StoreAvailability } from "./apple";
import { STORES } from "./catalog";
import type { Config } from "./config";
import { alertEmail, formatJst, MailError, restockEmail, type Email, type Mailer, type SlackSender } from "./notify";

export interface Storage {
  get<T>(key: string): Promise<T | undefined>;
  put<T>(key: string, value: T): Promise<void>;
}

export class MemoryStorage implements Storage {
  readonly data = new Map<string, unknown>();
  async get<T>(key: string): Promise<T | undefined> {
    const value = this.data.get(key);
    return value === undefined ? undefined : structuredClone(value as T);
  }
  async put<T>(key: string, value: T): Promise<void> {
    this.data.set(key, structuredClone(value));
  }
}

interface AvailabilityRecord {
  available: boolean;
  since: number;
  storeName: string;
  quote: string;
}

interface Health {
  failingSince: number | null;
  alerted: boolean;
  consecutiveFailures: number;
  consecutiveBlocks: number;
  lastError: string;
}

interface OutboxItem {
  email: Email;
  kind: "restock" | "alert";
  createdAt: number;
  attempts: number;
  failedCycles: number;
  nextAttemptAt: number;
  lastError?: string;
}

export interface HistoryEntry {
  at: string;
  kind: string;
  subject: string;
  status: "sent" | "failed" | "dropped";
  attempts: number;
  error?: string;
}

export interface LastCheck {
  at: string;
  ok: boolean;
  observed: number;
  expected: number;
  available: string[];
  errors: string[];
  /** この確認で実際に叩いた在庫APIのステータスと取得DO */
  httpStatus: string[];
  summary: string;
}

export interface UptimeCheck {
  /** 確認を始めた時刻（Unix epoch milliseconds） */
  at: number;
  /** 200 だけを稼働時間として数え、それ以外は稼働時間に数えない */
  status: number | null;
}

export interface CycleDeps {
  config: Config;
  storage: Storage;
  fetchFn: FetchFn;
  mailer: Mailer;
  /** 在庫復活時だけ送るSlack通知。警告・復旧メールは送らない。 */
  slack?: SlackSender;
  /** 在庫取得を別のDurable Objectなどへ委譲する場合の実装。 */
  inventory?: (parts: string[], store: string) => Promise<StoreAvailability[]>;
  /** recentChecks とサイクルログに付ける取得DOの識別子。 */
  inventoryLabel?: string;
  /** 切り替えに使う取得DOの数。541 のとき、全DOを試し終えるまでは 10 秒後に次のDOで確認する。 */
  inventoryCount?: number;
  now?: () => number;
  log?: (line: string) => void;
  sleep?: (ms: number) => Promise<void>;
  /** 件名に [テスト] を付ける（simulate 用） */
  test?: boolean;
}

export interface CycleResult {
  ok: boolean;
  observations: StoreAvailability[];
  restocked: StoreAvailability[];
  errors: string[];
  sent: number;
  /** 在庫 API から HTTP 541 を受け取った */
  blocked: boolean;
  /** 次の確認までの待ち時間（失敗が続くと延びる） */
  nextDelayMs: number;
}

const MAX_BACKOFF_MS = 10 * 60_000;
const BACKOFF_START_MS = 30_000;
const BLOCKED_SWITCH_DELAY_MS = 10_000;
const MAX_HISTORY = 50;
const RECENT_CHECKS = 50;
const UPTIME_WINDOW_MS = 60 * 60_000;
const MAX_UPTIME_CHECKS = 400;
const MAX_OUTBOX = 50;
const SEND_RETRY_DELAYS_MS = [0, 2_000, 5_000];

/**
 * 直近1時間で、直前の確認結果が200だった時間の割合を返す。
 * 確認結果は次の確認まで有効とみなし、確認より前から続く状態はウィンドウの先頭から数える。
 */
export function calculateUptimePercent(checks: UptimeCheck[], now: number): number {
  const windowStart = now - UPTIME_WINDOW_MS;
  const sorted = checks
    .filter((check) => Number.isFinite(check.at) && check.at <= now)
    .slice()
    .sort((a, b) => a.at - b.at);
  if (!sorted.length) return 0;

  let previous: UptimeCheck | undefined;
  for (const check of sorted) {
    if (check.at <= windowStart) previous = check;
    else break;
  }

  let cursor = windowStart;
  let uptimeMs = 0;
  for (const check of sorted) {
    if (check.at <= windowStart) continue;
    if (previous?.status === 200) uptimeMs += check.at - cursor;
    cursor = check.at;
    previous = check;
  }
  if (previous?.status === 200) uptimeMs += now - cursor;

  return Math.round((uptimeMs / UPTIME_WINDOW_MS) * 1000) / 10;
}

function uptimeStatus(httpStatus: string[]): number | null {
  if (!httpStatus.length) return null;
  const codes = httpStatus.map((value) => {
    const match = /^\s*(\d{3})(?:\s|$)/.exec(value);
    return match ? Number(match[1]) : null;
  });
  if (codes.some((code) => code === null)) return null;
  return codes.every((code) => code === 200) ? 200 : codes.find((code) => code !== 200) ?? null;
}

/** 541 のあとは 10 秒後に次の取得DOで確認する。全DOが続けて 541 を返したら、一巡ごとに 30 秒、1 分、2 分…最大 10 分待つ。 */
export function blockedRetryDelayMs(consecutiveBlocks: number, inventoryCount: number): number {
  const count = Math.max(1, inventoryCount);
  if (consecutiveBlocks % count !== 0) return BLOCKED_SWITCH_DELAY_MS;
  return exponentialRetryDelayMs(consecutiveBlocks / count);
}

export function failureRetryDelayMs(intervalSeconds: number, consecutiveFailures: number): number {
  return Math.min(intervalSeconds * 1000 * 2 ** Math.max(0, consecutiveFailures - 1), MAX_BACKOFF_MS);
}

function exponentialRetryDelayMs(consecutiveFailures: number): number {
  return Math.min(BACKOFF_START_MS * 2 ** Math.max(0, consecutiveFailures - 1), MAX_BACKOFF_MS);
}

export async function runCycle(deps: CycleDeps): Promise<CycleResult> {
  const { config, storage } = deps;
  const now = deps.now ?? Date.now;
  const log = deps.log ?? console.log;
  const checkedAt = now();
  const variants = new Map(config.variants.map((v) => [v.partNumber, v]));
  const parts = [...variants.keys()];

  // --- 在庫取得 -------------------------------------------------------------------------
  const wanted = new Set<string>(config.stores);
  const observed = new Map<string, StoreAvailability>();
  const errors: string[] = [];
  const httpStatus: string[] = [];
  let blocked = false;
  const fetchInventory = deps.inventory
    ?? ((requestParts: string[], store: string) => fetchPickup(deps.fetchFn, requestParts, store));
  const statusLabel = deps.inventoryLabel ? ` [${deps.inventoryLabel}]` : "";
  let pending: string[] = [...config.stores];
  while (pending.length) {
    const store = pending.shift()!;
    const covered = new Set<string>();
    let failed = false;
    for (let i = 0; i < parts.length; i += MAX_PARTS_PER_REQUEST) {
      try {
        const found = await fetchInventory(parts.slice(i, i + MAX_PARTS_PER_REQUEST), store);
        httpStatus.push(`200${statusLabel}`);
        for (const obs of found) {
          if (wanted.has(obs.storeNumber)) {
            observed.set(`${obs.storeNumber}:${obs.partNumber}`, obs);
            covered.add(obs.storeNumber);
          }
        }
      } catch (error) {
        if (!(error instanceof InventoryError)) throw error;
        httpStatus.push(`${error.status ? String(error.status) : "エラー"}${statusLabel}`);
        // 1 件失敗したら残りも同じ原因で失敗する可能性が高いので、このサイクルの照会は打ち切る
        errors.push(`${store}: ${error.message}`);
        blocked = error instanceof BlockedError;
        failed = true;
        break;
      }
    }
    if (failed) break;
    // searchNearby=true で近隣店舗もまとめて返るので、取得済みの店舗は照会を省く
    pending = pending.filter((s) => !covered.has(s));
  }
  const observations = [...observed.values()];
  const missing = config.stores.flatMap((s) => parts.filter((p) => !observed.has(`${s}:${p}`)).map((p) => `${s}:${p}`));
  const ok = missing.length === 0;

  // --- 状態遷移（在庫なし・初回 → 在庫あり だけを通知。取得できなかった組み合わせは変えない）--------
  const availability = (await storage.get<Record<string, AvailabilityRecord>>("availability")) ?? {};
  const restocked: StoreAvailability[] = [];
  let changed = false;
  for (const obs of observations) {
    const key = `${obs.storeNumber}:${obs.partNumber}`;
    const prev = availability[key];
    if (obs.available && !prev?.available) restocked.push(obs);
    if (!prev || prev.available !== obs.available) {
      availability[key] = { available: obs.available, since: checkedAt, storeName: obs.storeName, quote: obs.pickupQuote };
      changed = true;
    }
  }
  if (changed) await storage.put("availability", availability);

  const outbox = (await storage.get<OutboxItem[]>("outbox")) ?? [];
  if (restocked.length) {
    log(`★ 在庫復活を検知: ${[...new Set(restocked.map((r) => `Apple ${r.storeName}`))].join("、")}`);
    const message = restockEmail(restocked, variants, checkedAt, deps.test);
    outbox.push(newOutboxItem(message, "restock", checkedAt));
    if (deps.slack && !deps.test) {
      try {
        await deps.slack.send(message);
        log("Slackに在庫復活を通知しました");
      } catch (error) {
        // Slack障害で在庫確認・メール通知を止めない。警告や復旧はSlackへ送らない。
        log(`Slack通知に失敗しました: ${error instanceof Error ? error.message : String(error)}`);
      }
    }
  }

  // --- 異常の継続を知らせる ---------------------------------------------------------------
  const health = (await storage.get<Health>("health")) ?? {
    failingSince: null,
    alerted: false,
    consecutiveFailures: 0,
    consecutiveBlocks: 0,
    lastError: "",
  };
  // 既存の保存データにはこの項目がないので、初回は0として扱う。
  health.consecutiveBlocks ??= 0;
  const healthBefore = JSON.stringify(health);
  if (ok) {
    if (health.alerted) {
      outbox.push(
        newOutboxItem(
          alertEmail(
            `recovered-${checkedAt}`,
            "【duo-checker】在庫の取得が復旧しました",
            `${formatJst(health.failingSince ?? checkedAt)} 〜 ${formatJst(checkedAt)} の在庫は確認できていません。監視を再開しました。`,
          ),
          "alert",
          checkedAt,
        ),
      );
      log("監視が復旧しました");
    }
    Object.assign(health, { failingSince: null, alerted: false, consecutiveFailures: 0, consecutiveBlocks: 0, lastError: "" });
  } else {
    health.consecutiveFailures += 1;
    health.consecutiveBlocks = blocked ? health.consecutiveBlocks + 1 : 0;
    health.lastError = errors.join("; ") || `応答に含まれない店舗があります: ${missing.join(", ")}`;
    health.failingSince ??= checkedAt;
    if (!health.alerted && checkedAt - health.failingSince >= config.errorAlertMinutes * 60_000) {
      outbox.push(
        newOutboxItem(
          alertEmail(
            `failing-${health.failingSince}`,
            "【duo-checker】在庫を取得できていません",
            [
              `${formatJst(health.failingSince)} から在庫を取得できず、在庫が復活しても通知できません。`,
              `エラー: ${health.lastError}`,
              "",
              "直らなければ mac-safari ブランチの Mac 版（Safari 経由）を使ってください。復旧したらお知らせします。",
            ].join("\n"),
          ),
          "alert",
          checkedAt,
        ),
      );
      health.alerted = true;
      log(`在庫取得の失敗が ${config.errorAlertMinutes} 分以上続いたため警告メールを送ります`);
    }
  }
  if (JSON.stringify(health) !== healthBefore) await storage.put("health", health);

  // --- メール送信（失敗分は outbox に残して次のサイクルで再送）-----------------------------------
  const sent = await flushOutbox(deps, outbox, checkedAt);

  // --- ログ -----------------------------------------------------------------------------
  const summary = formatSummary(config.stores, parts, observed, ok);
  log(`確認 ${summary}${errors.length ? ` エラー=${errors.join("; ")}` : ""}`);
  const lastCheck: LastCheck = {
    at: formatJst(checkedAt),
    ok,
    observed: observations.length,
    expected: config.stores.length * parts.length,
    available: observations.filter((o) => o.available).map((o) => `Apple ${o.storeName} ${variants.get(o.partNumber)!.displayName}`),
    errors,
    httpStatus,
    summary,
  };
  await storage.put("lastCheck", lastCheck);
  // 状況ページで 200 / 541 の推移を見られるよう、直近50件を残す（例: "14:09:14 200 [do-2]"）
  const recentChecks = (await storage.get<string[]>("recentChecks")) ?? [];
  recentChecks.push(`${formatJst(checkedAt).slice(11)} ${httpStatus.join(",") || "-"}`);
  await storage.put("recentChecks", recentChecks.slice(-RECENT_CHECKS));

  const uptimeChecks = (await storage.get<UptimeCheck[]>("uptimeChecks")) ?? [];
  uptimeChecks.push({ at: checkedAt, status: uptimeStatus(httpStatus) });
  const uptimeCutoff = checkedAt - UPTIME_WINDOW_MS;
  const validUptimeChecks = uptimeChecks.filter((check) => Number.isFinite(check.at) && check.at <= checkedAt);
  const previousUptimeCheck = validUptimeChecks.filter((check) => check.at < uptimeCutoff).at(-1);
  const currentUptimeChecks = validUptimeChecks.filter((check) => check.at >= uptimeCutoff);
  await storage.put("uptimeChecks", [
    ...(previousUptimeCheck ? [previousUptimeCheck] : []),
    ...currentUptimeChecks,
  ].slice(-MAX_UPTIME_CHECKS));

  const interval = config.intervalSeconds * 1000;
  const failures = health.consecutiveFailures;
  let nextDelayMs: number;
  if (blocked) {
    nextDelayMs = blockedRetryDelayMs(health.consecutiveBlocks, deps.inventoryCount ?? 1);
    log(nextDelayMs === BLOCKED_SWITCH_DELAY_MS
      ? `HTTP 541 応答のため、${formatDelay(nextDelayMs)}後に次の取得DOで確認します（連続 ${health.consecutiveBlocks} 回）`
      : `HTTP 541 応答が ${health.consecutiveBlocks} 回続き、取得DOを一巡したため、次の確認まで ${formatDelay(nextDelayMs)} 待ちます`);
  } else {
    nextDelayMs = failureRetryDelayMs(config.intervalSeconds, failures);
    if (nextDelayMs !== interval) log(`失敗が ${failures} 回続いているため、次の確認まで ${Math.round(nextDelayMs / 1000)} 秒待ちます`);
  }
  return { ok, observations, restocked, errors, sent, blocked, nextDelayMs };
}

function formatDelay(delayMs: number): string {
  return delayMs < 60_000 ? `${Math.round(delayMs / 1000)} 秒` : `${Math.round(delayMs / 60_000)} 分`;
}

function newOutboxItem(email: Email, kind: OutboxItem["kind"], at: number): OutboxItem {
  return { email, kind, createdAt: at, attempts: 0, failedCycles: 0, nextAttemptAt: at };
}

async function flushOutbox(deps: CycleDeps, outbox: OutboxItem[], now: number): Promise<number> {
  if (!outbox.length) return 0;
  const log = deps.log ?? console.log;
  const sleep = deps.sleep ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
  const history = (await deps.storage.get<HistoryEntry[]>("history")) ?? [];
  const remaining: OutboxItem[] = [];
  let sent = 0;
  let attempted = false;
  for (const item of outbox) {
    if (item.nextAttemptAt > now) {
      remaining.push(item);
      continue;
    }
    attempted = true;
    let error: MailError | undefined;
    for (const delay of SEND_RETRY_DELAYS_MS) {
      if (delay) await sleep(delay);
      item.attempts += 1;
      try {
        await deps.mailer.send(item.email);
        error = undefined;
        break;
      } catch (e) {
        error = e instanceof MailError ? e : new MailError(String(e), false);
        if (error.permanent) break;
      }
    }
    const entry: HistoryEntry = { at: formatJst(now), kind: item.kind, subject: item.email.subject, status: "sent", attempts: item.attempts };
    if (!error) {
      sent += 1;
      log(`メールを送信しました: ${item.email.subject}`);
    } else {
      item.lastError = error.message;
      // 次の確認で再送し、失敗が続いたら間隔を倍々に延ばす（最大 30 分）。1 日たっても送れなければ諦める
      item.failedCycles += 1;
      item.nextAttemptAt = now + Math.min(15_000 * 2 ** Math.min(item.failedCycles, 10), 30 * 60_000);
      const expired = now - item.createdAt > 24 * 3600_000;
      entry.status = expired ? "dropped" : "failed";
      entry.error = error.message;
      if (!expired) remaining.push(item);
      log(`メール送信に失敗しました${expired ? "（1 日経過したため破棄）" : "（あとで再送）"}: ${item.email.subject} / ${error.message}`);
    }
    history.push(entry);
  }
  if (attempted) {
    await deps.storage.put("outbox", remaining.slice(-MAX_OUTBOX));
    await deps.storage.put("history", history.slice(-MAX_HISTORY));
  }
  return sent;
}

function formatSummary(stores: string[], parts: string[], observed: Map<string, StoreAvailability>, ok: boolean): string {
  // 例: OK 11/11 在庫あり=1 [MK244J/A] R718丸の内=× R005名古屋栄=○ ...
  const cells = stores.map((s) => {
    const marks = parts.map((p) => {
      const o = observed.get(`${s}:${p}`);
      return o === undefined ? "?" : o.available ? "○" : "×";
    });
    return `${s}${STORES[s as keyof typeof STORES] ?? ""}=${marks.join("")}`;
  });
  const available = [...observed.values()].filter((o) => o.available).length;
  return `${ok ? "OK" : "一部失敗"} ${observed.size}/${stores.length * parts.length} 在庫あり=${available} [${parts.join(",")}] ${cells.join(" ")}`;
}
