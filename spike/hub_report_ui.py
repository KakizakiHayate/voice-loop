# /// script
# dependencies = ["playwright>=1.48"]
# ///
"""画面でハブに頼み、あとから届く報告が履歴に出て、操作なしで再生されることを確かめる（WebKit・スマホ幅）。"""
import sys, time
from playwright.sync_api import sync_playwright
OUT = sys.argv[1] if len(sys.argv) > 1 else "."
ok = []
def check(name, cond, detail=""):
    ok.append(cond); print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)
with sync_playwright() as p:
    b = p.webkit.launch(); pg = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2).new_page()
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    frames = {"n": 0}
    pg.on("websocket", lambda ws: ws.on("framereceived", lambda f: frames.__setitem__("n", frames["n"] + 1) if isinstance(f, bytes) else None))
    pg.goto("http://127.0.0.1:8790")
    pg.wait_for_function("document.querySelectorAll('#agent option').length > 1 && document.getElementById('conn').classList.contains('on')"); pg.wait_for_timeout(800)
    pg.evaluate("() => { window.__ended = 0; player.addEventListener('ended', () => { if (player.src !== SILENT_URL) window.__ended++; }); }")
    notes0 = pg.evaluate("[...document.querySelectorAll('#log .sys')].filter(e => e.innerText.startsWith('通知:')).length")
    pg.fill("#typed", "ボイスループのテスト用のセッションに 四十秒待ってから 画面の試験完了 とだけ答えるように頼んで")
    t0 = time.time(); pg.click("#send")
    # ここから先は画面に触らない。その場の返答 → （相手の作業）→ 通知 → 報告、が履歴に出て、すべて再生されるのを待つ
    pg.wait_for_function(f"[...document.querySelectorAll('#log .sys')].filter(e => e.innerText.startsWith('通知:')).length > {notes0}", timeout=240000)
    t_note = time.time() - t0
    pg.wait_for_function("document.getElementById('statusText').innerText === '話し終わりました' && !playing && queue.length === 0", timeout=180000)
    pg.wait_for_timeout(1500)
    st = pg.evaluate("""() => { const kids = [...document.querySelectorAll('#log > *')]; const tail = kids.slice(-3).map(e => e.classList.contains('sys') ? 'note' : e.classList.contains('me') ? 'me' : 'ai');
        return { tail, note: kids.filter(e => e.classList.contains('sys')).pop().innerText, report: kids[kids.length - 1].innerText, ended: window.__ended,
                 tap: getComputedStyle(document.getElementById('tap')).display, speaking: document.querySelectorAll('.msg.speaking').length }; }""")
    check("履歴の並びが「その場の返答 → 通知 → 報告」", st["tail"] == ["ai", "note", "ai"] and "完了しました" in st["note"], f"{st['tail']}・{st['note']}")
    check("届いた音声を、画面に触らずにすべて再生した", st["ended"] == frames["n"] > 0 and st["tap"] == "none" and st["speaking"] == 0,
          f"再生 {st['ended']} / 受信 {frames['n']} 文・通知まで {t_note:.0f}秒・完了まで {time.time() - t0:.0f}秒")
    print("     報告:", st["report"][:110].replace("\n", " "))
    pg.screenshot(path=f"{OUT}/hub_report.png")
    check("ページのエラーなし", not errs, "; ".join(errs)[:200]); b.close()
print(f"{sum(ok)}/{len(ok)} passed"); sys.exit(0 if all(ok) else 1)
