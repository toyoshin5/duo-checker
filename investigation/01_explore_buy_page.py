"""調査用: iPhone Duo 購入ページを開き、画面構造と通信内容を記録する。

注文確定などの操作は一切行わない。
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)
URL = "https://www.apple.com/jp/shop/buy-iphone/iphone-duo"


def main() -> None:
    requests: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv)
        ctx = browser.new_context(locale="ja-JP", viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()

        def on_response(resp):
            req = resp.request
            if req.resource_type in ("xhr", "fetch", "document"):
                entry = {
                    "method": req.method,
                    "url": req.url,
                    "status": resp.status,
                    "type": req.resource_type,
                    "content_type": resp.headers.get("content-type", ""),
                }
                requests.append(entry)

        page.on("response", on_response)
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.screenshot(path=OUT / "01_buy_page.png", full_page=True)
        (OUT / "01_buy_page.html").write_text(page.content())

        # 埋め込み JSON / グローバル変数の候補を洗い出す
        info = page.evaluate(
            """() => {
              const scripts = [...document.querySelectorAll('script')].map(s => ({
                id: s.id, type: s.type, len: s.textContent.length,
                head: s.textContent.slice(0, 200)
              })).filter(s => s.len > 500);
              const globals = Object.keys(window).filter(k => /PRODUCT|BOOTSTRAP|bootstrap|ac|asMetrics|irVars/i.test(k));
              return { title: document.title, scripts, globals };
            }"""
        )
        (OUT / "01_page_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
        browser.close()

    (OUT / "01_requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))
    print(f"title={info['title']}")
    print(f"globals={info['globals']}")
    for s in info["scripts"]:
        print(f"script id={s['id']!r} type={s['type']!r} len={s['len']} head={s['head'][:120]!r}")
    print(f"--- {len(requests)} requests")
    for r in requests:
        print(r["status"], r["method"], r["type"], r["url"][:200])


if __name__ == "__main__":
    main()
