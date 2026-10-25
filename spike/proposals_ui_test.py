# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "playwright==1.63.0"]  # 手元に入っているブラウザ（chromium 1243・webkit 2359）に合わせる
# ///
"""画面の 💡 の提案の通し。本体の写しをこの試験の中で 127.0.0.1:8791 に立て、本物のブラウザ（Chromium と、iPhone を名乗る WebKit）で開く。
常駐の 8790 には触れない。セッションを立てる・送るは差し替える（herdr には触れない）。
見ること: 提案が出る（フォルダ・なぜ今・文・残り時間・取り消しにくい語の目印）、「送る」は2段（1回目では送らない・描き直しで確かめが消えない）、
送ったあとの案内と閉じた提案、立てたが送れなかったときの案内、見送る、文を文字として出す（タグを解釈しない）、提案が無ければ欄を出さない。
使い方: uv run spike/proposals_ui_test.py"""
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="claude", VL_INTAKE="1", VL_PROJECTS_DIR=f"{TMP}/projects")
for d in ("shop", "blog"):
    Path(f"{TMP}/projects/{d}").mkdir(parents=True)
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


s.boot_ensure_hub = nothing
s.keep_gateway_token = nothing
s.watch_blocked = nothing
s.watch_delegation = nothing
s.intake_loop = nothing  # 本物の拾う役を時間で起こさない
HUB = {"pane": "w1:p1", "cwd": str(s.HUB_DIR), "kind": "claude", "session": "h", "status": "idle", "title": "hub", "name": "", "hub": True}
AGENTS = [HUB]
s.list_agents = lambda: list(AGENTS)
SPAWN = {}
spawned = []


def fake_spawn(directory, project, model):
    spawned.append(project)
    r = dict(SPAWN.get("result") or {"ok": True, "pane": "w9:p1", "name": "x"})
    if r.get("pane"):
        AGENTS.append({"pane": r["pane"], "cwd": str(directory), "title": "ハブが起動", "status": "idle", "hub": False, "session": "", "kind": "claude", "name": ""})
    return r


s.spawn_session = fake_spawn
s.herdr = lambda *a, timeout=60: {"result": {"ok": True}}

NOW = datetime.now().astimezone()
TS = NOW.timestamp()
TEXT = "10/16 の打ち合わせ録音で決まったとおり、Excel のひな形を2種作ってください。"


def seed(texts):
    """話題と、開いている提案（texts の数だけ）を置く。"""
    topics, props = [], []
    for i, (folder, text) in enumerate(texts, 1):
        t = {"id": f"t{i}", "title": f"話題{i}", "now": "いま", "next": "次", "folder": folder, "until": "", "moved": NOW.date().isoformat(),
             "sources": [{"kind": "plaud", "id": f"p{i}", "at": NOW.isoformat(timespec="minutes"), "label": "録音"}]}
        topics.append(t)
        props.append({"n": i, "v": s._fp(i, text), "topic": t["id"], "folder": folder, "text": text, "why": "金曜の打ち合わせまでに要る",
                      "sources": [f"p{i}"], "basis": s.topic_basis(t), "at": TS, "expires": TS + 3 * 86400 - 60, "state": "open"})
    s.write_json(s.SITUATION_FILE, {"topics": topics, "faded": [], "seen": {}, "last_ok": TS - 600, "next_id": len(topics) + 1,
                                    "proposals": {"next": len(props) + 1, "open": props, "closed": [], "used": {}}})
    SPAWN.clear()
    spawned.clear()
    del AGENTS[1:]


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


def body(page):
    return page.text_content("#sitBody") or ""


def open_panel(page):
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody .sitHead")


def run(page, who):
    seed([("shop", TEXT), ("blog", "<img src=x onerror=\"window.__xss=1\">ブログを本番に反映してください")])
    open_panel(page)
    cards = page.eval_on_selector_all("#sitBody .prop", "els => els.map(e => e.textContent)")
    check(f"{who} 提案が出る（番号・フォルダ・なぜ今・文・残り時間）",
          (len(cards), all(x in cards[0] for x in ("[1] shop", "なぜ今: 金曜の打ち合わせまでに要る", "Excel のひな形を2種", "あと72時間で消えます"))), (2, True))
    check(f"{who} 文は文字として出す（タグを解釈しない）", ("<img src=x" in cards[1], page.evaluate("window.__xss || 0")), (True, 0))
    check(f"{who} 取り消しにくい操作の語を含む提案にだけ目印", ("取り消しにくい操作" in cards[0], "取り消しにくい操作" in cards[1]), (False, True))
    first = page.locator("#sitBody .prop").nth(0)
    first.locator(".acts button").nth(0).click()
    check(f"{who} 「送る」の1回目は確かめを出すだけ（送らない）", (first.locator(".acts button").nth(0).text_content(), spawned), ("shop に新しいセッションを立てて送る", []))
    page.evaluate("loadSituation()")  # 15 秒ごとの描き直しにあたる
    time.sleep(0.5)
    check(f"{who} 描き直しで確かめが消えない", page.locator("#sitBody .prop").nth(0).locator(".acts button").nth(0).text_content(), "shop に新しいセッションを立てて送る")
    page.locator("#sitBody .prop").nth(0).locator(".acts button").nth(0).click()
    check(f"{who} 2回目で送り、案内を出す", wait(lambda: "提案 1 を送りました: shop に新しいセッションを立てました" in body(page)), True)
    check(f"{who} 送った提案は欄から消え、閉じた提案に「送った」で残る",
          (page.locator("#sitBody .prop").count(), "送った｜shop" in (page.text_content("#sitBody details") or ""), spawned), (1, True, ["shop"]))
    page.locator("#sitBody .prop").nth(0).locator(".acts button").nth(1).click()
    check(f"{who} 見送ると欄から消え、閉じた提案に「見送った」で残る",
          wait(lambda: page.locator("#sitBody .prop").count() == 0 and "見送った｜blog" in (page.text_content("#sitBody details") or "")), True)
    check(f"{who} 提案が無ければ欄の見出しを出さない", page.evaluate("[...document.querySelectorAll('#sitBody h4')].some(h => h.textContent.startsWith('提案（'))"), False)

    seed([("shop", TEXT)])
    SPAWN["result"] = {"ok": False, "reason": "trust", "pane": "w9:p2"}
    page.click("#sitClose")
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody .prop")
    btn = page.locator("#sitBody .prop").nth(0).locator(".acts button").nth(0)
    btn.click()
    btn.click()
    check(f"{who} 立てたが信頼の確認で止まったときは、送れていないと赤で出し、立てたセッションを示す",
          wait(lambda: page.locator("#sitNote.bad").count() == 1 and all(x in (page.text_content("#sitNote") or "") for x in ("信頼しますか", "shop のワークスペースの「ハブが起動」のタブ・w9:p2"))), True)
    closed = page.text_content("#sitBody details") or ""
    check(f"{who} …閉じた提案に「送れていない」と立てたセッションで残り、欄には戻らない",
          ("送れていない｜shop" in closed, "（w9:p2）" in closed, page.locator("#sitBody .prop").count()), (True, True, 0))
    page.click("#sitClose")


with sync_playwright() as pw:
    b = pw.chromium.launch()
    run(b.new_page(viewport={"width": 390, "height": 844}), "Chromium:")
    b.close()
    b = pw.webkit.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
    run(ctx.new_page(), "WebKit:")
    b.close()

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
