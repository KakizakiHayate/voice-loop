# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40"]
# ///
"""画面のボタンからの返答（/api/prompt・/api/answer）の試験。止まっている画面を作り物にし、送るキーと断る理由を確かめる。
herdr には触れない（一覧・画面の読み取り・キーの送信を差し替える）。使い方: uv run spike/answer_test.py"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state")  # 本物の設定フォルダを読まない
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402

PERMISSION = """⏺ 一覧を見ます。
⏺ Bash(ls -la)
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
QUESTION = """⏺ どちらで進めますか？
 どちらの方式で進めますか？
 ❯ 1. A 案
     速い
   2. B 案
     安全
   3. Type something.
 Enter to select · ↑/↓ to navigate · Esc to cancel"""
UNKNOWN = "⏺ 考えています\n  何か入力してください"

AGENT = {"pane": "w9:p1", "title": "テスト", "cwd": "/tmp/proj", "kind": "claude", "session": "sess-1",
         "status": "blocked", "name": "", "hub": False}
state = {"status": "blocked", "screen": PERMISSION, "trust": False, "sent": []}
s.list_agents = lambda: [{**AGENT, "status": state["status"]}]
s.pane_shows_trust = lambda pane: state["trust"]
s.read_prompt = lambda pane: state["screen"]
s.herdr = lambda *a, **k: (state["sent"].append(a[-1]), {"result": {}})[1] if a[:2] == ("agent", "send-keys") else {"result": {}}


def prompt(key="sess-1"):
    return json.loads(s.api_prompt("w9:p1", key).body)


def answer(**body):
    state["sent"].clear()
    r = json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "sess-1", **body})).body)
    return r, list(state["sent"])


def case(name, **st):
    state.update({"status": "blocked", "screen": PERMISSION, "trust": False, **st})
    return name


results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


# 問いの読み取り
case("承認")
p = prompt()
check("承認の種類", (p["kind"], p["risky"], "ls -la" in p["text"], len(p["fp"])), ("permission", False, True, 40))
check("会話の上の部分は出さない", "一覧を見ます" in p["text"], False)
case("取り消しにくい承認", screen=RISKY)
check("取り消しにくい印", prompt()["risky"], True)
case("質問", screen=QUESTION)
check("質問の種類", prompt()["kind"], "question")
case("止まっていない", status="working")
check("止まっていない", prompt(), {"kind": "none"})
case("信頼", trust=True)
check("フォルダの信頼", prompt(), {"kind": "trust"})
case("読めない", trust=None)
check("画面を読めない", prompt(), {"kind": "unreadable"})
case("入れ替わり")
check("別のセッション", prompt("sess-2"), {"kind": "gone"})

# 返答
case("許可")
fp = prompt()["fp"]
check("許可は 1（今回だけ）", answer(fp=fp, choice="allow"), ({"ok": True, "said": "許可（今回だけ）"}, ["1"]))
check("断るは esc", answer(fp=fp, choice="deny"), ({"ok": True, "said": "断る"}, ["esc"]))
r, sent = answer(fp="0" * 40, choice="allow")
check("画面が変わったら送らない", (r["ok"], r["reason"], sent), (False, "changed", []))
r, sent = answer(fp=fp, choice="always")
check("知らない答えは送らない", (r["reason"], sent), ("bad_choice", []))
state["sent"].clear()
r = json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "sess-2", "fp": fp, "choice": "allow"})).body)
check("別のセッションには送らない", (r["reason"], state["sent"]), ("gone", []))

case("取り消しにくい許可", screen=RISKY)
fp = prompt()["fp"]
r, sent = answer(fp=fp, choice="allow")
check("取り消しにくい許可は、もう一度押すまで送らない", (r["reason"], sent), ("confirm", []))
check("もう一度押したら 1", answer(fp=fp, choice="allow", confirmed=True)[1], ["1"])
check("取り消しにくくても、断るはすぐ", answer(fp=fp, choice="deny")[1], ["esc"])

case("質問への返答", screen=QUESTION)
fp = prompt()["fp"]
r, sent = answer(fp=fp, choice="allow")
check("質問に許可は送らない", (r["reason"], sent), ("bad_choice", []))
check("質問は断れる", answer(fp=fp, choice="deny")[1], ["esc"])

case("わからない画面", screen=UNKNOWN)
fp = prompt()["fp"]
check("わからない画面には何も送らない", [answer(fp=fp, choice=c)[1] for c in ("allow", "deny")], [[], []])

for name, st in (("信頼", {"trust": True}), ("読めない", {"trust": None}), ("止まっていない", {"status": "idle"})):
    case(name, **st)
    r, sent = answer(fp=fp, choice="allow")
    check(f"{name}のときは送らない", (r["ok"], sent), (False, []))

bad = [f"{n}: {g!r}（期待 {w!r}）" for n, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
