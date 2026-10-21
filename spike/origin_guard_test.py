# /// script
# requires-python = ">=3.11"
# ///
"""接続元の確かめ（scripts/origin_guard.py）の判定の試験。通すべき接続と、断るべき接続を並べて確かめる。
使い方: uv run spike/origin_guard_test.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from origin_guard import problem

TS = "mini.example-tailnet.ts.net:8790"
PASS = [
    {"host": "127.0.0.1:8790"},  # hubctl・curl・画面の読み込み（Origin なし）
    {"host": "127.0.0.1:8790", "origin": "http://127.0.0.1:8790"},
    {"host": "localhost:8790", "origin": "http://localhost:8790"},
    {"host": "[::1]:8790", "origin": "http://[::1]:8790"},
    {"host": TS, "origin": f"https://{TS}"},  # Tailscale Serve が Host を渡す場合
    {"host": "127.0.0.1:8790", "x-forwarded-host": TS, "origin": f"https://{TS}"},  # Host を書き換える場合
    {"host": "MINI.example-tailnet.ts.net:8790", "origin": f"https://{TS}"},  # 大文字小文字
    {"host": "voice.home.arpa:8790", "origin": "http://voice.home.arpa:8790", "_env": "voice.home.arpa"},
    {"host": "host.docker.internal:8790", "_path": "/api/hub/sessions"},  # コンテナ（Hermes）の hubread
    {"host": "host.docker.internal:8790", "_path": "/api/hub/read"},
]
BLOCK = [
    {"host": "127.0.0.1:8790", "origin": "https://evil.example"},  # 別のサイトのページ
    {"host": "127.0.0.1:8790", "origin": "http://127.0.0.1:9999"},  # 同じ Mac の別のポートのページ
    {"host": "127.0.0.1:8790", "origin": "https://other.attacker-tailnet.ts.net"},  # 他人の Funnel のページ
    {"host": TS, "origin": "https://other.attacker-tailnet.ts.net"},
    {"host": "127.0.0.1:8790", "origin": "null"},  # file:// や sandbox の iframe
    {"host": "127.0.0.1:8790", "origin": "chrome-extension://abc"},
    {"host": "evil.example:8790"},  # DNS リバインディング（Origin なしの GET）
    {"host": "evil.example:8790", "origin": "http://evil.example:8790"},  # DNS リバインディング（同じ場所に見える）
    {"host": "192.168.1.5:8790", "origin": "http://192.168.1.5:8790"},  # LAN の IP（VL_ALLOWED_HOSTS に無い）
    {"host": "127.0.0.1.evil.example:8790"},
    {"host": "localhost.evil.example", "origin": "http://localhost.evil.example"},
    {"host": "ts.net.evil.example:8790"},
    {"host": ""},  # Host なし
    {"host": "127.0.0.1:8790", "origin": "http://[::1"},  # 読めない形
    {"host": "127.0.0.1:8790", "x-forwarded-host": "evil.example", "origin": "https://other.example"},
    {"host": "host.docker.internal:8790", "_path": "/api/hub/send", "_method": "POST"},  # コンテナから送る・答える・立てる
    {"host": "host.docker.internal:8790", "_path": "/api/hub/answer", "_method": "POST"},
    {"host": "host.docker.internal:8790", "_path": "/api/hub/spawn", "_method": "POST"},
    {"host": "host.docker.internal:8790", "_path": "/api/hub/screen"},  # 読む 2 本の外
    {"host": "host.docker.internal:8790", "_path": "/"},  # 画面
    {"host": "host.docker.internal:8790", "_path": "/ws", "_method": ""},  # WebSocket
    {"host": "host.docker.internal:8790", "_path": "/api/hub/sessions", "origin": "http://host.docker.internal:8790"},  # コンテナ経由のブラウザ
    {"host": "host.docker.internal.evil.example:8790", "_path": "/api/hub/sessions"},
]


def check(case: dict) -> str | None:
    os.environ["VL_ALLOWED_HOSTS"] = case.get("_env", "")
    return problem({k: v for k, v in case.items() if not k.startswith("_")}, case.get("_method", "GET"), case.get("_path", "/"))


bad = [f"通すはずが断った: {c} → {check(c)}" for c in PASS if check(c)]
bad += [f"断るはずが通った: {c}" for c in BLOCK if check(c) is None]
print("\n".join(bad) or f"通す {len(PASS)} 例・断る {len(BLOCK)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
