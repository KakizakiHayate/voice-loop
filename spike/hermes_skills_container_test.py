"""本物の Hermes のコンテナで、手順の書き足しを採る・捨てる台本（scripts/hermes_skills.py の SCRIPT）を確かめる。
コンテナの /tmp に一時の置き場（HERMES_HOME）を作り、承認待ちは Hermes 自身の stage_write で積む。本物の置き場（/opt/data）と常駐の 8790 には触れない。
確かめること: 使う関数が揃っている（check）、差分（Hermes の skill_pending_diff）、中身が違えば当てない（changed）、当てる（Hermes の利用者で書き、
承認待ちを消し、Hermes 自身の記録に残る）、二度目は当てない、失敗する patch は failed で手順は変わらない（batch は全部戻る）、扱えない形は呼ばない（unsupported）、捨てる、
実際の起動の形（目印つき）で動いている台本を pgrep -f で見つけられ、終われば見つからない。
使い方: python3 spike/hermes_skills_container_test.py [コンテナ名（既定 hermes）]"""
import json
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import hermes_skills as hs  # noqa: E402

C = sys.argv[1] if len(sys.argv) > 1 else "hermes"
H = f"/tmp/vlt-{uuid.uuid4().hex[:10]}"
ENV = {"HERMES_HOME": H}
DOCKER = hs.docker_bin()
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def sh(script: str, stdin: bytes = b"") -> bytes:
    """コンテナの中で Hermes の利用者として動かす（一時の置き場を指す）。"""
    return subprocess.run([DOCKER, "exec", "-i", "-u", hs.USER, "-w", hs.WORKDIR, "-e", f"HERMES_HOME={H}", C, "sh", "-c", script],
                          input=stdin, capture_output=True, check=True, timeout=60).stdout


def stage(payload: dict, summary: str) -> tuple[str, bytes]:
    """Hermes 自身の stage_write で承認待ちを積み、(id, 記録の中身) を返す。"""
    code = ("import json,sys\nfrom tools import write_approval as wa\n"
            "r = wa.stage_write(wa.SKILLS, json.loads(sys.stdin.read()), summary=sys.argv[1], origin='background_review')\nprint(r['id'])\n")
    pid = sh(f"{hs.PY} -c {shlex.quote(code)} {shlex.quote(summary)}", json.dumps(payload).encode()).decode().strip().splitlines()[-1]
    return pid, sh(f"cat {H}/pending/skills/{pid}.json")


def skill(name: str) -> str:
    return sh(f"cat {H}/skills/{name}/SKILL.md 2>/dev/null || true").decode()


def pending() -> list[str]:
    return sorted(n.removesuffix(".json") for n in sh(f"ls {H}/pending/skills 2>/dev/null || true").decode().split())


run = lambda mode, pid, raw: hs.run_script(C, mode, pid, raw, env=ENV)
patch = lambda old, new, name="demo": {"action": "batch", "operations": [{"action": "patch", "name": name, "old_string": old, "new_string": new}]}
BASE = "---\nname: demo\ndescription: demo skill\n---\n# Demo\n\nline one\n"
try:
    sh(f"mkdir -p {H}/skills/demo {H}/skills/keep && printf '%s' {shlex.quote(BASE)} > {H}/skills/demo/SKILL.md && "
       f"printf '%s' {shlex.quote(BASE.replace('demo', 'keep'))} > {H}/skills/keep/SKILL.md")
    check("点検: 使う Hermes の関数が揃っている", run("check", "", b""), {"result": "ok"})

    pid, raw = stage(patch("line one", "line one\nline two"), "二行目を足す")
    check("積んだ記録が承認待ちに見える（この試験の一覧の読み方の確かめ）", pid in pending(), True)
    d = run("diff", pid, raw)
    check("差分: Hermes の作り方で、足す行が出る", (d["result"], "+line two" in d["diff"]), ("diff", True))
    check("中身が違う記録では当てない（手順は変わらない）", (run("approve", pid, raw + b" ")["result"], skill("demo") == BASE), ("changed", True))
    out = run("approve", pid, raw)
    owner = sh(f"stat -c %u {H}/skills/demo/SKILL.md").decode().strip()
    check("当てる: 手順に入り、承認待ちから消え、Hermes の利用者のファイルのまま",
          (out, "line two" in skill("demo"), pid in pending(), owner), ({"result": "applied", "discarded": True}, True, False, "10000"))
    check("二度目は当てない（記録がもう無い）", run("approve", pid, raw)["result"], "changed")
    ledger = sh(f"{hs.PY} -m hermes_cli.main curator ledger 2>/dev/null || hermes curator ledger 2>/dev/null || true").decode()
    check("当てた書き換えは Hermes 自身の記録（curator の ledger）に残る（rollback で戻せる）",
          (sh(f"wc -l < {H}/skills/.curator_ledger.jsonl").decode().strip(), "patch" in ledger and "demo" in ledger), ("1", True))

    before = skill("demo")
    pid, raw = stage({"action": "batch", "operations": [{"action": "patch", "name": "keep", "old_string": "line one", "new_string": "changed"},
                                                         {"action": "patch", "name": "demo", "old_string": "no such line", "new_string": "x"}]}, "失敗する")
    out = run("approve", pid, raw)
    check("失敗する batch: failed を返し、手順はどちらも変わらない（Hermes の batch は全部戻す）",
          (out["result"], skill("demo") == before, skill("keep") == BASE.replace("demo", "keep"), pid in pending()), ("failed", True, True, True))

    pid_del, raw_del = stage({"action": "delete", "name": "keep"}, "手順を消す")
    check("扱えない形（delete）は、台本でも当てない", (run("approve", pid_del, raw_del)["result"], "keep" in skill("keep")), ("unsupported", True))
    out = run("reject", pid_del, raw_del)
    check("捨てる: 承認待ちから消し、手順は変わらない", (out, pid_del in pending(), "keep" in skill("keep")), ({"result": "rejected", "discarded": True}, False, True))

    pid, raw = stage({"action": "batch", "operations": [{"action": "create", "name": "fresh", "content": "---\nname: fresh\ndescription: new\n---\n# Fresh\n"},
                                                         {"action": "write_file", "name": "fresh", "file_path": "references/a.md", "file_content": "ref\n"}]}, "新しい手順")
    out = run("approve", pid, raw)
    check("create と write_file の batch を当てる", (out["result"], "# Fresh" in skill("fresh"), sh(f"cat {H}/skills/fresh/references/a.md").decode()),
          ("applied", True, "ref\n"))

    # 実際の起動の形（目印つき）で動いている台本を見つけられるか。stdin を閉じないあいだ、台本は読み込みで待つ
    cmd = hs.command(C, "approve", "abcdefgh", ENV)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    seen = None
    for _ in range(40):
        seen = hs.still_running(C)
        if seen:
            break
        time.sleep(0.25)
    proc.stdin.close()
    proc.wait(timeout=30)
    after = None
    for _ in range(40):
        after = hs.still_running(C)
        if after is False:
            break
        time.sleep(0.25)
    check("動いている台本を pgrep -f の目印で見つけ、終われば見つからない", (seen, after), (True, False))
    check("差分・点検の台本は、終わったかの確かめに数えない（命令のどこにも目印が入らない。台本の本文にも）",
          (any(hs.MARK in x for x in hs.command(C, "diff", "abcdefgh", ENV)), any(hs.MARK in x for x in hs.command(C, "check", "", ENV))), (False, False))
finally:
    if H.startswith("/tmp/vlt-"):
        subprocess.run([DOCKER, "exec", "-u", hs.USER, C, "rm", "-rf", H], capture_output=True, timeout=30)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
