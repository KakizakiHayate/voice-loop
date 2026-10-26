# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "playwright==1.63.0"]  # 手元に入っているブラウザ（chromium 1243・webkit 2359）に合わせる
# ///
"""PC の形（横幅 1100px 以上で 3 列）と、Hermes の記憶を足す・直す・消す画面の通し。本体の写しを 127.0.0.1:8791 に立て、本物のブラウザ
（Chromium と WebKit）で開く。Hermes の記憶の置き場は一時のフォルダ、コンテナの台本は差し替える（docker・常駐の 8790 には触れない）。声の一覧は
この試験の中の小さな偽物（127.0.0.1:8795）から取る。
見ること: 1400px で 3 列（左にセッションの一覧と頼んだ件、真ん中に会話、右に記録のタブ）・💡 📋 のボタンと ✕ を隠す・タブの切り替えと「状況」の数・
セッションの一覧で話し相手が替わる（文字で送ると選んだ相手に届く）・記憶の一覧（字数・上限・文字として出す）・足す・直す（取り直しても編集の途中が残る）・
消す（2 段）・当たったか分からないときの赤い案内と次の書き込みを断ること・1099px と 1100px の境・横にはみ出さない・PC でも話す・停止・文字・声・🔇・↻ が出る・
スマホの幅に戻すと今の形（💡 📋 の相互の開閉、覚えていることの追加と削除）に戻り、もう一度 PC にしても壊れない。
PC の見た目（Chromium）: 頼んだ件を「動いている」「終わった」に分け、状態の色の帯・指示の文は 2 行で切って押すと全文（取り直しても開いたまま）・
ハブの返事の行頭の【見出し】を太く・記憶の上限との棒・長い項目は 4 行で切って「続きを表示」・直す・消すはマウスを載せた項目と消すの確かめ中だけ出す・
入力は文字の欄が上で、話す・停止は相手と声と同じ行に小さく。
使い方: uv run spike/pc_ui_test.py"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp()
HOME = Path(TMP) / "hermes"
MEM = HOME / "memories"
MEM.mkdir(parents=True)
TTS_PORT = 8795
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
                  VL_HERMES_HOME=str(HOME), VL_INTAKE="1", VL_VOICE_URL=f"http://127.0.0.1:{TTS_PORT}")
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


class Speakers(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps([{"name": "試しの声", "styles": [{"id": 1, "name": "ふつう"}]}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


threading.Thread(target=HTTPServer(("127.0.0.1", TTS_PORT), Speakers).serve_forever, daemon=True).start()


async def nothing(*a, **k):
    return None


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.intake_loop = s.watch_hermes_skills = nothing
AGENTS = [{"pane": "w1:p1", "title": "記事", "cwd": "/x/blog", "kind": "claude", "session": "s1", "status": "idle", "name": "", "hub": False},
          {"pane": "w2:p1", "title": "受注", "cwd": "/x/shop", "kind": "claude", "session": "s2", "status": "blocked", "name": "", "hub": False}]
s.list_agents = lambda: [dict(a) for a in AGENTS]
s.jsonl_path = lambda a: None
s.read_prompt = lambda pane: "Do you want to proceed?\n ❯ 1. Yes\n   2. No"
CALLS, OUT, STILL = [], {}, {"v": False}


def entries(t):
    p = MEM / s.hermes_memory.TARGETS[t]
    return s.hermes_memory.parse(p.read_bytes()) if p.exists() else []


def fake_mem(container, mode, req=None, wait=90, env=None):
    """コンテナの台本の代わり（記憶のファイルを Hermes と同じ区切りで書く）。"""
    CALLS.append((mode, dict(req or {})))
    if mode in OUT:
        return dict(OUT[mode])
    if mode == "limits":
        return {"result": "limits", "memory_enabled": True, "user_profile_enabled": True, "memory_char_limit": 2200, "user_char_limit": 1375}
    t = req["target"]
    es = entries(t)
    if mode == "add":
        es.append(req["content"].strip())
    elif req["entry"] not in es:
        return {"result": "changed", "why": "その項目は、いまの記憶にありません"}
    elif mode == "edit":
        es[es.index(req["entry"])] = req["content"].strip()
    else:
        es.remove(req["entry"])
    (MEM / s.hermes_memory.TARGETS[t]).write_text(s.hermes_memory.DELIM.join(es))
    return {"result": "applied", "shape": ""}


s.hermes_memory.run_script = fake_mem
s.hermes_memory.still_running = lambda c: STILL["v"]
XSS = "<img src=x onerror=\"window.__xss=1\">タグは文字のまま"


def seed():
    (MEM / "USER.md").write_text(s.hermes_memory.DELIM.join(["利用者の名前は山田", XSS]))
    (MEM / "MEMORY.md").write_text("週次定例は情報交換の場")
    s.HMEM_FILE.unlink(missing_ok=True)
    s.HMEM_SHOWN.clear()
    s.HMEM_LIMITS.update(at=0.0, value=None, why="")
    CALLS.clear()
    OUT.clear()
    STILL["v"] = False
    now = time.time()
    t = {"id": "t1", "title": "話題", "now": "いま", "next": "次", "folder": "shop", "until": "", "moved": time.strftime("%Y-%m-%d"),
         "sources": [{"kind": "plaud", "id": "p1", "at": time.strftime("%Y-%m-%dT%H:%M+09:00"), "label": "録音"}]}
    s.write_json(s.SITUATION_FILE, {"topics": [t], "faded": [], "seen": {}, "last_ok": now - 600, "next_id": 2, "proposals": {
        "next": 2, "open": [{"n": 1, "v": "v1", "topic": "t1", "folder": "shop", "text": "ひな形を作ってください", "why": "金曜まで", "sources": ["p1"],
                             "basis": s.topic_basis(t), "at": now, "expires": now + 86400, "state": "open"}], "closed": [], "used": {}}})
    s.write_json(s.FACTS_FILE, {"next": 1, "facts": []})


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


def box(page, sel):
    b = page.locator(sel).bounding_box()
    return b and (round(b["x"]), round(b["width"]))


def visible(page, sel):
    return page.locator(sel).is_visible()


def mem_text(page):
    return page.text_content("#memPanel") or ""


def open_page(page, width):
    page.set_viewport_size({"width": width, "height": 900 if width >= 1100 else 844})
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")


def run_wide(page, who):
    seed()
    open_page(page, 1400)
    check(f"{who} 1400px で PC の形になる", page.evaluate("document.body.classList.contains('wide')"), True)
    page.wait_for_selector("#memPanel .mem")
    side, log, rec = box(page, "#side"), box(page, "#log"), box(page, "#rec")
    check(f"{who} 3 列（1400px で左 280px＝幅の 2 割・真ん中・右 364px＝2.6 割が横に並ぶ）", (side[1], rec[1], side[0] < log[0] < rec[0]), (280, 364, True))
    check(f"{who} 頼んだ件は左の列に入って常に出る・状況は右の列に入る",
          (page.evaluate("document.getElementById('tasks').parentElement.id"), visible(page, "#tasks"), page.evaluate("document.getElementById('situation').parentElement.id")),
          ("side", True, "rec"))
    check(f"{who} 💡 📋 のボタンと ✕ は隠す", [visible(page, x) for x in ("#sitBtn", "#tasksBtn", "#tasksClose", "#sitClose")], [False] * 4)
    check(f"{who} 横にはみ出さない", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), True)
    check(f"{who} PC でも 話す・停止・文字の欄・送る・声・🔇・↻ が出ている", [visible(page, x) for x in ("#talk", "#stop", "#typed", "#send", "#speaker", "#mute", "#reload")], [True] * 7)

    # タブ
    check(f"{who} 右は「記憶」が先に開く（状況のタブに、提案の数が出る）", (visible(page, "#memPanel"), visible(page, "#situation"), wait(lambda: page.text_content("#sitCount") == "1")),
          (True, False, True))
    page.click("#tabSit")
    check(f"{who} 「状況」を押すと、今の 💡 の中身（提案・覚えていること）が出る", (visible(page, "#situation"), visible(page, "#memPanel"), wait(lambda: "ひな形を作ってください" in (page.text_content("#sitBody") or ""))),
          (True, False, True))
    page.click("#tabMem")

    # セッションの一覧
    names = lambda: page.eval_on_selector_all("#sessions .sess", "els => els.map(e => [e.querySelector('.nm').textContent, e.querySelector('.st').textContent, e.classList.contains('on')])")
    check(f"{who} 左の一覧: ハブが先頭で選ばれていて、承認待ちのセッションはそう出る", wait(lambda: len(names()) == 3) and names(),
          [["ハブ（Hermes）", "待機", True], ["blog｜記事", "待機", False], ["shop｜受注", "承認・入力待ち", False]])
    page.locator("#sessions .sess").nth(1).click()
    check(f"{who} 一覧で押すと話し相手が替わる（select も同じ相手）", (page.evaluate("document.getElementById('agent').value"), wait(lambda: names()[1][2])), ("w1:p1", True))
    wait(lambda: not page.locator("#send").is_disabled())
    page.evaluate("(() => { window.__sent = []; const o = ws.send.bind(ws); ws.send = m => { window.__sent.push(m); if (typeof m !== 'string' || JSON.parse(m).type !== 'text') o(m); }; })()")
    page.fill("#typed", "PC から送る文")
    page.press("#typed", "Enter")
    sent = page.evaluate("window.__sent.map(m => JSON.parse(m)).filter(m => m.type === 'text')")
    check(f"{who} 文字の欄で Enter を押すと、一覧で選んだ相手に送る", [(m["pane"], m["text"]) for m in sent], [("w1:p1", "PC から送る文")])
    page.locator("#sessions .sess").nth(0).click()

    # 記憶の一覧
    text = mem_text(page)
    check(f"{who} 記憶: ユーザーについて・メモの 2 つと、字数／上限", ("ユーザーについて（USER.md）" in text, "メモ（MEMORY.md）" in text,
                                                      f"{len(s.hermes_memory.DELIM.join(['利用者の名前は山田', XSS]))} / 1375 字" in text, "利用者の名前は山田" in text),
          (True, True, True, True))
    check(f"{who} 記憶の文は文字として出す（タグを解釈しない）", (XSS in text, page.evaluate("window.__xss || 0")), (True, 0))
    check(f"{who} 記憶のタブにも「ハブの会話を新しくする」がある", "ハブの会話を新しくする" in text, True)

    # 足す
    page.fill("#memPanel .memAdd textarea", "録音の「山だ」は山田のこと")
    page.click("#memPanel .memAdd button")
    check(f"{who} 足す: ユーザーについてに足され、案内が出て、欄が空になる",
          (wait(lambda: "足しました" in mem_text(page)), entries("user")[-1], page.input_value("#memPanel .memAdd textarea")),
          (True, "録音の「山だ」は山田のこと", ""))

    # 直す（取り直しても途中が残る）
    card = lambda i: page.locator("#memPanel .memT").nth(0).locator(".mem").nth(i)
    card(0).locator("button", has_text="直す").click()
    card(0).locator("textarea").fill("利用者の名前は山田（やまだ）")
    page.evaluate("loadMemory()")
    time.sleep(0.5)
    check(f"{who} 直している途中に取り直しても、文の欄と打った文は残る", card(0).locator("textarea").input_value(), "利用者の名前は山田（やまだ）")
    page.click("#tabSit")
    page.click("#tabMem")
    check(f"{who} タブを替えて戻っても、直している途中は残る", card(0).locator("textarea").input_value(), "利用者の名前は山田（やまだ）")
    card(0).locator("button", has_text="保存").click()
    check(f"{who} 直す: 項目が置き換わり、案内が出る", (wait(lambda: "直しました" in mem_text(page)), entries("user")[0]), (True, "利用者の名前は山田（やまだ）"))

    # 直す途中に、ほかの欄を打ったりタブを往復したり、二つ目を直し始めたりしても、直す途中は消えない
    card(0).locator("button", has_text="直す").click()
    card(0).locator("textarea").fill("保存前の編集内容")
    page.fill("#memPanel .memAdd textarea", "別の追加下書き")
    page.click("#tabSit")
    page.click("#tabMem")
    check(f"{who} 直す途中に足す欄を打ってタブを往復しても、直す途中と足す欄の打ちかけは残る",
          (wait(lambda: card(0).locator("textarea").count() == 1), card(0).locator("textarea").input_value(), page.input_value("#memPanel .memAdd textarea")),
          (True, "保存前の編集内容", "別の追加下書き"))
    card(2).locator("button", has_text="直す").click()
    card(2).locator("textarea").fill("二つ目の編集")
    card(1).locator("button", has_text="消す").click()
    page.evaluate("document.activeElement.blur(); loadMemory()")
    time.sleep(0.6)
    check(f"{who} 二つを直す途中と、別の項目の消すの確かめが、取り直しのあとも残る",
          (card(0).locator("textarea").input_value(), card(2).locator("textarea").input_value(), card(1).locator("button.sure").count()),
          ("保存前の編集内容", "二つ目の編集", 1))
    card(0).locator("button", has_text="やめる").click()
    card(2).locator("button", has_text="やめる").click()
    page.fill("#memPanel .memAdd textarea", "")
    check(f"{who} やめると直す途中を閉じ、記憶は変わらない", (page.locator("#memPanel .mem textarea").count(), entries("user")[0]), (0, "利用者の名前は山田（やまだ）"))
    page.evaluate("memConfirm.clear(); loadMemory()")
    time.sleep(0.4)

    # 直す途中に、ファイルが一時読めなくなっても（Codex の再現）、文は残り、読めるように戻れば元の直す途中に戻る
    card(0).locator("button", has_text="直す").click()
    card(0).locator("textarea").fill("保存前の編集内容")
    raw = (MEM / "USER.md").read_bytes()
    (MEM / "USER.md").write_bytes(b"\xff\xfe broken")
    page.click("#tabSit")
    page.click("#tabMem")
    orphan = page.locator("#memPanel .mem", has_text="見当たりません")
    check(f"{who} 直す途中の項目が読めなくなっても、文は「見当たりません」のカードに残る（捨てない）",
          (wait(lambda: "このファイルは読めません" in mem_text(page) and orphan.count() == 1), orphan.locator("textarea").input_value() if orphan.count() else None),
          (True, "保存前の編集内容"))
    (MEM / "USER.md").write_bytes(raw)
    page.evaluate("loadMemory()")
    check(f"{who} 読めるように戻れば、元の項目の直す途中に戻る", (wait(lambda: card(0).locator("textarea").count() == 1), card(0).locator("textarea").input_value(), orphan.count()),
          (True, "保存前の編集内容", 0))
    card(0).locator("button", has_text="やめる").click()
    (MEM / "MEMORY.md").write_text("週次定例は情報交換の場")
    page.locator("#memPanel .memT").nth(1).locator(".mem").nth(0).locator("button", has_text="直す").click()
    page.locator("#memPanel .memT").nth(1).locator(".mem textarea").fill("Hermes に先に書き換えられる")
    (MEM / "MEMORY.md").write_text("週次定例は情報交換の場（Hermes が直した）")
    page.evaluate("document.activeElement.blur(); loadMemory()")
    check(f"{who} 直す途中の項目を Hermes が書き換えたら、文は残して「新しい項目として足す」「捨てる」を出す",
          (wait(lambda: orphan.count() == 1), orphan.locator("textarea").input_value() if orphan.count() else None, [b.text_content() for b in orphan.locator("button").all()]),
          (True, "Hermes に先に書き換えられる", ["新しい項目として足す", "捨てる"]))
    orphan.locator("button", has_text="捨てる").click()
    check(f"{who} 捨てると、カードが消え、記憶は Hermes の直したまま", (wait(lambda: orphan.count() == 0), entries("memory")), (True, ["週次定例は情報交換の場（Hermes が直した）"]))

    # 消す（2 段）
    card(1).locator("button", has_text="消す").click()
    check(f"{who} 消すの 1 回目は確かめを出すだけ（消さない）", (card(1).locator("button.sure").text_content(), XSS in entries("user")), ("この項目を消す", True))
    page.evaluate("loadMemory()")
    time.sleep(0.4)
    check(f"{who} 確かめの途中に取り直しても、確かめは消えない", card(1).locator("button.sure").count(), 1)
    card(1).locator("button.sure").click()
    check(f"{who} 消すの 2 回目で消える", (wait(lambda: "消しました" in mem_text(page)), XSS in entries("user")), (True, False))

    # 当たったか分からない → 赤い案内、次は確かめてから
    OUT["add"] = {"result": "timeout", "why": "90 秒待っても終わりませんでした"}
    page.fill("#memPanel .memAdd textarea", "時間切れになる文")
    page.click("#memPanel .memAdd button")
    check(f"{who} 当たったか分からないときは赤で出し、打った文は欄に残す（自動では送り直さない）",
          (wait(lambda: page.locator("#memNote.bad").count() == 1 and "当たったか分かりません" in page.text_content("#memNote")),
           page.input_value("#memPanel .memAdd textarea"), [c[0] for c in CALLS].count("add")), (True, "時間切れになる文", 2))
    OUT.pop("add")
    STILL["v"] = None
    page.click("#memPanel .memAdd button")
    check(f"{who} 前の書き込みが終わったか確かめられない間は、次の書き込みを断る",
          (wait(lambda: "確かめられないので、書きません" in (page.text_content("#memNote") or "")), [c[0] for c in CALLS].count("add")), (True, 2))
    STILL["v"] = False
    page.click("#memPanel .memAdd button")
    check(f"{who} 前の台本がいないと確かめられれば、書き込める", (wait(lambda: "足しました" in mem_text(page)), entries("user")[-1]), (True, "時間切れになる文"))

    # 幅の境
    page.set_viewport_size({"width": 1099, "height": 900})
    check(f"{who} 1099px ではスマホの形（3 列にしない）", wait(lambda: not page.evaluate("document.body.classList.contains('wide')")), True)
    page.set_viewport_size({"width": 1100, "height": 900})
    check(f"{who} 1100px で PC の形に戻る（はみ出さない）", (wait(lambda: page.evaluate("document.body.classList.contains('wide')")),
                                                page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")), (True, True))


LONG = "長い記憶の項目です。" * 40


def run_design(page, who):
    """PC の見た目の作り（色の帯・切り方・開いたままにすること・直す・消すの出し方）"""
    seed()
    (MEM / "USER.md").write_text(s.hermes_memory.DELIM.join(["利用者の名前は山田", LONG]))
    now = time.time()
    s.TASKS.clear()
    short = "更新内容と影響範囲を確認してからレビューの結果を報告してください。"  # 文は短いが、種別と時刻が付くと 2 行からはみ出す
    for i, (st, text, age, kind) in enumerate([("blocked", "承認待ちの指示。" * 12, 5, "send"), ("watching", "短い指示", 10, "send"), ("done", "終わった指示。" * 12, 60, "send"),
                                               ("done", short, 70, "answer"), ("done", "漢" * 25, 80, "answer"), ("lost", "切れた指示", 90, "send")]):
        s.TASKS[f"d{i}"] = {"id": f"d{i}", "kind": kind, "pane": "w1:p1", "project": "blog", "title": "記事", "text": text,
                            "started": now - age * 60, "updated": now - age * 60, "status": st, "note": ""}
    s.hermes_log("ai", "【全体】本文の一行目\n【次】二行目\nふつうの行【途中】")
    open_page(page, 1400)
    page.wait_for_selector("#taskList .task")
    groups = page.eval_on_selector_all("#taskList .tgh, #taskList .tgs", "els => els.map(e => e.textContent)")
    lanes = page.eval_on_selector_all("#taskList .task", "els => els.map(e => [e.dataset.lane || '', e.classList.contains('ended')])")
    check(f"{who} 頼んだ件は「動いている」「終わった」に分け、状態の色の帯を付ける", (groups, lanes),
          (["頼んだ件6 件", "動いている　2", "終わった（3日で一覧から消えます）　4"], [["wait", False], ["run", False], ["done", True], ["done", True], ["done", True], ["bad", True]]))
    first = page.locator("#taskList .task").nth(0)
    h2 = lambda: first.locator(".what").evaluate("e => [e.getBoundingClientRect().height, parseFloat(getComputedStyle(e).lineHeight)]")
    hh, lh = h2()
    check(f"{who} 長い指示の文は 2 行で切る", hh <= lh * 2 + 1, True)
    first.click()
    check(f"{who} 押すと全文を出す（aria-expanded も替わる）", (h2()[0] > lh * 2 + 1, first.get_attribute("aria-expanded")), (True, "true"))
    page.evaluate("loadTasks()")
    page.wait_for_function("document.querySelector('#taskList .task').getAttribute('aria-expanded') === 'true'")
    first = page.locator("#taskList .task").nth(0)
    check(f"{who} 取り直しても開いたまま（全文のまま）", (first.get_attribute("aria-expanded"), "open" in first.get_attribute("class"), h2()[0] > lh * 2 + 1), ("true", True, True))
    page.locator("#taskList .task").nth(0).focus()
    page.keyboard.press("Enter")
    check(f"{who} キーの Enter でも閉じられる", page.locator("#taskList .task").nth(0).get_attribute("aria-expanded"), "false")
    check(f"{who} 2 行に収まる指示は押して開くものにしない", page.locator("#taskList .task").nth(1).get_attribute("role"), None)
    for width in (1400, 1100):  # 文は短くても、2 行からはみ出す件は開ける（列の幅が狭いときも）
        page.set_viewport_size({"width": width, "height": 900})
        page.evaluate("loadTasks()")
        able = wait(lambda: page.evaluate("document.querySelectorAll('#taskList .task').length === 6 && document.querySelectorAll('#taskList .task')[3].getAttribute('role') === 'button'"), 4)
        nth = page.locator("#taskList .task").nth(3)
        cut = nth.locator(".what").evaluate("e => e.scrollHeight > e.clientHeight + 2")
        nth.click()
        whole = nth.locator(".what").evaluate("e => e.scrollHeight <= e.clientHeight + 2 && e.textContent.includes('報告してください。')")
        check(f"{who} {width}px: 文は短くても 2 行からはみ出す件は、押すと全文が読める", (able, cut, whole), (True, True, True))
        if whole:
            nth.click()
    # 幅を変えた直後（取り直しを挟まない）。広い幅で 2 行に収まっていた件が、狭めて切れたら、すぐ押して開ける。広げて収まれば押せるものから外す
    fit = lambda: page.locator("#taskList .task").nth(4).locator(".what").evaluate("e => e.scrollHeight <= e.clientHeight + 2")
    role = lambda: page.locator("#taskList .task").nth(4).get_attribute("role")
    page.set_viewport_size({"width": 1920, "height": 900})
    page.evaluate("loadTasks()")
    wide_ok = wait(lambda: page.evaluate("document.querySelectorAll('#taskList .task').length === 6") and fit() and role() is None, 4)
    page.evaluate("clearInterval(tasksTimer)")  # 10 秒ごとの取り直しを止め、取り直しで直ったのではないことを見る
    page.set_viewport_size({"width": 1100, "height": 900})
    narrow_ok = wait(lambda: not fit() and role() == "button", 4)
    if narrow_ok:
        page.locator("#taskList .task").nth(4).click()
    opened = fit() and page.locator("#taskList .task").nth(4).get_attribute("aria-expanded") == "true"
    check(f"{who} 幅を変えた直後: 1920px で収まっていた件を取り直さずに 1100px へ狭めると、すぐ押して全文を開ける", (wide_ok, narrow_ok, opened), (True, True, True))
    page.locator("#taskList .task").nth(4).click()
    page.set_viewport_size({"width": 1920, "height": 900})
    check(f"{who} 幅を戻して収まれば、押せるものから外す", wait(lambda: fit() and role() is None, 4), True)
    page.set_viewport_size({"width": 1400, "height": 900})
    page.evaluate("runTasks(true)")

    wait(lambda: page.locator("#log .msg.ai .lead").count() > 0)
    leads = page.eval_on_selector_all("#log .msg.ai .lead", "els => els.map(e => [e.textContent, getComputedStyle(e).fontWeight])")
    check(f"{who} 返事の行頭の【見出し】だけを太くする（行の途中のものはそのまま・文は変えない）",
          (leads, "【全体】本文の一行目" in (page.text_content("#log .msg.ai") or "")), ([["【全体】", "700"], ["【次】", "700"]], True))

    page.wait_for_selector("#memPanel .mem")
    d = page.evaluate("fetch('/api/hermes/memory').then(r => r.json())")["targets"]["user"]
    width = page.locator("#memPanel .memT").nth(0).locator(".meter i").evaluate("e => e.style.width")
    check(f"{who} 上限との棒は、字数と上限の比のとおり", float(width.rstrip("%")), round(min(1, d["chars"] / d["limit"]) * 100, 1))
    more = lambda: page.eval_on_selector_all("#memPanel .mem > .more", "els => els.map(e => !e.hidden)")
    check(f"{who} 「続きを表示」は、4 行に収まらない項目にだけ出る", wait(lambda: more() == [False, True, False]), True)
    page.locator("#memPanel .mem > .more").nth(1).click()
    check(f"{who} 押すと全文になり、取り直しても開いたまま",
          (wait(lambda: page.locator("#memPanel .mem > .more").nth(1).text_content() == "たたむ"), page.evaluate("loadMemory().then(() => document.querySelectorAll('#memPanel .text.clamp').length)")),
          (True, 2))
    page.mouse.move(700, 450)
    acts = lambda i: page.locator("#memPanel .memT").nth(0).locator(".mem").nth(i).locator(".acts").evaluate("e => getComputedStyle(e).opacity")
    check(f"{who} 直す・消すは、マウスを載せていない項目には出さない", acts(0), "0")
    page.locator("#memPanel .memT").nth(0).locator(".mem").nth(0).hover()
    check(f"{who} マウスを載せた項目には出す", acts(0), "1")
    page.locator("#memPanel .memT").nth(0).locator(".mem").nth(0).locator("button", has_text="消す").click()
    page.evaluate("document.activeElement && document.activeElement.blur()")  # フォーカスで出ているのではないことを見る
    page.mouse.move(700, 450)
    check(f"{who} 消すの確かめ中は、マウスを外しても出したまま", (acts(0), page.locator("#memPanel .mem button.sure").count()), ("1", 1))
    page.locator("#memPanel .memT").nth(0).locator(".mem").nth(1).locator("button", has_text="直す").focus()
    check(f"{who} キーで移った項目にも出す", acts(1), "1")

    t, a, k, st = box(page, "#typed"), page.locator("#agent").bounding_box(), page.locator("#talk").bounding_box(), page.locator("#stop").bounding_box()
    check(f"{who} 入力は文字の欄が上、話す・停止は相手と声と同じ行に小さく",
          (page.locator("#typed").bounding_box()["y"] < a["y"], round(k["y"]) == round(a["y"]) == round(st["y"]), round(k["height"])), (True, True, 36))
    s.TASKS.clear()


def run_phone(page, who):
    seed()
    open_page(page, 390)
    order = "[...document.body.children].map(e => e.id).filter(Boolean).join(',')"
    check(f"{who} スマホの幅では今の形（3 列にしない・PC の箱は出さない・並びは今のまま）",
          (page.evaluate("document.body.classList.contains('wide')"), visible(page, "#side"), visible(page, "#rec"), page.evaluate(order)),
          (False, False, False, "side,rec,asks,tasks,situation,log,bar"))
    check(f"{who} スマホでは 💡 📋 が出て、どちらも閉じている", (visible(page, "#sitBtn"), visible(page, "#tasksBtn"), visible(page, "#situation"), visible(page, "#tasks")),
          (True, True, False, False))
    # PC にして、戻す（行き来）
    for _ in range(2):
        page.set_viewport_size({"width": 1400, "height": 900})
        wait(lambda: page.evaluate("document.body.classList.contains('wide')"))
        page.set_viewport_size({"width": 390, "height": 844})
        wait(lambda: not page.evaluate("document.body.classList.contains('wide')"))
    check(f"{who} PC にして戻すと、元の並びと、閉じた状態に戻る", (page.evaluate(order), visible(page, "#situation"), visible(page, "#tasks")),
          ("side,rec,asks,tasks,situation,log,bar", False, False))
    page.click("#sitBtn")
    check(f"{who} 💡 で状況が開く", wait(lambda: visible(page, "#situation") and "ひな形を作ってください" in (page.text_content("#sitBody") or "")), True)
    page.click("#tasksBtn")
    check(f"{who} 📋 を押すと状況は閉じて頼んだ件が開く（今の相互の開閉）", (visible(page, "#tasks"), visible(page, "#situation")), (True, False))
    page.click("#sitBtn")
    page.fill("#factText", "スマホから覚える")
    page.press("#factText", "Enter")
    check(f"{who} スマホに戻したあとも、覚えていることを足せる", wait(lambda: "スマホから覚える" in (page.text_content("#sitBody") or "")), True)
    page.locator("#sitBody .fact button").first.click()
    check(f"{who} …そして × で忘れられる", wait(lambda: "スマホから覚える" not in (page.text_content("#sitBody") or "")), True)
    page.set_viewport_size({"width": 1400, "height": 900})
    check(f"{who} スマホで 💡 を開いたまま PC にしても、3 列で両方出る", (wait(lambda: page.evaluate("document.body.classList.contains('wide')")),
                                                          visible(page, "#tasks"), page.evaluate("document.getElementById('situation').parentElement.id")), (True, True, "rec"))
    page.set_viewport_size({"width": 390, "height": 844})
    check(f"{who} スマホに戻すと、💡 を開いていた状態に戻る", wait(lambda: visible(page, "#situation") and not visible(page, "#tasks")), True)


with sync_playwright() as pw:
    b = pw.chromium.launch()
    run_wide(b.new_page(), "Chromium:")
    run_design(b.new_page(), "Chromium（見た目）:")
    run_phone(b.new_page(), "Chromium（スマホ）:")
    b.close()
    b = pw.webkit.launch()
    # デスクトップの設定の WebKit は、headless のこの試験では、PC の形と関係なく画面が固まる（main の画面でも同じ。2026-10-26 に確かめた）。
    # モバイルの設定のまま 1400px で開いて、PC の形を見る（実機の Mac の Safari では、入れ替えたあとに目で確かめる）
    run_wide(b.new_context(viewport={"width": 1400, "height": 900}, is_mobile=True, has_touch=True).new_page(), "WebKit（1400px）:")
    ctx = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
    run_phone(ctx.new_page(), "WebKit（スマホ）:")
    b.close()

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
