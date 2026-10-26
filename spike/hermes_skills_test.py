# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "httpx>=0.27"]
# ///
"""Hermes の手順の書き足し（承認待ち）を画面で採る・捨てる、と、Hermes の記憶を拾う役に渡す、の試験。
Hermes の置き場は一時のフォルダに作り、コンテナの台本（hermes_skills.run_script / still_running）は差し替える。docker にも、常駐の 8790 にも触れない。
本物のコンテナでの台本の確かめは spike/hermes_skills_container_test.py。
確かめること:
  - 読み方: symlink の記録・symlink の途中のフォルダ・通常でないファイル（FIFO・フォルダ）・大きすぎる・ほかの名前からも指される・id の形・形の違う JSON
  - 扱える形の見分け（create・edit・content の無い patch・write_file・それらの batch。delete・remove_file・content のある patch・知らない欄・知らない action は扱わない）
  - 採る: 画面の版が控えと違えば断る／台本に控えの中身が渡る／changed・unsupported は承認待ちに戻る／applied は当てた（消せなかったときの案内）／
    failed・例外・形の違い・時間切れは分からない／当てた・分からない id は二度採れない／捨てても二度当てない印は残る／
    時間切れのあとは、台本がまだ動いている・確かめられない間は断る（いないと確かめたら解く）／同時の採る／状態を書けなければ当てない／
    状態のファイルが読めなければ断る／コンテナが使えなければ採らない（分からないにしない）／本体の停止で当てている途中だった id は分からない
  - 台本の命令の形（-u 10000:10000・timeout・目印・stdin）と答えの読み方、pgrep の終了コードの読み方
  - 通知（新しい id だけ・一度だけ）・朝のまとめの件数・操作の記録・控え（0600）
  - コンテナからは採る・捨てる・差分・一覧が使えない
  - 拾う役の入力に hub_memory と hub_memory_note が入る・字数で切る・symlink の記憶は読まない・ハブが Hermes でなければ入れない
使い方: uv run spike/hermes_skills_test.py"""
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
HOME = Path(TMP) / "hermes"
PEND = HOME / "pending" / "skills"
PEND.mkdir(parents=True)
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9", VL_HERMES_HOME=str(HOME),
                  VL_HERMES_CONTAINER="hermes-test", VL_INTAKE="1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
import hermes_skills as hs  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


async def nothing(*a, **k):
    return None


pushes: list[tuple[str, str, str]] = []


async def fake_push(title, body, tag, urgency="normal", only=None):
    pushes.append((title, body, tag))
    return "sent"


s.push_all = fake_push
s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.intake_loop = s.watch_hermes_skills = nothing
s.list_agents = lambda: []

# ---------- コンテナの台本の差し替え ----------
CALLS: list[tuple[str, str, bytes]] = []
OUT: dict = {}  # 種類 → 結果（関数なら呼ぶ）
RUNNING = {"v": False}


def fake_run(container, mode, pid, raw, wait=90, env=None):
    CALLS.append((mode, pid, raw))
    r = OUT.get(mode, {"result": "ok"} if mode == "check" else {"result": "diff", "diff": f"差分 {pid}"} if mode == "diff" else
                {"result": "applied", "discarded": True} if mode == "approve" else {"result": "rejected", "discarded": True})
    return r() if callable(r) else dict(r)


REAL_RUN, REAL_STILL = hs.run_script, hs.still_running  # 命令の形の試験で使う本物
s.hermes_skills.run_script = fake_run
s.hermes_skills.still_running = lambda container: RUNNING["v"]


def modes(kind):
    return [c for c in CALLS if c[0] == kind]


def record(pid, payload, summary="読み替えに「山だ」を足す", origin="background_review"):
    d = {"id": pid, "subsystem": "skills", "action": payload.get("action", "") if isinstance(payload, dict) else "", "summary": summary,
         "origin": origin, "created_at": time.time(), "payload": payload}
    raw = json.dumps(d, ensure_ascii=False).encode()
    (PEND / f"{pid}.json").write_bytes(raw)
    return raw


PATCH = {"action": "batch", "operations": [{"action": "patch", "name": "voice-loop-hub", "old_string": "a", "new_string": "a\nb"}]}


def reset():
    for p in PEND.iterdir():
        if p.is_dir() and not p.is_symlink():
            p.rmdir()
        else:
            p.unlink()
    s.HSK_FILE.unlink(missing_ok=True)
    s.HSK_SHOWN.clear()
    s.HSK_DIFFS.clear()
    s._probes.pop("hermes-skills", None)
    CALLS.clear()
    OUT.clear()
    RUNNING["v"] = False
    pushes.clear()


# ---------- 扱える形の見分け ----------
op = lambda **k: {"action": "patch", "name": "x", "old_string": "a", "new_string": "b", **k}
shapes = {
    "create": ({"action": "create", "name": "x", "content": "c", "category": "y"}, True),
    "edit": ({"action": "edit", "name": "x", "content": "c"}, True),
    "content の無い patch": (op(), True),
    "replace_all・file_path つきの patch": (op(replace_all=True, file_path="refs/a.md"), True),
    "write_file": ({"action": "write_file", "name": "x", "file_path": "a.md", "file_content": "c"}, True),
    "batch（patch と create）": ({"action": "batch", "operations": [op(), {"action": "create", "name": "y", "content": "c"}]}, True),
    "content のある patch（全文の置き換え）": (op(content="全部"), False),
    "delete": ({"action": "delete", "name": "x"}, False),
    "remove_file": ({"action": "remove_file", "name": "x", "file_path": "a.md"}, False),
    "知らない action": ({"action": "rename", "name": "x"}, False),
    "知らない欄": (op(absorbed_into="y"), False),
    "欠けた欄（new_string が無い）": ({"action": "patch", "name": "x", "old_string": "a"}, False),
    "名前が無い": ({"action": "edit", "content": "c"}, False),
    "欄の型が違う": (op(new_string=3), False),
    "batch に delete": ({"action": "batch", "operations": [op(), {"action": "delete", "name": "x"}]}, False),
    "空の batch": ({"action": "batch", "operations": []}, False),
    "batch の上に知らない欄": ({"action": "batch", "operations": [op()], "name": "x"}, False),
    "辞書でない": ("patch", False),
}
check("扱える形の見分け", {k: hs.shape_problem(p) == "" for k, (p, _) in shapes.items()}, {k: ok for k, (_, ok) in shapes.items()})
check("コンテナの台本にも同じ見分けの関数が入る", ("def shape_problem(payload)" in hs.SCRIPT, "ID_RE = re.compile" in hs.SCRIPT), (True, True))

# ---------- 読み方 ----------
OUTSIDE = Path(TMP) / "outside.json"
OUTSIDE.write_text(json.dumps({"id": "dddddddd", "payload": PATCH, "summary": "外のファイル"}))
good = record("aaaaaaaa", PATCH)
LINKED_TO = Path(TMP) / "outside-linked.json"  # symlink の先（ほかの名前からは指されていない、通常のファイル）
LINKED_TO.write_text(json.dumps({"id": "bbbbbbbb", "payload": PATCH, "summary": "symlink の先"}))
(PEND / "bbbbbbbb.json").symlink_to(LINKED_TO)
os.mkfifo(PEND / "cccccccc.json")
(PEND / "dddddddd.json").hardlink_to(OUTSIDE)
(PEND / "eeeeeeee.json").write_bytes(b"x" * (hs.RECORD_MAX + 1))
(PEND / "short.json").write_text("{}")
(PEND / "ffffffff.json").write_text("{not json")
(PEND / "gggggggg.json").write_text(json.dumps({"id": "other123", "payload": PATCH}))
(PEND / "hhhhhhhh.json").mkdir()
(PEND / "iiiiiiii.json.tmp").write_text("{}")
t0 = time.time()
got = hs.list_pending(HOME)
check("読み方: 通常の記録だけを読み、ほか（symlink・FIFO・ほかの名前から指される・大きすぎる・id の形・壊れた JSON・id の食い違い・フォルダ）は読めないに数える",
      ([r["id"] for r in got["records"]], got["unreadable"]), (["aaaaaaaa"], 8))
check("読み方: FIFO で待たされない", time.time() - t0 < 2, True)
check("読み方: 指紋は記録の中身から作り、中身はそのまま控える", (got["records"][0]["raw"] == good, len(got["records"][0]["v"])), (True, 16))
check("名前だけの一覧（通知用）は、形の合う id を名前で拾う", hs.pending_ids(HOME), ["aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd", "eeeeeeee", "ffffffff", "gggggggg", "hhhhhhhh"])
LINKED = Path(TMP) / "linked-home"
(LINKED / "pending").mkdir(parents=True)
(LINKED / "pending" / "skills").symlink_to(PEND)
try:
    hs.list_pending(LINKED)
    linked = "読めた"
except OSError:
    linked = "OSError"
check("読み方: 承認待ちのフォルダが symlink なら開かない", (linked, hs.pending_ids(LINKED)), ("OSError", None))
LINKED2 = Path(TMP) / "linked-home2"
LINKED2.mkdir()
(LINKED2 / "pending").symlink_to(HOME / "pending")
try:
    hs.list_pending(LINKED2)
    linked = "読めた"
except OSError:
    linked = "OSError"
check("読み方: 途中のフォルダ（pending）が symlink でも開かない", linked, "OSError")
check("読み方: 承認待ちのフォルダが無ければ空", hs.list_pending(Path(TMP) / "cfg"), {"records": [], "unreadable": 0})

# ---------- 台本の命令の形と、答えの読み方 ----------
real_sub = subprocess.run
seen_cmd = {}


def fake_sub(answer):
    def run(cmd, input=None, capture_output=True, timeout=None):
        seen_cmd.update(cmd=cmd, input=input, timeout=timeout)
        if isinstance(answer, Exception):
            raise answer
        return subprocess.CompletedProcess(cmd, answer[0], answer[1], answer[2])
    return run


os.environ["VL_DOCKER"] = "/usr/bin/docker-test"
hs.subprocess.run = fake_sub((0, b"log line\nVLRESULT {\"result\": \"applied\", \"discarded\": true}\n", b""))
out = REAL_RUN("hermes-test", "approve", "aaaaaaaa", good)
cmd = seen_cmd["cmd"]
check("台本の命令: Hermes の利用者で・中で打ち切り・目印つき・中身は stdin",
      (cmd[:7], cmd[cmd.index("hermes-test") + 1:cmd.index("hermes-test") + 5], cmd[-3:], seen_cmd["input"] == good, seen_cmd["timeout"], out),
      (["/usr/bin/docker-test", "exec", "-i", "-u", "10000:10000", "-w", "/opt/hermes"], ["timeout", "--kill-after=5", "60", hs.PY],
       [hs.MARK, "approve", "aaaaaaaa"], True, 90, {"result": "applied", "discarded": True}))
REAL_RUN("hermes-test", "diff", "aaaaaaaa", good)
check("差分・点検の台本は、当てる・捨てるの目印を使わない", (seen_cmd["cmd"][-3], hs.MARK in seen_cmd["cmd"][-3]), (hs.CHECK_MARK, False))
REAL_RUN("hermes-test", "check", "", b"", env={"HERMES_HOME": "/tmp/x"})
check("試験のときだけ、置き場を -e で渡せる", "HERMES_HOME=/tmp/x" in seen_cmd["cmd"], True)
answers = {
    "時間切れ（ホスト）": (subprocess.TimeoutExpired("docker", 90), "timeout"),
    "時間切れ（コンテナの中の timeout）": ((124, b"", b""), "timeout"),
    "強制終了（kill-after）": ((137, b"", b""), "timeout"),
    "答えの行が無い": ((0, b"hello\n", b""), "error"),
    "例外（終了コード 1）": ((1, b"", b"Traceback\nImportError: x\n"), "error"),
    "答えの形が違う": ((0, b"VLRESULT [1]\n", b""), "error"),
    "docker を動かせない": (FileNotFoundError(), "error"),
}
got = {}
for name, (ans, _) in answers.items():
    hs.subprocess.run = fake_sub(ans)
    got[name] = REAL_RUN("hermes-test", "approve", "aaaaaaaa", good)["result"]
check("台本の答えの読み方", got, {k: v for k, (_, v) in answers.items()})
pg = {"いる（0）": ((0, b"123\n", b""), True), "いない（1・何も出さない）": ((1, b"", b""), False),
      "docker の失敗（1・エラーを出す）": ((1, b"", b"Error: container is not running\n"), None), "そのほかの終了コード": ((2, b"", b""), None),
      "docker が答えない": (subprocess.TimeoutExpired("docker", 20), None)}
got = {}
for name, (ans, _) in pg.items():
    hs.subprocess.run = fake_sub(ans)
    got[name] = REAL_STILL("hermes-test")
check("終わったかの確かめ（pgrep）の読み方", got, {k: v for k, (_, v) in pg.items()})
check("終わったかの確かめは、当てる・捨てるの目印を探す", seen_cmd["cmd"][-3:], ["pgrep", "-f", hs.MARK])
hs.subprocess.run = real_sub
del os.environ["VL_DOCKER"]

# ---------- 画面の口 ----------
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    view = lambda: c.get("/api/hermes/skills").json()
    post = lambda path, **b: c.post(path, json=b).json()

    reset()
    raw_a = record("aaaaaaaa", PATCH)
    raw_b = record("bbbbbbbb", {"action": "delete", "name": "voice-loop-hub"}, summary="手順を消す", origin="assistant_tool")
    v = view()
    items = {i["id"]: i for i in v["items"]}
    check("一覧: 承認待ちが出る（いつ・どこから・要約・採れるか）",
          (v["on"], v["problem"], items["aaaaaaaa"]["summary"], items["aaaaaaaa"]["origin"], items["aaaaaaaa"]["can_apply"], items["aaaaaaaa"]["unsupported"]),
          (True, "", "読み替えに「山だ」を足す", "background_review", True, ""))
    check("一覧: 扱えない形は採れない（理由つき）", (items["bbbbbbbb"]["can_apply"], items["bbbbbbbb"]["unsupported"]), (False, "delete は扱えません"))
    va = items["aaaaaaaa"]["v"]
    r = post("/api/hermes/skills/diff", id="aaaaaaaa", v=va)
    post("/api/hermes/skills/diff", id="aaaaaaaa", v=va)
    check("差分: 画面に出した中身で台本を呼ぶ・版ごとに覚える", (r, [(m, p, raw == raw_a) for m, p, raw in modes("diff")]),
          ({"ok": True, "diff": "差分 aaaaaaaa"}, [("diff", "aaaaaaaa", True)]))
    check("差分: 画面の版が違えば読まない", post("/api/hermes/skills/diff", id="aaaaaaaa", v="0" * 16).get("reason"), "stale")
    check("id の形が違えば 400", c.post("/api/hermes/skills/approve", json={"id": "../x", "v": va}).status_code, 400)

    r = post("/api/hermes/skills/approve", id="aaaaaaaa", v="0" * 16)
    check("採る: 画面の版が控えと違えば断り、台本を呼ばない", (r.get("reason"), modes("approve")), ("stale", []))
    r = post("/api/hermes/skills/approve", id="bbbbbbbb", v=items["bbbbbbbb"]["v"])
    check("採る: 扱えない形は断り、台本を呼ばない", (r.get("reason"), modes("approve")), ("unsupported", []))

    r = post("/api/hermes/skills/approve", id="aaaaaaaa", v=va)
    st = s.hsk_state()["items"]["aaaaaaaa"]
    arch = s.HSK_DIR / "approved" / "aaaaaaaa.json"
    check("採る: 控えの中身を台本に渡して当てる（当てる前にコンテナを確かめ直す）",
          (r, [c_[0] for c_ in CALLS[-2:]], modes("approve")[0][2] == raw_a), ({"ok": True, "discarded": True}, ["check", "approve"], True))
    check("採る: 当てた印・控え（0600・中身つき）・操作の記録が残る",
          (st["state"], oct(arch.stat().st_mode & 0o777), json.loads(arch.read_text())["record"] == raw_a.decode(),
           [(l["act"], l["result"]) for l in map(json.loads, s.HSK_LOG.read_text().splitlines())][-1]),
          ("applied", "0o600", True, ("approve", "applied")))
    check("操作の記録に記録の中身（差分の文）は書かない", "new_string" in s.HSK_LOG.read_text(), False)
    n = len(modes("approve"))
    check("採る: 当てた id は二度採れない（記録が残っていても）", (post("/api/hermes/skills/approve", id="aaaaaaaa", v=va).get("reason"), len(modes("approve"))), ("applied", n))

    reset()
    record("aaaaaaaa", PATCH)
    OUT["approve"] = {"result": "applied", "discarded": False}
    va = view()["items"][0]["v"]
    post("/api/hermes/skills/approve", id="aaaaaaaa", v=va)
    it = view()["items"][0]
    check("当てたが承認待ちを消せなかったとき: 残り、採れず、案内が出る", (it["state"], it["can_apply"], "捨てるで消せます" in it["note"]), ("applied", False, True))
    r = post("/api/hermes/skills/reject", id="aaaaaaaa", v=va)
    check("…捨てると、承認待ちは消すが、当てた印は消さない", (r["ok"], s.hsk_state()["items"]["aaaaaaaa"]["state"], "dropped" in s.hsk_state()["items"]["aaaaaaaa"]),
          (True, "applied", True))

    for res, want_state in (("changed", None), ("unsupported", None)):
        reset()
        record("aaaaaaaa", PATCH)
        OUT["approve"] = {"result": res, "why": "x"}
        va = view()["items"][0]["v"]
        r = post("/api/hermes/skills/approve", id="aaaaaaaa", v=va)
        check(f"台本が {res}: 当てていないので承認待ちに戻る（印も控えも残さない）",
              (r.get("reason"), s.hsk_state()["items"].get("aaaaaaaa"), (s.HSK_DIR / "approved" / "aaaaaaaa.json").exists()), (res, want_state, False))
    check("…当てていないと分かったあとは、画面の取り直しが要る（同じ版では押せない）", post("/api/hermes/skills/approve", id="aaaaaaaa", v=va).get("reason"), "stale")

    for res in ({"result": "failed", "why": "patch would fail"}, {"result": "error", "why": "ImportError"}, lambda: 1 / 0):
        reset()
        record("aaaaaaaa", PATCH)
        OUT["approve"] = res
        va = view()["items"][0]["v"]
        r = post("/api/hermes/skills/approve", id="aaaaaaaa", v=va)
        it = view()["items"][0]
        name = res["result"] if isinstance(res, dict) else "本体の例外"
        check(f"台本が {name}: 当たったか分からない。もう採れない（捨てるは出す）",
              (r.get("reason"), it["state"], it["can_apply"], "当たったか分かりません" in it["note"]), ("unknown", "unknown", False, True))
        n = len(modes("approve"))
        check(f"…{name} の id は二度採らない", (post("/api/hermes/skills/approve", id="aaaaaaaa", v=va).get("reason"), len(modes("approve"))), ("unknown", n))
    r = post("/api/hermes/skills/reject", id="aaaaaaaa", v=va)
    check("分からない id も捨てられ、捨てても分からない印は残る", (r["ok"], s.hsk_state()["items"]["aaaaaaaa"]["state"]), (True, "unknown"))

    reset()
    record("aaaaaaaa", PATCH)
    record("bbbbbbbb", PATCH)
    OUT["approve"] = {"result": "timeout", "why": "90 秒待っても終わりませんでした"}
    v = view()
    vs = {i["id"]: i["v"] for i in v["items"]}
    r = post("/api/hermes/skills/approve", id="aaaaaaaa", v=vs["aaaaaaaa"])
    check("時間切れ: 分からない・前の処理の終わりを確かめるまで断る印", (r.get("reason"), s.hsk_state()["items"]["aaaaaaaa"]["state"], s.hsk_state()["verify"], view()["verify"]),
          ("unknown", "unknown", True, True))
    OUT["approve"] = {"result": "applied", "discarded": True}
    n = len(modes("approve"))
    RUNNING["v"] = True
    r1 = post("/api/hermes/skills/approve", id="bbbbbbbb", v=vs["bbbbbbbb"])
    r2 = post("/api/hermes/skills/reject", id="bbbbbbbb", v=vs["bbbbbbbb"])
    RUNNING["v"] = None
    r3 = post("/api/hermes/skills/approve", id="bbbbbbbb", v=vs["bbbbbbbb"])
    check("…台本がまだ動いている・確かめられない間は、採る・捨てるを断る（時間だけでは解かない）",
          (r1.get("reason"), "まだ動いて" in r1.get("why", ""), r2.get("reason"), r3.get("reason"), "確かめられません" in r3.get("why", ""), len(modes("approve")), len(modes("reject"))),
          ("busy", True, "busy", "busy", True, n, 0))
    RUNNING["v"] = False
    r = post("/api/hermes/skills/approve", id="bbbbbbbb", v=vs["bbbbbbbb"])
    check("…いないと確かめたら印を解き、次を当てる", (r["ok"], s.hsk_state()["verify"], len(modes("approve"))), (True, False, n + 1))

    reset()
    record("aaaaaaaa", PATCH)
    OUT["check"] = {"result": "error", "why": "Error: container hermes-test is not running"}
    v = view()
    r = post("/api/hermes/skills/approve", id="aaaaaaaa", v=v["items"][0]["v"])
    check("コンテナが使えない: 欄に理由を出し、採るを出さない", (v["problem"].startswith("Hermes のコンテナ（hermes-test）で台本を動かせません"), v["items"][0]["can_apply"]), (True, False))
    check("…押されても当てず、分からないにもしない", (r.get("reason"), modes("approve"), s.hsk_state()["items"].get("aaaaaaaa")), ("broken", [], None))

    reset()
    record("aaaaaaaa", PATCH)
    va = view()["items"][0]["v"]
    s._probes.pop("hermes-skills", None)
    OUT["check"] = {"result": "error", "why": "AttributeError: apply_skill_pending"}
    r = post("/api/hermes/skills/approve", id="aaaaaaaa", v=va)
    check("一覧のあとにコンテナが変わっても、当てる直前に確かめ直す", (r.get("reason"), modes("approve")), ("broken", []))

    reset()
    record("aaaaaaaa", PATCH)
    va = view()["items"][0]["v"]
    s.HSK_DIR.mkdir(parents=True, exist_ok=True)
    s.HSK_DIR.chmod(0o500)
    try:
        r = post("/api/hermes/skills/approve", id="aaaaaaaa", v=va)
    finally:
        s.HSK_DIR.chmod(0o700)
    check("状態を書けなければ当てない", (r.get("reason"), modes("approve")), ("state", []))

    reset()
    record("aaaaaaaa", PATCH)
    va = view()["items"][0]["v"]
    s.HSK_FILE.write_text("{壊れた")
    r = post("/api/hermes/skills/approve", id="aaaaaaaa", v=va)
    v = view()
    check("状態のファイルが読めなければ、採る・捨てるを断り、欄に出す", (r.get("reason"), modes("approve"), v["items"][0]["can_apply"], "読めません" in v["problem"]),
          ("busy", [], False, True))

    reset()
    record("aaaaaaaa", PATCH)
    va = view()["items"][0]["v"]
    OUT["approve"] = lambda: (time.sleep(0.6), {"result": "applied", "discarded": False})[1]
    outs = []
    ths = [threading.Thread(target=lambda: outs.append(post("/api/hermes/skills/approve", id="aaaaaaaa", v=va))) for _ in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    check("同時の採る: 当てるのは1回だけ", (sorted(str(o.get("ok")) + str(o.get("reason")) for o in outs), len(modes("approve"))), (["Falseapplied", "TrueNone"], 1))

    reset()
    record("aaaaaaaa", PATCH)
    va = view()["items"][0]["v"]
    r = post("/api/hermes/skills/reject", id="aaaaaaaa", v=va)
    check("捨てる: 控えの中身を台本に渡し、捨てた印を残す", (r, modes("reject")[0][2] == (PEND / "aaaaaaaa.json").read_bytes(), s.hsk_state()["items"]["aaaaaaaa"]["state"]),
          ({"ok": True, "discarded": True}, True, "rejected"))
    check("…同じ画面の版で二度は捨てない（画面の取り直しが要る）", post("/api/hermes/skills/reject", id="aaaaaaaa", v=va).get("reason"), "stale")
    reset()
    record("aaaaaaaa", PATCH)
    va = view()["items"][0]["v"]
    OUT["reject"] = {"result": "timeout", "why": "x"}
    r = post("/api/hermes/skills/reject", id="aaaaaaaa", v=va)
    check("捨てるの時間切れでも、前の処理の終わりを確かめるまで断る", (r.get("reason"), s.hsk_state()["verify"]), ("timeout", True))

    reset()
    s.write_json(s.HSK_FILE, {"items": {"aaaaaaaa": {"state": "applying", "at": time.time(), "summary": "x"}}, "told": {}})
    s._recover_hermes_skills()
    check("本体の停止で当てている途中だった id は、起動のとき分からないにし、終わりを確かめるまで断る",
          (s.hsk_state()["items"]["aaaaaaaa"]["state"], s.hsk_state()["verify"]), ("unknown", True))

    # 保存の失敗が重なっても、台本の終わりを確かめるまで断る（a2a 1 回目の指摘の再現手順）
    real_wj = s.write_json
    FAIL = {"on": False}

    def flaky_wj(path, data):
        if FAIL["on"] and Path(path) == s.HSK_FILE:
            raise OSError(28, "No space left on device")
        return real_wj(path, data)

    def then_fail(result):
        def f():
            FAIL["on"] = True
            return dict(result)
        return f

    PGREPS = []
    s.write_json = flaky_wj
    s.hermes_skills.still_running = lambda container: (PGREPS.append(container), RUNNING["v"])[1]

    reset()
    record("aaaaaaaa", PATCH)
    va = view()["items"][0]["v"]
    post("/api/hermes/skills/approve", id="aaaaaaaa", v=va)
    PGREPS.clear()
    record("bbbbbbbb", PATCH)
    vb = {i["id"]: i["v"] for i in view()["items"]}["bbbbbbbb"]
    post("/api/hermes/skills/reject", id="bbbbbbbb", v=vb)
    check("ふつうに当て・捨てたあとは印が残らない（次の操作で終わりの確かめを呼ばない）", (s.hsk_state()["verify"], PGREPS), (False, []))

    reset()
    record("aaaaaaaa", PATCH)
    va = view()["items"][0]["v"]
    OUT["approve"] = then_fail({"result": "timeout", "why": "x"})
    post("/api/hermes/skills/approve", id="aaaaaaaa", v=va)
    FAIL["on"] = False
    st = s.hsk_state()
    check("時間切れのあと保存に失敗しても、当てている途中と終わり未確認の印は残る（当てる前に書いてある）",
          (st["items"]["aaaaaaaa"]["state"], st["verify"]), ("applying", True))
    PGREPS.clear()
    RUNNING["v"] = True
    r = post("/api/hermes/skills/reject", id="aaaaaaaa", v=va)
    check("…台本がまだ動いている間は、捨てるも断る（終わりを確かめに行く）", (r.get("reason"), len(PGREPS), modes("reject")), ("busy", 1, []))
    RUNNING["v"] = False
    r = post("/api/hermes/skills/reject", id="aaaaaaaa", v=va)
    check("…いないと確かめたら、当てている途中は分からないにして、捨てる（分からない印は残す）",
          (r.get("ok"), s.hsk_state()["items"]["aaaaaaaa"]["state"], s.hsk_state()["verify"]), (True, "unknown", False))

    reset()
    record("aaaaaaaa", PATCH)
    record("bbbbbbbb", PATCH)
    vs = {i["id"]: i["v"] for i in view()["items"]}
    OUT["reject"] = then_fail({"result": "timeout", "why": "x"})
    post("/api/hermes/skills/reject", id="aaaaaaaa", v=vs["aaaaaaaa"])
    FAIL["on"] = False
    check("捨てるの時間切れのあと保存に失敗しても、終わり未確認の印は残る（動かす前に書いてある）", s.hsk_state()["verify"], True)
    PGREPS.clear()
    RUNNING["v"] = True
    r = post("/api/hermes/skills/approve", id="bbbbbbbb", v=vs["bbbbbbbb"])
    check("…台本がまだ動いている間は、ほかの書き足しも採らない", (r.get("reason"), len(PGREPS), modes("approve")), ("busy", 1, []))
    RUNNING["v"] = False
    check("…いないと確かめたら採る", post("/api/hermes/skills/approve", id="bbbbbbbb", v=vs["bbbbbbbb"]).get("ok"), True)

    reset()
    real_wj(s.HSK_FILE, {"items": {"aaaaaaaa": {"state": "applying", "at": time.time(), "summary": "x"}}, "told": {}})
    FAIL["on"] = True
    s._recover_hermes_skills()
    FAIL["on"] = False
    record("bbbbbbbb", PATCH)
    vb = view()["items"][0]["v"]
    PGREPS.clear()
    RUNNING["v"] = True
    r = post("/api/hermes/skills/approve", id="bbbbbbbb", v=vb)
    check("起動時の回復の保存に失敗しても（当てている途中が残り、印が無い）、終わりを確かめるまで採らない",
          (r.get("reason"), len(PGREPS), modes("approve")), ("busy", 1, []))
    RUNNING["v"] = False
    r = post("/api/hermes/skills/approve", id="bbbbbbbb", v=vb)
    check("…いないと確かめたら、残っていた当てている途中は分からないにして、次を採る",
          (r.get("ok"), s.hsk_state()["items"]["aaaaaaaa"]["state"]), (True, "unknown"))
    s.write_json = real_wj
    s.hermes_skills.still_running = lambda container: RUNNING["v"]

    reset()
    dock = {"Host": "host.docker.internal:8790", "X-VL-Key": "k1"}
    record("aaaaaaaa", PATCH)
    va = view()["items"][0]["v"]
    codes = (c.get("/api/hermes/skills", headers=dock).status_code,
             c.post("/api/hermes/skills/diff", json={"id": "aaaaaaaa", "v": va}, headers=dock).status_code,
             c.post("/api/hermes/skills/approve", json={"id": "aaaaaaaa", "v": va}, headers=dock).status_code,
             c.post("/api/hermes/skills/reject", json={"id": "aaaaaaaa", "v": va}, headers=dock).status_code)
    check("コンテナからは、一覧・差分・採る・捨てるのどれも使えない（鍵つきでも）", (codes, modes("approve"), modes("reject")), ((403, 403, 403, 403), [], []))

    def tree():
        """置き場の中のファイルの名前・中身・時刻（symlink はたどらない）。"""
        return sorted((str(q.relative_to(HOME)), q.is_symlink(), q.lstat().st_mtime_ns,
                       q.read_bytes() if q.is_file() and not q.is_symlink() else b"") for q in HOME.rglob("*"))
    reset()
    record("aaaaaaaa", PATCH)
    record("bbbbbbbb", PATCH)
    record("cccccccc", PATCH)
    (PEND / "dddddddd.json").symlink_to(OUTSIDE)
    before, outside_before = tree(), OUTSIDE.read_bytes()
    vs = {i["id"]: i["v"] for i in view()["items"]}
    post("/api/hermes/skills/diff", id="aaaaaaaa", v=vs["aaaaaaaa"])
    post("/api/hermes/skills/approve", id="aaaaaaaa", v=vs["aaaaaaaa"])
    OUT["approve"] = {"result": "timeout", "why": "x"}
    post("/api/hermes/skills/approve", id="bbbbbbbb", v=vs["bbbbbbbb"])
    post("/api/hermes/skills/reject", id="cccccccc", v=vs["cccccccc"])
    s._hsk_tidy(set(hs.pending_ids(HOME)))
    hs.read_memory(HOME, 3000)
    check("本体の口を一通り使っても、Hermes の置き場の中は変わらない（書き換えはコンテナの台本だけ）・symlink の先にも触れない",
          (tree() == before, OUTSIDE.read_bytes() == outside_before), (True, True))

    s.HERMES_HUB = False
    check("ハブが Hermes でなければ欄を出さない", view(), {"on": False})
    s.HERMES_HUB = True

# ---------- 通知・朝のまとめ ----------
reset()
record("aaaaaaaa", PATCH)
record("bbbbbbbb", PATCH)
new1 = s._hsk_tidy(set(hs.pending_ids(HOME)))
new2 = s._hsk_tidy(set(hs.pending_ids(HOME)))
record("cccccccc", PATCH)
new3 = s._hsk_tidy(set(hs.pending_ids(HOME)))
check("通知: まだ知らせていない id だけを、一度だけ", (new1, new2, new3), (["aaaaaaaa", "bbbbbbbb"], [], ["cccccccc"]))
old = time.time() - s.HSK_KEEP - 10
s.write_json(s.HSK_FILE, {"items": {"zzzzzzzz": {"state": "applied", "at": old}, "aaaaaaaa": {"state": "unknown", "at": old}},
                          "told": {"zzzzzzzz": old, "aaaaaaaa": old}})
s._hsk_tidy(set(hs.pending_ids(HOME)))
st = s.hsk_state()
check("30 日を過ぎた状態と印は消すが、承認待ちに残っている id のものは残す", (sorted(st["items"]), sorted(st["told"])), (["aaaaaaaa"], ["aaaaaaaa", "bbbbbbbb", "cccccccc"]))


pushes.clear()
s.write_json(s.SITUATION_FILE, {"topics": [], "faded": [], "seen": {}, "last_ok": time.time(), "next_id": 1})
asyncio.run(s._intake_notify({"ok": True, "morning": True}))
check("朝のまとめに、承認待ちの件数を足す", "Hermes の書き足し 3 件（💡）" in pushes[-1][1], True)
check("通知に中身（要約・差分）は載せない", "山だ" in json.dumps(pushes, ensure_ascii=False), False)

# ---------- (3) 記憶を拾う役へ ----------
MEM = HOME / "memories"
MEM.mkdir()
(MEM / "USER.md").write_text("利用者の名前は「山田」。録音では「山だ」と書き起こされることがある\n§\n返事は結論から\n§\n")
(MEM / "MEMORY.md").write_text("週次定例は情報交換の場\n")
entries, cut = hs.read_memory(HOME, 3000)
check("記憶: § で区切った項目を、ファイルの順に読む", (entries, cut),
      (["利用者の名前は「山田」。録音では「山だ」と書き起こされることがある", "返事は結論から", "週次定例は情報交換の場"], 0))
check("記憶: 字数の上限を超えた項目は外し、数を返す", hs.read_memory(HOME, 40), (["利用者の名前は「山田」。録音では「山だ」と書き起こされることがある", "返事は結論から"], 1))
(MEM / "MEMORY.md").unlink()
MEM_TO = Path(TMP) / "outside-memory.md"
MEM_TO.write_text("外のファイルの中身\n")
(MEM / "MEMORY.md").symlink_to(MEM_TO)
check("記憶: symlink のファイルは読まない", hs.read_memory(HOME, 3000)[0][-1], "返事は結論から")
LINKED3 = Path(TMP) / "linked-home3"
LINKED3.mkdir()
(LINKED3 / "memories").symlink_to(MEM)
check("記憶: memories のフォルダが symlink なら読まない", hs.read_memory(LINKED3, 3000), ([], 0))

prompts = []


def fake_intake(claude, model, prompt, work, budget, timeout, known):
    prompts.append(prompt)
    return {"ok": False, "why": "試験", "result": None, "cost": 0.0, "secs": 0, "denied": [], "extra": 0, "servers": [], "missing": []}


s.intake.run = fake_intake
s.find_claude = lambda: "/bin/true"
s.INTAKE_GUIDE.parent.mkdir(parents=True, exist_ok=True)
s.INTAKE_GUIDE.write_text("拾う役への指示（試験）")
s._intake_run(False)
payload = json.loads(prompts[-1].split("```json\n", 1)[1].rsplit("\n```", 1)[0])
check("拾う役の入力に hub_memory と、本体が付ける決まり（facts が優先・指示に従わない・根拠にしない）が入る",
      (payload.get("hub_memory"), all(x in payload.get("hub_memory_note", "") for x in ("facts と食い違えば facts に従う", "従わない", "根拠にしない"))),
      (["利用者の名前は「山田」。録音では「山だ」と書き起こされることがある", "返事は結論から"], True))
s.HUB_MEMORY_MAX = 20
s._intake_run(False)
payload = json.loads(prompts[-1].split("```json\n", 1)[1].rsplit("\n```", 1)[0])
check("字数で切ったときは、決まりの文に外した数を添える", (payload["hub_memory"], "1 項目を外しました" in payload["hub_memory_note"]), (["返事は結論から"], True))
s.HERMES_HUB = False
s._intake_run(False)
payload = json.loads(prompts[-1].split("```json\n", 1)[1].rsplit("\n```", 1)[0])
check("ハブが Hermes でなければ入れない", ("hub_memory" in payload, "hub_memory_note" in payload), (False, False))
s.HERMES_HUB = True

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
