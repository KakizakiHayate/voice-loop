# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "httpx>=0.27"]
# ///
"""Hermes との会話の記録（hermes-log.jsonl）の試験。
画面を閉じた・止めた間の返事、画面が無い間の報告が記録に残り、履歴に出ること。Hermes とのやり取りが同時に1件だけなこと。
Hermes の API・herdr・語り手・音声合成には触れない（差し替える）。使い方: uv run spike/hermes_log_test.py"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

plan: list = []  # 次の Hermes の呼び出しの (待つ秒, 返事か例外)
calls: list[str] = []
active = {"now": 0, "max": 0}
mu = threading.Lock()


def fake_ask_hermes(text):
    delay, out = plan.pop(0) if plan else (0, "わかりました")
    with mu:
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
    try:
        calls.append(text)
        time.sleep(delay)
        if isinstance(out, BaseException):
            raise out
        return out
    finally:
        with mu:
            active["now"] -= 1


async def nothing(*a, **k):
    return None


s.ask_hermes = fake_ask_hermes
s.list_agents = lambda: []
s.speak_to = nothing  # 読み上げは関係ない
s.boot_ensure_hub = nothing  # 起動時に本物の Hermes やトークンを見に行かない
s.keep_gateway_token = nothing
WS = "ws://127.0.0.1:8790/ws"
D = {"pane": "w9:p1", "cwd": "/x/proj", "project": "proj", "title": "テスト", "text": "テストして", "offset": 0, "was_working": False, "gen": 1}

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def records():
    return [(d["role"], d["text"]) for d in s.hermes_records()]


def wait_until(fn, limit=5.0):
    end = time.time() + limit
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def say(w, text):
    w.send_json({"type": "text", "text": text, "pane": s.HERMES_PANE, "speaker": 0})


def until(w, kind):
    """kind が来るまで受け取る。来たものの種類の並びを返す。"""
    seen = []
    while not seen or seen[-1]["type"] != kind:
        seen.append(w.receive_json())
    return seen


with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    def history():
        return c.get("/api/history", params={"pane": s.HERMES_PANE}).json()

    check("記録が無いうちは、履歴は空（記録の側から）", history(), {"source": "hermes", "messages": []})

    # ---- 開いたまま待つ: 返事が画面に届いた時点で、記録にも載っている（画面は記録から描き直す）
    plan.append((0.2, "3つ開いています"))
    with c.websocket_connect(WS) as w:
        say(w, "全体どうなってる？")
        got = until(w, "reply")
        at_reply = history()["messages"][-2:]
    check("返事は画面に届く", got[-1]["text"], "3つ開いています")
    check("届いた時点で、発言と返事が記録にある", at_reply, [{"role": "me", "text": "全体どうなってる？"}, {"role": "ai", "text": "3つ開いています"}])
    t1 = s.hub_human_turn()
    check("守りの台帳にも返事", s.hub_reply_to(t1), "3つ開いています")

    # ---- 送ってすぐ画面を閉じる
    plan.append((1.0, "brew の件を送りました"))
    with c.websocket_connect(WS) as w:
        say(w, "brew の件を頼んで")
        until(w, "status")
    check("閉じたあとも、返事を最後まで待って記録する", wait_until(lambda: records()[-1:] == [("ai", "brew の件を送りました")]), True)
    check("閉じたあとの返事が履歴に出る", history()["messages"][-2:], [{"role": "me", "text": "brew の件を頼んで"}, {"role": "ai", "text": "brew の件を送りました"}])
    check("閉じたあとの返事も台帳に入る", s.hub_reply_to(s.hub_human_turn()), "brew の件を送りました")

    # ---- 止める（読み上げや待ちを止めても、Hermes は答えて覚えている）
    plan.append((1.0, "止められる前の返事"))
    with c.websocket_connect(WS) as w:
        say(w, "長い話をして")
        until(w, "status")
        w.send_json({"type": "stop"})
        until(w, "stopped")
    check("止めても、返事は記録する", wait_until(lambda: records()[-1:] == [("ai", "止められる前の返事")]), True)

    # ---- 閉じた直後の自動報告: 前の返事を待ってから送り、画面が無くても記録に残る
    calls.clear()
    active["max"] = 0
    plan.extend([(1.0, "テストを頼みました"), (0, "テストの件が終わりました。全部通っています")])
    with c.websocket_connect(WS) as w:
        say(w, "テストを頼んで")
        until(w, "status")
    check("画面は0", len(s.CLIENTS), 0)
    c.portal.call(s.report, D, "done")
    check("Hermes とのやり取りは同時に1件だけ", active["max"], 1)
    check("Hermes に届いた順は、発言 → 通知", [t.startswith(s.NOTE_PREFIX) for t in calls], [False, True])
    check("記録の順: 発言・返事・通知の見出し・報告", records()[-4:], [
        ("me", "テストを頼んで"), ("ai", "テストを頼みました"), ("note", "proj｜テスト が完了しました"), ("ai", "テストの件が終わりました。全部通っています")])
    rec = s.hermes_records()[-1]
    check("報告の記録には印と件名", (rec.get("report"), rec.get("about")), (True, "proj｜テスト"))
    check("通知は台帳（ユーザーの発言）に載らない", s.hub_turns(1)[0][1], "テストを頼んで")
    check("画面に出す履歴では、通知は note", history()["messages"][-2], {"role": "note", "text": "proj｜テスト が完了しました"})

    # ---- 画面が開いているときの報告: 画面に届き、記録にも残る
    plan.append((0, "承認待ちです。ls を実行していいか聞いています"))
    s.list_agents = lambda: [{"pane": "w9:p1", "cwd": "/x/proj", "status": "blocked", "hub": False}]
    with c.websocket_connect(WS) as w:
        w.send_json({"type": "hello", "speaker": 0})
        c.portal.call(s.report, D, "blocked")
        m = until(w, "reply")[-1]
    s.list_agents = lambda: []
    check("画面に報告が届く", (m["report"], m["about"], m["pane"]), (True, "proj｜テスト", "hermes"))
    check("記録にも残る", records()[-2:], [("note", "proj｜テスト が承認・入力待ちで止まっています"), ("ai", "承認待ちです。ls を実行していいか聞いています")])

    # ---- 報告を受け取れなかった（Hermes が落ちている・待ちきれない）
    plan.append((0, RuntimeError("Hermes に接続できません（refused）。コンテナが動いているか確認してください")))
    c.portal.call(s.report, D, "done")
    check("報告の失敗も記録（見出しのあとに）", records()[-2:], [
        ("note", "proj｜テスト が完了しました"), ("error", "ハブから報告を受け取れませんでした: Hermes に接続できません（refused）。コンテナが動いているか確認してください")])
    plan.append((0, TimeoutError("timed out")))
    c.portal.call(s.report, D, "done")
    check("待ちきれなかった報告も記録", records()[-1], ("error", "ハブから報告を受け取れませんでした: TimeoutError: timed out"))
    plan.append((0, ""))
    c.portal.call(s.report, D, "done")
    check("報告の返事が空でも記録", records()[-1], ("error", "ハブから報告を受け取れませんでした: 返事が空でした"))
    check("画面に出す履歴では、失敗は error", history()["messages"][-1]["role"], "error")

    # ---- 発言が Hermes に届かなかった・返事が無かった
    plan.append((0, RuntimeError("Hermes が 500 を返しました: oops")))
    with c.websocket_connect(WS) as w:
        say(w, "状況は？")
        m = until(w, "error")[-1]
    check("画面に失敗が届く", m["text"], "Hermes が 500 を返しました: oops")
    check("記録にも発言と失敗", records()[-2:], [("me", "状況は？"), ("error", "Hermes が 500 を返しました: oops")])
    plan.append((0.5, TimeoutError("timed out")))
    with c.websocket_connect(WS) as w:
        say(w, "まだ？")
        until(w, "status")
    check("閉じたあとに待ちきれなくても、記録に残る", wait_until(lambda: records()[-1:] == [("error", "TimeoutError: timed out")]), True)
    plan.append((0, ""))
    with c.websocket_connect(WS) as w:
        say(w, "聞こえる？")
        m = until(w, "error")[-1]
    check("返事が空なら、画面に知らせて記録する", (m["text"], records()[-1]), ("Hermes の返事が空でした", ("error", "Hermes の返事が空でした")))
    check("返事が空の発言は、台帳の返事も空", s.hub_reply_to(s.hub_human_turn()), "")

    # ---- 再起動: 台帳は記録から戻る。発言の ID は再起動をまたいでも重ならない
    before = [list(r) for r in s.HERMES_TURNS]
    s.HERMES_TURNS.clear()
    s._load_hermes_turns()
    check("台帳が記録から戻る（発言・返事とも）", [list(r) for r in s.HERMES_TURNS], before)
    check("戻した台帳で、前の発言への返事が読める", s.hub_reply_to(before[1][0]), "brew の件を送りました")
    new = s.hermes_turn_begin("再起動のあと")
    check("新しい発言の ID は前のどれとも違う", new in {r[0] for r in before}, False)
    s.hermes_turn_end(new, "はい")

    # ---- 壊れた行は飛ばす
    with s.HERMES_LOG.open("a") as f:
        f.write("壊れた行\n[1, 2]\n" + json.dumps({"role": "ai", "text": 3}) + "\n" + json.dumps({"role": "me"}) + "\n")
    check("壊れた行があっても履歴は読める", history()["messages"][-2:], [{"role": "me", "text": "再起動のあと"}, {"role": "ai", "text": "はい"}])
    s.HERMES_TURNS.clear()
    s._load_hermes_turns()
    check("壊れた行があっても台帳は戻る", s.hub_turns(1)[0][1], "再起動のあと")

    # ---- ハブが Claude Code のとき: Hermes は話し相手の1人。会話は同じように記録し、ハブの報告は Hermes の記録に入れない
    s.HERMES_HUB = False
    plan.append((0, "Hermes です"))
    with c.websocket_connect(WS) as w:
        say(w, "こんにちは")
        until(w, "reply")
    check("ハブでなくても Hermes との会話は記録する", records()[-2:], [("me", "こんにちは"), ("ai", "Hermes です")])
    check("一覧の鍵は hermes（ハブではない）", [a["key"] for a in json.loads(s.api_agents().body)], ["hermes"])
    n = len(records())
    s.list_agents = lambda: [{"pane": "w1:p1", "cwd": "/x/hub", "title": "hub", "kind": "claude", "session": "s", "status": "idle", "name": "hub", "hub": True}]
    real_ask = s.ask

    async def claude_hub_ask(hub, text):  # Claude Code のハブは自分の記録に残すので、本体は Hermes の記録に書かない
        return "done", "テストの件が終わりました"

    s.ask = claude_hub_ask
    c.portal.call(s.report, D, "done")
    s.ask = real_ask
    s.list_agents = lambda: []
    check("Claude Code のハブへの報告は Hermes の記録に入らない", len(records()), n)
    s.HERMES_HUB = True

    # ---- 前の書き込みが途中で切れていた（末尾に改行が無い）: 次の発言をつなげずに書く
    with s.HERMES_LOG.open("a") as f:
        f.write('{"role": "ai", "text": "途中で切れた')
    plan.append((0, "切れたあとの返事"))
    with c.websocket_connect(WS) as w:
        say(w, "切れたあとの発言")
        until(w, "reply")
    check("切れた行のあとも、発言と返事が履歴に出る", history()["messages"][-2:], [{"role": "me", "text": "切れたあとの発言"}, {"role": "ai", "text": "切れたあとの返事"}])
    check("切れた行は1行のまま残り、次の行とつながらない", s.HERMES_LOG.read_text().splitlines()[-3], '{"role": "ai", "text": "途中で切れた')

    # ---- 記録に書けない（ディスクがいっぱい・権限など）: 会話は止めず、画面に「記録に書けなかった」と伝える
    n = len(records())
    s.HERMES_LOG.chmod(0o444)
    plan.extend([(0, "書けないときの返事"), (0, RuntimeError("Hermes が 502 を返しました")), (0, "書けないときの報告")])
    with c.websocket_connect(WS) as w:
        say(w, "書けないとき")
        m = until(w, "reply")[-1]
        check("書けなかった返事は saved=False で届く", (m["text"], m["saved"]), ("書けないときの返事", False))
        say(w, "書けないときの失敗")
        m = until(w, "error")[-1]
        check("書けなかった失敗も saved=False で届く", (m["text"], m["saved"]), ("Hermes が 502 を返しました", False))
        c.portal.call(s.report, D, "done")
        m = until(w, "reply")[-1]
        check("書けなかった報告も saved=False で届く", (m["text"], m["report"], m["saved"]), ("書けないときの報告", True, False))
    s.HERMES_LOG.chmod(0o644)
    check("書けない間、記録は増えない", len(records()), n)
    check("書けなくても台帳には入る（守りは会話どおりに働く）", s.hub_turns(2), [(s.hub_turns(2)[0][0], "書けないときの失敗"), (s.hub_turns(2)[1][0], "書けないとき")])
    check("書けなかった印は持ち越さない", s._unsaved, set())
    real_log = s.hermes_log
    s.hermes_log = lambda role, text, **k: False if role == "me" else real_log(role, text, **k)  # 発言だけ書けなかった
    plan.append((0, "発言だけ書けなかった返事"))
    with c.websocket_connect(WS) as w:
        say(w, "発言だけ書けない")
        m = until(w, "reply")[-1]
    s.hermes_log = real_log
    check("発言だけ書けなくても saved=False（履歴から発言が欠ける）", (m["saved"], records()[-1]), (False, ("ai", "発言だけ書けなかった返事")))
    plan.append((0, "戻ったあとの返事"))
    with c.websocket_connect(WS) as w:
        say(w, "戻ったあと")
        m = until(w, "reply")[-1]
    check("書けるようになれば saved=True", m["saved"], True)

    # ---- 本当に起動し直す（別のプロセスで本体を読み込む）: 台帳が記録から戻り、新しい発言の ID は前と重ならない
    probe = subprocess.run([sys.executable, "-c", "import json, server as s; n = len(s.HERMES_TURNS); t = s.hermes_turn_begin('起動し直したあと');"
                            " print(json.dumps({'turns': [list(r) for r in s.HERMES_TURNS][:n], 'new': t}))"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
    after = json.loads(probe.stdout.strip().splitlines()[-1]) if probe.returncode == 0 else {"turns": None, "new": None}
    check("起動し直すと、台帳が記録から戻る（記録に書けなかった発言は戻らない）", after["turns"], [list(r) for r in s.HERMES_TURNS if not r[1].startswith(("書けないとき", "発言だけ書けない"))])
    check("起動し直したあとの発言の ID は、前のどれとも違う", after["new"] in {r[0] for r in s.HERMES_TURNS}, False)

    # ---- 記録を読めなくても、本体は起動する（守りの台帳は空から。履歴は「読めなかった」と出す）
    def start_with(prepare, hub="hermes"):
        state = Path(tempfile.mkdtemp())
        prepare(state / "hermes-log.jsonl")
        env = {**os.environ, "VL_STATE_DIR": str(state), "VL_HUB": hub, "VL_HERMES_KEY": "k1" if hub == "hermes" else ""}
        code = ("import json, server as s; h = s.hermes_history(); ok = s.hermes_log('me', 'x');"
                " print(json.dumps({'turns': len(s.HERMES_TURNS), 'history': [m['role'] for m in h], 'last': h[-1]['text'] if h else '', 'write': ok}))")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {"exit": r.returncode, "err": r.stderr.strip().splitlines()[-1:]}

    got = start_with(lambda path: path.mkdir(parents=True), hub="claude")
    check("記録の場所がフォルダでも起動する（ハブが Claude Code でも）", (got.get("turns"), got.get("history"), got.get("write")), (0, ["error"], False))
    check("読めないことは履歴に出る", "読めませんでした" in got.get("last", ""), True)

    def unreadable(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"role": "me", "text": "読めない", "turn": "hX"}) + "\n")
        path.chmod(0)

    got = start_with(unreadable)
    check("権限が無くて読めなくても起動する", (got.get("turns"), got.get("history")), (0, ["error"]))

    def deep(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[" * 200_000 + "]" * 200_000 + "\n" + json.dumps({"role": "me", "text": "深い行のあと", "turn": "hY"}) + "\n")

    got = start_with(deep)
    check("入れ子が深すぎる行は飛ばし、ほかの行は戻る", (got.get("turns"), got.get("history")), (1, ["me"]))

    # ---- 記録が大きくなったら、一つ前の分だけ残して回す
    real_max = s.HERMES_LOG_MAX
    s.HERMES_LOG_MAX = 200
    s.hermes_log("me", "回したあと")  # ここまでの記録は 200 バイトを超えているので、書く前に回る
    s.HERMES_LOG_MAX = real_max
    old = s.HERMES_LOG.with_name("hermes-log.1.jsonl")
    check("回した分は hermes-log.1.jsonl に", "こんにちは" in old.read_text() if old.exists() else None, True)
    check("回したあとの履歴は新しい記録から", history()["messages"], [{"role": "me", "text": "回したあと"}])

    # ---- Hermes 以外の相手の履歴は、これまでどおり
    check("記録を読めない相手は none", c.get("/api/history", params={"pane": "w9:p9"}).json(), {"source": "none", "messages": []})

bad = [f"{n}: {g!r}（期待 {w!r}）" for n, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
