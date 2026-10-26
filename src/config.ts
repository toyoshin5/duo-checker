// 設定の型と検証。利用者が編集するのはリポジトリ直下の config.ts。

import { CAPACITIES, COLORS, STORES, makeVariant, type Capacity, type Color, type StoreNumber, type Variant } from "./catalog";

export interface UserConfig {
  /** 監視を動かすか。false にすると確認もメール送信も止まる */
  enabled?: boolean;
  /** メール通知を送るか。false にしても監視は続ける */
  emailNotificationsEnabled?: boolean;
  /** 監視するカラー（複数可） */
  colors: Color[];
  /** 監視する容量（複数可） */
  capacities: Capacity[];
  /** 監視する店舗。"all" なら全国の全店舗 */
  stores: "all" | StoreNumber[];
  /** 通知先メールアドレス（複数可） */
  mailTo: string[];
  /** 送信元。Resend で独自ドメインを認証していなければ onboarding@resend.dev のまま */
  mailFrom?: string;
  /** 確認間隔（秒、5 以上。既定 30） */
  intervalSeconds?: number;
  /** 在庫を取得できない状態がこの分数続いたら警告メールを送る */
  errorAlertMinutes?: number;
}

export interface Config {
  enabled: boolean;
  emailNotificationsEnabled: boolean;
  variants: Variant[];
  stores: StoreNumber[];
  mailTo: string[];
  mailFrom: string;
  intervalSeconds: number;
  errorAlertMinutes: number;
}

export class ConfigError extends Error {}

export const DEFAULT_MAIL_FROM = "duo-checker <onboarding@resend.dev>";
const PLACEHOLDER_ADDRESS = "you@example.com";

/** config.ts で使う。型補完とタイプミスの検出のためのヘルパー */
export function defineConfig(config: UserConfig): UserConfig {
  return config;
}

export function resolveConfig(raw: UserConfig): Config {
  const errors: string[] = [];
  const colors = unique(raw.colors ?? []);
  const capacities = unique(raw.capacities ?? []);
  for (const c of colors) if (!(c in COLORS)) errors.push(`colors の値が不正です: ${c}（${Object.keys(COLORS).join(" / ")}）`);
  for (const c of capacities) {
    if (!(c in CAPACITIES)) errors.push(`capacities の値が不正です: ${c}（${Object.keys(CAPACITIES).join(" / ")}）`);
  }
  if (colors.length === 0) errors.push("colors を 1 つ以上指定してください");
  if (capacities.length === 0) errors.push("capacities を 1 つ以上指定してください");

  const stores = raw.stores === "all" ? (Object.keys(STORES) as StoreNumber[]) : unique(raw.stores ?? []);
  for (const s of stores) if (!(s in STORES)) errors.push(`stores に未知の店舗番号があります: ${s}`);
  if (stores.length === 0) errors.push('stores を 1 つ以上指定するか "all" にしてください');

  const mailTo = unique((raw.mailTo ?? []).map((m) => m.trim()).filter(Boolean));
  if (mailTo.length === 0) errors.push("mailTo を 1 つ以上指定してください");
  for (const m of mailTo) {
    if (m === PLACEHOLDER_ADDRESS) errors.push(`mailTo が初期値（${PLACEHOLDER_ADDRESS}）のままです。通知先のアドレスに書き換えてください`);
    else if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(m)) errors.push(`mailTo のアドレスが不正です: ${m}`);
  }

  const intervalSeconds = raw.intervalSeconds ?? 30;
  if (!Number.isFinite(intervalSeconds) || intervalSeconds < 5) errors.push("intervalSeconds は 5 以上にしてください");
  const errorAlertMinutes = raw.errorAlertMinutes ?? 10;
  if (!Number.isFinite(errorAlertMinutes) || errorAlertMinutes < 1) errors.push("errorAlertMinutes は 1 以上にしてください");

  if (errors.length) throw new ConfigError(`config.ts の設定に問題があります:\n- ${errors.join("\n- ")}`);
  return {
    enabled: raw.enabled ?? true,
    emailNotificationsEnabled: raw.emailNotificationsEnabled ?? true,
    variants: colors.flatMap((c) => capacities.map((cap) => makeVariant(c, cap))),
    stores,
    mailTo,
    mailFrom: raw.mailFrom?.trim() || DEFAULT_MAIL_FROM,
    intervalSeconds,
    errorAlertMinutes,
  };
}

function unique<T>(values: T[]): T[] {
  return [...new Set(values)];
}
