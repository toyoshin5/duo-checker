"""調査用: 容量選択後のキャリア選択セクションの状態を確認する。"""
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "out"
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(locale="ja-JP", viewport={"width": 1400, "height": 1000})
    page.goto("https://www.apple.com/jp/shop/buy-iphone/iphone-duo", wait_until="networkidle")
    page.locator('input[name="dimensionColor"][value="nightsky"]').check(force=True)
    page.wait_for_timeout(1000)
    page.locator('input[name="dimensionCapacity"][value="256gb"]').check(force=True)
    page.wait_for_timeout(2500)
    info = page.evaluate("""() => {
      const r = document.querySelector('input[name="carrierModel"]');
      let el = r; for (let i=0;i<8 && el;i++) el = el.parentElement;
      const sec = el;
      return {
        radios: [...document.querySelectorAll('input[name="carrierModel"]')].map(x => ({v:x.value, disabled:x.disabled, aria:x.getAttribute('aria-disabled')})),
        labelAttrs: [...document.querySelectorAll('label[for]')].filter(l=>l.className.includes('carriermodel')).map(l => ({cls:l.className, aria:l.getAttribute('aria-disabled')})),
        text: sec ? sec.innerText.slice(0, 1500) : null,
        formFields: [...document.querySelectorAll('input[type=radio]')].map(x=>x.name).filter((v,i,a)=>a.indexOf(v)===i),
      };
    }""")
    import json; print(json.dumps(info, ensure_ascii=False, indent=1))
    page.screenshot(path=OUT / "03_after_capacity_full.png", full_page=True)
    b.close()
