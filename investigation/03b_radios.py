"""調査用: 各ステップのラジオボタン値を列挙する。"""
import json
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(locale="ja-JP")
    page.goto("https://www.apple.com/jp/shop/buy-iphone/iphone-duo", wait_until="networkidle")
    print(json.dumps(page.evaluate("""() => [...document.querySelectorAll('input[type=radio]')].map(x => ({
        name: x.name, value: x.value, id: x.id, autom: x.dataset.autom,
        label: (document.querySelector(`label[for="${x.id}"]`)||{}).innerText?.slice(0,50)}))"""), ensure_ascii=False, indent=1))
    b.close()
