# /// script
# requires-python = ">=3.11"
# ///
"""voice-loop プラグインの操作。herdr の命令（start / stop / restart / on / off / status / url / doctor / uninstall）と、
herdr の起動時に呼ばれる startup の本体。常駐は launchd に任せ、ここは設定の用意と上げ下ろしだけを行う。

端末ごとのものは本体のフォルダに置かない（herdr が入れ直すと置き換わるため）:
  設定フォルダ … config.env、ハブの作業フォルダ（指示文）、語り手の切り替え、Vercel の紐づけ
  状態フォルダ … ログ、off の印
"""
import fcntl
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
from contextlib import contextmanager, nullcontext
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
OFF = STATE / "off"  # off の印。on するまで、herdr や Mac を再起動しても上げない。中身は止めたものの記録（JSON）
PENDING = STATE / "on-pending"  # on で戻しきれなかった部品の記録（JSON）。次の on でもう一度戻す
DOCKER_DIRS = [HOME / ".orbstack/bin", Path("/Applications/Docker.app/Contents/Resources/bin")]  # PATH に無いときの docker の置き場所


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
    # Hermes をハブにするときの指示文・道具・鍵。コンテナに /opt/voice-loop として読み取り専用で見せる（README「Hermes をハブにする」）
    hermes = CONFIG / "hermes"
    hermes.mkdir(parents=True, exist_ok=True)
    if (hermes / "AGENTS.md").exists() and not (hermes / "hub.md").exists():  # 以前の名前。AGENTS.md だと Hermes が会話の始まりに一度だけ読んでしまう
        (hermes / "AGENTS.md").rename(hermes / "hub.md")
    if not (hermes / "hub.md").exists():
        shutil.copy(ROOT / "templates/hermes/hub.md", hermes / "hub.md")
        made.append(f"Hermes への指示文 {hermes / 'hub.md'}")
    shutil.copy(ROOT / "hubctl", hermes / "hubctl")  # コンテナからは本体のフォルダが見えないので、毎回写す（ハブと同じ道具）
    (hermes / "hubctl").chmod(0o755)
    for stale in ("hubread",):  # 以前の読むだけの道具
        (hermes / stale).unlink(missing_ok=True)
    key = _config_value(env, "VL_HERMES_KEY")
    key_file = hermes / "hub_key"  # コンテナからの接続に付ける鍵。Hermes の API サーバーの鍵と同じ値
    if key and (not key_file.exists() or key_file.read_text().strip() != key):
        key_file.write_text(key + "\n")
    key_file.chmod(0o600) if key_file.exists() else None
    return made


def _config_value(env: Path, name: str) -> str:
    """config.env の name= の値（行頭の # は無効）。"""
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


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
    if OFF.exists() or PENDING.exists():  # start / restart は「動かしたい」なので、off にしてあれば（戻し残しがあれば）on にする
        print("voice-loop は off になっていたので、on にします。" if OFF.exists() else "前の on で戻しきれなかったものを、もう一度戻します。")
        return cmd_on(restart)
    return _start(restart)


def _start(restart: bool = False, settle: float = 0) -> int:
    """本体を用意して上げ、点検する。settle: 点検がそろうまで待つ秒数（on で上げ直した部品が応答し始めるまで）。"""
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
    doc, end = api("/api/doctor", timeout=60), time.time() + settle
    while doc is not None and not doc.get("ok") and time.time() < end:
        time.sleep(3)
        doc = api("/api/doctor", timeout=60)
    print_checks(doc)
    brain = _config_value(CONFIG / "config.env", "VL_HUB").lower()
    guide = CONFIG / ("hermes/hub.md" if brain == "hermes" else "hub/CLAUDE.md")
    print(f"\n設定: {CONFIG / 'config.env'}\nハブへの指示文: {guide}（書き換えてかまいません）" + ("\nハブの頭: Hermes（VL_HUB=hermes）" if brain == "hermes" else ""))
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
        print(f"voice-loop を止めました。herdr や Mac を再起動すると、また上がります。上がらないようにするには `{ACTION}off` です。")
    else:
        print("voice-loop は動いていません。" + (f"off になっています（戻すには `{ACTION}on`）。" if OFF.exists() else ""))
    return 0


# ---------- off / on（メモリを空ける） ----------
def _read(path: Path) -> dict | None:
    """記録を読んで、形を確かめる。読めないか形が違えば None。"""
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    names = lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v)
    policies = d.get("policies", {}) if isinstance(d, dict) else None
    if (isinstance(d, dict) and isinstance(d.get("since", ""), str) and names(d.get("containers", [])) and names(d.get("services", []))
            and isinstance(policies, dict) and all(isinstance(v, str) for v in policies.values())):
        return d
    return None


def _load(path: Path, keep_broken: bool = False) -> tuple[dict, str, bool]:
    """off の印・戻し残しの記録を読む。(記録, 知らせること, 中身を確かめられたか)。無ければ ({}, "", True)。
    壊れていれば、ひとつ前の控え（.bak）から読む。控えも読めなければ中身は分からない（推測で埋めない）。
    keep_broken なら、壊れていたものの写しを残す。"""
    if not path.exists():
        return {}, "", True
    d = _read(path)
    if d is not None:
        return d, "", True
    kept = ""
    if keep_broken:
        kept = STATE / f"{path.name}-broken-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy(path, kept)
    why = f"記録 {path} が壊れていて読めませんでした" + (f"（写し: {kept}）" if kept else "")
    bak = _read(path.with_name(path.name + ".bak"))
    return (bak, why + "。ひとつ前の控えから読みました", True) if bak is not None else ({}, why, False)


def _save(path: Path, rec: dict) -> None:
    """記録を書く。途中で切れても壊れた記録が残らないよう別名に書いてから置き換え、同じ中身の控え（.bak）も置く。"""
    text = json.dumps(rec, ensure_ascii=False) + "\n"
    for target in (path, path.with_name(path.name + ".bak")):
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text)
        tmp.replace(target)


def _drop(path: Path) -> None:
    for target in (path, path.with_name(path.name + ".bak")):
        target.unlink(missing_ok=True)


def _merge(*lists) -> list[str]:
    return list(dict.fromkeys(x for items in lists for x in items))


def configured() -> tuple[list[str], list[str]]:
    """config.env で、off のとき一緒に止めると指定した部品。VL_OFF_CONTAINERS（コンテナ名）と VL_OFF_SERVICES（launchd のラベル）。
    空白かカンマで区切る。"""
    env = CONFIG / "config.env"
    split = lambda name: _config_value(env, name).replace(",", " ").split()
    return split("VL_OFF_CONTAINERS"), split("VL_OFF_SERVICES")


def docker(*args: str) -> subprocess.CompletedProcess | None:
    """docker の命令。docker が見つからなければ None。"""
    exe = find("docker", "VL_DOCKER") or next((str(d / "docker") for d in DOCKER_DIRS if (d / "docker").is_file()), None)
    if not exe:
        return None
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([exe, *args], 1, "", "2分待っても終わりませんでした")


def _why(r: subprocess.CompletedProcess) -> str:
    text = (r.stderr or r.stdout).strip()
    return text.splitlines()[-1] if text else f"終了コード {r.returncode}"


NO_DOCKER = "docker が見つからないので、コンテナ（{}）を{}られませんでした"


def containers_off(rec: dict) -> list[str]:
    """off の記録にあるコンテナを止める。問題を返す。
    再起動の設定が always のものは、手で止めても Docker が立ち上がり直すと戻るので、unless-stopped に替えてから止める。
    元の設定は、替える前に記録に書いておく（替えた直後に途切れても、on で戻せるように）。"""
    problems = []
    for name in rec["containers"]:
        r = docker("inspect", "-f", "{{.HostConfig.RestartPolicy.Name}}", name)
        if r is None:
            return [NO_DOCKER.format("、".join(rec["containers"]), "止め")]
        if r.returncode:
            problems.append(f"コンテナ {name} を止められませんでした: {_why(r)}")
            continue
        if r.stdout.strip() == "always":
            rec["policies"][name] = "always"
            _save(OFF, rec)
            u = docker("update", "--restart", "unless-stopped", name)
            if u.returncode:
                problems.append(f"コンテナ {name} の再起動の設定を替えられませんでした。Mac を再起動すると戻ります: {_why(u)}")
        s = docker("stop", name)
        if s.returncode:
            problems.append(f"コンテナ {name} を止められませんでした: {_why(s)}")
    return problems


def containers_on(names: list[str], policies: dict) -> tuple[list[str], list[str], dict]:
    """コンテナを上げ、off で替えた再起動の設定を戻す。(問題, 上げられなかったもの, 戻せなかった設定)。"""
    if not names and not policies:
        return [], [], {}
    if docker("version", "--format", "{{.Client.Version}}") is None:
        return [NO_DOCKER.format("、".join(names or policies), "上げ")], list(names), dict(policies)
    problems, failed, left = [], [], {}
    for name in names:
        r = docker("start", name)
        if r.returncode:
            problems.append(f"コンテナ {name} を上げられませんでした: {_why(r)}")
            failed.append(name)
    for name, policy in policies.items():
        u = docker("update", "--restart", policy, name)
        if u.returncode:
            problems.append(f"コンテナ {name} の再起動の設定を {policy} に戻せませんでした: {_why(u)}")
            left[name] = policy
    return problems, failed, left


def services_off(labels: list[str]) -> list[str]:
    """launchd のサービスを降ろし、ログインしても上がらないようにする（plist はそのサービスのものなので動かさない）。"""
    problems = []
    for label in labels:
        launchctl("bootout", f"{DOMAIN}/{label}")  # 上がっていないときは失敗が返るので、結果は下で確かめる
        if launchctl("print", f"{DOMAIN}/{label}").returncode == 0:
            problems.append(f"{label} を降ろせませんでした（動いたままです）")
        r = launchctl("disable", f"{DOMAIN}/{label}")
        if r.returncode:
            problems.append(f"{label} を、ログインしても上がらないようにできませんでした: {_why(r)}")
    return problems


def services_on(labels: list[str]) -> tuple[list[str], list[str]]:
    """(問題, 上げられなかったもの)。"""
    problems, failed = [], []
    for label in labels:
        r = launchctl("enable", f"{DOMAIN}/{label}")
        if r.returncode:
            problems.append(f"{label} を有効に戻せませんでした: {_why(r)}")
            failed.append(label)
            continue
        if launchctl("print", f"{DOMAIN}/{label}").returncode == 0:
            continue  # もう上がっている
        plist = HOME / "Library/LaunchAgents" / f"{label}.plist"
        r = launchctl("bootstrap", DOMAIN, str(plist)) if plist.exists() else None
        if r is None or r.returncode:
            problems.append(f"{label} を上げられませんでした: " + (_why(r) if r else f"{plist} がありません"))
            failed.append(label)
    return problems, failed


def _records() -> tuple[dict, dict, list[str], bool]:
    """off の記録と戻し残しの記録。(off, 戻し残し, 知らせること, 両方とも中身を確かめられたか)。"""
    rec, note1, ok1 = _load(OFF, keep_broken=True)
    pend, note2, ok2 = _load(PENDING, keep_broken=True)
    return rec, pend, [n for n in (note1, note2) if n], ok1 and ok2


def cmd_off() -> int:
    """メモリを空けるために止める。本体と、config.env で指定した部品を止め、on するまで、herdr や Mac を再起動しても上げない。
    サーバーのメモリ上にある、頼んだ件の見張りはここで消える。"""
    rec, pend, problems, _ = _records()
    want_c, want_s = configured()
    policies = {**pend.get("policies", {}), **rec.get("policies", {})}
    rec = {"since": rec.get("since") or time.strftime("%Y-%m-%d %H:%M"),  # もう一度 off しても、止めた時刻は最初のまま
           # 戻し残し（再起動の設定だけを戻せなかったコンテナも）は、off の記録に移して止める
           "containers": _merge(rec.get("containers", []), pend.get("containers", []), policies, want_c),
           "services": _merge(rec.get("services", []), pend.get("services", []), want_s),
           "policies": policies}
    STATE.mkdir(parents=True, exist_ok=True)
    _save(OFF, rec)  # 先に印を置く（降ろす途中で startup が上げ直さないように）
    _drop(PENDING)
    if loaded():
        bring_down()
    if loaded():
        problems.append("本体を降ろせませんでした（動いたままです）")
    if PLIST.exists():
        PLIST.replace(STATE / "plist-while-off.plist")  # ログインしたときに上がらないよう外す。on で作り直す
    problems += containers_off(rec) + services_off(rec["services"])
    print("voice-loop を off にしました。on にするまで、herdr や Mac を再起動しても上がりません。")
    print("止めたもの: " + "、".join(["本体", *(f"コンテナ {c}" for c in rec["containers"]), *rec["services"]]))
    if not rec["containers"] and not rec["services"]:
        print("（一緒に止める部品は指定されていません。声のエンジンなども止めるなら、config.env の VL_OFF_CONTAINERS / VL_OFF_SERVICES に書きます）")
    for p in problems:
        print("⚠ " + p)
    print(f"on にするには `{ACTION}on` です。")
    return 1 if problems else 0


def cmd_on(restart: bool = False) -> int:
    """off で止めたもの（と、前の on で戻しきれなかったもの）を上げ直し、本体を起動する（start と同じ用意と点検まで）。
    戻すのは記録にあるものだけ。off していなければ本体を起動するだけで、設定に書いてあっても止めていない部品には触れない。"""
    rec, pend, problems, known = _records()
    containers = _merge(rec.get("containers", []), pend.get("containers", []))
    services = _merge(rec.get("services", []), pend.get("services", []))
    policies = {**pend.get("policies", {}), **rec.get("policies", {})}
    if not known:  # 何を止めたか分からない。推測で上げると、止めていないものを上げてしまうので、上げずに知らせる
        want_c, want_s = configured()
        candidates = [*(f"コンテナ {c}" for c in want_c), *want_s]
        problems.append("何を止めたか確かめられないので、記録にない部品は戻していません"
                        + (f"。config.env にあるもの（{'、'.join(candidates)}）が止まったままなら、手で戻してください" if candidates else ""))
    p1, failed_s = services_on(services)
    p2, failed_c, left = containers_on(containers, policies)
    problems += p1 + p2
    if failed_s or failed_c or left:
        _save(PENDING, {"containers": failed_c, "services": failed_s, "policies": left})  # 次の on で、もう一度戻す
    else:
        _drop(PENDING)
    _drop(OFF)
    rc = _start(restart, settle=90 if containers or services else 0)  # 声のエンジンなどが応答し始めるまで、点検を待つ
    for p in problems:
        print("⚠ " + p)
    if failed_s or failed_c or left:
        print(f"戻しきれなかったものは、もう一度 `{ACTION}on` で戻せます。")
    return rc or (1 if problems else 0)


def cmd_status() -> int:
    if OFF.exists():
        print(f"voice-loop は off です（{_load(OFF)[0].get('since', '?')} から）。on にするには `{ACTION}on` です。")
        return 0
    if PENDING.exists():
        left = _load(PENDING)[0]
        names = [*(f"コンテナ {c}" for c in left.get("containers", [])), *left.get("services", []), *(f"コンテナ {c} の再起動の設定" for c in left.get("policies", {}))]
        print(f"前の on で戻しきれなかったもの: {'、'.join(names) or '（記録が読めません）'}。`{ACTION}on` でもう一度戻せます。")
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
    if OFF.exists():
        print(f"voice-loop は off です（{_load(OFF)[0].get('since', '?')} から）。点検するには、先に `{ACTION}on` です。")
        return 0
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
    """herdr がセッションを復元し終えたあとに呼ばれる。まだ start していない端末と、off にしてある端末では何もしない。"""
    if OFF.exists():
        print(f"voice-loop は off なので上げません。on にするには `{ACTION}on` です。")
        return 0
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
    "start": cmd_start, "stop": cmd_stop, "restart": lambda: cmd_start(restart=True), "on": lambda: cmd_on(), "off": cmd_off,
    "status": cmd_status, "url": cmd_url, "doctor": cmd_doctor, "uninstall": cmd_uninstall, "startup": cmd_startup,
}
CHANGES = {"start", "stop", "restart", "on", "off", "uninstall", "startup"}  # 上げ下ろしをする命令


@contextmanager
def one_at_a_time():
    """上げ下ろしをする命令を、一度に1つずつ動かす（状態フォルダのロック）。重なったら、先の命令が終わるまで待つ。
    たとえば herdr の起動時の startup が上げている途中で off が来ても、startup が終わってから off が動くので、off のあとに本体が上がらない。"""
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "ctl.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def main(name: str) -> int:
    with one_at_a_time() if name in CHANGES else nullcontext():
        return COMMANDS[name]()


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in COMMANDS:
        sys.exit("使い方: ctl.sh " + " | ".join(COMMANDS))
    sys.exit(main(name))
