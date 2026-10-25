# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "httpx>=0.27"]
# ///
"""提案（拾う役が出す、セッションへの指示の案）の試験。本物の claude・herdr・Plaud には触れない（立てる・送る・拾う役を差し替える）。
確かめること: 預かり方（new:<位置>・フォルダ・字数・根拠・同じ根拠の出し直し・件数・入れ替え）、放っておけば消える（期限・話題が外れた・中身が変わった。
moved だけでは消えない）、送る（版・二度押し・同時・立てていない／立てた／分からない・立てているあいだに古くなった・書き戻しで巡回の変更を消さない・
起動時の送っている途中）、見送る、ハブの send / start が提案の写しを断る、コンテナから送れない、拾う役の回と通知、Hermes の案内、hubctl proposals。
hubctl の確かめには、この試験の中で 127.0.0.1:8792 に本体の写しを立てる（常駐の 8790 には触れない）。
使い方: uv run spike/proposals_test.py"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="claude", VL_HERMES_KEY="k1", VL_PROJECTS_DIR=f"{TMP}/projects",
                  VL_INTAKE="1", VL_INTAKE_HOURS="9-19", VL_INTAKE_DAYS="1-5")
for d in ("shop", "blog", "docs"):
    Path(f"{TMP}/projects/{d}").mkdir(parents=True)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


pushes: list[tuple[str, str, str]] = []


async def fake_push(title, body, tag, urgency="normal", only=None):
    pushes.append((title, body, tag))
    return "sent"


async def no_watch(*a, **k):
    return None


s.push_all = fake_push
s.watch_delegation = no_watch  # 見張りは動かさない（台帳に載るところまでを見る）
# 起動時の常駐の仕事は動かさない（本物の herdr に触れない・本物の拾う役を時間で起こさない）
s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.intake_loop = no_watch
AGENTS: list[dict] = []
s.list_agents = lambda: list(AGENTS)

NOW = datetime.now().astimezone()
TS = NOW.timestamp()
iso = lambda dt: dt.isoformat(timespec="minutes")
src = lambda sid, ago_days=0, label="録音": {"kind": "plaud", "id": sid, "at": iso(NOW - timedelta(days=ago_days)), "label": label}
FOLDERS = {"shop", "blog", "docs"}
TEXT_A = "10/16 の打ち合わせ録音で決まったとおり、Excel のひな形を2種（空・記入例）作り、仕様の資料を Word にまとめてください。"


def prop(topic, text=TEXT_A, folder="shop", sources=("p1",), why="金曜の打ち合わせまでに要る"):
    return {"topic": topic, "folder": folder, "text": text, "why": why, "sources": list(sources)}


def run(cur, delta, ts=TS):
    """拾う役の回の当て方（_intake_run の中と同じ順）。"""
    ids: dict = {}
    new, _ = s.apply_delta(cur, {"update": [], "retire": [], "seen": [], **delta}, FOLDERS, datetime.fromtimestamp(ts).astimezone(), ids)
    rep = s.apply_proposals(new, delta, ids, FOLDERS, ts)
    s.tidy_proposals(new, ts)
    return new, rep


EMPTY = {"topics": [], "faded": [], "seen": {}, "last_ok": None, "next_id": 1}
base, _ = s.apply_delta(EMPTY, {"add": [
    {"title": "ネットショップの改修", "now": "ひな形と資料を頼まれた", "next": "利用者がひな形を出す", "folder": "shop", "sources": [src("p1", 2), src("p2", 1)]},
    {"title": "フォルダの無い相談", "now": "相談が来た", "sources": [src("p3", 1)]},
    {"title": "ブログ", "now": "下書き中", "folder": "blog", "sources": [src("p5", 1)]},
    {"title": "資料集め", "now": "集め中", "folder": "docs", "sources": [src("p6", 1)]},
]}, FOLDERS, NOW)
T = {t["title"]: t["id"] for t in base["topics"]}

# ---------- 預かり方 ----------
sit, rep = run(base, {"propose": [prop(T["ネットショップの改修"])]})
p = sit["proposals"]
check("提案を預かる（番号・フォルダ・期限3日・開いた状態）", (rep["proposed"], p["open"][0]["n"], p["open"][0]["folder"], round((p["open"][0]["expires"] - TS) / 86400), p["open"][0]["state"]),
      (1, 1, "shop", 3, "open"))
check("回の記録には番号だけ（文やフォルダは載せない）", sorted(rep), ["proposal_n", "proposal_rejected", "proposal_replaced", "proposed"])
check("根拠に使った出どころを、話題ごとに控える", p["used"], {T["ネットショップの改修"]: ["p1"]})

cases = [
    ("フォルダの無い話題には出さない", prop(T["フォルダの無い相談"], folder="shop", sources=["p3"]), "folder"),
    ("話題と違うフォルダには出さない", prop(T["ブログ"], folder="shop", sources=["p5"]), "folder"),
    ("200字を超える文は預からない", prop(T["ブログ"], text="あ" * 201, folder="blog", sources=["p5"]), "length"),
    ("話題の出どころに無い根拠は預からない", prop(T["ブログ"], folder="blog", sources=["zz"]), "sources"),
    ("根拠の無い提案は預からない", prop(T["ブログ"], folder="blog", sources=[]), "sources"),
    ("無い話題には出さない", prop("t99", folder="blog", sources=["p5"]), "topic"),
    ("形の壊れた提案は預からない", {"topic": T["ブログ"], "text": "x"}, "shape"),
    ("前に使った根拠だけでは出し直さない", prop(T["ネットショップの改修"], text="同じ根拠で言い換えた提案です", sources=["p1"]), "same_basis"),
]
for name, item, why in cases:
    _, r = run(sit, {"propose": [item]})
    check(name, (r["proposed"], r["proposal_rejected"]), (0, [why]))

_, r = run(sit, {"propose": [prop(T["ブログ"], folder="blog", sources=["p5"], text="ブログの下書きを仕上げてください"), prop(T["資料集め"], folder="docs", sources=["p6"], text="資料を集めてください")]})
check("1回に預かるのは1件だけ（2件目は断る）", (r["proposed"], r["proposal_rejected"]), (1, ["more"]))

sit2, r = run(sit, {"update": [{"id": T["ネットショップの改修"], "sources": [src("p7", 0)]}],
                    "propose": [prop(T["ネットショップの改修"], text="新しい録音（10/26）をふまえ、ひな形を作り直してください", sources=["p1", "p7"])]})
closed = sit2["proposals"]["closed"]
check("新しい根拠があれば、同じ話題の提案を入れ替える（前のは replaced）",
      (r["proposed"], r["proposal_replaced"], [x["n"] for x in sit2["proposals"]["open"]], [(x["n"], x["state"]) for x in closed]), (1, True, [2], [(1, "replaced")]))
check("出どころを足しただけ（中身は同じ）の直しでは、提案は古くならない", sit2["proposals"]["open"][0]["state"], "open")

# 件数の上限（開いている提案は3件まで）
full, _ = run(sit, {"propose": [prop(T["ブログ"], folder="blog", sources=["p5"], text="ブログの下書きを仕上げてください")]})
full, _ = run(full, {"propose": [prop(T["資料集め"], folder="docs", sources=["p6"], text="資料を集めてください")]})
four, r = run(full, {"add": [{"title": "4つ目", "now": "x", "folder": "blog", "sources": [src("p8", 0)]}],
                     "propose": [prop("new:0", folder="blog", sources=["p8"], text="4つ目の作業をしてください")]})
check("開いている提案は3件まで（4件目は断る）", (len(full["proposals"]["open"]), r["proposed"], r["proposal_rejected"]), (3, 0, ["full"]))

_, r = run(sit, {"add": ["壊れた項目", {"title": "新しい話題", "now": "始まった", "folder": "docs", "sources": [src("p9", 0)]}],
                 "propose": [prop("new:1", folder="docs", sources=["p9"], text="新しい話題の下調べをしてください")]})
check("new:<位置> で、今回足した話題に出せる（壊れた項目があっても位置で解く）", (r["proposed"], r["proposal_rejected"]), (1, []))
_, r = run(sit, {"add": [{"title": "出どころの無い話題", "now": "x", "folder": "docs", "sources": []}],
                 "propose": [prop("new:0", folder="docs", sources=["p9"], text="採られなかった話題への提案")]})
check("採られなかった追加への new:<位置> は断る", r["proposal_rejected"], ["topic"])

# ---------- 放っておけば消える ----------
later, _ = run(sit, {}, TS + 3 * 86400 + 1)
check("3日で消える（expired）", [(x["n"], x["state"]) for x in later["proposals"]["closed"]], [(1, "expired")])
changed, _ = run(sit, {"update": [{"id": T["ネットショップの改修"], "now": "ひな形は要らなくなった"}]})
check("話題の中身（いま）が変わったら古くなる（stale）", [(x["state"], x["note"]) for x in changed["proposals"]["closed"]], [("stale", "話題の中身が変わった")])
gone, _ = run(sit, {"retire": [{"id": T["ネットショップの改修"], "reason": "終わった"}]})
check("話題が外れたら古くなり、根拠の控えも消える", ([(x["state"], x["note"]) for x in gone["proposals"]["closed"]], gone["proposals"]["used"]), ([("stale", "話題が外れた")], {}))
sending = json.loads(json.dumps(sit))
sending["proposals"]["open"][0].update(state="sending", attempt="a1")
s.tidy_proposals(sending, TS + 10 * 86400)
check("送っている途中の提案は、期限・古くするの対象にしない", sending["proposals"]["open"][0]["state"], "sending")
_, r = run(sending, {"update": [{"id": T["ネットショップの改修"], "sources": [src("p7", 0)]}],
                     "propose": [prop(T["ネットショップの改修"], text="入れ替えようとする提案", sources=["p7"])]})
check("送っている途中の話題には、新しい提案を出さない（入れ替えない）", r["proposal_rejected"], ["sending"])
old = json.loads(json.dumps(later))
s.tidy_proposals(old, TS + 11 * 86400)
check("閉じた提案は7日で記録から消え、番号は使い回さない", (old["proposals"]["closed"], old["proposals"]["next"]), ([], 2))

# 入れ替えが何度続いても、7日のあいだの写しの判定と、話題がある間の同じ根拠の判定は外れない（件数で切らない）
churn = json.loads(json.dumps(base))
first = None
for i in range(52):
    churn, r = run(churn, {"update": [{"id": T["ブログ"], "sources": [src(f"s{i}", 0)]}],
                           "propose": [prop(T["ブログ"], folder="blog", sources=[f"s{i}"], text=f"ブログの{i}番目の作業をしてください。根拠は録音 s{i}")]})
    first = first or churn["proposals"]["open"][-1]["n"]
s.write_json(s.SITUATION_FILE, churn)
check("52回入れ替えても、最初の提案（7日以内）の写しを断る", s.proposal_copy("ブログの0番目の作業をしてください。根拠は録音 s0"), first)
churn, _ = run(churn, {"update": [{"id": T["ブログ"], "sources": [src("s0", 0)]}]})
_, r = run(churn, {"propose": [prop(T["ブログ"], folder="blog", sources=["s0"], text="最初の根拠をもう一度使う提案")]})
check("52回入れ替えたあとでも、最初の根拠だけの提案は断る", r["proposal_rejected"], ["same_basis"])

# ---------- 写しを断る ----------
s.write_json(s.SITUATION_FILE, sit)
copies = [
    ("同じ文", TEXT_A, 1),
    ("空白・句読点だけ違う", TEXT_A.replace("、", " ").replace("。", ""), 1),
    ("提案をまるごと含む", "お願いします: " + TEXT_A + " よろしく", 1),
    ("30字以上の一部", TEXT_A[10:50], 1),
    ("30字未満の一部は写しとみなさない", TEXT_A[:12], None),
    ("言い換えは写しとみなさない（範囲どおり）", "ひな形を二つと、仕様書のワード版を用意してほしい", None),
]
for name, text, want in copies:
    check(f"写しの判定: {name}", s.proposal_copy(text), want)

# ---------- 画面・ハブの口 ----------
SPAWN: dict = {}
spawned: list[str] = []
prompted: list[tuple[str, str]] = []
HERDR_REPLY: dict = {"result": {"ok": True}}


def fake_spawn(directory, project, model):
    spawned.append(project)
    if SPAWN.get("during"):
        SPAWN["during"]()
    r = dict(SPAWN.get("result") or {"ok": True, "pane": "w9:p1", "name": "x"})
    if r.get("ok") or r.get("pane"):
        AGENTS.append({"pane": r["pane"], "cwd": str(directory), "title": "ハブが起動", "status": "idle", "hub": False, "session": "", "kind": "claude", "name": ""})
    return r


def fake_herdr(*args, timeout=60):
    if args[:2] == ("agent", "prompt"):
        prompted.append((args[2], args[3]))
        return HERDR_REPLY
    return {"result": {}}


s.spawn_session = fake_spawn
s.herdr = fake_herdr


def reset(state=None):
    """状況を sit（提案1番が開いている）に戻す。"""
    s.write_json(s.SITUATION_FILE, json.loads(json.dumps(state or sit)))
    SPAWN.clear()
    spawned.clear()
    prompted.clear()
    AGENTS.clear()


def closed_row(n):
    return next((x for x in s.situation()["proposals"]["closed"] if x["n"] == n), None)


V1 = sit["proposals"]["open"][0]["v"]
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    send = lambda n=1, v=V1: c.post("/api/proposals/send", json={"n": n, "v": v}).json()
    reset()
    check("版が違えば送らない", (send(v="x")["reason"], spawned), ("changed", []))
    check("無い番号は送らない", send(n=9)["reason"], "no_such")
    check("番号の形が違えば断る", c.post("/api/proposals/send", json={"n": "1", "v": V1}).status_code, 400)
    r = send()
    row = closed_row(1)
    check("送る: 新しいセッションを立て、提案の文を最初の指示として送る", (r["ok"], spawned, prompted), (True, ["shop"], [("w9:p1", TEXT_A)]))
    check("送ったら閉じた提案に「送った」と、立てた pane と頼んだ件の id が残る", (row["state"], row["pane"], row["task"] in s.TASKS), ("sent", "w9:p1", True))
    check("頼んだ件の台帳に「提案から」で載る", s.TASKS[row["task"]]["kind"], "proposal")
    check("片づいたときのハブへの知らせは「ユーザーが 💡 の提案から送った指示」と書く（ハブが送ったのではない）",
          ("💡 の提案から送った指示" in s.report_about({"text": TEXT_A, "task": row["task"]}), s.report_about({"text": "x", "task": None}).startswith("あなたが送った指示")), (True, True))
    check("二度押しは送らない（もう閉じている）", (send()["reason"], spawned), ("closed", ["shop"]))
    d = c.get("/api/situation").json()
    check("画面: 閉じた提案に、頼んだ件の状態が出る", (d["closed"][0]["state"], d["closed"][0]["task_status"]), ("sent", "watching"))
    s.TASKS.pop(row["task"])
    check("画面: 頼んだ件が台帳から消えたら「保存期間外」（完了とは書かない）", c.get("/api/situation").json()["closed"][0]["task_status"], "expired")

    reset()
    SPAWN["result"] = {"ok": False, "reason": "herdr が応答しません", "created": False}
    r = send()
    st = s.situation()["proposals"]["open"]
    check("立てていないと分かっているときは、開いた状態に戻す（もう一度押せる）", (r["reason"], [(x["n"], x["state"], "attempt" in x) for x in st]), ("not_started", [(1, "open", False)]))
    SPAWN.clear()
    check("…戻したあと、もう一度押せば送れる", send()["ok"], True)

    notes = {}
    for name, result, reason, state in [
        ("立てたか分からない（herdr の応答なし）→ unknown", {"ok": False, "reason": "タブを作れませんでした", "created": None}, "unknown", "unknown"),
        ("立てたが信頼の確認で止まった → failed（pane を残す）", {"ok": False, "reason": "trust", "pane": "w9:p2"}, "trust", "failed"),
        ("立てたが起動できなかった → failed（pane を残す）", {"ok": False, "reason": "セッションを起動できませんでした", "pane": "w9:p3"}, "start_failed", "failed"),
    ]:
        reset()
        SPAWN["result"] = result
        r = send()
        row = closed_row(1)
        notes[reason] = row["note"] if row else ""
        check(name + "。開いた状態には戻さない", (r["reason"], row["state"] if row else None, row.get("pane") if row else None, s.situation()["proposals"]["open"], prompted),
              (reason, state, result.get("pane"), [], []))
    check("信頼の確認で止まった・起動できなかったときは「指示はまだ送っていない」と残る",
          ("信頼" in notes["trust"], "まだ送っていません" in notes["trust"], "まだ送っていません" in notes["start_failed"]), (True, True, True))

    reset()
    def change_topic():  # 立てているあいだに巡回が話題の中身を変える
        cur = s.situation()
        next(t for t in cur["topics"] if t["id"] == T["ネットショップの改修"])["now"] = "方針が変わった"
        cur["topics"].append({"id": "t50", "title": "巡回が足した話題", "now": "x", "next": "", "folder": "", "until": "", "moved": NOW.date().isoformat(), "sources": []})
        s.write_json(s.SITUATION_FILE, cur)
    SPAWN["during"] = change_topic
    r = send()
    row = closed_row(1)
    check("立てているあいだに話題が変わったら、指示は送らずに stale（立てた pane を記録）", (r["reason"], row["state"], row["pane"], prompted), ("stale", "stale", "w9:p1", []))
    check("書き戻しで、送っているあいだの巡回の変更を消さない", any(t["id"] == "t50" for t in s.situation()["topics"]), True)

    reset()
    real_list = s.list_agents
    def list_and_change():  # 立てたセッションを一覧で探しているあいだに、巡回が話題の中身を変える
        cur = s.situation()
        next(t for t in cur["topics"] if t["id"] == T["ネットショップの改修"])["next"] = "先方の返事待ちに変わった"
        s.write_json(s.SITUATION_FILE, cur)
        return list(AGENTS)
    s.list_agents = list_and_change
    r = send()
    s.list_agents = real_list
    check("一覧を取っているあいだに話題が変わっても、送る直前に確かめて送らない", (r["reason"], closed_row(1)["state"], prompted), ("stale", "stale", []))

    for name, reply, reason, state in [
        ("指示を herdr がはっきり断った → failed", {"error": {"code": "agent_busy"}}, "send_failed", "failed"),
        ("指示を送れたか分からない（herdr の応答なし）→ unknown", {"raw": "timeout", "code": -1}, "unknown", "unknown"),
    ]:
        reset()
        HERDR_REPLY = reply
        r = send()
        check(name, (r["reason"], closed_row(1)["state"]), (reason, state))
    HERDR_REPLY = {"result": {"ok": True}}

    reset()
    async def both():  # 別の画面から同時に押す
        return await asyncio.gather(s.api_proposal_send({"n": 1, "v": V1}), s.api_proposal_send({"n": 1, "v": V1}))
    rs = [json.loads(x.body) for x in asyncio.run(both())]
    check("同時に押しても1回だけ送る", (sorted(x["ok"] for x in rs), spawned), ([False, True], ["shop"]))

    reset()
    check("見送る", (c.post("/api/proposals/drop", json={"n": 1}).json()["ok"], closed_row(1)["state"]), (True, "dropped"))
    check("見送ったものは、もう見送れない・送れない", (c.post("/api/proposals/drop", json={"n": 1}).json()["reason"], send()["reason"]), ("no_such", "closed"))

    reset()
    stuck = json.loads(json.dumps(sit))
    stuck["proposals"]["open"][0].update(state="sending", attempt="zz")
    s.write_json(s.SITUATION_FILE, stuck)
    s._recover_proposals()
    check("起動のとき、送っている途中だった提案は unknown（もう一度は送らない）", (closed_row(1)["state"], s.situation()["proposals"]["open"]), ("unknown", []))

    # ハブの send / start は提案の写しを断る（start は立てる前に）
    reset()
    AGENTS.append({"pane": "w1:p1", "cwd": f"{TMP}/projects/shop", "title": "注文", "status": "idle", "hub": False, "session": "", "kind": "claude", "name": ""})
    r = c.post("/api/hub/send", json={"pane": "w1:p1", "text": TEXT_A}).json()
    check("ハブの send: 提案の文の写しは断る", (r["reason"], r["n"], prompted), ("proposal", 1, []))
    r = c.post("/api/hub/spawn", json={"project": "shop", "text": "これを頼みます。" + TEXT_A}).json()
    check("ハブの start: 提案の文を含む最初の指示は、立てる前に断る", (r["reason"], spawned), ("proposal", []))
    s.write_json(s.SITUATION_FILE, later)  # 提案1番は期限で閉じている（7日のあいだは写しを断る）
    check("閉じた提案（7日以内）の写しも断る", c.post("/api/hub/send", json={"pane": "w1:p1", "text": TEXT_A}).json()["reason"], "proposal")
    check("言い換えた文は通る（範囲どおり。ハブの判断に任される）",
          c.post("/api/hub/send", json={"pane": "w1:p1", "text": "ひな形を二つと、仕様書のワード版を用意してほしい"}).json()["ok"], True)

    reset()
    dock = {"Host": "host.docker.internal:8790", "X-VL-Key": "k1"}
    check("コンテナから: 提案を読めるが、送る・見送るは使えない",
          (c.get("/api/hub/proposals", headers=dock).status_code, c.post("/api/proposals/send", json={"n": 1, "v": V1}, headers=dock).status_code,
           c.post("/api/proposals/drop", json={"n": 1}, headers=dock).status_code), (200, 403, 403))
    check("コンテナからの送るは、断られて何も起きない", (spawned, s.situation()["proposals"]["open"][0]["state"]), ([], "open"))
    risky = json.loads(json.dumps(sit))
    risky["proposals"]["open"][0]["text"] = "ひな形を作り、本番に反映してください"
    s.write_json(s.SITUATION_FILE, risky)
    check("画面: 取り消しにくい操作の語を含む提案に目印", c.get("/api/situation").json()["proposals"][0]["risky"], True)

    # Hermes への案内には、数と決まりだけ（提案の文は入れない）
    reset()
    s.HERMES_DIR.mkdir(parents=True, exist_ok=True)
    (s.HERMES_DIR / "hub.md").write_text("ハブへの指示（試験）")
    sysmsg = s.hermes_messages("こんにちは")[0]["content"]
    check("Hermes への案内: 提案の数と「送るのはユーザーが 💡 から」が入り、文は入らない",
          ("開いている提案" in sysmsg and "1 件" in sysmsg, "画面の 💡 から送ります" in sysmsg, "Excel" in sysmsg), (True, True, False))

# ---------- 拾う役の回と通知 ----------
captured: dict = {}


def fake_run(claude, model, prompt, cwd, budget, timeout, known):
    captured["prompt"] = prompt
    return {"ok": True, "why": "", "result": captured["delta"], "cost": 0.1, "secs": 3, "denied": [], "extra": [], "servers": [], "missing": []}


s.intake.run = fake_run
s.INTAKE_GUIDE.parent.mkdir(parents=True, exist_ok=True)
s.INTAKE_GUIDE.write_text("拾う役への指示（試験）")
s.INTAKE_CLAUDE = sys.executable
reset(base)
s._runs_append({"at": time.time(), "ok": True})  # 今日はもう朝の回が済んでいる
captured["delta"] = {"add": [], "update": [], "retire": [], "seen": [], "counts": {"read": 1, "private": 0, "unsure": 0},
                     "propose": [prop(T["ネットショップの改修"])]}
rec = s._intake_run(False)
payload = json.loads(captured["prompt"].split("```json\n", 1)[1].rsplit("\n```", 1)[0])
check("拾う役への入力: 話題の出どころの ID と、開いている・最近閉じた提案", (payload["topics"][0]["sources"][0]["id"], sorted(payload["proposals"])), ("p1", ["open", "recent"]))
check("拾う役の回: 提案を預かり、回の記録は数と番号だけ", (rec["proposed"], rec["proposal_n"], rec["proposals_open"], "text" in json.dumps(rec, ensure_ascii=False)), (1, 1, 1, False))
pushes.clear()
asyncio.run(s._intake_notify(rec))
check("新しい提案が出た回は「提案が1件あります: フォルダ」を1通（文は載せない）", pushes, [("voice-loop｜提案", "提案が1件あります: shop（💡 で見て、送るか決められます）", "intake-proposal")])
pushes.clear()
asyncio.run(s._intake_notify({**rec, "proposal_replaced": True}))
check("入れ替わった提案では通知しない", pushes, [])
asyncio.run(s._intake_notify({**rec, "morning": True}))
check("朝の回なら朝のまとめに入れて1通にする", (len(pushes), "提案 1 件" in pushes[0][1], pushes[0][0]), (1, True, "voice-loop｜朝のまとめ"))
captured["delta"] = {**captured["delta"], "propose": [prop(T["ネットショップの改修"], text="同じ根拠でもう一度")]}
rec = s._intake_run(False)
check("次の回で同じ根拠の提案は断り、理由の種類を記録する", (rec["proposed"], rec["proposal_rejected"]), (0, ["same_basis"]))

# ---------- hubctl（127.0.0.1:8792 に本体の写しを立てる） ----------
import uvicorn  # noqa: E402

reset()
AGENTS.append({"pane": "w1:p1", "cwd": f"{TMP}/projects/shop", "title": "注文", "status": "idle", "hub": False, "session": "", "kind": "claude", "name": ""})
with socket.socket() as probe_sock:
    busy = probe_sock.connect_ex(("127.0.0.1", 8792)) == 0
if busy:
    check("8792 が空いている（hubctl の試験を立てられる）", busy, False)
else:
    srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=8792, log_level="warning", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    end = time.time() + 10
    while not srv.started and time.time() < end:
        time.sleep(0.05)
    env = {**os.environ, "VL_HUB_URL": "http://127.0.0.1:8792/api/hub", "VL_HUB_KEY_FILE": ""}
    hub = lambda *args: subprocess.run(["uv", "run", "--quiet", "--script", str(ROOT / "hubctl"), *args], capture_output=True, text=True, env=env, timeout=120)
    out = hub("proposals").stdout
    check("hubctl proposals: 注意書き・番号・フォルダ・文・送るのはユーザー", ("引用データ" in out, "[1] フォルダ: shop" in out, "Excel のひな形" in out, "画面の 💡 から送ります" in out), (True, True, True, True))
    r = hub("send", "w1:p1", TEXT_A)
    check("hubctl send: 提案の写しは断り、💡 から送るよう案内する", (r.returncode != 0, "提案 1 番" in r.stderr, "💡" in r.stderr), (True, True, True))
    r = hub("start", "shop", TEXT_A)
    check("hubctl start: 提案の写しは断る", (r.returncode != 0, "提案 1 番" in r.stderr, spawned), (True, True, []))
    srv.should_exit = True
    th.join(5)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
