import { describe, expect, it } from "vitest";
import { ConfigError, DEFAULT_MAIL_FROM, resolveConfig, type UserConfig } from "../src/config";

const base: UserConfig = { colors: ["starwhite"], capacities: ["256gb"], stores: "all", mailTo: ["me@example.org"] };

describe("config.ts の検証", () => {
  it("既定値を補う", () => {
    const config = resolveConfig(base);
    expect(config.variants.map((v) => v.partNumber)).toEqual(["MK244J/A"]);
    expect(config.stores).toHaveLength(11);
    expect(config).toMatchObject({ intervalSeconds: 60, errorAlertMinutes: 10, enabled: true, mailFrom: DEFAULT_MAIL_FROM });
  });

  it("色と容量の組み合わせをすべて監視する", () => {
    const config = resolveConfig({ ...base, colors: ["starwhite", "nightsky"], capacities: ["256gb", "1tb"] });
    expect(config.variants.map((v) => v.partNumber)).toEqual(["MK244J/A", "MK284J/A", "MK254J/A", "MK294J/A"]);
  });

  it("誤りをまとめて知らせる", () => {
    const bad = { ...base, colors: ["gold"], capacities: [], stores: ["R999"], mailTo: ["you@example.com"], intervalSeconds: 10 } as unknown as UserConfig;
    expect(() => resolveConfig(bad)).toThrow(ConfigError);
    try {
      resolveConfig(bad);
    } catch (e) {
      const message = (e as Error).message;
      for (const s of ["gold", "capacities", "R999", "初期値", "intervalSeconds"]) expect(message).toContain(s);
    }
  });
});
