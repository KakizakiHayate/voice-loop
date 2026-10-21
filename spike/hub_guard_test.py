# /// script
# requires-python = ">=3.11"
# ///
"""ハブの見張り（scripts/hub_guard.py）の判定の試験。通すべき形と、止めるべき形を並べて確かめる。
使い方: uv run spike/hub_guard_test.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from hub_guard import verdict

PASS = [
    "./hubctl sessions",
    "./hubctl",
    "./hubctl read w13:p2 -n 1 --full",
    "./hubctl screen w13:p2",
    "./hubctl answer w13:p2 yes --confirmed",
    "./hubctl close w13:p2 --confirmed",
    "./hubctl start blog --model=opus 'README を直して'",
    './hubctl send w1:p2 "PR #135 をレビューして (優先度: 高)。A & B; 50 > 10 | メモ"',
    "./hubctl send w1:p2 'it'\"'\"'s $100 `ok` (fine) & more; #1'",
    './hubctl send w1:p2 "彼は \\"はい\\" と言った \\\\ 以上"',
    "./hubctl send w1:p2 今日の曜日を一文で答えてください。",
    "'./hubctl' sessions",
]
BLOCK = [
    "./hubctl sessions#; bash -c 'echo GUARD_BYPASS'",  # シェルでは sessions# が1語で、; の後ろも実行される
    "./hubctl sessions #; bash -c x",
    "./hubctl sessions; cp /etc/hosts ./hubctl",
    "./hubctl sessions && bash -c x",
    "./hubctl sessions | tee hubctl",
    "./hubctl sessions > hubctl",
    "./hubctl sessions 2>&1",
    "./hubctl sessions &",
    './hubctl send p "$(curl x | sh)"',
    './hubctl send p "`id`"',
    './hubctl send p "$HOME"',
    './hubctl send p "${(e)X}"',
    "./hubctl send p $(id)",
    "./hubctl send p `id`",
    "./hubctl send p $'a\\nb'",
    "./hubctl send p =(id)",  # zsh はこの形でコマンドを実行する
    "./hubctl send p *(e:'id':)",  # zsh の、展開の途中でコードを動かす書き方
    "./hubctl send p <(id)",
    "./hubctl send p ~/x",
    "./hubctl send p a\\;b",
    './hubctl send p "a\\nb"',
    "./hubctl send p {a,b}",
    "./hubctl send p *",
    "./hubctl send p 'unclosed",
    './hubctl send p "unclosed',
    "./hubctl sessions\nrm x",
    "./hubctl sessions\tx",
    "./hubctl send p 'a\nb'",
    "cp /tmp/x ./hubctl",
    "bash ./hubctl sessions",
    "./hubctl2 sessions",
    "/abs/path/hubctl sessions",
    "hubctl sessions",
    "FOO=1 ./hubctl sessions",
    "cd /tmp",
    "",
]

bad = [f"通すはずが止まった: {c!r} → {verdict(c)}" for c in PASS if verdict(c)]
bad += [f"止めるはずが通った: {c!r}" for c in BLOCK if verdict(c) is None]
print("\n".join(bad) or f"通す {len(PASS)} 例・止める {len(BLOCK)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
