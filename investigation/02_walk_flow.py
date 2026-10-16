"""調査用: カラー → 容量 → キャリア → 在庫状況確認（店舗検索）までを実ブラウザで操作し、
各ステップのスクリーンショットと在庫関連 API の通信内容を記録する。

バッグへの追加・注文確定は行わない。
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)
URL = "https://www.apple.com/jp/shop/buy-iphone/iphone-duo"
POSTAL = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "100-0005"
INTERESTING = ("fulfillment", "product-locator", "pickup", "retail", "locality", "address", "typeahead")


def shot(page: Page, name: str) -> None:
    page.screenshot(path=OUT / f"02_{name}.png", full_page=False)
    print(f"[shot] {name}")


def main() -> None:
    captured: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv, channel="chrome" if "--chrome" in sys.argv else None)
        ctx = browser.new_context(locale="ja-JP", viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()

        def on_response(resp):
            url = resp.url
            if resp.request.resource_type in ("xhr", "fetch") and any(k in url for k in INTERESTING):
                try:
                    body = resp.text()
                except Exception as e:  # noqa: BLE001
                    body = f"<unreadable: {e}>"
                captured.append(
                    {
                        "method": resp.request.method,
                        "url": url,
                        "status": resp.status,
                        "req_headers": resp.request.headers,
                        "resp_headers": resp.headers,
                        "body": body,
                    }
                )
                print(f"[net] {resp.status} {resp.request.method} {url[:220]}")

        page.on("response", on_response)
        page.goto(URL, wait_until="networkidle", timeout=60000)
        shot(page, "0_loaded")

        # ステップ1: カラー
        page.locator('input[name="dimensionColor"][value="nightsky"]').check(force=True)
        page.wait_for_timeout(1500)
        shot(page, "1_color")

        # ステップ2: 容量
        page.locator('input[name="dimensionCapacity"][value="256gb"]').check(force=True)
        page.wait_for_timeout(1500)
        shot(page, "2_capacity")

        # 下取り（利用しない）— これに答えないとキャリア選択が有効化されない
        page.locator('[data-autom="choose-noTradeIn"]').check(force=True)
        page.wait_for_timeout(1500)

        # ステップ3: キャリア（SIMフリー）
        page.locator('[data-autom="carrierModelUNLOCKED_JP"]').check(force=True)
        page.wait_for_timeout(2500)
        shot(page, "3_carrier")
        print("[url]", page.url)

        # 受け取り関連の UI を探す
        candidates = page.evaluate(
            """() => [...document.querySelectorAll('button, a')]
                .filter(e => /在庫|ストア|受け取|受取/.test(e.textContent))
                .map(e => ({tag: e.tagName, text: e.textContent.trim().slice(0, 60),
                            cls: e.className, autom: e.getAttribute('data-autom')}))"""
        )
        print(json.dumps(candidates, ensure_ascii=False, indent=1))
        (OUT / "02_candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2))

        # 以降の支払い・AppleCare も選択しておく（在庫照会ボタンの活性化条件になっている可能性があるため）
        page.locator('[data-autom="purchaseGroupOptionfullprice"]').check(force=True)
        page.wait_for_timeout(1500)
        page.locator('[data-autom="noapplecare"]').check(force=True)
        page.wait_for_timeout(2500)
        shot(page, "3b_all_selected")
        candidates = page.evaluate(
            """() => [...document.querySelectorAll('button, a')]
                .filter(e => /在庫|ストア|受け取|受取/.test(e.textContent))
                .map(e => ({tag: e.tagName, text: e.textContent.trim().slice(0, 60),
                            cls: e.className, autom: e.getAttribute('data-autom')}))"""
        )
        print(json.dumps(candidates, ensure_ascii=False, indent=1))

        btn = page.locator("button", has_text="在庫状況を確認").first
        if btn.count():
            btn.scroll_into_view_if_needed()
            btn.click()
            page.wait_for_timeout(2500)
            shot(page, "4_locator_open")
            q = page.locator('input[type="text"]:visible, input[type="search"]:visible').first
            q.fill(POSTAL)
            q.press("Enter")
            page.wait_for_timeout(5000)
            shot(page, "5_locator_results")
            (OUT / "02_locator.html").write_text(page.content())
        else:
            print("在庫状況確認ボタンが見つからない")

        browser.close()

    (OUT / "02_captured.json").write_text(json.dumps(captured, ensure_ascii=False, indent=2))
    print(f"captured {len(captured)} responses")


if __name__ == "__main__":
    main()
