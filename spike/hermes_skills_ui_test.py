# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "playwright==1.63.0"]  # 手元に入っているブラウザ（chromium 1243・webkit 2359）に合わせる
# ///
"""画面の 💡 の「ハブが手順に書き足したいこと（Hermes）」の通し。本体の写しをこの試験の中で 127.0.0.1:8791 に立て、本物のブラウザ（Chromium と、
iPhone を名乗る WebKit）で開く。Hermes の置き場は一時のフォルダ、コンテナの台本は差し替える（docker・常駐の 8790 には触れない）。
見ること: 欄が提案より上に出る（要約・いつ・どこから）、扱えない形は採るを出さない、差分は開いたときに取り、描き直しても開いたまま、
文は文字として出す（タグを解釈しない）、「採る」は2段（1回目では当てない・描き直しで確かめが消えない）、採った・捨てたの案内、
分からないの赤い案内と採るが消えること、コンテナが使えないときの理由、承認待ちが無ければ欄を出さない。
使い方: uv run spike/hermes_skills_ui_test.py"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
HOME = Path(TMP) / "hermes"
PEND = HOME / "pending" / "skills"
PEND.mkdir(parents=True)
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9", VL_HERMES_HOME=str(HOME), VL_INTAKE="1")
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


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.intake_loop = s.watch_hermes_skills = nothing
s.list_agents = lambda: []
CALLS = []
OUT = {}


def fake_run(container, mode, pid, raw, wait=90, env=None):
    CALLS.append((mode, pid))
    r = OUT.get(mode)
    if r is None:
        r = {"check": {"result": "ok"}, "diff": {"result": "diff", "diff": f"--- a/SKILL.md\n+++ b/SKILL.md\n+<b>足す行</b> {pid}\n"},
             "approve": {"result": "applied", "discarded": True}, "reject": {"result": "rejected", "discarded": True}}[mode]
    if r.get("result") in ("applied", "rejected") and r.get("discarded"):
        (PEND / f"{pid}.json").unlink(missing_ok=True)  # 台本が承認待ちから消すのにあたる
    return dict(r)


s.hermes_skills.run_script = fake_run
s.hermes_skills.still_running = lambda c: False


def record(pid, payload, summary, origin="background_review"):
    d = {"id": pid, "subsystem": "skills", "action": payload["action"], "summary": summary, "origin": origin, "created_at": time.time(), "payload": payload}
    (PEND / f"{pid}.json").write_text(json.dumps(d, ensure_ascii=False))


PATCH = {"action": "batch", "operations": [{"action": "patch", "name": "voice-loop-hub", "old_string": "a", "new_string": "a\nb"}]}


def seed():
    for p in PEND.iterdir():
        p.unlink()
    s.HSK_FILE.unlink(missing_ok=True)
    s.HSK_SHOWN.clear()
    s.HSK_DIFFS.clear()
    s._probes.pop("hermes-skills", None)
    CALLS.clear()
    OUT.clear()
    record("aaaaaaaa", PATCH, "読み替えに「山だ」を足す")
    record("bbbbbbbb", {"action": "delete", "name": "voice-loop-hub"}, "<img src=x onerror=\"window.__xss=1\">手順を消す", origin="assistant_tool")
    now = time.time()
    t = {"id": "t1", "title": "話題", "now": "いま", "next": "次", "folder": "shop", "until": "", "moved": time.strftime("%Y-%m-%d"),
         "sources": [{"kind": "plaud", "id": "p1", "at": time.strftime("%Y-%m-%dT%H:%M+09:00"), "label": "録音"}]}
    s.write_json(s.SITUATION_FILE, {"topics": [t], "faded": [], "seen": {}, "last_ok": now - 600, "next_id": 2, "proposals": {
        "next": 2, "open": [{"n": 1, "v": "v1", "topic": "t1", "folder": "shop", "text": "ひな形を作ってください", "why": "金曜まで", "sources": ["p1"],
                             "basis": s.topic_basis(t), "at": now, "expires": now + 86400, "state": "open"}], "closed": [], "used": {}}})


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


def heads(page):
    return page.eval_on_selector_all("#sitBody h4", "els => els.map(e => e.textContent)")


def run(page, who):
    seed()
    open_panel(page)
    page.wait_for_selector("#sitBody .prop.hsk")
    h = heads(page)
    check(f"{who} 欄が出て、提案より上にある", (h[0].startswith("ハブが手順に書き足したいこと（Hermes・2）"), h[1].startswith("提案（1）")), (True, True))
    cards = page.locator("#sitBody .prop.hsk")
    texts = [cards.nth(i).text_content() for i in range(cards.count())]
    check(f"{who} 要約・どこから（見直し・会話の中）・扱えない形の理由", (cards.count(), "読み替えに「山だ」を足す" in texts[0], "会話のあとの見直しから" in texts[0],
                                                       "会話の中から" in texts[1], "この形は画面では扱えません（delete は扱えません）" in texts[1]), (2, True, True, True, True))
    check(f"{who} 要約は文字として出す（タグを解釈しない）", ("<img src=x" in texts[1], page.evaluate("window.__xss || 0")), (True, 0))
    btns = lambda i: [b.text_content() for b in page.locator("#sitBody .prop.hsk").nth(i).locator(".acts button").all()]
    check(f"{who} 扱える形には採ると捨てる、扱えない形には捨てるだけ", (btns(0), btns(1)), (["採る", "捨てる"], ["捨てる"]))
    check(f"{who} 似た書き足しなら捨ててよい、の一言", "似た書き足しがもう手順にあれば、捨ててかまいません" in body(page), True)

    first = page.locator("#sitBody .prop.hsk").nth(0)
    first.locator("summary").click()
    check(f"{who} 差分は開いたときに取り、文字として出す", wait(lambda: "+<b>足す行</b> aaaaaaaa" in (first.locator("pre.diff").text_content() or "")), True)
    page.evaluate("loadSituation()")
    time.sleep(0.6)
    first = page.locator("#sitBody .prop.hsk").nth(0)
    check(f"{who} 描き直しても差分は開いたまま（取り直さない）",
          (first.locator("details").get_attribute("open") is not None, "+<b>足す行</b>" in (first.locator("pre.diff").text_content() or ""),
           [c for c in CALLS if c[0] == "diff"]), (True, True, [("diff", "aaaaaaaa")]))

    first.locator(".acts button").nth(0).click()
    check(f"{who} 「採る」の1回目は確かめを出すだけ（当てない）",
          (first.locator(".acts button").nth(0).text_content(), [c for c in CALLS if c[0] == "approve"]), ("Hermes の手順に書き足す", []))
    page.evaluate("loadSituation()")
    time.sleep(0.5)
    check(f"{who} 描き直しで確かめが消えない", page.locator("#sitBody .prop.hsk").nth(0).locator(".acts button").nth(0).text_content(), "Hermes の手順に書き足す")
    page.locator("#sitBody .prop.hsk").nth(0).locator(".acts button").nth(0).click()
    check(f"{who} 2回目で当て、案内を出し、欄から消える",
          (wait(lambda: "手順に書き足しました。次からの会話で使われます" in body(page)), wait(lambda: page.locator("#sitBody .prop.hsk").count() == 1),
           [c for c in CALLS if c[0] == "approve"]), (True, True, [("approve", "aaaaaaaa")]))
    page.locator("#sitBody .prop.hsk").nth(0).locator(".acts button").nth(0).click()
    check(f"{who} 捨てると案内を出し、承認待ちが無くなれば欄の見出しも出さない",
          (wait(lambda: "書き足しを捨てました" in body(page)), wait(lambda: not any(x.startswith("ハブが手順に") for x in heads(page)))), (True, True))

    seed()
    OUT["approve"] = {"result": "failed", "why": "patch would fail"}
    page.click("#sitClose")
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody .prop.hsk")
    b = page.locator("#sitBody .prop.hsk").nth(0).locator(".acts button").nth(0)
    b.click()
    b.click()
    check(f"{who} 当たったか分からないときは赤で出し、その書き足しは採るを出さない（捨てるは出す）",
          (wait(lambda: page.locator("#sitNote.bad").count() == 1 and "当たったか分かりません" in (page.text_content("#sitNote") or "")),
           wait(lambda: btns(0) == ["捨てる"] and "当たったか分かりません" in (page.locator("#sitBody .prop.hsk").nth(0).text_content() or ""))), (True, True))

    seed()
    OUT["check"] = {"result": "error", "why": "Error: container hermes is not running"}
    page.click("#sitClose")
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody .prop.hsk")
    check(f"{who} コンテナが使えないときは理由を出し、採るを出さない",
          (wait(lambda: "Hermes のコンテナ（hermes）で台本を動かせません" in body(page)), btns(0)), (True, ["捨てる"]))
    page.click("#sitClose")

    # ハブの会話を新しくする（2段。1回目は確かめだけ）
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody .sitHead")
    row = lambda: page.locator("#sitBody .sitHead").nth(1)
    before = s.hermes_session()
    check(f"{who} ハブの会話の欄に、いまの会話の名前と、毎朝4時に新しくなることが出る",
          wait(lambda: before in (row().text_content() or "") and "毎朝4時に新しくなります" in (row().text_content() or "")), True)
    row().locator("button").click()
    check(f"{who} 「ハブの会話を新しくする」の1回目は確かめを出すだけ（新しくしない）", (row().locator("button").text_content(), s.hermes_session()),
          ("前の会話を持ち越さずに始める", before))
    row().locator("button").click()
    check(f"{who} 2回目で新しくし、案内と新しい会話の名前を出す",
          (wait(lambda: "ハブの会話を新しくしました" in body(page)), wait(lambda: s.hermes_session() != before and s.hermes_session() in (row().text_content() or ""))),
          (True, True))
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
