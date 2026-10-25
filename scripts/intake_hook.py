"""拾う役（claude -p）が呼べる道具を、許可リストの名前と完全に一致するものだけに絞る見張り（Claude Code の PreToolUse フック）。

拾う役は利用者の Claude Code の設定をそのまま読む（claude.ai の連携を使うため）。その設定で「確認なしで許可」に
なっている送る道具（Slack への送信など）も、ここで止める。許可リストは環境変数 VL_INTAKE_ALLOW（カンマ区切り）。
通すのは、呼び出しの内容を読めて、道具の名前が許可リストにあるときだけ。それ以外（読めない・例外）はすべて止める。
本体はこの命令を sh で包み、終了コードが 0 でなければ 2 に直して渡す（フック自体が動かなくても通さない）。
"""
import json
import os
import sys


def main() -> int:
    try:
        allow = {t for t in os.environ.get("VL_INTAKE_ALLOW", "").split(",") if t}
        name = json.load(sys.stdin).get("tool_name")
        if isinstance(name, str) and name in allow:
            return 0
        print(f"拾う役は {name} を使えません（読むだけの道具に限っています）", file=sys.stderr)
    except Exception as e:  # 読めない入力・想定外の形は、通さない
        print(f"拾う役の見張りが呼び出しを確かめられませんでした（{type(e).__name__}）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
