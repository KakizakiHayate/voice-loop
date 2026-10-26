# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "httpx>=0.27"]
# ///
"""ハブ（Hermes）の会話を日ごとに新しくすること・Hermes に渡す指示を毎回同じ文にすること・hubctl sessions と read の試験。
Hermes の API と herdr には触れない（差し替える）。使い方: uv run spike/hermes_session_test.py"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1", VL_HERMES_SESSION="hub")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import server as s  # noqa: E402
from origin_guard import problem  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


c = TestClient(s.app, base_url="http://127.0.0.1:8790")
at = lambda text: datetime.strptime(text, "%Y-%m-%d %H:%M")

# ---------- 会話の名前（毎朝4時に変わる・画面で新しくする） ----------
check("4時より前は前の日の会話", (s.hermes_session(at("2026-10-26 03:59")), s.hermes_session(at("2026-10-26 04:00"))), ("hub-20261025", "hub-20261026"))
check("新しくするたびに、その日の回数が進む",
      (s.hermes_session_new(at("2026-10-26 10:00")), s.hermes_session_new(at("2026-10-26 11:00")), s.hermes_session(at("2026-10-26 12:00"))),
      ("hub-20261026-1", "hub-20261026-2", "hub-20261026-2"))
check("次の日の4時には、回数を持ち越さずに新しい会話", (s.hermes_session(at("2026-10-27 03:59")), s.hermes_session(at("2026-10-27 04:00"))),
      ("hub-20261026-2", "hub-20261027"))
for bad in ('{"day": "20261026", "n": true}', '{"day": "20261026", "n": -3}', '{"day": "20261026", "n": "2"}', "[1, 2]", "壊れた"):
    s.HERMES_SESSION_FILE.write_text(bad)
    check(f"状態のファイルが壊れていても、その日の最初の会話の名前で続ける（{bad}）", s.hermes_session(at("2026-10-26 12:00")), "hub-20261026")
s.HERMES_SESSION_FILE.unlink()
hour = s.HERMES_RESET_HOUR
s.HERMES_RESET_HOUR = None
check("日ごとに変えない設定（VL_HERMES_RESET_HOUR=off）なら、元の名前のまま", (s.hermes_session(at("2026-10-26 12:00")), s.hermes_session_new(at("2026-10-26 12:00"))), ("hub", "hub-1"))
s.HERMES_SESSION_FILE.unlink()
s.HERMES_RESET_HOUR = hour

# Hermes に送るときは、いまの会話の名前を付ける
sent = []


class Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def fake_urlopen(req, timeout=None):
    sent.append({"session": req.get_header("X-hermes-session-id"), "body": json.loads(req.data)})
    return Resp({"choices": [{"message": {"content": "はい"}, "finish_reason": "stop"}]})


s.urllib.request.urlopen = fake_urlopen
s.ask_hermes("こんにちは")
check("Hermes に送るときは、いまの会話の名前（元の名前＋日付）を付ける", sent[-1]["session"], s.hermes_session())
r = c.post("/api/hermes/session/new").json()
s.ask_hermes("もう一度")
check("画面で新しくすると、次の発言から新しい会話の名前で送る", (r["ok"], sent[-1]["session"], r["session"].endswith("-1")), (True, r["session"], True))
check("新しくしたことは、画面の履歴に知らせとして残る", s.hermes_history()[-1], {"role": "note", "text": "ハブの会話を新しくしました（Hermes の記憶と手順書は残ります）"})
check("話し相手の一覧の Hermes の項目に、いまの会話の名前が出る", next(a for a in c.get("/api/agents").json() if a.get("hermes"))["session"], r["session"])
s.HERMES_SESSION_FILE.unlink()
s.HERMES_SESSION_FILE.mkdir()
before = s.hermes_session()
check("状態を書けないときは新しくせず、そう返す", (c.post("/api/hermes/session/new").json(), s.hermes_session()), ({"ok": False, "reason": "state"}, before))
s.HERMES_SESSION_FILE.rmdir()
key = s.HERMES_KEY
s.HERMES_KEY = ""
check("Hermes がつながっていなければ断る", c.post("/api/hermes/session/new").json(), {"ok": False, "reason": "off"})
s.HERMES_KEY = key
check("ハブ（コンテナ）からは、鍵があっても新しくする口を使えない",
      problem({"host": "127.0.0.1:8790", "x-vl-key": "k1"}, "POST", "/api/hermes/session/new", container=True) is not None, True)

# ---------- Hermes に渡す指示は毎回同じ文（件数や時刻は発言の後ろに） ----------
s.HERMES_DIR.mkdir(parents=True, exist_ok=True)
(s.HERMES_DIR / "hub.md").write_text("ハブへの指示（試験）")
s.INTAKE = True
sit = s.situation()
sit["topics"] = [{"id": "t1", "title": "ブログ", "now": "初稿を書いている", "next": "", "folder": "", "until": "", "moved": "2026-10-20", "sources": []}]
sit["last_ok"] = time.time() - 60
s.write_json(s.SITUATION_FILE, sit)
s.write_json(s.FACTS_FILE, {"next": 2, "facts": [{"n": 1, "text": "週次定例は情報交換の場", "at": time.time(), "via": "screen"}]})
first = s.hermes_messages("こんにちは")
sit["last_ok"] = time.time() - 3600  # 「何分前」が変わる
sit["topics"].append({**sit["topics"][0], "id": "t2", "title": "ネットショップ"})  # 件数が変わる
s.write_json(s.SITUATION_FILE, sit)
s.write_json(s.FACTS_FILE, {"next": 3, "facts": [{"n": 1, "text": "週次定例は情報交換の場", "at": time.time(), "via": "screen"},
                                                 {"n": 2, "text": "blog は個人の案件", "at": time.time(), "via": "screen"}]})
second = s.hermes_messages("こんにちは")
check("件数や「何分前」が変わっても、指示（system）は1字も変わらない（キャッシュを書き直させない）", first[0] == second[0], True)
check("件数と時刻は、発言の後ろの案内で変わる", (first[-1]["content"] != second[-1]["content"], "話題 2 件" in second[-1]["content"], "覚えていること 2 件" in second[-1]["content"]),
      (True, True, True))
check("発言はそのまま先頭に残り、案内はユーザーの言葉ではないと書いてある",
      second[-1]["content"].startswith("こんにちは\n\n［voice-loop の案内（ユーザーの言葉ではありません）"), True)
check("指示には、件数・時刻・話題の本文が入らない", any(w in second[0]["content"] for w in ("2 件", "分前", "最終更新 1", "ネットショップ", "初稿")), False)
s.INTAKE = False
for p in (s.SITUATION_FILE, s.FACTS_FILE):
    p.unlink()
check("話題も覚えていることも無く、拾う役も切ってあれば、案内は足さない", s.hermes_messages("やあ"), [{"role": "system", "content": "ハブへの指示（試験）"}, {"role": "user", "content": "やあ"}])

# ---------- sessions: 最終更新の時刻と、直近の依頼がどこから来たか ----------
LOG = Path(TMP) / "w.jsonl"
A = {"pane": "w9:p1", "title": "テスト", "cwd": "/x/proj", "kind": "claude", "session": "sess-1", "status": "idle", "name": "", "hub": False}
s.list_agents = lambda: [dict(A)]
s.jsonl_path = lambda a: LOG


def session_log(user, reply):
    LOG.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in [
        {"type": "user", "uuid": "u1", "message": {"content": user}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}}]) + "\n")


def row():
    return c.get("/api/hub/sessions").json()[0]


s.TASKS.clear()
session_log("README の 誤字を直して", "直しました")
s.task_new({"pane": "w9:p1", "project": "proj", "title": "テスト", "text": "README の  誤字を直して "}, "send")
r = row()
check("ハブが送った依頼なら voice-loop（空白の数の違いは無視）", (r["asked_by"], r["updated_at"] == datetime.fromtimestamp(LOG.stat().st_mtime).strftime("%m/%d %H:%M")), ("voice-loop", True))
session_log("ついでに CHANGELOG も直して", "直しました")
check("ハブを通さずに頼まれた依頼なら other", row()["asked_by"], "other")
s.task_new({"pane": "w8:p1", "project": "other", "title": "別", "text": "ついでに CHANGELOG も直して"}, "send")
check("同じ文でも、別のセッションに送ったものは数えない", row()["asked_by"], "other")
old = time.time() - 4 * 86400
os.utime(LOG, (old, old))
check("頼んだ件の台帳が残る3日より古い依頼は、分からない（None）", row()["asked_by"], None)
s.task_new({"pane": "w9:p1", "project": "proj", "title": "テスト", "text": "README の誤字を直して"}, "send")
s.TASKS.clear()
s._load_tasks()
session_log("README の誤字を直して", "直しました")
check("台帳を読み直しても（本体の再起動）、送った文の指紋は残る", row()["asked_by"], "voice-loop")
check("台帳のファイルに、送った文の指紋が書かれる", '"fp"' in s.TASKS_FILE.read_text(), True)

# ---------- read: 長いターンでも結論（末尾）を残す ----------
long_reply = "途中経過。" * 600 + "【結論】マージしてよいです"
session_log("どうなった？", long_reply)
default = c.get("/api/hub/read", params={"pane": "w9:p1", "n": 1}).json()["messages"][-1]["text"]
full = c.get("/api/hub/read", params={"pane": "w9:p1", "n": 1, "full": "true"}).json()["messages"][-1]["text"]
check("read（既定）: 長い返答でも末尾の結論が残り、長さは 1,600 字ほどまで", (default.endswith("【結論】マージしてよいです"), len(default) <= 1600), (True, True))
check("read --full: 末尾の結論が残る", full.endswith("【結論】マージしてよいです"), True)
short = "短い返答です"
session_log("どう？", short)
check("read: 短い返答はそのまま", c.get("/api/hub/read", params={"pane": "w9:p1", "n": 1}).json()["messages"][-1]["text"], short)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
