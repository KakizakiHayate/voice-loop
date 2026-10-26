# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "playwright==1.63.0"]  # 手元に入っているブラウザ（chromium 1243・webkit 2359）に合わせる
# ///
"""画面の 💡（いまの状況）の通し。本体の写しをこの試験の中で 127.0.0.1:8791 に立て、本物のブラウザ（Chromium と、iPhone を名乗る WebKit）で開く。
常駐の 8790 には触れない。拾う役（claude）は動かさない（「いま拾う」は呼ばれたことだけを見る）。
見ること: 話題・覚えていること・最近外れた話題・最近の回が出る、打って覚えさせる・× で忘れる、「いま拾う」、📋 と同時には開かない、
中身を文字として出す（タグを解釈しない）、切ってあるときの案内。
使い方: uv run spike/situation_ui_test.py"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="claude", VL_INTAKE="1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import server as s  # noqa: E402

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


async def nothing(*a, **k):
    return None


kicked = []


async def fake_once(manual=False):
    kicked.append(manual)
    return {"ok": True}


s.boot_ensure_hub = nothing
s.keep_gateway_token = nothing
s.watch_blocked = nothing
s.intake_once = fake_once
HUB = {"pane": "w1:p1", "cwd": str(s.HUB_DIR), "kind": "claude", "session": "h", "status": "idle", "title": "hub", "name": "", "hub": True}
s.list_agents = lambda: [HUB]
now = time.time()
s.write_json(s.SITUATION_FILE, {"topics": [
    {"id": "t1", "title": "ネットショップの改修", "now": "見積もりを出した", "next": "先方の返事待ち", "folder": "shop", "until": "2026-10-30", "moved": "2026-10-25",
     "sources": [{"kind": "plaud", "id": "p1", "at": "2026-10-25T10:00+09:00", "label": "定例"}]},
    {"id": "t2", "title": "<img src=x onerror=\"window.__xss=1\">", "now": "<b>太字</b>", "next": "", "folder": "", "until": "", "moved": "2026-10-24", "sources": []},
], "faded": [{"title": "終わった相談", "reason": "外した: 見送りと決まった", "at": now - 3600, "moved": "2026-10-01"}], "seen": {}, "last_ok": now - 600, "next_id": 3})
s.write_json(s.FACTS_FILE, {"next": 2, "facts": [{"n": 1, "text": "週次定例は情報交換の場", "at": now, "via": "screen"}], "remembered": [], "forgot": []})
with open(s.INTAKE_RUNS, "w") as f:
    f.write('{"at": %f, "ok": true, "added": 1, "updated": 2, "retired": 0, "faded": 1, "read": 4, "private": 1}\n' % (now - 600))
    f.write('{"at": %f, "ok": false, "why": "結果が返りませんでした"}\n' % (now - 300))

srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=PORT, log_level="warning"))
th = threading.Thread(target=srv.run, daemon=True)
th.start()
for _ in range(200):
    if srv.started:
        break
    time.sleep(0.05)


def wait(fn, limit=8.0):
    end = time.time() + limit
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def texts(page, sel):
    return page.eval_on_selector_all(sel, "els => els.map(e => e.textContent)")


def run(page, who):
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    check(f"{who} 💡 が出る", page.is_visible("#sitBtn"), True)
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody .topic")
    check(f"{who} 💡 で開き、色がつく", (page.is_visible("#situation"), page.get_attribute("#sitBtn", "aria-pressed")), (True, "true"))
    topics = texts(page, "#sitBody .topic")
    check(f"{who} 話題: 名前（フォルダ）・いま・次（予定）・最後に動いた日と出どころ",
          all(x in topics[0] for x in ("ネットショップの改修（shop）", "見積もりを出した", "次: 先方の返事待ち（予定 2026-10-30）", "最後に動いた日 2026-10-25・定例")), True)
    check(f"{who} 中身は文字として出す（タグを解釈しない）", ("<img src=x" in topics[1], "<b>太字</b>" in topics[1], page.evaluate("window.__xss || 0")), (True, True, 0))
    body = page.text_content("#sitBody")
    check(f"{who} 覚えていること・最近外れた話題・最近の回", all(x in body for x in ("[1] 週次定例は情報交換の場", "終わった相談（外した: 見送りと決まった", "成功（足す 1・直す 2・外す 1・読んだ 4・私用 1）", "失敗: 結果が返りませんでした")), True)
    page.fill("#factText", "請求は月末")
    page.click("#factForm button")
    check(f"{who} 打って覚えさせる（番号つきで並ぶ）", wait(lambda: "] 請求は月末" in (page.text_content("#sitBody") or "")), True)
    check(f"{who} 覚えたら入力欄は空になる", page.input_value("#factText"), "")
    page.click("#sitBody .fact:has-text('請求は月末') button")
    check(f"{who} × で忘れる", wait(lambda: "請求は月末" not in (page.text_content("#sitBody") or "")), True)
    page.click("#sitBody .sitHead button")
    check(f"{who} 「いま拾う」で拾う役が呼ばれる", wait(lambda: kicked and kicked[-1] is True), True)
    page.click("#tasksBtn")
    check(f"{who} 📋 を開くと 💡 は閉じる", (page.is_visible("#situation"), page.is_visible("#tasks")), (False, True))
    page.click("#sitBtn")
    check(f"{who} 💡 を開くと 📋 は閉じる", (page.is_visible("#situation"), page.is_visible("#tasks")), (True, False))
    page.click("#sitClose")
    check(f"{who} ✕ で閉じる", (page.is_visible("#situation"), page.get_attribute("#sitBtn", "aria-pressed")), (False, "false"))


# 「いま拾う」の合図は、本体の起動時に立つ本物の巡回（intake_loop）が受け、差し替えた拾う役（fake_once）を呼ぶ
with sync_playwright() as pw:
    b = pw.chromium.launch()
    run(b.new_page(viewport={"width": 390, "height": 844}), "Chromium:")
    b.close()
    b = pw.webkit.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
    run(ctx.new_page(), "WebKit:")
    b.close()

    s.INTAKE = False
    b = pw.chromium.launch()
    page = b.new_page(viewport={"width": 390, "height": 844})  # 💡 のボタンはスマホの幅のもの（1100px 以上は 3 列の PC の形で、💡 の中身は右の列に常に出る）
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody .sitHead")
    check("切ってあるときは案内を出し、「いま拾う」は出さない", ("VL_INTAKE=1 で動きます" in page.text_content("#sitBody .sitHead"), page.query_selector("#sitBody .sitHead button")), (True, None))
    b.close()

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
