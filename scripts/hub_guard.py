# /// script
# requires-python = ">=3.11"
# ///
"""ハブのセッションが Bash で実行できるものを ./hubctl だけに絞る見張り（Claude Code の PreToolUse フック）。

ハブは他のセッションの記録を読むので、そこに紛れた指示に乗せられることがありうる。./hubctl は確認なしで通る設定に
してあるため、ほかのコマンドで hubctl 自体を書き換えられると、何でも確認なしで実行できてしまう。利用者が全体の設定で
広く許可しているコマンド（cp や bash など）も、ハブでは通さない。

判定は「通してよい形」を決めて、それ以外をすべて止める。シェルの書き方を読み解いて危ないものを探す方式だと、
読み方がシェル（bash や zsh）と少しでもずれたところが抜け道になるため。通すのは次の形だけ:
  - 1行で、最初の語が ./hubctl
  - 引用符の外は、英数字と _ . : / - = だけ（; | & > # $ ~ * ( ) などは使えない）
  - '…' の中は何でもよい（シェルは中身を解釈しない）
  - "…" の中は $ と ` 以外（シェルが展開するものを入れない）

標準入力に呼び出しの内容（JSON）が届く。通すなら終了コード 0、止めるなら 2（標準エラーの文がハブに返る）。
"""
import json
import sys

WHY = "ハブが Bash で実行できるのは ./hubctl だけです（{}）。ほかの作業は、各セッションに hubctl send で頼んでください。"
BARE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/-=")
CONTROL = "改行や制御文字は使えません。1行で書いてください"
UNCLOSED = "引用符が閉じていません"


def is_control(c: str) -> bool:
    return c < " " or c == "\x7f"


def verdict(command: str) -> str | None:
    """止める理由。通してよければ None。"""
    words, cur, in_word = [], [], False
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if is_control(c):
            return CONTROL
        if c == " ":
            if in_word:
                words.append("".join(cur))
                cur, in_word = [], False
            i += 1
        elif c == "'":
            end = command.find("'", i + 1)
            if end < 0:
                return UNCLOSED
            inner = command[i + 1:end]
            if any(map(is_control, inner)):
                return CONTROL
            cur.append(inner)
            in_word, i = True, end + 1
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    return UNCLOSED
                d = command[i]
                if d == '"':
                    break
                if is_control(d):
                    return CONTROL
                if d in "$`":
                    return "\"…\" の中で $ と ` は使えません。その文は '…' で囲んでください"
                if d == "\\":
                    if command[i + 1:i + 2] not in ('"', "\\"):
                        return "\"…\" の中の \\ は、\\\" と \\\\ の形でだけ使えます"
                    i += 1
                    d = command[i]
                cur.append(d)
                i += 1
            in_word, i = True, i + 1
        elif c in BARE or c > "\x7f":  # 日本語などの文字は、シェルにとってただの文字
            cur.append(c)
            in_word, i = True, i + 1
        else:
            return f"引用符の外で {c} は使えません。命令は1つだけにし、指示文は引用符で囲んでください"
    if in_word:
        words.append("".join(cur))
    if not words or words[0] != "./hubctl":
        return "./hubctl で始まっていません"
    return None


def main() -> int:
    try:
        call = json.load(sys.stdin)
    except ValueError:
        print(WHY.format("呼び出しの内容を読めませんでした"), file=sys.stderr)
        return 2
    if call.get("tool_name") != "Bash":
        return 0
    why = verdict(str((call.get("tool_input") or {}).get("command", "")).strip(" "))
    if why:
        print(WHY.format(why), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
