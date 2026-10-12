# /// script
# requires-python = ">=3.11"
# ///
"""voice-loop プラグインの操作。herdr の命令（start / stop / restart / status / url / doctor / uninstall）と、
herdr の起動時に呼ばれる startup の本体。常駐は launchd に任せ、ここは設定の用意と上げ下ろしだけを行う。

端末ごとのものは本体のフォルダに置かない（herdr が入れ直すと置き換わるため）:
  設定フォルダ … config.env、ハブの作業フォルダ（指示文）、語り手の切り替え、Vercel の紐づけ
  状態フォルダ … ログ
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_ID = "u-space.voice-loop"  # herdr-plugin.toml の id と同じ
LABEL = "local.voice-loop"
PORT = 8790
ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
CONFIG = Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or HOME / ".config/herdr/plugins/config" / PLUGIN_ID)
STATE = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR") or HOME / ".local/state/herdr/plugins" / PLUGIN_ID)
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG = STATE / "server.log"
BASE = f"http://127.0.0.1:{PORT}"
DOMAIN = f"gui/{os.getuid()}"
SEARCH = [HOME / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"), HOME / ".cargo/bin",
          HOME / ".local/share/mise/shims", HOME / ".nix-profile/bin", Path("/opt/local/bin")]  # scripts/ctl.sh と揃える
VERSION = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())["version"]
HUB_ALLOW = "Bash(./hubctl:*)"
HUB_DENY = ["Edit", "Write", "NotebookEdit"]  # ハブは自分でファイルを書かない。hubctl や設定を書き換えさせない
HUB_GUARD = '"$CLAUDE_PROJECT_DIR"/.claude/hub-guard'
ACTION = f"herdr plugin action invoke {PLUGIN_ID}."  # 案内に書く命令の頭


def find(name: str, env: str | None = None) -> str | None:
    """道具の場所。herdr から呼ばれると PATH が最小限なので、よくある置き場所も探す。"""
    if env and os.environ.get(env):
        return os.environ[env]
    hit = shutil.which(name)
    if hit:
        return hit
    return next((str(d / name) for d in SEARCH if (d / name).is_file()), None)


def api(path: str, method: str = "GET", timeout: float = 10):
    """サーバーに聞く。届かなければ None。"""
    req = urllib.request.Request(BASE + path, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return None
    except (OSError, ValueError):
        return None


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def loaded() -> bool:
    return launchctl("print", f"{DOMAIN}/{LABEL}").returncode == 0


# ---------- 用意 ----------
def scaffold(uv: str) -> list[str]:
    """設定フォルダに、無いものだけを置く。利用者が書き換えたものは上書きしない。新しく置いたものを返す。"""
    made = []
    hub = CONFIG / "hub"
    for d in (CONFIG, STATE, hub, hub / ".claude"):
        d.mkdir(parents=True, exist_ok=True)
    env = CONFIG / "config.env"
    if not env.exists():
        shutil.copy(ROOT / "templates/config.env", env)
        env.chmod(0o600)  # API キーを書くことがある
        made.append(f"設定ファイル {env}")
    readings = CONFIG / "readings.txt"
    if not readings.exists():
        shutil.copy(ROOT / "templates/readings.txt", readings)
        made.append(f"読み替えの表 {readings}")
    for name in ("CLAUDE.md", "speaking.md"):
        if not (hub / name).exists():
            shutil.copy(ROOT / "templates/hub" / name, hub / name)
            made.append(f"ハブへの指示文 {hub / name}")
    # ハブは自分の作業フォルダから ./hubctl を呼ぶ。本体の場所は入れ直しで変わるので、毎回書き直す
    wrapper = hub / "hubctl"
    wrapper.write_text(f'#!/bin/sh\nexec "{uv}" run --quiet --script "{ROOT / "hubctl"}" "$@"\n')
    wrapper.chmod(0o755)
    # ハブの Bash を ./hubctl だけに絞る見張り。動かせなかったときは、通さずに止める
    guard = hub / ".claude/hub-guard"
    guard.write_text(
        f'#!/bin/sh\n"{uv}" run --quiet --script "{ROOT / "scripts/hub_guard.py"}"\nrc=$?\n[ "$rc" -eq 0 ] && exit 0\n'
        f'[ "$rc" -eq 2 ] || echo "ハブの見張りを動かせなかったので、実行を止めました。利用者がプラグインの start をやり直す必要があります。" >&2\nexit 2\n')
    guard.chmod(0o755)
    if hub_settings(hub / ".claude/settings.json"):
        made.append(f"ハブの許可ルール {hub / '.claude/settings.json'}")
    return made


def hub_settings(path: Path) -> bool:
    """ハブの作業フォルダに限った Claude Code の設定。新しく作ったら True。
    ./hubctl は確認なしで通す（毎回聞かれると、声だけでは進めなくなる）。そのかわり、ファイルを書く道具は外し、
    Bash は見張りを通して ./hubctl だけにする。利用者が足した設定は残し、この3点だけを揃える。"""
    try:
        cur, new = json.loads(path.read_text()), False
    except OSError:
        cur, new = {}, True
    except ValueError:  # 壊れていて読めない。作り直す前に控えを取る
        shutil.copy(path, STATE / f"hub-settings-broken-{time.strftime('%Y%m%d-%H%M%S')}.json")
        cur, new = {}, True
    want = json.loads(json.dumps(cur))
    perms = want.setdefault("permissions", {})
    for key, items in (("allow", [HUB_ALLOW]), ("deny", HUB_DENY)):
        have = perms.setdefault(key, [])
        have.extend(i for i in items if i not in have)
    pre = want.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if not any(h.get("command") == HUB_GUARD for entry in pre for h in entry.get("hooks", [])):
        pre.append({"matcher": "Bash", "hooks": [{"type": "command", "command": HUB_GUARD}]})
    if want != cur:
        path.write_text(json.dumps(want, ensure_ascii=False, indent=2) + "\n")
    return new


def wanted_plist(uv: str, herdr: str) -> dict:
    dirs = [str(Path(uv).parent), str(Path(herdr).parent), *map(str, SEARCH), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "server.py"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {
            "PATH": ":".join(dict.fromkeys(dirs)),  # launchd の既定 PATH には uv も herdr も無い
            "LANG": "ja_JP.UTF-8",
            "PYTHONUTF8": "1",
            "VL_CONFIG_DIR": str(CONFIG),
            "VL_STATE_DIR": str(STATE),
            "VL_PLUGIN_VERSION": VERSION,  # 版が上がると plist が変わり、次の start / startup でサーバーが新しい本体に替わる
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }


def write_plist(uv: str, herdr: str) -> bool:
    """launchd の設定を書く。中身が変わったら True。前のものは状態フォルダに控える。"""
    want = wanted_plist(uv, herdr)
    if PLIST.exists():
        try:
            if plistlib.loads(PLIST.read_bytes()) == want:
                return False
        except Exception:
            pass
        shutil.copy(PLIST, STATE / f"plist-before-{time.strftime('%Y%m%d-%H%M%S')}.plist")
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(want))
    return True


def bring_up(restart: bool, changed: bool) -> None:
    if loaded() and changed:
        bring_down()  # 降りきる前に上げ直すと失敗する
    if not loaded():
        for _ in range(8):
            if launchctl("bootstrap", DOMAIN, str(PLIST)).returncode == 0:
                break
            time.sleep(1)
    elif restart:
        launchctl("kickstart", "-k", f"{DOMAIN}/{LABEL}")
        time.sleep(1.5)  # 古いサーバーが落ちるまでは、まだ応答してしまう
    else:
        launchctl("kickstart", f"{DOMAIN}/{LABEL}")


def wait_server(seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if api("/api/agents", timeout=3) is not None:
            return True
        time.sleep(1)
    return False


# ---------- 表示 ----------
def phone_url() -> str | None:
    """tailscale serve でこのサーバーを出していれば、その URL。"""
    ts = find("tailscale") or next((p for p in ["/Applications/Tailscale.app/Contents/MacOS/Tailscale"] if Path(p).is_file()), None)
    if not ts:
        return None
    try:
        web = json.loads(subprocess.run([ts, "serve", "status", "--json"], capture_output=True, text=True, timeout=10).stdout).get("Web") or {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    for host, v in web.items():
        if any(str(h.get("Proxy", "")).rstrip("/").endswith(f":{PORT}") for h in (v.get("Handlers") or {}).values()):
            return "https://" + host.removesuffix(":443")
    return None


def print_urls() -> None:
    print(f"  この Mac から: {BASE}")
    phone = phone_url()
    if phone:
        print(f"  スマホから:   {phone}")
    else:
        print(f"  スマホから:   まだ開いていません。Tailscale を使うなら `tailscale serve --bg --https={PORT} {BASE}`")
        print("                （スマホのマイクは https でないと使えません）")


def print_checks(doc: dict | None) -> bool:
    """点検の結果を「あとやること」の形で出す。必須がすべて揃っていれば True。"""
    if doc is None:
        print("サーバーに届かないので、点検できませんでした。")
        return False
    todo = [c for c in doc["checks"] if not c["ok"] and not c["optional"]]
    extra = [c for c in doc["checks"] if not c["ok"] and c["optional"]]
    for c in doc["checks"]:
        if c["ok"]:
            print(f"  ✓ {c['name']}")
    if todo:
        print(f"\nあと {len(todo)} つ、手でやることがあります:")
        for i, c in enumerate(todo, 1):
            print(f"  {i}. {c['name']}: {c['todo']}")
        print(f"済んだら `{ACTION}doctor` で確かめられます。")
    else:
        print("\n準備はすべて揃っています。")
    for c in extra:
        print(f"  （任意）{c['name']}: {c['todo']}")
    return not todo


# ---------- 命令 ----------
def cmd_start(restart: bool = False) -> int:
    uv, herdr = find("uv", "VL_UV"), find("herdr", "HERDR_BIN_PATH")
    if not uv or not herdr:
        print(f"{'uv' if not uv else 'herdr'} が見つかりません。入れてから、もう一度実行してください。")
        return 1
    made = scaffold(uv)
    changed = write_plist(uv, herdr)
    bring_up(restart, changed)
    if not wait_server(180):  # 初回は依存の取得で時間がかかる
        print(f"サーバーが応答しません。ログを見てください: {LOG}")
        return 1
    api("/api/hub/start", "POST", timeout=150)  # ハブが居なければ立てる。結果は下の点検に出る
    print("voice-loop が動いています。")
    print_urls()
    if made:
        print("\n初回の用意として、次のものを置きました:")
        for m in made:
            print(f"  - {m}")
    print()
    print_checks(api("/api/doctor", timeout=60))
    print(f"\n設定: {CONFIG / 'config.env'}\nハブへの指示文: {CONFIG / 'hub/CLAUDE.md'}（書き換えてかまいません）")
    return 0  # 手でやることが残っていても、起動そのものは済んでいる（herdr の記録を「失敗」にしない）


def bring_down() -> None:
    launchctl("bootout", f"{DOMAIN}/{LABEL}")
    for _ in range(40):  # 降りきるまで少しかかる
        if not loaded():
            break
        time.sleep(0.25)


def cmd_stop() -> int:
    if loaded():
        bring_down()
        print(f"voice-loop を止めました。Mac を再起動すると、また上がります。上がらないようにするには `{ACTION}uninstall` です。")
    else:
        print("voice-loop は動いていません。")
    return 0


def cmd_status() -> int:
    up = api("/api/agents", timeout=5)
    print(f"常駐サービス: {'登録されています' if loaded() else '登録されていません'}")
    print(f"サーバー: {'応答あり' if up is not None else '応答なし'}")
    if up is None:
        print(f"ログ: {LOG}")
        return 1
    doc = api("/api/doctor", timeout=60) or {"checks": []}
    hub = next((c for c in doc["checks"] if c["name"] == "ハブ"), None)
    print(f"ハブ: {'います' if hub and hub['ok'] else (hub['todo'] if hub else 'いません')}")
    print(f"語り手: {doc.get('narrator', '不明')}")
    print_urls()
    return 0


def cmd_url() -> int:
    print_urls()
    return 0


def cmd_doctor() -> int:
    uv, herdr = find("uv", "VL_UV"), find("herdr", "HERDR_BIN_PATH")
    local = [
        ("uv", bool(uv), "https://docs.astral.sh/uv/ の手順で入れてください"),
        ("herdr のコマンド", bool(herdr), "herdr を入れてください"),
        ("常駐サービス", loaded(), f"`{ACTION}start` を実行してください"),
        ("サーバー", api("/api/agents", timeout=5) is not None, f"`{ACTION}start` を実行してください。上がらないときはログを見ます: {LOG}"),
    ]
    missing = [(n, t) for n, ok, t in local if not ok]
    for n, ok, _ in local:
        if ok:
            print(f"  ✓ {n}")
    if missing:
        print(f"\nあと {len(missing)} つ、手でやることがあります:")
        for i, (n, t) in enumerate(missing, 1):
            print(f"  {i}. {n}: {t}")
        return 1
    return 0 if print_checks(api("/api/doctor", timeout=60)) else 2


def cmd_uninstall() -> int:
    if loaded():
        bring_down()
    if PLIST.exists():
        STATE.mkdir(parents=True, exist_ok=True)
        kept = STATE / f"plist-removed-{time.strftime('%Y%m%d-%H%M%S')}.plist"
        shutil.move(PLIST, kept)
        print(f"常駐の登録を外しました（控え: {kept}）。")
    else:
        print("常駐の登録はありませんでした。")
    print(f"設定とハブへの指示文は残してあります: {CONFIG}\nもう一度使うときは `{ACTION}start` です。")
    return 0


def cmd_startup() -> int:
    """herdr がセッションを復元し終えたあとに呼ばれる。まだ start していない端末では何もしない。"""
    if not PLIST.exists():
        return 0
    uv, herdr = find("uv", "VL_UV"), find("herdr", "HERDR_BIN_PATH")
    if not uv or not herdr:
        print(f"{'uv' if not uv else 'herdr'} が見つからないので、voice-loop を上げられませんでした。")
        return 1
    # プラグインを入れ直すと本体の場所や版が変わる。plist と、ハブの作業フォルダに置いた呼び出し口を、いまの本体に合わせ直す
    scaffold(uv)
    changed = write_plist(uv, herdr)
    bring_up(restart=False, changed=changed)
    if not wait_server(180 if changed else 60):
        print(f"voice-loop のサーバーが応答しません。ログを見てください: {LOG}")
        return 1
    # herdr だけが立ち上がり直したとき、サーバーは動き続けていてハブだけが居ない。ここで立て直す
    api("/api/hub/start", "POST", timeout=150)
    return 0


COMMANDS = {
    "start": cmd_start, "stop": cmd_stop, "restart": lambda: cmd_start(restart=True), "status": cmd_status,
    "url": cmd_url, "doctor": cmd_doctor, "uninstall": cmd_uninstall, "startup": cmd_startup,
}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in COMMANDS:
        sys.exit("使い方: ctl.sh " + " | ".join(COMMANDS))
    sys.exit(COMMANDS[name]())
