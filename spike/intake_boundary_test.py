# /// script
# requires-python = ">=3.11"
# ///
"""拾う役の道具の境界の試験。本物の claude（Claude Code）を使う。常駐の 8790 には触れない。
  既定: 見張りのフック単体と、本物の claude の起動直後の一覧（init）だけを見る。モデルは呼ばない（費用なし）
    - フック: 許可リストと完全一致なら通し、それ以外・読めない入力は止める。フックの置き場所が壊れていても止める
    - 外す規則を渡すと、見える道具が許可リスト（と、あとからつながった連携）だけになる
  --live: haiku を2回だけ呼ぶ（合わせて 1 セント未満）。読むだけの道具で、送る・作る・消すものは呼ばない
    - 利用者の設定で許可済みの道具でも、許可リストに無ければフックで止まる
    - フックが動かないとき、許可リストの道具も止まる（通さない側に倒れる）
使い方: uv run spike/intake_boundary_test.py [--live]"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import intake  # noqa: E402

results = []
LIVE = "--live" in sys.argv
WORK = Path(tempfile.mkdtemp())
CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude")


def check(name, got, want):
    results.append((name, got == want, got, want))


def hook(stdin: str, command: str | None = None) -> int:
    cmd = command or json.loads(intake.settings_json())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    return subprocess.run(cmd, shell=True, input=stdin, capture_output=True, text=True, env=intake.clean_env({"VL_INTAKE_ALLOW": ",".join(intake.ALLOW)})).returncode


# ---- フック単体
check("許可リストの道具は通す", hook(json.dumps({"tool_name": "mcp__claude_ai_Plaud__get_note"})), 0)
check("結果を返す道具は通す", hook(json.dumps({"tool_name": "StructuredOutput"})), 0)
check("送る道具は止める", hook(json.dumps({"tool_name": "mcp__claude_ai_Gmail__send_message"})), 2)
check("組み込みの道具（Bash）は止める", hook(json.dumps({"tool_name": "Bash"})), 2)
check("名前の一部が同じだけのものは止める", hook(json.dumps({"tool_name": "mcp__claude_ai_Plaud__get_note_x"})), 2)
check("読めない入力は止める", hook("{壊れた"), 2)
broken = json.loads(intake.settings_json())["hooks"]["PreToolUse"][0]["hooks"][0]["command"].replace("intake_hook.py", "no_such_hook.py")
check("フックの置き場所が壊れていても止める", hook(json.dumps({"tool_name": "mcp__claude_ai_Plaud__get_note"}), broken), 2)


# ---- 本物の claude の一覧（モデルは呼ばない）
def init_with(extra: list[str]) -> dict | None:
    p = intake._spawn(intake.base_args(CLAUDE, "haiku") + extra, "x", WORK)
    try:
        for line in p.stdout:
            if line.startswith("{"):
                d = json.loads(line)
                if d.get("subtype") == "init":
                    return d
    finally:
        intake._kill(p)
    return None


first = intake.probe(CLAUDE, "haiku", WORK)
check("claude の一覧が取れる", first is not None, True)
tools = first["tools"] if first else []
check("組み込みの道具は一つも無い（--tools \"\"）", [t for t in tools if not t.startswith("mcp__")], [])
missing = [s for s in intake.SOURCES if not any(intake.server_of(t) == s for t in tools)]
print(f"（この端末で見えた道具 {len(tools)} 個。つながっていない連携: {missing or 'なし'}）")
rules, servers = intake.deny_rules(tools, set())
second = init_with(["--allowedTools", ",".join(intake.ALLOW), "--disallowedTools", ",".join(rules)])
visible = second["tools"] if second else []
late = sorted({intake.server_of(t) for t in visible if t not in intake.ALLOW} - servers)
check("外す規則を渡すと、見えるのは許可リストと、あとからつながった連携だけ",
      [t for t in visible if t not in intake.ALLOW and intake.server_of(t) not in late], [])
print(f"（外したあとに見えた道具 {len(visible)} 個。あとからつながった連携: {late or 'なし'}。これらは次の回から外し、呼び出しはフックが止める）")


# ---- 実際に呼ばせる（--live）
def live(settings: dict, prompt: str) -> dict:
    args = [CLAUDE, "-p", "--model", "haiku", "--tools", "", "--permission-mode", "dontAsk", "--permission-prompts", "none", "--no-session-persistence",
            "--output-format", "stream-json", "--verbose", "--settings", json.dumps(settings), "--allowedTools", ",".join(intake.ALLOW),
            "--max-budget-usd", "0.10", "--json-schema", json.dumps({"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]})]
    if rules:
        args += ["--disallowedTools", ",".join(r for r in rules if not r.startswith("mcp__claude_ai_Google_Calendar"))]  # 試しに呼ばせる道具は見えるようにする
    r = subprocess.run(args, input=prompt, capture_output=True, text=True, cwd=WORK, env=intake.clean_env({"VL_INTAKE_ALLOW": ",".join(intake.ALLOW)}), timeout=180)
    out = {"denied": [], "ran": [], "result": None}
    for line in r.stdout.splitlines():
        if not line.startswith("{"):
            continue
        d = json.loads(line)
        if d.get("type") == "assistant":
            out["ran"] += [b["name"] for b in d["message"]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
        if d.get("type") == "result":
            out["result"] = d
            out["denied"] = [x["tool_name"] for x in d.get("permission_denials") or []]
    return out


if LIVE:
    s1 = json.loads(intake.settings_json())
    s1["permissions"] = {"allow": ["mcp__claude_ai_Google_Calendar__list_calendars"]}  # 利用者の設定で許可済み、の代わり
    r = live(s1, "Call mcp__claude_ai_Google_Calendar__list_calendars once (no arguments). Then return note='done'. Do not call anything else.")
    check("設定で許可済みでも、許可リストに無い道具はフックで止まる", "mcp__claude_ai_Google_Calendar__list_calendars" in r["denied"], True)
    check("結果は決まった形で返る", (r["result"] or {}).get("structured_output", {}).get("note") is not None, True)
    s2 = json.loads(intake.settings_json().replace("intake_hook.py", "no_such_hook.py"))
    r = live(s2, "Call mcp__claude_ai_Google_Calendar__get_event with calendarId 'primary' and eventId 'voice-loop-test-none' once. Then return note='done'.")
    check("フックが動かないと、許可リストの道具も止まる", "mcp__claude_ai_Google_Calendar__get_event" in r["denied"], True)
    print(f"（--live の費用: {(r['result'] or {}).get('total_cost_usd')} ドル相当〔2回目〕）")

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
