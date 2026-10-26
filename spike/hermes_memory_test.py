# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "httpx>=0.27"]
# ///
"""Hermes の記憶を画面（PC の「記憶」）から足す・直す・消す口の試験（server.py と scripts/hermes_memory.py のホストの側）。
コンテナの台本は差し替える（本物のコンテナでの確かめは spike/hermes_memory_container_test.py）。使い方: uv run spike/hermes_memory_test.py"""
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
HOME = Path(TMP) / "hermes"
MEM = HOME / "memories"
MEM.mkdir(parents=True)
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1", VL_HERMES_HOME=str(HOME))
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import server as s  # noqa: E402
import hermes_memory as hm  # noqa: E402
from origin_guard import problem  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


c = TestClient(s.app, base_url="http://127.0.0.1:8790")
CALLS, OUT, STILL = [], {}, {"v": False}


def fake(container, mode, req=None, wait=90, env=None):
    CALLS.append((mode, dict(req or {})))
    if mode == "limits":
        return {"result": "limits", "memory_enabled": True, "user_profile_enabled": True, "memory_char_limit": 2200, "user_char_limit": 1375}
    return dict(OUT.get(mode, {"result": "applied", "shape": ""}))


s.hermes_memory.run_script = fake
s.hermes_memory.still_running = lambda container: STILL["v"]


def view():
    return c.get("/api/hermes/memory").json()


def reset():
    CALLS.clear()
    OUT.clear()
    STILL["v"] = False
    s.HMEM_SHOWN.clear()
    s.HMEM_FILE.unlink(missing_ok=True)
    s.HMEM_LIMITS.update(at=0.0, value=None, why="")


# ---------- ホストの読み方 ----------
check("区切りはちょうど「\\n§\\n」。前後の空白を除き、空を捨て、重複は先を残す",
      hm.parse("﻿一つ目\r\n§\r\n 二つ目 \n§\n\n§\n一つ目\n§\na § b".encode()), ["一つ目", "二つ目", "a § b"])
(MEM / "USER.md").write_text("利用者の名前は山田\n§\n報告は件数から")
(MEM / "MEMORY.md").write_bytes(b"\xff\xfe broken")
t = hm.read_targets(HOME)
check("ファイルごとに 読めた・無い・読めない（壊れた UTF-8）", (t["user"]["status"], t["user"]["entries"], t["memory"]["status"]), ("ok", ["利用者の名前は山田", "報告は件数から"], "unreadable"))
(MEM / "MEMORY.md").unlink()
os.symlink(MEM / "USER.md", MEM / "MEMORY.md")
check("symlink のファイルは読めない（たどらない）", hm.read_targets(HOME)["memory"]["status"], "unreadable")
(MEM / "MEMORY.md").unlink()
check("無いファイルは missing", hm.read_targets(HOME)["memory"], {"status": "missing", "entries": []})
check("版は target と本文の SHA-256（全桁）", (len(hm.version("user", "a")), hm.version("user", "a") != hm.version("memory", "a")), (64, True))
check("「§」だけの行・空・長すぎる文は受け付けない",
      [bool(hm.text_problem(x)) for x in ("ふつうの文", "一行目\n  §  \n二行目", "  ", "あ" * (hm.TEXT_MAX + 1), None, "a § b")],
      [False, True, True, True, True, False])

# ---------- 一覧 ----------
reset()
d = view()
check("一覧: 項目・版・字数（区切り込み）・上限・有効", (d["on"], [e["text"] for e in d["targets"]["user"]["entries"]], d["targets"]["user"]["chars"],
                                       d["targets"]["user"]["limit"], d["targets"]["memory"]["status"], d["verify"], d["state_ok"]),
      (True, ["利用者の名前は山田", "報告は件数から"], len("利用者の名前は山田\n§\n報告は件数から"), 1375, "missing", False, True))
view()
check("上限は 5 分覚える（台本を何度も動かさない）", [m for m, _ in CALLS].count("limits"), 1)
v0 = d["targets"]["user"]["entries"][0]["v"]

# ---------- 足す・直す・消す ----------
r = c.post("/api/hermes/memory/add", json={"target": "user", "content": "録音の「山だ」は山田"}).json()
check("足す: 台本に target と文を渡す", (r, CALLS[-1]), ({"ok": True, "shape": ""}, ("add", {"target": "user", "content": "録音の「山だ」は山田"})))
r = c.post("/api/hermes/memory/edit", json={"target": "user", "v": v0, "content": "利用者の名前は山田（やまだ）"}).json()
check("直す: 画面に出した版から本文を引き直して渡す（画面からの本文は信じない）", (r["ok"], CALLS[-1]),
      (True, ("edit", {"target": "user", "content": "利用者の名前は山田（やまだ）", "entry": "利用者の名前は山田"})))
r = c.post("/api/hermes/memory/remove", json={"target": "user", "v": v0, "entry": "画面が偽った本文"}).json()
check("消す: 本文は版から引く（体で送られた本文は使わない）", (r["ok"], CALLS[-1]), (True, ("remove", {"target": "user", "entry": "利用者の名前は山田"})))
n = len(CALLS)
check("覚えていない版は stale（台本を動かさない）", (c.post("/api/hermes/memory/remove", json={"target": "user", "v": "0" * 64}).json(), len(CALLS) == n),
      ({"ok": False, "reason": "stale"}, True))
check("版の target と違う target なら stale", c.post("/api/hermes/memory/remove", json={"target": "memory", "v": v0}).json(), {"ok": False, "reason": "stale"})
check("知らない target は 400", c.post("/api/hermes/memory/add", json={"target": "config", "content": "x"}).status_code, 400)
check("「§」だけの行を含む文は断る（台本を動かさない）",
      (c.post("/api/hermes/memory/add", json={"target": "user", "content": "a\n§\nb"}).json()["reason"], len(CALLS) == n), ("invalid", True))

# ---------- 結果の扱いと「終わり未確認」 ----------
for res in ("changed", "refused", "invalid"):
    OUT["add"] = {"result": res, "why": "理由"}
    r = c.post("/api/hermes/memory/add", json={"target": "user", "content": "x"}).json()
    check(f"決着した結果（{res}）は、そのまま返し、終わり未確認の印は残さない", (r, s.hmem_state()), ({"ok": False, "reason": res, "why": "理由"}, {"verify": False}))
for res in ("timeout", "error", "failed"):
    s.HMEM_FILE.unlink(missing_ok=True)
    OUT["add"] = {"result": res, "why": "分からない"}
    r = c.post("/api/hermes/memory/add", json={"target": "user", "content": "x"}).json()
    check(f"当たったか分からない（{res}）は unknown で返し、印を残す（自動で送り直さない）", (r["reason"], s.hmem_state(), view()["verify"]), ("unknown", {"verify": True}, True))
OUT.pop("add")
n = len(CALLS)
STILL["v"] = True
check("印があり、前の台本がまだ動いていれば busy（動かさない）", (c.post("/api/hermes/memory/add", json={"target": "user", "content": "x"}).json(), len(CALLS) == n),
      ({"ok": False, "reason": "busy"}, True))
STILL["v"] = None
check("確かめられなければ unverifiable（動かさない）", (c.post("/api/hermes/memory/add", json={"target": "user", "content": "x"}).json(), len(CALLS) == n),
      ({"ok": False, "reason": "unverifiable"}, True))
STILL["v"] = False
check("いないと確かめられれば、印を外して書く", (c.post("/api/hermes/memory/add", json={"target": "user", "content": "x"}).json()["ok"], s.hmem_state()), (True, {"verify": False}))
s.HMEM_FILE.write_text("壊れた")
n = len(CALLS)
check("状態のファイルが読めなければ書かない", (c.post("/api/hermes/memory/add", json={"target": "user", "content": "x"}).json(), len(CALLS) == n, view()["state_ok"]),
      ({"ok": False, "reason": "state"}, True, False))
s.HMEM_FILE.unlink()
s.HMEM_FILE.mkdir()
check("状態のファイルを書けなければ、台本を動かさない", (c.post("/api/hermes/memory/add", json={"target": "user", "content": "x"}).json(), len(CALLS) == n),
      ({"ok": False, "reason": "state"}, True))
s.HMEM_FILE.rmdir()

# ---------- 守り ----------
check("記憶の口は、どれもコンテナ（ハブ）から使えない（鍵があっても）",
      [problem({"host": "127.0.0.1:8790", "x-vl-key": "k1"}, m, p, container=True) is not None
       for m, p in (("GET", "/api/hermes/memory"), ("POST", "/api/hermes/memory/add"), ("POST", "/api/hermes/memory/edit"), ("POST", "/api/hermes/memory/remove"))],
      [True] * 4)
check("足す・直す・消すの台本の命令にだけ目印が入る（上限を読む台本と、台本の本文には入らない）",
      (hm.WRITE_MARK in (hm.command("hermes", "add") or [hm.WRITE_MARK]), any(hm.WRITE_MARK in x for x in (hm.command("hermes", "limits") or [])), hm.WRITE_MARK in hm.SCRIPT),
      (True, False, False))
hub = s.HERMES_HUB
s.HERMES_HUB = False
check("Hermes がハブでなければ、一覧も書き込みも off", (view(), c.post("/api/hermes/memory/add", json={"target": "user", "content": "x"}).json()),
      ({"on": False}, {"ok": False, "reason": "off"}))
s.HERMES_HUB = hub

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
