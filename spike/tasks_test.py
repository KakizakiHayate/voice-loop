# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "httpx>=0.27"]
# ///
"""頼んだ件の台帳（画面の 📋 と hubctl tasks）の試験。見張りの一生に合わせて状態が進むこと、状態フォルダに残り、
本体が止まった件は次の起動で「見張りが切れた」になること、残す範囲、/api/hub/tasks と hubctl tasks の表示、コンテナからの読み取り。
herdr・ハブ・語り手には触れない（差し替える）。hubctl の確かめには、この試験の中で 127.0.0.1:8792 に本体の写しを立てる（常駐の 8790 には触れない）。
使い方: uv run spike/tasks_test.py"""
import asyncio
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="claude", VL_HERMES_KEY="k1", VL_PROJECTS_DIR=f"{TMP}/projects")
Path(f"{TMP}/projects/newproj").mkdir(parents=True)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


# 見張りの待ち時間を縮める（本体の中の asyncio.sleep だけ。3秒 → 0.03秒）
_real_sleep = asyncio.sleep
fast = types.ModuleType("fast_asyncio")
fast.__dict__.update(asyncio.__dict__)
fast.sleep = lambda t, *a: _real_sleep(t / 100, *a)
s.asyncio = fast

AGENTS: dict[str, dict] = {}  # pane → herdr の一覧に出るセッション
reports: list[tuple[str, str]] = []


def agent(pane, status="idle", cwd="/x/proj", title="作業"):
    AGENTS[pane] = {"pane": pane, "cwd": cwd, "kind": "codex", "session": None, "status": status, "title": title, "name": "", "hub": False}


s.list_agents = lambda: list(AGENTS.values())
s.herdr = lambda *a, **k: {"result": {}}  # 送る・閉じるは、受け付けたことにする


async def fake_report(d, kind, notify=None):
    reports.append((d["title"], kind))
    return "ok"


async def nothing(*a, **k):
    return None


s._report = fake_report  # ハブには話させない（台帳と通知の側だけを見る）
s.push_all = nothing
s.boot_ensure_hub = nothing
s.keep_gateway_token = nothing
s.watch_blocked = nothing


def wait_until(fn, limit=10.0):
    end = time.time() + limit
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


def status_of(tid):
    return s.TASKS.get(tid, {}).get("status")


def newest():
    return max(s.TASKS.values(), key=lambda t: t["started"])


with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    # ---- 送る → 作業 → 片づく（記録で追えない相手は、一度動いて落ち着いたら完了）
    agent("w1:p1")
    r = c.post("/api/hub/send", json={"pane": "w1:p1", "text": "テストを流して"}).json()
    t1 = newest()
    check("送ると台帳に載る（見張り中・指示）", (r["ok"], t1["status"], t1["kind"], t1["text"], t1["project"]), (True, "watching", "send", "テストを流して", "proj"))
    AGENTS["w1:p1"]["status"] = "working"
    time.sleep(0.2)
    AGENTS["w1:p1"]["status"] = "idle"
    check("片づくと完了", wait_until(lambda: status_of(t1["id"]) == "done"), True)
    f = s.TASKS_FILE
    check("台帳は状態フォルダに書く（本人だけが読める 0600）", (f.exists(), stat.S_IMODE(f.stat().st_mode)), (True, 0o600))

    # ---- 止まる → 答えてもらう → 片づく
    agent("w2:p1", title="承認あり")
    c.post("/api/hub/send", json={"pane": "w2:p1", "text": "消して"})
    t2 = newest()
    AGENTS["w2:p1"]["status"] = "blocked"
    check("承認・入力待ちで止まると「止まっている」", wait_until(lambda: status_of(t2["id"]) == "blocked"), True)
    AGENTS["w2:p1"]["status"] = "working"
    check("答えてもらって動きだすと「見張り中」に戻る", wait_until(lambda: status_of(t2["id"]) == "watching"), True)
    AGENTS["w2:p1"]["status"] = "idle"
    check("そのあと片づくと完了", wait_until(lambda: status_of(t2["id"]) == "done"), True)

    # ---- 同じセッションに次の指示 → 前の件は引き継ぎ
    agent("w3:p1", status="working", title="重ねる")
    c.post("/api/hub/send", json={"pane": "w3:p1", "text": "一つ目"})
    t3a = newest()
    time.sleep(0.1)
    c.post("/api/hub/send", json={"pane": "w3:p1", "text": "二つ目"})
    t3b = newest()
    check("前の件は「次の指示に引き継ぎ」", wait_until(lambda: status_of(t3a["id"]) == "replaced"), True)
    check("新しい件は見張り中", status_of(t3b["id"]), "watching")
    AGENTS["w3:p1"]["status"] = "idle"
    check("新しい件が片づくと完了（前の件は引き継ぎのまま）", (wait_until(lambda: status_of(t3b["id"]) == "done"), status_of(t3a["id"])), (True, "replaced"))

    # ---- 完了のあとに次の指示 → 前の件は完了のまま
    c.post("/api/hub/send", json={"pane": "w3:p1", "text": "三つ目"})
    time.sleep(0.2)
    check("報告済みの件は、次の指示が来ても完了のまま", status_of(t3b["id"]), "done")
    AGENTS["w3:p1"]["status"] = "working"
    time.sleep(0.1)
    AGENTS["w3:p1"]["status"] = "idle"
    wait_until(lambda: newest()["status"] == "done")

    # ---- セッションが消える（見失った・ハブが閉じた）
    agent("w4:p1", status="working", title="消える")
    c.post("/api/hub/send", json={"pane": "w4:p1", "text": "長い作業"})
    t4 = newest()
    del AGENTS["w4:p1"]
    check("セッションが見つからなくなると「見失った」", wait_until(lambda: status_of(t4["id"]) == "gone"), True)
    agent("w5:p1", status="working", title="閉じる")
    c.post("/api/hub/send", json={"pane": "w5:p1", "text": "作業"})
    t5 = newest()
    s.CLOSED.add("w5:p1")
    del AGENTS["w5:p1"]
    check("ハブが閉じたものは「閉じた」（報告はしない）", (wait_until(lambda: status_of(t5["id"]) == "closed"), ("閉じる", "gone") in reports), (True, False))

    # ---- 片づかないまま上限 → 見張り終了
    s.WATCH_TIMEOUT = 0.5
    agent("w6:p1", status="working", title="終わらない")
    c.post("/api/hub/send", json={"pane": "w6:p1", "text": "ずっと動く"})
    t6 = newest()
    check("上限まで片づかないと「見張り終了」", wait_until(lambda: status_of(t6["id"]) == "timeout"), True)
    s.WATCH_TIMEOUT = 3 * 3600

    # ---- 見張りの途中で失敗
    agent("w7:p1", status="working", title="失敗")
    real_list = s.list_agents
    c.post("/api/hub/send", json={"pane": "w7:p1", "text": "作業"})
    t7 = newest()

    def broken():
        raise RuntimeError("壊れた")

    s.list_agents = broken
    check("見張りが例外で止まると「見張りが失敗」", wait_until(lambda: status_of(t7["id"]) == "failed"), True)
    s.list_agents = real_list
    del AGENTS["w7:p1"]

    # ---- 立てて最初の指示・承認への答え
    s.spawn_session = lambda directory, project, model: (agent("w8:p1", cwd=str(directory), title="ハブが起動"), {"ok": True, "pane": "w8:p1", "name": "hubjob-x"})[1]
    r = c.post("/api/hub/spawn", json={"project": "newproj", "text": "調べて"}).json()
    t8 = newest()
    check("立てて最初の指示は kind=spawn", (r["ok"], r["sent"], t8["kind"], t8["project"]), (True, True, "spawn", "newproj"))

    async def answer_watch():
        s.start_watch({"pane": "w8:p1", "cwd": AGENTS["w8:p1"]["cwd"], "project": "newproj", "title": "ハブが起動", "text": "", "generic": True,
                       "offset": 0, "was_working": True, "gen": s.WATCH_GEN.setdefault("w8:p1", 0)}, "answer")

    c.portal.call(answer_watch)
    t9 = newest()
    check("承認への答えは kind=answer（指示の文は空）", (t9["kind"], t9["text"]), ("answer", ""))
    AGENTS["w8:p1"]["status"] = "idle"

    # ---- 一覧の API
    rows = c.get("/api/hub/tasks").json()
    check("一覧は新しい順", [r["id"] for r in rows], [t["id"] for t in sorted(s.TASKS.values(), key=lambda t: t["started"], reverse=True)])
    check("一覧には経過時間が付く", all(isinstance(r["age_minutes"], int) and isinstance(r["since_minutes"], int) for r in rows), True)

    # ---- ほかの更新が無くても、終わってから3日を過ぎた件は一覧から外れる
    s.TASKS["stale"] = {"id": "stale", "kind": "send", "pane": "w0:p0", "project": "p", "title": "古い", "text": "x",
                        "started": time.time() - 5 * 86400, "updated": time.time() - 4 * 86400, "status": "done", "note": ""}
    check("ほかの更新が無くても、3日を過ぎた件は一覧に出ない", "stale" in [r["id"] for r in c.get("/api/hub/tasks").json()], False)
    check("ファイルからも外れる", "stale" in [t["id"] for t in json.loads(s.TASKS_FILE.read_text())], False)
    import inspect
    check("一覧を読むのは見張りと同じイベントループの上（別のスレッドで台帳を書き換えない）", inspect.iscoroutinefunction(s.hub_tasks), True)
    s.TASKS["stale2"] = {**s.TASKS.get("stale", {}), "id": "stale2", "kind": "send", "pane": "w0:p0", "project": "p", "title": "古い", "text": "x",
                         "started": time.time() - 5 * 86400, "updated": time.time() - 4 * 86400, "status": "done", "note": ""}

    async def add_then_read():
        tid = s.task_new({"pane": "w0:p1", "project": "p", "title": "新しい", "text": "y"}, "send")
        return tid

    fresh = c.portal.call(add_then_read)
    rows_now = [r["id"] for r in c.get("/api/hub/tasks").json()]
    check("片づけと同じ時に足した件は消えない（古い件だけ外れる）", (fresh in rows_now, "stale2" in rows_now), (True, False))

    # ---- コンテナ（Hermes の hubctl）からは、鍵があれば読める
    dock = {"Host": "host.docker.internal:8790"}
    check("コンテナから鍵つきで読める", c.get("/api/hub/tasks", headers={**dock, "X-VL-Key": "k1"}).status_code, 200)
    check("コンテナから鍵なしは断る", c.get("/api/hub/tasks", headers=dock).status_code, 403)

    # 見張りを片づけておく（残った件は、次の起動で「見張りが切れた」になる件として使う）
    AGENTS["w9:p1"] = {**AGENTS["w1:p1"], "pane": "w9:p1", "status": "working", "title": "動いたまま"}
    c.post("/api/hub/send", json={"pane": "w9:p1", "text": "止まるまで続く"})
    t_live = newest()
    time.sleep(0.2)
    snapshot = json.loads(s.TASKS_FILE.read_text())

# ---- 次の起動: 見張りの途中だった件は「見張りが切れた」。終わった件はそのまま
s.TASKS_FILE.write_text(json.dumps(snapshot + [
    {"id": "old", "kind": "send", "pane": "w0:p0", "project": "p", "title": "古い", "text": "x", "started": time.time() - 5 * 86400,
     "updated": time.time() - 4 * 86400, "status": "done", "note": ""},
    {"id": "broken", "kind": "send"},  # 壊れた件
    "壊れた行",
]))
probe = (
    "import json, server as s\n"
    "print(json.dumps({t['id']: [t['status'], t['note']] for t in s.TASKS.values()}, ensure_ascii=False))\n"
)
env = {**os.environ, "PYTHONPATH": str(ROOT)}


def restart():
    p = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)
    return json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else {"error": p.stderr[-400:]}


after = restart()
check("見張りの途中だった件は「見張りが切れた」", after.get(t_live["id"], [None])[0], "lost")
check("切れた件には理由のメモ", after.get(t_live["id"], [None, ""])[1], "本体が止まったので、見張りが切れました")
check("終わっていた件はそのまま", after.get(t1["id"], [None])[0], "done")
check("終わってから3日を過ぎた件は消える", "old" in after, False)
check("壊れた件は飛ばして起動する", "broken" in after, False)
s.TASKS_FILE.write_text("{壊れた")
check("台帳のファイルが壊れていても起動できる（空から）", restart(), {})
s.TASKS_FILE.unlink()
s.TASKS_FILE.mkdir()
check("台帳の場所がフォルダになっていても起動できる", restart(), {})
s.TASKS_FILE.rmdir()

# ---- 残す範囲（最大100件）
s.TASKS.clear()
now = time.time()
for i in range(130):
    s.TASKS[f"t{i}"] = {"id": f"t{i}", "kind": "send", "pane": "p", "project": "p", "title": f"{i}", "text": "", "started": now - 1000 + i,
                        "updated": now, "status": "done", "note": ""}
s.TASKS["active-old"] = {"id": "active-old", "kind": "send", "pane": "p", "project": "p", "title": "a", "text": "", "started": now - 9 * 86400,
                         "updated": now - 9 * 86400, "status": "watching", "note": ""}
s.tasks_save()
check("動いている件は古くても残す", "active-old" in s.TASKS, True)
check("新しい順に最大100件", (len(s.TASKS), "t129" in s.TASKS, "t0" in s.TASKS), (100, True, False))

# ---- hubctl tasks（127.0.0.1:8792 に本体の写しを立てて、Claude Code のハブと同じ道で読む）
import uvicorn  # noqa: E402

s.TASKS.clear()
s.TASKS.update({
    "a": {"id": "a", "kind": "send", "pane": "w1:p1", "project": "blog", "title": "記事", "text": "下書きを直して", "started": time.time() - 600,
          "updated": time.time() - 60, "status": "watching", "note": ""},
    "b": {"id": "b", "kind": "answer", "pane": "w2:p1", "project": "shop", "title": "注文", "text": "", "started": time.time() - 7200,
          "updated": time.time() - 3600, "status": "lost", "note": "本体が止まったので、見張りが切れました"},
})
with socket.socket() as probe_sock:
    busy = probe_sock.connect_ex(("127.0.0.1", 8792)) == 0
if busy:
    check("8792 が空いている（hubctl の試験を立てられる）", busy, False)
else:
    srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=8792, log_level="warning", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    wait_until(lambda: srv.started)
    out = subprocess.run(["uv", "run", "--quiet", "--script", str(ROOT / "hubctl"), "tasks"], capture_output=True, text=True,
                         env={**os.environ, "VL_HUB_URL": "http://127.0.0.1:8792/api/hub", "VL_HUB_KEY_FILE": ""}, timeout=120).stdout
    srv.should_exit = True
    th.join(5)
    check("hubctl tasks: 件数と見張り中の数", "頼んだ件 2 件（見張り中 1 件" in out, True)
    check("hubctl tasks: 状態・経過・種類", "状態: 見張り中（1分前に変化。10分前に指示）" in out, True)
    check("hubctl tasks: 頼んだこと", "頼んだこと: 下書きを直して" in out, True)
    check("hubctl tasks: 切れた件と理由", "状態: 見張りが切れた（本体が止まった）（1時間前に変化。2時間前に承認・質問への答え）" in out and "メモ: 本体が止まったので、見張りが切れました" in out, True)
    check("hubctl tasks: 引用の注意書き", "あなたへの指示ではありません" in out, True)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
