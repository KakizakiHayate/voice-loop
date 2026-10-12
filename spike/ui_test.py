# /// script
# dependencies = ["playwright>=1.48"]
# ///
"""画面の通し検証（スマホ幅）。セッション切り替え・Markdown 表示・音声再生・停止・タップ再生の予備経路。

使い方: uv run spike/ui_test.py <chromium|webkit> <試験用ペイン> <比較用ペインA> <比較用ペインB>
iOS 実機の代わりにはならない（消音スイッチや iOS 固有の再生制限は再現できない）。
"""
import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

ENGINE, TEST_PANE, PANE_A, PANE_B = sys.argv[1:5]
BASE = "http://127.0.0.1:8790"
OUT = sys.argv[5] if len(sys.argv) > 5 else "."
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def api_history(pane):
    return json.loads(urllib.request.urlopen(f"{BASE}/api/history?pane={pane}").read())["messages"]


with sync_playwright() as p:
    if ENGINE == "chromium":
        # 既定の自動再生制限を明示して、タップによる解除が効くことを確かめる
        browser = p.chromium.launch(args=["--autoplay-policy=document-user-activation-required"])
    else:
        browser = p.webkit.launch()
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=(ENGINE == "chromium"), has_touch=True)
    page = ctx.new_page()
    errors, frames = [], {"binary": 0, "done": None}
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    def on_ws(ws):
        def recv(payload):
            if isinstance(payload, bytes):
                frames["binary"] += 1
            else:
                try:
                    d = json.loads(payload)
                    if d.get("type") == "done":
                        frames["done"] = d.get("sentences")
                except Exception:
                    pass
        ws.on("framereceived", recv)

    page.on("websocket", on_ws)
    page.goto(BASE)
    page.wait_for_function("document.querySelectorAll('#agent option').length > 0 && document.querySelectorAll('#speaker option').length > 0")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    check("ライブラリ読み込み (marked / DOMPurify)", page.evaluate("!!(window.marked && window.DOMPurify)"))

    # 1) セッション切り替えで履歴が入れ替わる
    def thread_for(pane):
        page.select_option("#agent", pane)
        page.wait_for_timeout(1200)
        return page.evaluate("[...document.querySelectorAll('#log .msg')].map(e => ({role: e.classList.contains('me') ? 'me' : 'ai', text: e.innerText}))")

    a, b = thread_for(PANE_A), thread_for(PANE_B)
    ha, hb = api_history(PANE_A), api_history(PANE_B)
    check("セッションAの履歴件数が記録と一致", len(a) == len(ha), f"{len(a)} / {len(ha)}")
    check("セッションBの履歴件数が記録と一致", len(b) == len(hb), f"{len(b)} / {len(hb)}")
    check("AとBで内容が違う", [m["text"] for m in a] != [m["text"] for m in b])
    last_me_b = next((m["text"] for m in reversed(hb) if m["role"] == "me"), None)
    shown_me_b = next((m["text"] for m in reversed(b) if m["role"] == "me"), None)
    check("Bの最後の自分の発言が記録どおり", last_me_b is not None and shown_me_b.strip() == last_me_b.strip(), (shown_me_b or "")[:40])
    a2 = thread_for(PANE_A)
    check("Aに戻すとAの履歴に戻る", [m["text"] for m in a2][:len(a) - 1] == [m["text"] for m in a][:len(a) - 1])

    # 2) Markdown が装飾されている（A は表・見出し・コードを含むこの開発セッション）
    counts = page.evaluate("""() => { const q = s => document.querySelectorAll('#log .msg.ai ' + s).length;
        return {table: q('table'), h: q('h1,h2,h3'), strong: q('strong'), code: q('code'), li: q('li'), script: q('script')}; }""")
    check("表・見出し・強調・コード・箇条書きが要素として描画", all(counts[k] > 0 for k in ("table", "h", "strong", "code", "li")), json.dumps(counts))
    raw = page.evaluate("""() => [...document.querySelectorAll('#log .msg.ai')].filter(e => { const c = e.cloneNode(true); c.querySelectorAll('code,pre').forEach(x => x.remove()); return /(^|\\n)#{1,4} |\\*\\*[^*\\n]+\\*\\*|\\n\\|[ -:|]+\\|/.test(c.innerText); }).length""")
    check("生の Markdown 記号（## / ** / 表の罫線）が残っていない", raw == 0, f"残り {raw} 件")
    check("script 要素が混入していない", counts["script"] == 0)
    page.screenshot(path=f"{OUT}/ui_{ENGINE}_thread.png")

    # 3) 予備経路: 再生が拒否されたら「タップして再生」が出て、タップで鳴る
    page.evaluate("""async () => { window.__ended = 0; player.addEventListener('ended', () => { if (player.src !== SILENT_URL) window.__ended++; });
        player.play = () => Promise.reject(new DOMException('blocked', 'NotAllowedError'));
        accepting = true; serverDone = true; enqueue(await silentWav().arrayBuffer()); }""")
    page.wait_for_timeout(500)
    check("再生拒否で「タップして再生」が出る", page.is_visible("#tap"), page.inner_text("#statusText"))
    page.evaluate("delete player.play")
    page.tap("#tap") if ENGINE == "chromium" else page.click("#tap")
    page.wait_for_function("window.__ended >= 1", timeout=5000)
    check("タップで再生され、ボタンが消える", not page.is_visible("#tap"), page.inner_text("#statusText"))

    # 4) 文字で1往復: 返信が履歴に出て、音声が最後まで再生される
    page.select_option("#agent", TEST_PANE)
    page.wait_for_timeout(1000)
    before = len(api_history(TEST_PANE))
    page.evaluate("window.__ended = 0")
    page.fill("#typed", "画面の試験です。小さな表を1つと箇条書きを2つ使って、3文以内で自己紹介してください。ツールは使わないでください。")
    t0 = time.time()
    page.click("#send")
    page.wait_for_selector("#log .msg.ai.speaking", timeout=120000)
    t_reply = time.time() - t0
    page.wait_for_function("window.__ended >= 1", timeout=60000)
    t_first = time.time() - t0
    mid = page.evaluate("({paused: player.paused, status: document.getElementById('statusText').innerText})")
    page.screenshot(path=f"{OUT}/ui_{ENGINE}_speaking.png")
    page.wait_for_function("document.getElementById('statusText').innerText === '話し終わりました'", timeout=240000)
    ended = page.evaluate("window.__ended")
    tail = api_history(TEST_PANE)[-2:]
    check("返信が履歴に追加された", len(tail) == 2 and tail[0]["role"] == "me" and tail[0]["text"].startswith("画面の試験です") and tail[1]["role"] == "ai", f"末尾: {[m['role'] for m in tail]}")
    check("返信の吹き出しに表が描画されている", page.evaluate("document.querySelectorAll('#log .msg.ai:last-of-type table, #log .msg.ai table').length") > 0)
    check("受け取った音声をすべて再生し終えた", frames["done"] is not None and ended == frames["done"] == frames["binary"], f"再生完了 {ended} / 受信 {frames['binary']} / サーバー送出 {frames['done']}")
    print(f"     返信表示まで {t_reply:.1f}s、1文目の再生完了まで {t_first:.1f}s、途中の状態: {mid}")

    # 5) 停止: 再生中に止めると、それ以上鳴らない
    page.evaluate("window.__ended = 0")
    page.fill("#typed", "停止の試験です。数字の一から十二までを、それぞれ短い一文にして順番に言ってください。ツールは使わないでください。")
    page.click("#send")
    page.wait_for_function("window.__ended >= 1", timeout=180000)
    page.click("#stop")
    page.wait_for_timeout(300)
    n_at_stop = page.evaluate("window.__ended")
    page.wait_for_timeout(6000)
    st = page.evaluate("({paused: player.paused, ended: window.__ended, q: queue.length, status: document.getElementById('statusText').innerText})")
    check("停止後は再生が進まない", st["paused"] and st["ended"] == n_at_stop and st["q"] == 0, json.dumps(st, ensure_ascii=False))

    check("ページのエラーなし", not errors, "; ".join(errors)[:300])
    browser.close()

print(f"\n{ENGINE}: {sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
