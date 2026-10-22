# /// script
# requires-python = ">=3.11"
# ///
"""off / on（メモリを空ける）の試験。scripts/ctl.py の launchctl・docker・本体への問い合わせを作り物に差し替え、
ホーム・設定・状態のフォルダも一時フォルダにして動かす（本物の常駐・コンテナ・launchd には触れない）。
使い方: uv run spike/power_test.py"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ.update(HOME=str(TMP / "home"), HERDR_PLUGIN_CONFIG_DIR=str(TMP / "cfg"), HERDR_PLUGIN_STATE_DIR=str(TMP / "state"))
(TMP / "home/Library/LaunchAgents").mkdir(parents=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ctl  # noqa: E402

assert str(ctl.PLIST).startswith(str(TMP)) and str(ctl.STATE).startswith(str(TMP)), "一時フォルダの外を指している"


def no_real(*a, **k):
    raise AssertionError(f"本物の命令を呼ぼうとした: {a}")


ctl.subprocess.run = no_real  # 差し替え漏れがあれば落ちる
ctl.time.sleep = lambda s: None
ctl.find = lambda name, env=None: f"/fake/{name}"
ctl.phone_url = lambda: None

WHISPER = "local.whisper-server"
AGENTS = TMP / "home/Library/LaunchAgents"
(AGENTS / f"{WHISPER}.plist").write_text("whisper")
world = {"loaded": {WHISPER}, "disabled": set(), "running": {"aivis", "hermes"}, "docker": True, "doctor_ng": 0,
         "policy": {"aivis": "unless-stopped", "hermes": "unless-stopped"}, "fail": set()}  # fail: 失敗させる (命令, 相手)
calls = []


def done(rc=0, err="", out=""):
    return subprocess.CompletedProcess([], rc, out, err)


def fake_launchctl(*args):
    calls.append(("launchctl", *args))
    verb, label = args[0], args[-1].rsplit("/", 1)[-1]
    if verb == "bootstrap":
        label = Path(args[-1]).stem
    if (verb, label) in world["fail"]:
        return done(1, f"{verb} failed: Operation not permitted")
    if verb == "print":
        return done(0 if label in world["loaded"] else 113)
    if verb == "bootout":
        hit = label in world["loaded"]
        world["loaded"].discard(label)
        return done(0 if hit else 3)
    if verb == "bootstrap":
        if label in world["disabled"]:
            return done(5, "Bootstrap failed: 5: Input/output error")
        world["loaded"].add(label)
        return done()
    if verb in ("enable", "disable"):
        (world["disabled"].discard if verb == "enable" else world["disabled"].add)(label)
        return done()
    return done()  # kickstart


def fake_docker(*args):
    calls.append(("docker", *args))
    if not world["docker"]:
        return None
    verb, name = args[0], args[-1]
    if verb == "version":
        return done(out="27.0.0")
    if name not in world["policy"]:
        return done(1, f"Error response from daemon: No such container: {name}")
    if (verb, name) in world["fail"]:
        return done(1, f"Error response from daemon: {verb} failed for {name}")
    if verb == "inspect":
        return done(out=world["policy"][name] + "\n")
    if verb == "update":
        world["policy"][name] = args[2]
    elif verb in ("start", "stop"):
        (world["running"].add if verb == "start" else world["running"].discard)(name)
    return done()


def fake_api(path, method="GET", timeout=10):
    if ctl.LABEL not in world["loaded"]:
        return None
    if path == "/api/doctor":
        ok = world["doctor_ng"] <= 0
        world["doctor_ng"] -= 1
        return {"ok": ok, "checks": [{"name": "声（AivisSpeech）", "ok": ok, "optional": False, "todo": "AivisSpeech が動いていません"}]}
    return [] if path == "/api/agents" else {}


ctl.launchctl, ctl.docker, ctl.api = fake_launchctl, fake_docker, fake_api


def run(cmd):
    """命令を動かし、(終了コード, 出力) を返す。"""
    calls.clear()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ctl.COMMANDS[cmd]()
    return rc, out.getvalue()


def login():
    """Mac の再起動（ログイン）の代わり。いったん全部降り、LaunchAgents にあって止められていないものだけ上がる。
    Docker も立ち上がり直す（再起動の設定が always のコンテナは、手で止めてあっても上がる）。"""
    world["loaded"] = {p.stem for p in AGENTS.glob("*.plist") if p.stem not in world["disabled"]}
    world["running"] |= {n for n, pol in world["policy"].items() if pol == "always"}


def set_env(text):
    (TMP / "cfg").mkdir(parents=True, exist_ok=True)
    (TMP / "cfg/config.env").write_text(text)


results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


up = lambda: ctl.LABEL in world["loaded"]

rc, out = run("start")
check("start で本体が上がり、自動起動の設定ができる", (rc, up(), ctl.PLIST.exists()), (0, True, True))

set_env('VL_OFF_CONTAINERS="aivis, hermes"\nVL_OFF_SERVICES=local.whisper-server\n')
rc, out = run("off")
rec = json.loads(ctl.OFF.read_text())
check("off: 本体を降ろし、自動起動の設定を外す（控えは状態フォルダ）", (rc, up(), ctl.PLIST.exists(), (ctl.STATE / "plist-while-off.plist").exists()), (0, False, False, True))
check("off: 指定したコンテナを止める", world["running"], set())
check("off: 指定した launchd のサービスを降ろし、ログインしても上がらないようにする", (WHISPER in world["loaded"], WHISPER in world["disabled"]), (False, True))
check("off: 止めたものを記録する", (rec["containers"], rec["services"]), (["aivis", "hermes"], [WHISPER]))
check("off: 何を止めたかと、on の仕方を伝える", ("コンテナ aivis" in out and WHISPER in out and "on にするには" in out), True)

rc, out = run("startup")
check("off の間は、herdr の起動時に上げない", (rc, up(), [c for c in calls if c[0] == "launchctl"]), (0, False, []))
(ctl.STATE / "plist-while-off.plist").replace(ctl.PLIST)  # 自動起動の設定が何かの理由で戻っていても（古い版の start など）
rc, out = run("startup")
check("off の印があれば、自動起動の設定が戻っていても herdr の起動時に上げない", (rc, up()), (0, False))
ctl.PLIST.replace(ctl.STATE / "plist-while-off.plist")
login()
check("off の間は、Mac を再起動しても本体もサービスも上がらない", (up(), WHISPER in world["loaded"]), (False, False))
check("off の間の status は off と答え、失敗にしない", run("status")[0], 0)
check("off の間の status の文", "off です" in run("status")[1], True)
check("off の間の doctor は、先に on するよう案内する", (run("doctor")[0], "先に" in run("doctor")[1]), (0, True))
check("off の間の stop は、off だと伝える", "off になっています" in run("stop")[1], True)

since = rec["since"]
rc, out = run("off")
check("off をもう一度しても、止めた時刻は最初のまま", (rc, json.loads(ctl.OFF.read_text())["since"]), (0, since))

set_env("")  # off のあとで config.env から消しても、on で戻す
world["doctor_ng"] = 2  # 上げ直した直後は、声のエンジンがまだ応答しない
rc, out = run("on")
check("on: コンテナとサービスを戻し、本体を上げ、off の印を消す",
      (rc, world["running"], WHISPER in world["loaded"], WHISPER in world["disabled"], up(), ctl.PLIST.exists(), ctl.OFF.exists()),
      (0, {"aivis", "hermes"}, True, False, True, True, False))
check("on: 部品が応答し始めるまで点検を待ってから結果を出す", ("準備はすべて揃っています" in out, world["doctor_ng"] < 0), (True, True))
check("on のあとは、herdr の起動時にまた上げる", (run("startup")[0], up()), (0, True))

set_env("VL_OFF_CONTAINERS=aivis hermes\n")
run("off")
rc, out = run("start")
check("off の間の start は on として動く", (rc, "on にします" in out, up(), world["running"], ctl.OFF.exists()), (0, True, True, {"aivis", "hermes"}, False))
run("off")
rc, out = run("restart")
check("off の間の restart も on として動く", (rc, up(), ctl.OFF.exists()), (0, True, False))

set_env("VL_OFF_CONTAINERS=aivis nosuch\n")
rc, out = run("off")
check("off: 無いコンテナがあれば知らせ、ほかは止める", (rc, "No such container: nosuch" in out, up(), "aivis" in world["running"]), (1, True, False, False))
set_env("VL_OFF_CONTAINERS=aivis\n")
ctl.OFF.unlink()
run("start")
world["docker"] = False
run("off")
check("off: docker が見つからなくても本体は止め、知らせる", (up(), ctl.OFF.exists()), (False, True))
rc, out = run("on")
check("on: docker が見つからなければ知らせるが、本体は上げる", (rc, "docker が見つからない" in out, up()), (1, True, True))
check("on: 戻しきれなかったものは記録に残す（off の印は消す）", (json.loads(ctl.PENDING.read_text())["containers"], ctl.OFF.exists()), (["aivis"], False))
check("status は、戻しきれなかったものを伝える", "戻しきれなかったもの: コンテナ aivis" in run("status")[1], True)
world["docker"] = True
set_env("")  # 設定から消しても、記録に残っていれば戻す（Codex 第1回の指摘1）
rc, out = run("on")
check("戻し残しは、次の on で戻し、記録を消す", (rc, "aivis" in world["running"], ctl.PENDING.exists()), (0, True, False))

rc, out = run("off")
check("一緒に止める部品の指定が無ければ、本体だけ止めて、指定の仕方を伝える", (rc, up(), "VL_OFF_CONTAINERS" in out, [c for c in calls if c[0] == "docker"]), (0, False, True, []))
run("on")

# off していない on は、設定に書いてあっても、止めていない部品に触れない（指摘4）
world["disabled"].add(WHISPER)
world["loaded"].discard(WHISPER)  # 利用者が自分で止めているサービス
world["running"].discard("hermes")  # 利用者が自分で止めているコンテナ
set_env("VL_OFF_CONTAINERS=hermes\nVL_OFF_SERVICES=local.whisper-server\n")
rc, out = run("on")
check("off していない on は、止めていない部品に触れない", (rc, WHISPER in world["disabled"], "hermes" in world["running"], [c for c in calls if c[0] == "docker"]), (0, True, False, []))
world["disabled"].discard(WHISPER)
world["loaded"].add(WHISPER)
world["running"].add("hermes")

# launchctl の失敗を成功扱いしない（指摘2）
world["fail"] = {("bootout", WHISPER)}
rc, out = run("off")
check("サービスを降ろせなければ、off は失敗を返して知らせる", (rc, f"{WHISPER} を降ろせませんでした" in out), (1, True))
world["fail"] = {("enable", WHISPER)}
rc, out = run("on")
check("サービスを有効に戻せなければ、on は失敗を返し、戻し残しに残す", (rc, json.loads(ctl.PENDING.read_text())["services"]), (1, [WHISPER]))
world["fail"] = set()
check("その戻し残しは、次の on で戻す", (run("on")[0], WHISPER in world["loaded"], WHISPER in world["disabled"], ctl.PENDING.exists()), (0, True, False, False))
world["fail"] = {("bootout", ctl.LABEL)}
rc, out = run("off")
check("本体を降ろせなければ、off は失敗を返して知らせる", (rc, "本体を降ろせませんでした" in out), (1, True))
world["fail"] = set()
run("on")

# 壊れた off の記録で落ちない・記録を失わない（指摘3）
set_env("VL_OFF_CONTAINERS=aivis\n")
for broken in ("これは JSON ではない", "[]", "null", '{"containers": null}', '{"since": 1}', '{"policies": {"aivis": 1}}'):
    run("off")
    ctl.OFF.write_text(broken)
    for f in ctl.STATE.glob("off-broken-*"):
        f.unlink()
    check(f"壊れた off の記録 {broken!r} でも status は落ちない", run("status")[0], 0)
    rc, out = run("on")
    check(f"壊れた off の記録 {broken!r}: on は落ちず、控えを残し、設定の部品を戻して知らせる",
          (rc, len(list(ctl.STATE.glob("off-broken-*"))), "aivis" in world["running"], up(), ctl.OFF.exists(), "壊れていて" in out), (1, 1, True, True, False, True))

# 再起動の設定が always のコンテナも、off の間は止まったまま（指摘5）
world["policy"]["aivis"] = "always"
rc, out = run("off")
check("always のコンテナは、unless-stopped に替えてから止め、元の設定を記録する",
      (rc, world["policy"]["aivis"], json.loads(ctl.OFF.read_text())["policies"]), (0, "unless-stopped", {"aivis": "always"}))
login()
check("always だったコンテナも、Mac を再起動して止まったまま", "aivis" in world["running"], False)
rc, out = run("on")
check("on で上げて、再起動の設定を always に戻す", (rc, "aivis" in world["running"], world["policy"]["aivis"]), (0, True, "always"))
world["policy"]["aivis"] = "unless-stopped"

bad = [f"{name}: {g!r}（期待 {w!r}）" for name, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
