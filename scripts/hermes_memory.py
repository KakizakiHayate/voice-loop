"""Hermes の記憶（memories/USER.md・MEMORY.md）を、画面（PC の「記憶」）から読む・足す・直す・消すための部品。

Hermes は記憶を「§」だけの行で区切った項目として持ち、会話の始まりに一度だけ読み込む（直した記憶は次の会話から効く）。
守り（README「守り」）。相手は外の文面に乗せられた Hermes で、置き場（ホストの ~/.hermes = コンテナの /opt/data）を同じ利用者で書ける:
  - ホストの側は、置き場で何も書かない。読むのは記憶の 2 ファイルだけで、hermes_skills と同じく symlink をたどらずに開く
  - 書くのは、コンテナの中で Hermes の利用者として、ここに固定した台本（SCRIPT）で行う。利用者の文は stdin の JSON で渡す
  - 台本は Hermes の設定の loader（hermes_cli.config）を読み込まない。読み込むだけで置き場に SOUL.md や設定の控えを書くため。
    設定は自分で読み、記憶の Store を明示して作り、Hermes 自身の apply_memory_pending で当てる（承認待ちは作らない。画面で利用者が押すのが承認）
  - 書く前に、記憶のフォルダとファイル（.lock を含む）が symlink・hardlink でない、ふつうの形かを確かめる。確かめてから書くまでの間に、
    同じ利用者のプログラムが差し替える競り合いまでは防がない（コンテナの利用者がもともと持っている以上の書き込みの権限は増えない）
"""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from hermes_skills import MEMORY_MAX, PY, RUN_LIMIT, HOST_WAIT, USER, WORKDIR, _open_dir, _read_at, docker_bin

TARGETS = {"user": "USER.md", "memory": "MEMORY.md"}  # Hermes の target と、ファイルの名前
DELIM = "\n§\n"  # Hermes の区切り（ちょうどこの 3 字）
BAD_LINE = re.compile(r"(?m)^[ \t]*§[ \t]*$")  # 「§」だけの行。項目の文に入れると、読み直したときに項目が割れる
TEXT_MAX = 5000  # 画面から書く 1 項目の上限（実際の上限は Hermes の設定の字数で、Store が判断する）
WRITE_MARK = "vl-memwrite"  # 足す・直す・消すの台本の命令に入る目印。終わったかの確かめ（pgrep -f）に使う
LIMITS_MARK = "vl-memlimits"  # 上限を読むだけの台本の目印（終わったかの確かめに数えない）
CONFIG_MAX = 1024 * 1024


def parse(raw: bytes) -> list[str]:
    """Hermes と同じ読み方: utf-8-sig の厳密な decode（失敗は UnicodeDecodeError）、改行をそろえ、ちょうど区切りで分け、前後の空白を除き、
    空を捨て、重複は先のものを残す（tools/memory_tool_store.py の _read_raw_checked・_parse_entries・load_from_disk）。"""
    text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    return list(dict.fromkeys(e for e in (x.strip() for x in text.split(DELIM)) if e))


def read_targets(home: Path) -> dict:
    """記憶のファイルごとに {status: ok / missing / unreadable, entries}。symlink のフォルダ・ファイル、通常でないもの、大きすぎるもの、
    壊れた UTF-8 は unreadable（読めない）。"""
    try:
        dfd = _open_dir(home, ("memories",))
    except FileNotFoundError:
        return {t: {"status": "missing", "entries": []} for t in TARGETS}
    except OSError:
        return {t: {"status": "unreadable", "entries": []} for t in TARGETS}
    out = {}
    try:
        for t, name in TARGETS.items():
            try:
                out[t] = {"status": "ok", "entries": parse(_read_at(dfd, name, MEMORY_MAX))}
            except FileNotFoundError:
                out[t] = {"status": "missing", "entries": []}
            except (OSError, ValueError):  # UnicodeDecodeError も ValueError
                out[t] = {"status": "unreadable", "entries": []}
    finally:
        os.close(dfd)
    return out


def version(target: str, text: str) -> str:
    """画面に出した項目の版（本文と target の SHA-256）。"""
    return hashlib.sha256(f"{target}\0{text}".encode()).hexdigest()


def text_problem(text) -> str:
    """足す・直す文として受け付けない理由（受け付けるなら空）。"""
    if not isinstance(text, str) or not text.strip():
        return "文が空です"
    if len(text) > TEXT_MAX:
        return f"長すぎます（{TEXT_MAX} 字まで）"
    if BAD_LINE.search(text):
        return "「§」だけの行は書けません（記憶の区切りになるため）"
    return ""


# ---------- コンテナの中の台本 ----------
# 引数は「目印 操作」。操作は limits（設定の上限と有効だけを返す。書かない）・add・edit・remove。足す・直す・消すの中身は stdin の JSON。
# 結果: limits / applied（当てた。shape: 書いたあとの形の確かめ）/ changed（項目がもう無い。当てていない）/
#       refused（Hermes や確かめが断った。当てていない）/ invalid（中身の形。当てていない）/ failed（当てる処理が例外。当たったか分からない）
_SCRIPT_MAIN = r'''
import json, os, stat, sys


def done(**kw):
    sys.stdout.write("\nVLRESULT " + json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    os._exit(0)


mode = sys.argv[2] if len(sys.argv) > 2 else ""
if mode not in ("limits", "add", "edit", "remove"):
    done(result="invalid", why="知らない操作です")
from hermes_constants import get_hermes_home, get_managed_system
home = str(get_hermes_home())


def shape():
    """置き場・記憶のフォルダ・ファイル（.lock を含む）の形。崩れていればその説明（空ならふつう）。"""
    try:
        st = os.lstat(home)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return "置き場がふつうのフォルダではありません"
        mdir = os.path.join(home, "memories")
        try:
            st = os.lstat(mdir)
        except FileNotFoundError:
            return ""
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return "記憶のフォルダがふつうのフォルダではありません（symlink など）"
        for name in ("USER.md", "MEMORY.md", "USER.md.lock", "MEMORY.md.lock"):
            try:
                st = os.lstat(os.path.join(mdir, name))
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                return name + " がふつうのファイルではありません（symlink・hardlink など）"
        return ""
    except OSError as e:
        return "形を確かめられません（" + type(e).__name__ + "）"


def config():
    """(記憶の設定, 断る理由)。Hermes の設定の loader は使わない（読み込むだけで置き場に書くため）。読めない・形が違えば断る（既定に倒さない）。"""
    if get_managed_system():
        return None, "管理された設定（managed）なので、画面からは書きません"
    try:
        dfd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fd = os.open("config.yaml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)  # 置き場のフォルダを起点に、symlink をたどらずに開く
        finally:
            os.close(dfd)
    except FileNotFoundError:
        cfg = None  # ファイルが無いときだけ既定
    except OSError as e:
        return None, "設定を開けません（" + type(e).__name__ + "）"
    else:
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > CONFIG_MAX:
                return None, "設定のファイルの形がふつうではありません"
            data = b""
            while len(data) <= CONFIG_MAX:
                chunk = os.read(fd, CONFIG_MAX + 1 - len(data))
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(fd)
        if len(data) > CONFIG_MAX:
            return None, "設定が大きすぎます"
        try:
            from utils import fast_safe_load
            cfg = fast_safe_load(data.decode("utf-8-sig"))
        except Exception as e:
            return None, "設定を読めません（" + type(e).__name__ + "）"
        if not isinstance(cfg, dict):  # 空のファイル・null・リストなど。書きかけや壊れた設定を「無い」と同じに扱わない
            return None, "設定の形が違います"
    mem = (cfg or {}).get("memory", {}) if cfg is not None else {}
    if not isinstance(mem, dict):  # memory: null も断る（欄が無いときだけ既定）
        return None, "設定の memory の形が違います"
    out = {}
    for k in ("memory_enabled", "user_profile_enabled"):
        v = mem.get(k, True)
        if not isinstance(v, bool):
            return None, "設定の memory." + k + " が真偽値ではありません"
        out[k] = v
    for k, d in (("memory_char_limit", 2200), ("user_char_limit", 1375)):
        v = mem.get(k, d)
        if isinstance(v, bool) or not isinstance(v, int) or not 100 <= v <= 100000:
            return None, "設定の memory." + k + " が 100〜100000 の整数ではありません"
        out[k] = v
    return out, ""


cfg, why = config()
if why:
    done(result="refused", why=why)
if mode == "limits":
    done(result="limits", **cfg)
try:
    req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
except Exception:
    done(result="invalid", why="中身を読めません")
target, content, entry = req.get("target"), req.get("content"), req.get("entry")
if target not in ("user", "memory"):
    done(result="invalid", why="target が違います")
if mode in ("add", "edit"):
    if not isinstance(content, str) or not content.strip() or len(content) > TEXT_MAX or BAD_LINE.search(content):
        done(result="invalid", why="文の形が違います")
if mode in ("edit", "remove") and (not isinstance(entry, str) or not entry.strip()):
    done(result="invalid", why="項目の形が違います")
why = shape()
if why:
    done(result="refused", why=why)
from tools.memory_tool import apply_memory_pending
from tools.memory_tool_store import MemoryStore
store = MemoryStore(cfg["memory_char_limit"], cfg["user_char_limit"],
                    memory_enabled=cfg["memory_enabled"], user_profile_enabled=cfg["user_profile_enabled"])
store.load_from_disk()
entries = list(store._entries_for(target))
limit = store._char_limit(target)
if any(len(e) > limit for e in entries):
    # この形のまま書く API を呼ぶと、Hermes は控え（.bak.<秒>）を書く。画面からは変えない
    done(result="refused", why="記憶の中に上限より長い項目があります。画面では変えられません（Hermes の対話画面で直してください）")
if mode != "add" and entry not in entries:
    done(result="changed", why="その項目は、いまの記憶にありません（Hermes が書き換えたか、消しました）")
payload = {"add": {"action": "add", "target": target, "content": content},
           "edit": {"action": "replace", "target": target, "old_text": entry, "content": content, "matched_entry": entry},
           "remove": {"action": "remove", "target": target, "old_text": entry, "matched_entry": entry}}[mode]
try:
    res = apply_memory_pending(payload, store)
except Exception as e:
    done(result="failed", why=type(e).__name__ + ": " + str(e)[:200])
after = shape()
if isinstance(res, dict) and res.get("success") is True:
    done(result="applied", shape=after)
done(result="refused", why=str(res.get("error") if isinstance(res, dict) and res.get("error") else res)[:300], shape=after)
'''
SCRIPT = (f"import re\nBAD_LINE = re.compile({BAD_LINE.pattern!r})\nTEXT_MAX = {TEXT_MAX}\nCONFIG_MAX = {CONFIG_MAX}\n" + _SCRIPT_MAIN)


def command(container: str, mode: str, env: dict | None = None) -> list[str] | None:
    """台本を動かす命令。docker が無ければ None。env は試験のときだけ（一時の置き場を指す）。"""
    exe = docker_bin()
    if not exe:
        return None
    extra = [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
    return [exe, "exec", "-i", "-u", USER, "-w", WORKDIR, *extra, container,
            "timeout", "--kill-after=5", str(RUN_LIMIT), PY, "-c", SCRIPT, LIMITS_MARK if mode == "limits" else WRITE_MARK, mode]


def run_script(container: str, mode: str, req: dict | None = None, wait: float = HOST_WAIT, env: dict | None = None) -> dict:
    """台本を動かして結果を返す（{"result": …}）。動かせない・形の違う答え → error、時間切れ → timeout（どちらも why つき）。"""
    cmd = command(container, mode, env)
    if not cmd:
        return {"result": "error", "why": "docker が見つかりません（config.env の VL_DOCKER に場所を書けます）"}
    try:
        p = subprocess.run(cmd, input=json.dumps(req or {}, ensure_ascii=False).encode(), capture_output=True, timeout=wait)
    except subprocess.TimeoutExpired:
        return {"result": "timeout", "why": f"{wait:.0f} 秒待っても終わりませんでした"}
    except OSError as e:
        return {"result": "error", "why": f"docker を動かせません（{type(e).__name__}）"}
    if p.returncode in (124, 137):
        return {"result": "timeout", "why": "コンテナの中で時間切れになりました"}
    line = next((l for l in reversed(p.stdout.decode("utf-8", "replace").splitlines()) if l.startswith("VLRESULT ")), "")
    if p.returncode != 0 or not line:
        tail = p.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [f"終了コード {p.returncode}"]
        return {"result": "error", "why": tail[0][:200]}
    try:
        out = json.loads(line[len("VLRESULT "):])
    except ValueError:
        out = None
    if not isinstance(out, dict) or not isinstance(out.get("result"), str):
        return {"result": "error", "why": "台本の答えの形が違います"}
    return out


def still_running(container: str) -> bool | None:
    """足す・直す・消すの台本が、コンテナの中でまだ動いているか。True 動いている・False いない・None 確かめられない（hermes_skills と同じ読み方）。"""
    exe = docker_bin()
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "exec", "-u", USER, container, "pgrep", "-f", WRITE_MARK], capture_output=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if p.returncode == 0:
        return True
    if p.returncode == 1 and not p.stdout.strip() and not p.stderr.strip():
        return False
    return None
