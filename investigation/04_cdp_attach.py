"""調査用: 普通に起動した Chrome（--enable-automation なし）に CDP で接続し、
スターホワイト / 256GB / SIMフリー / AppleCareなし を選択して在庫 API の応答を記録する。

バッグへの追加・購入は行わない。
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)
URL = "https://www.apple.com/jp/shop/buy-iphone/iphone-duo"
CDP = "http://127.0.0.1:9222"


def main() -> None:
    captured: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = ctx.new_page()
        page.set_viewport_size({"width": 1400, "height": 1000})

        def on_response(resp):
            url = resp.url
            if "fulfillment" in url or "shld" in url or "product-locator" in url:
                try:
                    body = resp.text()
                except Exception as e:  # noqa: BLE001
                    body = f"<unreadable: {e}>"
                captured.append({"status": resp.status, "method": resp.request.method, "url": url, "body": body})
                print(f"[net] {resp.status} {resp.request.method} {url[:200]}")

        page.on("response", on_response)
        page.goto(URL, wait_until="networkidle", timeout=60000)
        print("webdriver =", page.evaluate("navigator.webdriver"))
        page.wait_for_timeout(2000)
        for sel in (
            '[data-autom="dimensionColorstarwhite"]',
            '[data-autom="dimensionCapacity256gb"]',
            '[data-autom="choose-noTradeIn"]',
            '[data-autom="carrierModelUNLOCKED_JP"]',
            '[data-autom="purchaseGroupOptionfullprice"]',
            '[data-autom="noapplecare"]',
        ):
            page.locator(sel).check(force=True)
            page.wait_for_timeout(1500)
        page.wait_for_timeout(3000)
        page.screenshot(path=OUT / "04_selected.png", full_page=True)
        (OUT / "04_selected.html").write_text(page.content())
        if "--keep" not in sys.argv:
            page.close()

    (OUT / "04_captured.json").write_text(json.dumps(captured, ensure_ascii=False, indent=2))
    print(f"captured {len(captured)}")


if __name__ == "__main__":
    main()
