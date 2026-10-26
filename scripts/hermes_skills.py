"""Hermes の手順（スキル）の書き足しを、画面の 💡 で採る・捨てるための部品。と、Hermes の記憶を読む部品（拾う役に渡す）。

Hermes は skills.write_approval が入っていると、手順の書き足しを承認待ちに積む（<置き場>/pending/skills/<id>.json）。
Hermes の承認の口は対話画面の /skills だけなので、同じ処理（Hermes の apply_skill_pending）を本体から呼べるようにする。

守り（README「守り」）。相手は外の文面に乗せられた Hermes で、置き場（ホストの ~/.hermes = コンテナの /opt/data）をいつでも書ける:
  - ホストの側は、置き場で何も動かさない・書かない・移さない（git も動かさない。.git/hooks などを仕込まれると、ホストの利用者の権限で動く）。
    読むのは承認待ちの記録と記憶のファイルだけで、置き場の根から 1 段ずつ symlink をたどらずに開き、通常のファイルで大きさの上限内のものだけを読む
  - 当てる・捨てる・差分は、コンテナの中で Hermes の利用者（10000）として、ここに固定した台本（SCRIPT）で行う。記録の中身は台本に stdin で渡す
  - 状態と控え（どれを当てたか・画面に出した中身）は、Hermes が書けない側に置く（server.py の STATE とメモリ）
"""
import hashlib
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

ID_RE = re.compile(r"[A-Za-z0-9]{8,32}")  # 承認待ちの id（Hermes は uuid の頭 8 字）
RECORD_MAX = 256 * 1024  # 記録 1 件の上限
MEMORY_MAX = 64 * 1024  # 記憶のファイル 1 つの上限
LIST_MAX = 100  # 読む記録の数の上限（超えた分は「読めない記録」に数える）
DIFF_MAX = 200_000
MARK = "vl-skill-approve"  # 当てる・捨てるの台本のコマンドラインに必ず入る目印。終わったかの確かめ（pgrep -f）に使う
CHECK_MARK = "vl-skill-diff"  # 差分と点検の台本の目印（当てる・捨てるの確かめに数えない）
PY = "/opt/hermes/.venv/bin/python"
WORKDIR = "/opt/hermes"
USER = "10000:10000"  # Hermes の利用者。root で書くと Hermes が読めないファイルができる
RUN_LIMIT = 60  # コンテナの中の打ち切り（秒）
HOST_WAIT = 90  # ホストの側で待つ上限（秒）。超えたら、台本がまだ動いていないかを still_running で確かめるまで次を断る
DOCKER_DIRS = [Path.home() / ".orbstack/bin", Path("/Applications/Docker.app/Contents/Resources/bin")]  # PATH に無いときの docker の置き場所


# ---------- 置き場を安全に読む ----------
def _open_dir(root: Path, parts: tuple[str, ...]) -> int:
    """置き場の根から、1 段ずつ symlink をたどらずにフォルダを開く。開いたフォルダの fd を返す（呼び出し側が閉じる）。
    根そのものは利用者が決めた場所なので、ふつうに開く（コンテナの中からは、根が何を指すかは変えられない）。"""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for p in parts:
            nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_at(dfd: int, name: str, cap: int) -> bytes:
    """フォルダ dfd の中の name を、symlink をたどらずに開いて読む。通常のファイルで、ほかの名前から指されておらず、cap 以下のときだけ。
    O_NONBLOCK は、通常のファイルでないもの（FIFO など）を開いたときに待たされないため。"""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("通常のファイルではありません")
        if st.st_nlink != 1:
            raise ValueError("ほかの名前からも指されています")
        if st.st_size > cap:
            raise ValueError("大きすぎます")
        data = b""
        while len(data) <= cap:
            chunk = os.read(fd, cap + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > cap:
            raise ValueError("大きすぎます")
        return data
    finally:
        os.close(fd)


def shape_problem(payload) -> str:
    """画面で扱える形か。扱えるなら ""、扱えないなら理由。コンテナの中の台本でも同じ関数で確かめる（SCRIPT に写す）。
    扱うのは create（content）・edit（content）・patch（old_string と new_string。content を持つ patch は全文の置き換えになり、
    差分の見せ方と食い違うので扱わない）・write_file（file_path と file_content）と、それらだけからなる batch。
    delete・remove_file・知らない action・知らない欄・欠けた欄は扱わない（版が変わって形が変わったら、黙って扱わない）。"""
    keys = {"action", "name", "content", "category", "file_path", "file_content", "old_string", "new_string", "replace_all"}
    need = {"create": ("content",), "edit": ("content",), "patch": ("old_string", "new_string"), "write_file": ("file_path", "file_content")}

    def one(op) -> str:
        if not isinstance(op, dict):
            return "形が違います"
        act = op.get("action")
        if act not in need:
            return f"{act} は扱えません"
        if set(op) - keys:
            return "知らない欄があります"
        if not isinstance(op.get("name"), str) or not op["name"]:
            return "手順の名前がありません"
        for k in need[act]:
            if not isinstance(op.get(k), str):
                return f"{k} がありません"
        for k in keys - {"action", "replace_all"}:
            if op.get(k) is not None and not isinstance(op[k], str):
                return "形が違います"
        if op.get("replace_all") is not None and not isinstance(op["replace_all"], bool):
            return "形が違います"
        if act == "patch" and op.get("content"):
            return "全文を置き換える patch は扱えません"
        return ""

    if not isinstance(payload, dict):
        return "形が違います"
    if payload.get("action") == "batch":
        if set(payload) - {"action", "operations"}:
            return "知らない欄があります"
        ops = payload.get("operations")
        if not isinstance(ops, list) or not ops:
            return "中身がありません"
        for op in ops:
            why = one(op)
            if why:
                return why
        return ""
    return one(payload)


def parse_record(raw: bytes, pid: str) -> dict:
    """承認待ちの記録の中身から、画面に出すもの。形が違えば ValueError。v は中身の指紋（画面の版。採るのときに照らす）。"""
    rec = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(rec, dict) or rec.get("id") != pid or not isinstance(rec.get("payload"), dict):
        raise ValueError("記録の形が違います")
    at = rec.get("created_at")
    return {"id": pid, "v": hashlib.sha1(raw).hexdigest()[:16], "raw": raw,
            "at": at if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
            "origin": rec.get("origin") if isinstance(rec.get("origin"), str) else "",
            "summary": rec.get("summary")[:300] if isinstance(rec.get("summary"), str) else "",
            "why": shape_problem(rec["payload"])}


def list_pending(home: Path) -> dict:
    """承認待ちの記録を読む。{"records": [...], "unreadable": 読めなかった数}。置き場に承認待ちのフォルダが無ければ空。
    フォルダが symlink などで開けないときは OSError（呼び出し側が「読めない」と出す）。"""
    try:
        dfd = _open_dir(home, ("pending", "skills"))
    except FileNotFoundError:
        return {"records": [], "unreadable": 0}
    records, bad = [], 0
    try:
        for name in sorted(os.listdir(dfd)):
            if not name.endswith(".json"):
                continue
            pid = name[:-5]
            if not ID_RE.fullmatch(pid) or len(records) >= LIST_MAX:
                bad += 1
                continue
            try:
                records.append(parse_record(_read_at(dfd, name, RECORD_MAX), pid))
            except (OSError, ValueError, RecursionError):
                bad += 1
    finally:
        os.close(dfd)
    records.sort(key=lambda r: r["at"] or 0)
    return {"records": records, "unreadable": bad}


def pending_ids(home: Path) -> list[str] | None:
    """承認待ちの id（ファイルの名前だけ。中身は読まない）。読めなければ None。"""
    try:
        dfd = _open_dir(home, ("pending", "skills"))
    except FileNotFoundError:
        return []
    except OSError:
        return None
    try:
        return sorted(n[:-5] for n in os.listdir(dfd) if n.endswith(".json") and ID_RE.fullmatch(n[:-5]))
    except OSError:
        return None
    finally:
        os.close(dfd)


def read_memory(home: Path, limit: int) -> tuple[list[str], int]:
    """Hermes の記憶（memories/USER.md・MEMORY.md）の項目（Hermes は「§」だけの行で区切る）。(項目, 字数の上限で外した数)。
    読めないファイル（無い・symlink・大きすぎるなど）は飛ばす。"""
    try:
        dfd = _open_dir(home, ("memories",))
    except OSError:
        return [], 0
    entries: list[str] = []
    try:
        for name in ("USER.md", "MEMORY.md"):
            try:
                text = _read_at(dfd, name, MEMORY_MAX).decode("utf-8", "replace")
            except (OSError, ValueError):
                continue
            entries += [e.strip() for e in re.split(r"\n[ \t]*§[ \t]*\n", "\n" + text + "\n") if e.strip()]
    finally:
        os.close(dfd)
    out, used, cut = [], 0, 0
    for e in entries:
        if used + len(e) > limit:
            cut += 1
            continue
        out.append(e)
        used += len(e)
    return out, cut


# ---------- コンテナの中の台本 ----------
# 引数は「目印 種類 id」。記録の中身（ホストが読んで画面に出したもの）を stdin で受け取り、結果を「VLRESULT {JSON}」の 1 行で返す。
#   check   … 使う Hermes の関数が揃っているか
#   diff    … 渡された記録の差分（Hermes の skill_pending_diff。対象の今の中身に対するもの）
#   approve … 置き場の今の記録が渡された中身と同じで、形が扱えるものなら、apply_skill_pending で当て、成功したら承認待ちから消す
#   reject  … 置き場の今の記録が渡された中身と同じなら、承認待ちから消す
# 結果: ok / diff / changed（記録が無い・違う。当てていない）/ unsupported（形。当てていない）/ applied（discarded: 消せたか）/
#       failed（当てる処理が失敗を返した・例外。一部当たったかもしれない）/ rejected（discarded）
_SCRIPT_MAIN = r'''
import json, os, sys


def done(**kw):
    sys.stdout.write("\nVLRESULT " + json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    os._exit(0)


mode, pid = sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""
raw = sys.stdin.buffer.read()
from tools import write_approval as wa
if mode == "check":
    from tools.skill_manager_tool import apply_skill_pending  # noqa: F401
    for f in ("discard_pending", "skill_pending_diff", "_pending_path"):
        getattr(wa, f)
    done(result="ok")
rec = json.loads(raw.decode("utf-8-sig"))
if mode == "diff":
    done(result="diff", diff=wa.skill_pending_diff(rec)[:DIFF_MAX])
if not ID_RE.fullmatch(pid) or rec.get("id") != pid:
    done(result="changed", why="id が違います")
try:
    fd = os.open(wa._pending_path(wa.SKILLS, pid), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        cur = f.read(RECORD_MAX + 1)
except FileNotFoundError:
    done(result="changed", why="承認待ちの記録がもうありません")
if cur != raw:
    done(result="changed", why="承認待ちの記録が変わっていました")
if mode == "reject":
    done(result="rejected", discarded=bool(wa.discard_pending(wa.SKILLS, pid)))
why = shape_problem(rec.get("payload"))
if why:
    done(result="unsupported", why=why)
from tools.skill_manager_tool import apply_skill_pending
try:
    res = apply_skill_pending(rec["payload"])
except Exception as e:
    done(result="failed", why=type(e).__name__ + ": " + str(e)[:200])
try:
    data = json.loads(res)
except Exception:
    data = None
if not isinstance(data, dict) or data.get("success") is not True or data.get("staged"):
    done(result="failed", why=str(data.get("error") if isinstance(data, dict) and data.get("error") else res)[:300])
done(result="applied", discarded=bool(wa.discard_pending(wa.SKILLS, pid)))
'''
SCRIPT = ("import re\n" + inspect.getsource(shape_problem)
          + f"\nID_RE = re.compile({ID_RE.pattern!r})\nRECORD_MAX = {RECORD_MAX}\nDIFF_MAX = {DIFF_MAX}\n" + _SCRIPT_MAIN)


def docker_bin() -> str | None:
    """docker の場所。launchd から動くと PATH が限られるので、よくある置き場所も探す。"""
    return os.environ.get("VL_DOCKER") or shutil.which("docker") or next(
        (str(d / "docker") for d in DOCKER_DIRS if (d / "docker").is_file()), None)


def command(container: str, mode: str, pid: str, env: dict | None = None) -> list[str] | None:
    """台本を動かす命令。docker が無ければ None。env は試験のときだけ（一時の置き場を指す）。"""
    exe = docker_bin()
    if not exe:
        return None
    extra = [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
    return [exe, "exec", "-i", "-u", USER, "-w", WORKDIR, *extra, container,
            "timeout", "--kill-after=5", str(RUN_LIMIT), PY, "-c", SCRIPT,
            MARK if mode in ("approve", "reject") else CHECK_MARK, mode, pid]


def run_script(container: str, mode: str, pid: str, raw: bytes, wait: float = HOST_WAIT, env: dict | None = None) -> dict:
    """台本を動かして結果を返す（{"result": …}）。動かせない・形の違う答え → error、時間切れ → timeout（どちらも why つき）。"""
    cmd = command(container, mode, pid, env)
    if not cmd:
        return {"result": "error", "why": "docker が見つかりません（config.env の VL_DOCKER に場所を書けます）"}
    try:
        p = subprocess.run(cmd, input=raw, capture_output=True, timeout=wait)
    except subprocess.TimeoutExpired:
        return {"result": "timeout", "why": f"{wait:.0f} 秒待っても終わりませんでした"}
    except OSError as e:
        return {"result": "error", "why": f"docker を動かせません（{type(e).__name__}）"}
    if p.returncode in (124, 137):  # timeout が打ち切った
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
    """当てる・捨てるの台本が、コンテナの中でまだ動いているか。True 動いている・False いない・None 確かめられない。
    pgrep の終了コード 0（いる）と 1（いない。何も出さない）だけを読む。docker の失敗（コンテナに届かないなど）も 1 で終わるが、
    そのときはエラーを出すので、何か出ていれば確かめられないとする。"""
    exe = docker_bin()
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "exec", "-u", USER, container, "pgrep", "-f", MARK], capture_output=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if p.returncode == 0:
        return True
    if p.returncode == 1 and not p.stdout.strip() and not p.stderr.strip():
        return False
    return None
