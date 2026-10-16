"""調査用: iPhone Duo 以外の製品でも在庫 API が 541 になるか比較する（通常起動の Chrome に CDP 接続）。"""
import json, re
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    ctx = p.chromium.connect_over_cdp("http://127.0.0.1:9222").contexts[0]
    page = ctx.new_page()
    page.goto("https://www.apple.com/jp/shop/buy-iphone", wait_until="networkidle")
    links = sorted({h for h in page.eval_on_selector_all("a[href*='/jp/shop/buy-iphone/']", "els => els.map(e => e.href.split('?')[0])")})
    print("\n".join(links))
    # 既存機種のページで part 番号を拾う
    target = next((l for l in links if "iphone-17" in l and "pro" not in l), None) or next(l for l in links if "iphone-duo" not in l)
    page.goto(target, wait_until="networkidle")
    html = page.content()
    parts = sorted(set(re.findall(r'"partNumber":"([A-Z0-9]{5}J/A)"', html)))[:3]
    print("target", target, "parts", parts)
    for part in parts[:1] + ["MK244J/A"]:
        r = page.evaluate("""async (part) => {
            const u = `/jp/shop/fulfillment-messages?fae=true&pl=true&mts.0=regular&parts.0=${encodeURIComponent(part)}&location=100-0005`;
            const res = await fetch(u, {headers: {'x-skip-redirect': 'true'}});
            const t = await res.text();
            return {status: res.status, head: t.slice(0, 300)};
        }""", part)
        print(part, r["status"], r["head"][:200].replace("\n", " "))
    page.close()
