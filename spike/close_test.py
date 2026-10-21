# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40"]
# ///
"""ハブがセッションを閉じる（/api/hub/close）と、立てる数に上限が無いこと（/api/hub/spawn）の試験。
ハブの記録は作り物の JSONL に発言と返答を書き足して進め、閉じてよい時だけ herdr に閉じる命令が出るかを確かめる。
herdr には触れない（一覧・閉じる命令・起動を差し替える）。使い方: uv run spike/close_test.py"""
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

HUB_LOG = Path(TMP) / "hub.jsonl"
HUB = {"pane": "w1:p1", "title": "hub", "cwd": str(s.HUB_DIR), "kind": "claude", "session": "hub-sess", "status": "idle", "name": "hub", "hub": True}
TARGET = {"pane": "w9:p1", "title": "テスト", "cwd": "/x/proj", "kind": "claude", "session": "sess-1", "status": "idle", "name": "", "hub": False}
ASK = "proj の「テスト」のセッションを閉じていいですか。"  # 聞いたときのハブの返答（ユーザーが聞いたもの）
agents = [HUB, TARGET]
closed: list[str] = []
fail = {"herdr": False}
s.list_agents = lambda: [dict(a) for a in agents]
s.jsonl_path = lambda a: HUB_LOG if a["hub"] else None
s.herdr = lambda *a, **k: ((closed.append(a[2]), {} if fail["herdr"] else {"result": {}})[1] if a[:2] == ("pane", "close") else {"result": {}})
n = 0


def say(text: str, reply: str = "わかりました", note: bool = False):
    """ハブの記録に、ユーザーの発言（note なら自動通知）と、ツールの結果、ハブの返答を書き足す。"""
    global n
    n += 1
    with HUB_LOG.open("a") as f:
        f.write(json.dumps({"type": "user", "uuid": f"u{n}", "message": {"content": (s.NOTE_PREFIX + " " if note else "") + text}}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "確かめます"}, {"type": "tool_use", "input": {}}]}}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "user", "uuid": f"t{n}", "message": {"content": [{"type": "tool_result", "content": "x"}]}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}}, ensure_ascii=False) + "\n")


def close(pane="w9:p1", **body):
    closed.clear()
    r = json.loads(asyncio.run(s.hub_close({"pane": pane, **body})).body)
    return r.get("reason", "ok"), list(closed)


def asked(first="テストのセッションを閉じて", reply=ASK):
    """ユーザーが閉じてと頼み、ハブが close の1回目を打って、reply で聞いた。"""
    say(first, reply)
    s.CLOSING.pop("w9:p1", None)
    return close()


results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


HUB_LOG.write_text("")
say("テストのセッションを閉じて")
say("テストが完了しました", note=True)
check("ユーザー本人の発言（ツールの結果と自動通知は数えない）", s.human_turns(HUB_LOG, 3), [("u1", "テストのセッションを閉じて")])
check("承認への回答で使う「最後の発言」は前と同じ", s.hub_human_turn(), "u1")
check("その発言への返答は、最後のツールより後の文", s.reply_to(HUB_LOG, "u1"), "わかりました")

r = json.loads(asyncio.run(s.hub_close({"pane": "w9:p1"})).body)
check("1回目は閉じずに、閉じる相手を返す", (r["reason"], r["project"], r["title"], r["status"], closed), ("confirm", "proj", "テスト", "idle", []))
check("聞いたのと同じ発言の中では閉じない", close(confirmed=True), ("same_turn", []))
asked()
say("はい、閉じて")
check("聞いた次の発言が「はい」なら閉じる", close(confirmed=True), ("ok", ["w9:p1"]))
check("閉じたら、聞いている印を消し、閉じた pane として覚える", ("w9:p1" in s.CLOSING, "w9:p1" in s.CLOSED), (False, True))
check("もう一度 confirmed しても、聞き直しから", close(confirmed=True), ("confirm", []))

s.CLOSED.clear(); s.CLOSING.clear()
say("別のテストも閉じて", ASK)
check("聞かずにいきなり confirmed は閉じない（ここから聞く）", close(confirmed=True), ("confirm", []))
say("はい")
check("その次の発言なら閉じる", close(confirmed=True), ("ok", ["w9:p1"]))

# ユーザーに聞かずに1回目だけ打ち、次の発言を許可に見立てる（Codex 第1回の指摘）
asked(first="README を要約して", reply="README は導入の手順と設定の説明です。")
say("はい、ありがとう")
check("聞いたときの返答に、閉じるかの問いが無ければ閉じない", close(confirmed=True), ("not_asked", []))
asked(reply="テストのセッションを閉じていいですか。")
say("はい")
check("聞いたときの返答に、プロジェクト名が無ければ閉じない", close(confirmed=True), ("not_asked", []))
for reply in ("道具は閉じませんでした。閉じようとした相手は proj の「テスト」で、開いたままです。",
              "proj のセッションを閉じると何が起きるか説明しましょうか。",  # Codex 第2回の再現
              "proj の「テスト」を閉じました。ほかに閉じるものはありますか？"):
    asked(reply=reply)
    say("はい")
    check(f"聞いたときの返答が「{reply}」（閉じていいかの問いでない）なら閉じない", close(confirmed=True), ("not_asked", []))
for reply in ("proj の「テスト」を閉じてもいいですか？", "proj のテストのセッション、**閉じていい？**", "閉じるのは proj の「テスト」です。閉じますか",
              "proj の「テスト」を閉じていいか、もう一度だけ聞かせてください。"):  # 最後は 2026-10-21 の通しで本物のハブが言った形
    asked(reply=reply)
    say("はい")
    check(f"聞いたときの返答「{reply}」は問いとして読む", close(confirmed=True), ("ok", ["w9:p1"]))
for answer in ("README を見せて", "閉じないで", "いいえ", "いや、やめておいて", "ええと、どうしようかな", "うーん", "待って", "はい、でもまだ残して",
               "ううん", "うん、閉じるのは後でお願い"):  # 最後の2つは Codex 第2回の再現
    asked()
    say(answer)
    check(f"答えが「{answer}」なら閉じない", close(confirmed=True), ("not_yes", []))
for answer in ("うん、お願い", "OKです", "閉じちゃって", "いいよ", "ええ、どうぞ", "yes"):
    asked()
    say(answer)
    check(f"答えが「{answer}」なら閉じる", close(confirmed=True), ("ok", ["w9:p1"]))
asked()
say("閉じないで")
close(confirmed=True)
say("はい")
check("断られて聞き直していないなら、その次の「はい」でも閉じない", close(confirmed=True), ("not_asked", []))

asked()
say("ところで天気は？")
say("やっぱり閉じて")
check("あいだに別の発言をはさんだら、閉じずに聞き直し", close(confirmed=True), ("confirm", []))

asked()
say("テストが完了しました", note=True)  # 聞いたあと、ユーザーが答える前に自動通知が届いた
say("はい")
check("あいだの自動通知は発言に数えない", close(confirmed=True), ("ok", ["w9:p1"]))

asked()
say("はい")


async def twice():
    return await asyncio.gather(*(s.hub_close({"pane": "w9:p1", "confirmed": True}) for _ in range(2)))
closed.clear()
rs = sorted(json.loads(r.body).get("reason", "ok") for r in asyncio.run(twice()))
check("同じ許可が同時に2つ届いても、閉じるのは1回", (rs, closed), (["confirm", "ok"], ["w9:p1"]))

asked()
TARGET["session"] = "sess-2"  # 同じ pane で別のセッションに替わった
say("はい")
check("聞いたあとでセッションが入れ替わったら、閉じずに聞き直し", close(confirmed=True), ("confirm", []))
TARGET["session"] = None  # セッション ID が無い（会話を始める前など）
check("セッション ID が無い相手は閉じない", (close()[0], close(confirmed=True)), ("unidentified", ("unidentified", [])))
TARGET["session"] = "sess-1"

asked()
say("はい")
s.REPORTING = True
check("自動通知への応答中は閉じない", close(confirmed=True), ("reporting", []))
s.REPORTING = False
fail["herdr"] = True
check("herdr が閉じられなかったら、閉じた印を残さない", (close(confirmed=True), "w9:p1" in s.CLOSED), (("herdr", ["w9:p1"]), False))
fail["herdr"] = False
check("ハブ自身は閉じない", close(pane="w1:p1", confirmed=True), ("gone", []))
check("無いセッションは閉じない", close(pane="w7:p7", confirmed=True), ("gone", []))

HUB_LOG.write_text("")  # ハブにまだ発言が無い
check("ハブの発言が無いときは、聞いても閉じない", (close()[0], close(confirmed=True)), ("confirm", ("same_turn", [])))
s.list_agents = lambda: [dict(TARGET)]  # ハブが居ない
check("ハブが居ないときも閉じない", (close()[0], close(confirmed=True)), ("confirm", ("same_turn", [])))
s.list_agents = lambda: [dict(a) for a in agents]

# 見張り: ハブが閉じたセッションは「見つからなくなった」と知らせない。ほかの理由で消えたものは知らせる
reports = []


async def fake_report(d, kind):
    reports.append((d["pane"], kind))
s.report = fake_report
agents.remove(TARGET)
s.CLOSED.clear(); s.CLOSED.add("w9:p1")
for pane in ("w9:p1", "w8:p1"):
    s.WATCH_GEN[pane] = 1
    asyncio.run(s._watch_delegation({"pane": pane, "cwd": "/x/proj", "project": "proj", "title": "t", "text": "x", "offset": 0, "was_working": False, "gen": 1}))
check("閉じたものは知らせず、ほかの理由で消えたものは知らせる", reports, [("w8:p1", "gone")])

# 立てる数に上限は無い（ハブが立てたセッションが5つ居ても立てる）
(Path(TMP) / "projects/blog").mkdir(parents=True)
s.PROJECTS_DIR = Path(TMP) / "projects"
spawned = []
s.spawn_session = lambda directory, project, model: (spawned.append(project), {"ok": True, "pane": "w5:p9", "name": "hubjob-blog-1"})[1]
s.list_agents = lambda: [HUB] + [{**TARGET, "pane": f"w5:p{i}", "name": f"hubjob-blog-{i}"} for i in range(5)]
r = json.loads(asyncio.run(s.hub_spawn({"project": "blog"})).body)
check("ハブが立てたものが5つ居ても、6つ目を立てる", (r["ok"], spawned), (True, ["blog"]))

bad = [f"{name}: {g!r}（期待 {w!r}）" for name, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
