"""拾う役: 利用者の録音（Plaud）・予定（Google カレンダー）・メール（Gmail）を Claude Code（claude -p）に読ませ、
「いまの状況」の差分を JSON で受け取る。本体（server.py）が時間を見て呼び、受け取った差分を確かめてから保存する。

道具の境界（読むだけ）:
  - 組み込みの道具（Bash・ファイル・Web）は --tools "" で全部外す
  - 呼べる道具は ALLOW と完全に一致するものだけ。--settings で渡す PreToolUse フック（intake_hook.py）が、それ以外の呼び出しを止める。
    利用者の設定で「確認なしで許可」になっている送る道具も止まる。フックが動かないときも止まるよう、命令を sh で包む
  - 許可していない道具は --permission-mode dontAsk でも断られる。利用者の設定で許可済みの mcp__ の規則は --disallowedTools にも入れる
  - 費用のため、起動直後の一覧（init）を一度取り（モデルは呼ばない）、要らない道具とサーバーを --disallowedTools で文脈から外す
    （claude.ai の連携は利用者の設定から読まれるので、--strict-mcp-config や --setting-sources "" は使えない）
  - 結果は --json-schema の構造化出力だけで受け取る。拾う役のための道具（MCP）や本体の口は作らない
"""
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 読むだけの道具。名前は Claude Code に出る形（mcp__<連携>__<道具>）で、完全に一致するものだけを通す
SOURCES = {
    "claude_ai_Plaud": ["list_files", "get_note", "get_transcript"],
    "claude_ai_Google_Calendar": ["list_events", "search_events", "get_event"],
    "claude_ai_Gmail": ["search_threads", "get_thread"],
}
STRUCTURED = "StructuredOutput"  # --json-schema のときに Claude Code が足す、結果を返す道具
ALLOW = [f"mcp__{server}__{tool}" for server, tools in SOURCES.items() for tool in tools] + [STRUCTURED]
SETTINGS_FILES = [Path.home() / ".claude/settings.json", Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
_source = {"type": "object", "additionalProperties": False, "required": ["kind", "id", "at", "label"], "properties": {
    "kind": {"enum": ["plaud", "calendar", "gmail"]},
    "id": {"type": "string"},
    "at": {"type": "string", "description": "根拠の日時（ISO 8601）。録音は開始、メールは受信、予定は更新の日時"},
    "label": {"type": "string", "description": "出どころの短い名前（録音の題名・メールの件名・予定の題名）。私用のものは載せない"},
}}
_fields = {
    "title": {"type": "string", "description": "話題の名前（40 字まで）"},
    "now": {"type": "string", "description": "いまどうなっているか（200 字まで）"},
    "next": {"type": "string", "description": "次に起きること。あなたの番／相手待ち／次の予定（120 字まで。無ければ空）"},
    "folder": {"type": "string", "description": "関係するフォルダの名前（入力の folders にあるものだけ。無ければ空）"},
    "until": {"type": "string", "description": "関係する次の予定の日（YYYY-MM-DD。無ければ空）"},
    "sources": {"type": "array", "items": _source},
}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["add", "update", "retire", "seen", "counts"],
    "properties": {
        "add": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["title", "now", "sources"], "properties": _fields}},
        "update": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id"],
                                              "properties": {"id": {"type": "string"}, **_fields}}},
        "retire": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "reason"],
                                              "properties": {"id": {"type": "string"}, "reason": {"type": "string"}}}},
        "seen": {"type": "array", "items": {"type": "string"}, "description": "今回読み終えた出どころの ID（録音・メール）"},
        "counts": {"type": "object", "additionalProperties": False, "required": ["read", "private", "unsure"], "properties": {
            "read": {"type": "integer"}, "private": {"type": "integer"}, "unsure": {"type": "integer"}}},
    },
}

_running: set = set()  # 動いている claude（一覧を取る分も。本体が止まるときに全部止めるため）
_lock = threading.Lock()
_stopping = threading.Event()  # 本体が止まるところ。これが立ったら、新しく起動しない


def clean_env(extra: dict | None = None) -> dict:
    """拾う役に渡す環境。herdr と tmux の印を外す（利用者のフックが、拾う役を herdr のセッションとして登録しないように）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "TMUX"))}
    env.update(extra or {})
    return env


def settings_json() -> str:
    """--settings に渡す設定。許可リストと完全一致する呼び出しだけを通すフック。フックが動かなくても通さないよう包む。"""
    guard = shlex.join([os.environ.get("VL_INTAKE_PYTHON") or sys.executable, str(HERE / "intake_hook.py")])
    command = "sh -c " + shlex.quote(f'{guard}; rc=$?; [ "$rc" -eq 0 ] && exit 0; exit 2')
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}})


def preapproved() -> list[str]:
    """利用者の設定で「確認なしで許可」になっている mcp__ の規則のうち、許可リストに無いもの（deny は allow に勝つので、外す側に入れる）。"""
    out = []
    for f in SETTINGS_FILES:
        try:
            rules = json.loads(f.read_text()).get("permissions", {}).get("allow", [])
        except (OSError, ValueError, AttributeError):
            continue
        out += [r for r in rules if isinstance(r, str) and r.startswith("mcp__") and r not in ALLOW]
    return out


def base_args(claude: str, model: str) -> list[str]:
    return [claude, "-p", "--model", model, "--tools", "", "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--no-session-persistence", "--output-format", "stream-json", "--verbose", "--settings", settings_json()]


class Stopped(Exception):
    """本体が止まるところなので、起動しなかった。"""


def _spawn(args: list[str], prompt: str, cwd: Path) -> subprocess.Popen:
    """起動できなければ OSError（claude が無い・実行できない）。本体が止まるところなら Stopped。"""
    with _lock:  # 止める合図と起動を同じ鍵の中で見る（止めたあとに起動しない）
        if _stopping.is_set():
            raise Stopped
        p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=cwd,
                             env=clean_env({"VL_INTAKE_ALLOW": ",".join(ALLOW)}), start_new_session=True)  # 止めるときはグループごと
        _running.add(p)
    try:
        p.stdin.write(prompt)
        p.stdin.close()
    except OSError:  # すぐ落ちた。読む側で「結果が返らない」になる
        pass
    return p


def _kill(p: subprocess.Popen):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    with _lock:
        _running.discard(p)


def probe(claude: str, model: str, cwd: Path, timeout: float = 60) -> dict | None:
    """起動直後の一覧（init）だけを取って止める。モデルは呼ばない。取れなければ None。"""
    try:
        p = _spawn(base_args(claude, model), "x", cwd)
    except (OSError, Stopped):
        return None
    timer = threading.Timer(timeout, _kill, [p])
    timer.start()
    try:
        for line in p.stdout:
            if line.startswith("{"):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "system" and d.get("subtype") == "init":
                    return d
        return None
    finally:
        timer.cancel()
        _kill(p)


def server_of(tool: str) -> str:
    """mcp__<サーバー>__<道具> のサーバーの部分。mcp__ でなければ空。"""
    parts = tool.split("__")
    return parts[1] if tool.startswith("mcp__") and len(parts) >= 3 else ""


def deny_rules(tools: list[str], known_servers: set[str]) -> tuple[list[str], set[str]]:
    """文脈から外す規則と、見えたサーバーの名前。要らないサーバーは丸ごと、使うサーバーは許可リスト外の道具を名前で外す。"""
    servers = {server_of(t) for t in tools} - {""}
    rules = [t for t in tools if server_of(t) in SOURCES and t not in ALLOW]
    rules += [f"mcp__{s}" for s in sorted((servers | known_servers) - set(SOURCES))]
    rules += preapproved()
    return list(dict.fromkeys(rules)), servers


def run(claude: str, model: str, prompt: str, cwd: Path, budget: float, timeout: float, known_servers: set[str]) -> dict:
    """拾う役を1回動かす。返すもの: ok・why（失敗の理由）・result（構造化出力）・cost・secs・denied（止めた道具の名前）・
    extra（許可リスト外なのに見えた道具の数）・servers（見えたサーバー。次の回から外すため）・missing（つながっていない連携）。
    claude の出力そのものは返さない・残さない（読んだ中身を記録に残さないため）。"""
    t0 = time.time()
    cwd.mkdir(parents=True, exist_ok=True)
    out = {"ok": False, "why": "", "result": None, "cost": 0.0, "secs": 0, "denied": [], "extra": 0, "servers": [], "missing": []}
    init = None
    for _ in range(3):  # 連携は少し遅れてつながることがある。使う連携がそろうまで、何回か一覧を取り直す
        init = probe(claude, model, cwd)
        if _stopping.is_set():
            out["why"] = "本体が止まったので、途中でやめました"
            return out
        if init is None:
            out["why"] = "claude を起動できませんでした（一覧が返りません）"
            return out
        out["missing"] = sorted(s for s in SOURCES if not any(server_of(t) == s for t in init.get("tools", [])))
        if not out["missing"]:
            break
        if _stopping.wait(5):
            out["why"] = "本体が止まったので、途中でやめました"
            return out
    rules, servers = deny_rules(init.get("tools", []), known_servers)
    args = base_args(claude, model) + ["--allowedTools", ",".join(ALLOW), "--max-budget-usd", f"{budget:.2f}", "--json-schema", json.dumps(SCHEMA)]
    if rules:
        args += ["--disallowedTools", ",".join(rules)]
    try:
        p = _spawn(args, prompt, cwd)
    except Stopped:
        out["why"] = "本体が止まったので、途中でやめました"
        return out
    except OSError as e:
        out["why"] = f"claude を起動できませんでした（{type(e).__name__}）"
        return out
    timer = threading.Timer(timeout, _kill, [p])
    timer.start()
    result = None
    try:
        for line in p.stdout:
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "system" and d.get("subtype") == "init":
                seen = d.get("tools", [])
                out["extra"] = sum(t not in ALLOW for t in seen)
                servers |= {server_of(t) for t in seen} - {""}
            elif d.get("type") == "result":
                result = d
    finally:
        timer.cancel()
        _kill(p)
    out["secs"] = round(time.time() - t0)
    out["servers"] = sorted(servers)
    if result is None:
        out["why"] = f"{int(timeout)}秒で終わらなかったか、途中で止まりました" if out["secs"] >= timeout else "結果が返りませんでした"
        return out
    out["cost"] = float(result.get("total_cost_usd") or 0)
    out["denied"] = sorted({d.get("tool_name", "?") for d in result.get("permission_denials") or [] if d.get("tool_name") != STRUCTURED})
    structured = result.get("structured_output")
    if result.get("subtype") != "success" or result.get("is_error") or not isinstance(structured, dict):
        out["why"] = {"error_max_budget_usd": f"1回の上限（{budget:.2f} ドル相当）に届いて止まりました",
                      "error_max_turns": "やり取りの上限に届いて止まりました"}.get(result.get("subtype"), f"結果を受け取れませんでした（{result.get('subtype')}）")
        return out
    out["ok"], out["result"] = True, structured
    return out


def stop():
    """動いている拾う役を全部止め、このあと新しく起動しない（本体が止まるとき）。"""
    with _lock:
        _stopping.set()
        procs = list(_running)
    for p in procs:
        _kill(p)
