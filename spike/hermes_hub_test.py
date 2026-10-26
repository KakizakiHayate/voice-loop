# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40"]
# ///
"""Hermes がハブ（VL_HUB=hermes）のときの守りの試験。
ユーザーの発言と Hermes の返答は本体の台帳（HERMES_TURNS）に載り、承認に答える・閉じるの守りはそれで確かめる。
Hermes の API と herdr には触れない（差し替える）。使い方: uv run spike/hermes_hub_test.py"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402

PERMISSION = """⏺ Bash(ls -la)
  ⎿  Running…
 Bash command
   ls -la
   フォルダの中身を見る
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for ls commands in /Users/me/proj
   3. No, and tell Claude what to do differently (esc)
 Esc to cancel · Tab to amend"""
RISKY = PERMISSION.replace("ls -la", "rm -rf build").replace("ls commands", "rm commands")

TARGET = {"pane": "w9:p1", "title": "テスト", "cwd": "/x/proj", "kind": "claude", "session": "sess-1", "status": "idle", "name": "", "hub": False}
state = {"status": "idle", "screen": PERMISSION, "keys": [], "closed": [], "hermes_said": "わかりました"}
s.list_agents = lambda: [{**TARGET, "status": state["status"]}]
s.pane_shows_trust = lambda pane: False
s.read_prompt = lambda pane: state["screen"]
s.read_lines = lambda *a, **k: []
s.jsonl_path = lambda a: None


def fake_herdr(*a, **k):
    if a[:2] == ("agent", "send-keys"):
        state["keys"].append(a[-1])
    elif a[:2] == ("pane", "close"):
        state["closed"].append(a[2])
    return {"result": {}}


s.herdr = fake_herdr
s.ask_hermes = lambda text: state["hermes_said"]


def say(text: str, reply: str = "わかりました") -> str:
    """ユーザーが声で text と言い、Hermes が reply と答えた（本体の台帳に載る）。"""
    turn = s.hermes_turn_begin(text)
    s.hermes_turn_end(turn, reply)
    return turn


def begin(text: str) -> str:
    """ユーザーが text と言い、Hermes がまだ答えていない（道具を打っている最中）。"""
    return s.hermes_turn_begin(text)


def screen():
    return json.loads(s.hub_screen("w9:p1").body)


def answer(**body):
    state["keys"].clear()
    r = json.loads(asyncio.run(s.hub_answer({"pane": "w9:p1", "answer": "yes", **body})).body)
    return r.get("reason", "ok"), list(state["keys"])


def close(**body):
    state["closed"].clear()
    r = json.loads(asyncio.run(s.hub_close({"pane": "w9:p1", **body})).body)
    return r.get("reason", "ok"), list(state["closed"])


results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


# ---- 一覧とハブの確認
agents = json.loads(s.api_agents().body)
check("一覧の先頭が Hermes で、ハブ扱い・鍵は hub", (agents[0]["pane"], agents[0]["hub"], agents[0]["key"], agents[0]["hermes"]), ("hermes", True, "hub", True))
check("herdr のセッションはハブ扱いでない", [a["hub"] for a in agents[1:]], [False])
s.hermes_problem = lambda: ""
check("ハブの起動は、立てずに使える判定", s.ensure_hub(), {"ok": True, "pane": "hermes", "started": False})
s.hermes_problem = lambda: "止まっています"
check("Hermes が居ないときは理由を返す", s.ensure_hub(), {"ok": False, "reason": "hermes", "detail": "止まっています"})

# ---- 指示文は発言のたびに system として付く（書き換えが次の発言から効く）
s.HERMES_DIR.mkdir(parents=True, exist_ok=True)
(s.HERMES_DIR / "hub.md").write_text("ハブの決まり v1")
check("指示文が system で先頭に付く", s.hermes_messages("やあ"), [{"role": "system", "content": "ハブの決まり v1"}, {"role": "user", "content": "やあ"}])
(s.HERMES_DIR / "hub.md").write_text("ハブの決まり v2")
check("書き換えは次の発言から効く", s.hermes_messages("やあ")[0]["content"], "ハブの決まり v2")
(s.HERMES_DIR / "hub.md").unlink()
check("指示文が無ければ発言だけ", s.hermes_messages("やあ"), [{"role": "user", "content": "やあ"}])

# ---- 台帳
check("発言が無いうちは最後の発言も無い", s.hub_human_turn(), None)
t1 = say("全体どうなってる？", "3つ開いています")
check("台帳から最後の発言", s.hub_human_turn(), t1)
check("発言への返答", s.hub_reply_to(t1), "3つ開いています")
check("新しいものから n 件", s.hub_turns(2), [(t1, "全体どうなってる？")])

# ---- 承認に答える（見たあとの、ユーザーの次の発言でだけ。聞いた文にプロジェクト名と問い、発言は答えの文法）
Q = "proj の「テスト」が、フォルダの中身を見る（ls）許可を求めています。許可しますか？"


def asked_then(reply: str, utter: str) -> str:
    """いまの発言に Hermes が reply と答え、ユーザーが次に utter と言った（Hermes はまだ答えていない）。"""
    s.hermes_turn_end(s.hub_human_turn(), reply)
    return begin(utter)


state.update(status="blocked", screen=PERMISSION)
t2 = begin("テストの承認どうなってる？")
check("見ていない問いには答えない", answer(), ("unseen", []))
sc = screen()
check("画面を見ると問いの種類が分かる", (sc["status"], sc["prompt"]["kind"]), ("blocked", "permission"))
check("見たのと同じ発言の中では答えない", answer(), ("same_turn", []))
asked_then("ls を実行していいか聞いています", "はい、許可して")
check("聞いた返答にプロジェクト名と問いが無ければ答えない", answer(), ("not_asked", []))
asked_then("proj の承認です。ls を実行しようとしています。", "はい")
check("聞いた返答が問いの形でなければ答えない", answer(), ("not_asked", []))
asked_then("ls を実行していいですか？", "はい")
check("聞いた返答にプロジェクト名が無ければ答えない", answer(), ("not_asked", []))
for utter in ("いいえ、だめ", "はい、承認はしません", "うん、いい天気だね", "いいですか？", "許可するな", "ちょっと待って"):
    asked_then(Q, utter)
    check(f"発言が「{utter}」なら「はい」を送らない", answer(), ("not_said", []))
asked_then(Q, "はい、許可して")
check("次の発言で「はい」→ 1 を送る", answer(), ("ok", ["1"]))
s.hermes_turn_end(s.hub_human_turn(), "許可しました")

state.update(screen=RISKY)
begin("承認見て")
screen()
RQ = "proj の「テスト」が、build のフォルダを消す（rm -rf build）許可を求めています。許可しますか？"
asked_then(RQ, "許可")
check("取り消しにくい操作は聞き返す", answer(), ("confirm", []))
check("同じ発言の中の --confirmed は通らない", answer(confirmed=True), ("confirm", []))
asked_then("本当に、build のフォルダを消していいですか？", "はい、許可していい")
check("聞き返しの文にプロジェクト名が無ければ、--confirmed でも通らない", answer(confirmed=True), ("not_asked", []))
asked_then("proj で、本当に build のフォルダを消していいですか？", "やっぱりやめて")
check("聞き返しのあとの発言が断りなら、--confirmed でも通らない", answer(confirmed=True), ("not_said", []))
asked_then("proj で、本当に build のフォルダを消していいですか？", "はい、許可していい")
check("次の発言の --confirmed で通る", answer(confirmed=True), ("ok", ["1"]))
s.hermes_turn_end(s.hub_human_turn(), "許可しました")
begin("見て")
screen()
begin("だめ")
check("断るは esc", answer(answer="no"), ("ok", ["esc"]))
state.update(screen=PERMISSION)
begin("ほかの話")  # 新しい問いを、ハブはまだ見ていない
check("断る（いいえ）は、問いを見る前でも送れる（決めた範囲。「はい」は見たあとの発言のあとだけ）", (answer(answer="no"), answer()), (("ok", ["esc"]), ("unseen", [])))

# ---- 選択肢のある質問: 番号は、発言がその番号を選ぶ答えのときだけ
QUESTION = """⏺ AskUserQuestion
 どちらで書きますか？
 ❯ 1. Markdown
   2. HTML
   3. Type something.
 Esc to cancel"""
state.update(screen=QUESTION)
s.SEEN_PROMPT.clear()
begin("質問なんだっけ")
screen()
CQ = "proj の「テスト」から、どちらで書くか聞かれています。1番は Markdown、2番は HTML です。何番にしますか？"
for utter, want in (("2時間かかるかな", "not_said"), ("一番いい天気だね", "not_said"), ("2番じゃなくて1番", "not_said"), ("1番と2番", "not_said"),
                    ("Markdown の話を聞かせて", "not_said")):
    asked_then(CQ, utter)
    check(f"発言が「{utter}」なら 2 番は送らない", answer(answer="2"), (want, []))
asked_then(CQ, "じゃあ2番で")
check("「じゃあ2番で」なら 2 を送る", answer(answer="2"), ("ok", ["2"]))
s.hermes_turn_end(s.hub_human_turn(), "2番と伝えました")
begin("質問もう一度")
screen()
asked_then(CQ, "HTML にして")
check("選択肢の文言で選んでも、その番号だけ", (answer(answer="1"), answer(answer="2")), (("not_said", []), ("ok", ["2"])))
s.hermes_turn_end(s.hub_human_turn(), "2番と伝えました")

# ---- 直前に聞いた返答（heard）は、本体が受け取ったハブの最後の出力。記録からは取らない
state.update(screen=PERMISSION)
s.SEEN_PROMPT.clear()
begin("承認の件")
screen()
s.hermes_turn_end(s.hub_human_turn(), Q)


async def report_reply(text):
    return "blog の件が終わりました"


s.ask_hermes = lambda text: "blog の件が終わりました"
asyncio.run(s.report({"pane": "w8:p1", "cwd": "/x/blog", "project": "blog", "title": "記事", "text": "書いて", "offset": 0, "was_working": False, "gen": 1}, "done"))
begin("はい")
check("問いのあとに別件の報告をはさむと、その報告が直前に聞いた文になり、「はい」は送らない", answer(), ("not_asked", []))
s.hermes_turn_end(s.hub_human_turn(), Q)
real_log = s.hermes_log
s.hermes_log = lambda role, text, **extra: False if role == "ai" and extra.get("report") else real_log(role, text, **extra)
asyncio.run(s.report({"pane": "w8:p1", "cwd": "/x/blog", "project": "blog", "title": "記事", "text": "書いて", "offset": 0, "was_working": False, "gen": 1}, "done"))
s.hermes_log = real_log
begin("はい")
check("報告を記録に書けなかったときも、その報告が直前に聞いた文（古い問いを拾わない）", answer(), ("not_asked", []))
s.hermes_turn_end(s.hub_human_turn(), Q)
s.ask_hermes = lambda text: ""
asyncio.run(s.report({"pane": "w8:p1", "cwd": "/x/blog", "project": "blog", "title": "記事", "text": "書いて", "offset": 0, "was_working": False, "gen": 1}, "done"))
begin("はい")
check("報告を受け取れなかったとき（本体の失敗の知らせを聞いた）は、古い問いを拾わない", answer(), ("not_asked", []))
s.hermes_turn_end(s.hub_human_turn(), Q)


def broken(text):
    raise RuntimeError("Hermes に届きません")


s.ask_hermes = broken
asyncio.run(s.hermes_exchange("ほかの話"))
begin("はい")
check("返事を受け取れなかった発言のあと（失敗の知らせを聞いた）は、古い問いを拾わない", answer(), ("not_asked", []))
s.ask_hermes = lambda text: state["hermes_said"]
s.hermes_turn_end(s.hub_human_turn(), Q)
s.HERMES_HEARD["text"] = ""  # 本体を起動し直した直後にあたる（記録から戻した行には写しが無い）
for row in s.HERMES_TURNS:
    row[3] = None
begin("はい")
check("起動し直した直後の発言では答えない（聞き直してもらう）", answer(), ("not_asked", []))
s.hermes_turn_end(s.hub_human_turn(), Q)
begin("はい、許可して")
check("聞き直したあとの発言なら送る", answer(), ("ok", ["1"]))
s.hermes_turn_end(s.hub_human_turn(), "許可しました")

# ---- 閉じる（聞いた発言の、すぐ次の発言のはっきりした許可でだけ）
state.update(status="idle")
s.CLOSING.clear()
begin("テストのセッションを閉じて")
check("1回目は閉じない", close(), ("confirm", []))
check("同じ発言の中の --confirmed は閉じない", close(confirmed=True), ("same_turn", []))
s.hermes_turn_end(s.hub_human_turn(), "proj の「テスト」のセッションを閉じていいですか。")
say("はい、お願い")  # 返答はまだ（Hermes が --confirmed を打つ前）
check("次の発言の許可で閉じる", close(confirmed=True), ("ok", ["w9:p1"]))

s.CLOSING.clear()
begin("閉じて")
close()
s.hermes_turn_end(s.hub_human_turn(), "proj の「テスト」のセッションを閉じていいですか。")
begin("閉じないで")
check("断りの語があれば閉じない", close(confirmed=True), ("not_yes", []))

s.CLOSING.clear()
begin("閉じて")
close()
s.hermes_turn_end(s.hub_human_turn(), "閉じると何が起きるか説明しましょうか。")
begin("はい")
check("聞いたときの返答に問いが無ければ閉じない", close(confirmed=True), ("not_asked", []))

s.CLOSING.clear()
begin("閉じて")
close()
s.hermes_turn_end(s.hub_human_turn(), "proj の「テスト」のセッションを閉じていいですか。")
say("ところで blog はどう？", "順調です")
begin("はい")
check("あいだに別の発言をはさんだら聞き直し", close(confirmed=True), ("confirm", []))

# ---- 自動通知（通知は台帳に載らず、Hermes の報告が声になる。応答中は操作が働かない）
seen = {}


async def fake_ask_hermes_report(text):
    seen["note"] = text
    seen["reporting"] = s.REPORTING
    seen["close_during"] = json.loads((await s.hub_close({"pane": "w9:p1", "confirmed": True})).body).get("reason")
    return "テストの件が終わりました"


s.ask_hermes = lambda text: asyncio.run(fake_ask_hermes_report(text))
before = len(s.HERMES_TURNS)
asyncio.run(s.report({"pane": "w9:p1", "cwd": "/x/proj", "project": "proj", "title": "テスト", "text": "テストして", "offset": 0, "was_working": False, "gen": 1}, "done"))
check("通知は Hermes に届き、自動通知の目印が付く", seen.get("note", "").startswith(s.NOTE_PREFIX), True)
check("応答中は REPORTING で、閉じるは働かない", (seen.get("reporting"), seen.get("close_during")), (True, "reporting"))
check("通知は台帳（ユーザーの発言）に載らない", len(s.HERMES_TURNS), before)
check("応答が終わると REPORTING は戻る", s.REPORTING, False)

bad = [f"{n}: {g!r}（期待 {w!r}）" for n, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
