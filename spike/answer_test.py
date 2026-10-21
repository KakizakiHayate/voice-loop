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
# 2026-10-21 に本物の Claude Code で取った画面と同じ形（場所だけ架空に置き換えた）
REAL_WRITE = """⏺ Write(/Users/me/proj/shape.txt)
 Create file
 ../shape.txt
  1 ok
 Do you want to create shape.txt?
 ❯ 1. Yes
   2. Yes, and switch to accept edits (auto-approve file edits and common file commands) for this session; Yes,
      and always allow access to
      /Users/me/proj for this
      session (shift+tab)
   3. No
 Esc to cancel · Tab to amend"""
REAL_YESNO = """⏺ Write(/Users/me/proj/shape.txt)
  ⎿  User rejected write to /Users/me/proj/shape.txt
 ☐ 確認
続けますか？
❯ 1. Yes
     続けて作成を進める
  2. No
     作成を中止する
  3. Type something.
  4. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""
YESNO_SHORT = """⏺ どうしますか
 続けますか？
 ❯ 1. Yes
   2. No
   3. Type something.
 Enter to select · ↑/↓ to navigate · Esc to cancel"""  # Codex の再現と同じ形

AGENT = {"pane": "w9:p1", "title": "テスト", "cwd": "/tmp/proj", "kind": "claude", "session": "sess-1",
         "status": "blocked", "name": "", "hub": False}
state = {"status": "blocked", "screen": PERMISSION, "trust": False, "sent": []}
s.list_agents = lambda: [{**AGENT, "status": state["status"]}]
s.pane_shows_trust = lambda pane: state["trust"]
s.read_prompt = lambda pane: state["screen"]
s.herdr = lambda *a, **k: (state["sent"].append(a[-1]), {"result": {}})[1] if a[:2] == ("agent", "send-keys") else {"result": {}}


def prompt(key="sess-1"):
    return s._asked("w9:p1", key)[1]


def answer(keep=False, **body):
    """keep=False なら、前に答えた記録を消してから送る（答えたあと相手の画面が変わった、の代わり）。"""
    state["sent"].clear()
    if not keep:
        s.ANSWERED.clear()
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
for name, key in (("別のセッション", "sess-2"), ("key が空", ""), ("key が無い", None)):
    state["sent"].clear()
    s.ANSWERED.clear()
    req = {"pane": "w9:p1", "fp": fp, "choice": "allow", **({} if key is None else {"key": key})}
    r = json.loads(asyncio.run(s.api_answer(req)).body)
    check(f"{name}には送らない", (r["reason"], state["sent"]), ("gone", []))
check("問いの読み取りも key が要る", prompt(""), {"kind": "gone"})

# 二度押し・重ねた送信
answer(fp=fp, choice="allow")
r, sent = answer(keep=True, fp=fp, choice="allow")
check("同じ問いへの二度目は送らない", (r["reason"], sent), ("answered", []))
r, sent = answer(keep=True, fp=fp, choice="deny")
check("同じ問いに、続けて断るも送らない", (r["reason"], sent), ("answered", []))


async def both():
    return await asyncio.gather(*(s.api_answer({"pane": "w9:p1", "key": "sess-1", "fp": fp, "choice": "allow"}) for _ in range(2)))
state["sent"].clear()
s.ANSWERED.clear()
rs = sorted(json.loads(r.body).get("reason", "ok") for r in asyncio.run(both()))
check("同時に2つ届いても、送るのは1回", (rs, state["sent"]), (["answered", "ok"], ["1"]))

case("取り消しにくい許可", screen=RISKY)
fp = prompt()["fp"]
r, sent = answer(fp=fp, choice="allow")
check("取り消しにくい許可は、もう一度押すまで送らない", (r["reason"], sent), ("confirm", []))
check("もう一度押したら 1", answer(fp=fp, choice="allow", confirmed=True)[1], ["1"])
check("取り消しにくくても、断るはすぐ", answer(fp=fp, choice="deny")[1], ["esc"])

case("質問への返答", screen=QUESTION)
fp = prompt()["fp"]
check("質問には、許可も断るも送らない（承認の画面だけに答える）",
      [(answer(fp=fp, choice=c)[0]["reason"], state["sent"]) for c in ("allow", "deny")], [("not_permission", []), ("not_permission", [])])

case("本物の Write の承認", screen=REAL_WRITE)
check("本物の Write の承認はボタンを出す", prompt()["kind"], "permission")
check("本物の Write の承認に許可を送れる", answer(fp=prompt()["fp"], choice="allow")[1], ["1"])
for name, screen in (("本物の Yes／No の質問", REAL_YESNO), ("Yes／No の質問（短い形）", YESNO_SHORT)):
    case(name, screen=screen)
    p = prompt()
    check(f"{name}は承認と取り違えない", p["kind"], "question")
    check(f"{name}には許可も断るも送らない", [answer(fp=p["fp"], choice=c)[1] for c in ("allow", "deny")], [[], []])
case("Do you want to が無い", screen=PERMISSION.replace("Do you want to proceed?", "続けますか？"))
check("承認の形がそろわなければボタンを出さない", prompt()["kind"], "unknown")
# ペインの高さが足りず、選択肢が「1. Yes」しか映っていない（2026-10-21 実画面で確認）
case("選択肢が映りきっていない", screen="⏺ Write(/Users/me/proj/a.txt)\n Do you want to create a.txt?\n ❯ 1. Yes\n Esc to cancel · Tab to amend")
p = prompt()
check("選択肢が映りきっていなければボタンを出さず、送らない", (p["kind"], [answer(fp=p["fp"], choice=c)[1] for c in ("allow", "deny")]), ("unknown", [[], []]))

# セッションの立て直し（ハブも、key はセッション ID で見分ける）
AGENT_HUB = {**AGENT, "hub": True, "cwd": str(s.HUB_DIR), "session": "old-session"}
s.list_agents = lambda: [{**AGENT_HUB, "status": "blocked"}]
case("ハブの承認")
fp = s._asked("w9:p1", "old-session")[1]["fp"]
AGENT_HUB["session"] = "new-session"  # 同じ場所でハブが立て直された（画面の文字は同じ）
state["sent"].clear(); s.ANSWERED.clear()
r = json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "old-session", "fp": fp, "choice": "allow"})).body)
check("立て直されたハブには、古い欄から送らない", (r["reason"], state["sent"]), ("gone", []))
r = json.loads(asyncio.run(s.api_answer({"pane": "w9:p1", "key": "hub", "fp": fp, "choice": "allow"})).body)
check("ハブを \"hub\" で指しても送らない", (r["reason"], state["sent"]), ("gone", []))
AGENT_HUB["session"] = None  # セッション ID が無い
check("セッション ID が無ければボタンを出さない", s._ask_of({**AGENT_HUB, "status": "blocked"})["kind"], "unknown")
s.list_agents = lambda: [{**AGENT, "status": state["status"]}]

case("わからない画面", screen=UNKNOWN)
fp = prompt()["fp"]
check("わからない画面には何も送らない", [answer(fp=fp, choice=c)[1] for c in ("allow", "deny")], [[], []])

for name, st in (("信頼", {"trust": True}), ("読めない", {"trust": None}), ("止まっていない", {"status": "idle"})):
    case(name, **st)
    r, sent = answer(fp=fp, choice="allow")
    check(f"{name}のときは送らない", (r["ok"], sent), (False, []))

# 止まっているセッションすべての一覧（画面の上に並べるもの）
AGENTS = {"w1:p1": ("sess-a", "blocked", PERMISSION, False), "w2:p1": ("sess-b", "blocked", RISKY, False),
          "w3:p1": ("sess-c", "blocked", QUESTION, False), "w4:p1": ("sess-d", "blocked", "", True), "w5:p1": ("sess-e", "working", "", False)}
s.list_agents = lambda: [{**AGENT, "pane": p, "session": k, "status": st, "title": f"t{p[1]}", "cwd": f"/x/proj{p[1]}"}
                         for p, (k, st, _, _) in AGENTS.items()]
s.read_prompt = lambda pane: AGENTS[pane][2]
s.pane_shows_trust = lambda pane: AGENTS[pane][3]
got = json.loads(s.api_prompts().body)
check("一覧は止まっているものだけ・順番どおり", [g["pane"] for g in got], ["w1:p1", "w2:p1", "w3:p1", "w4:p1"])
check("一覧の中身（名前・key・種類・取り消しにくいか）",
      [(g["name"], g["key"], g["kind"], g.get("risky")) for g in got],
      [("proj1｜t1", "sess-a", "permission", False), ("proj2｜t2", "sess-b", "permission", True),
       ("proj3｜t3", "sess-c", "question", False), ("proj4｜t4", "sess-d", "trust", None)])
check("一覧の指紋は、それぞれの問いのもの", [g.get("fp") == s.prompt_fp(AGENTS[g["pane"]][2]) for g in got[:3]], [True, True, True])

bad = [f"{n}: {g!r}（期待 {w!r}）" for n, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
