#!/usr/bin/env bash
# プラグインの命令の入口。herdr は命令を最小限の環境で起動する（ログインシェルを通らない）ので、
# PATH に uv が無いことがある。uv を探して、本体の scripts/ctl.py に渡す。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${HERDR_PLUGIN_CONFIG_DIR:-$HOME/.config/herdr/plugins/config/u-space.voice-loop}"

find_uv() {
  local c="${VL_UV:-}"
  # 置き場所を自分で決めている人のための指定: 環境変数 VL_UV か、設定ファイルの「VL_UV=」の行
  if [ -z "$c" ] && [ -f "$CONFIG/config.env" ]; then
    c="$(sed -n 's/^[[:space:]]*VL_UV[[:space:]]*=[[:space:]]*//p' "$CONFIG/config.env" | tail -1 | tr -d "\"'" | sed 's/[[:space:]]*$//')"
    c="${c/#\~/$HOME}"
  fi
  if [ -n "$c" ]; then
    if [ -x "$c" ]; then echo "$c"; return; fi
    echo "VL_UV に書かれた場所に uv がありません: $c" >&2
    return 1
  fi
  if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
  # scripts/ctl.py の SEARCH と揃える
  for c in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.cargo/bin/uv" \
           "$HOME/.local/share/mise/shims/uv" "$HOME/.nix-profile/bin/uv" /opt/local/bin/uv; do
    if [ -x "$c" ]; then echo "$c"; return; fi
  done
  echo "uv が見つかりません。https://docs.astral.sh/uv/ の手順で入れてから、もう一度実行してください。" >&2
  echo "別の場所に入れてあるなら、$CONFIG/config.env に「VL_UV=uv の場所」と1行書いてください（ファイルが無ければ作ります）。" >&2
  return 1
}

UV="$(find_uv)" || exit 1

VL_UV="$UV" exec "$UV" run --quiet --script "$ROOT/scripts/ctl.py" "$@"
