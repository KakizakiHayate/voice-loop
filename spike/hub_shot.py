# /// script
# dependencies = ["playwright>=1.48"]
# ///
"""初回表示でハブが既定になり、ハブの履歴が出ることを確かめて画面を保存する。"""
import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.webkit.launch(); pg = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2).new_page()
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8790"); pg.wait_for_function("document.querySelectorAll('#agent option').length > 0"); pg.wait_for_timeout(1500)
    first = pg.evaluate("document.querySelector('#agent option').textContent")
    sel = pg.evaluate("document.getElementById('agent').selectedOptions[0].textContent")
    n = pg.evaluate("document.querySelectorAll('#log .msg').length")
    print("先頭:", first); print("選択中:", sel); print("履歴の吹き出し:", n, "| エラー:", errs or "なし")
    pg.screenshot(path=sys.argv[1]); b.close()
