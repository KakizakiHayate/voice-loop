# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0"]
# ///
"""コンテナから Host を偽っても、画面の口に届かないことを、本物のコンテナで確かめる。
本体の写しをこの試験の中で 127.0.0.1:8792 に立て（試験用の鍵・一時の状態）、動いている Docker のコンテナ（既定は hermes。
VL_TEST_CONTAINER で替えられる）の中から curl でたたく。常駐の 8790 には触れない。コンテナには何も書かない（curl で読むだけ）。
コンテナが無いときは、確かめられなかったと出して終わる（終了コード 2）。
使い方: uv run spike/container_guard_test.py"""
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
KEY = secrets.token_hex(16)
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="claude", VL_HERMES_KEY=KEY)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import urllib.request  # noqa: E402

import uvicorn  # noqa: E402

import server as s  # noqa: E402

PORT = 8792
CONTAINER = os.environ.get("VL_TEST_CONTAINER", "hermes")
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


up = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], capture_output=True, text=True)
if up.stdout.strip() != "true":
    print(f"コンテナ {CONTAINER} が動いていないので、確かめられませんでした（{up.stderr.strip() or up.stdout.strip()}）")
    sys.exit(2)
with socket.socket() as probe:
    if probe.connect_ex(("127.0.0.1", PORT)) == 0:
        print(f"{PORT} が使われているので、確かめられませんでした")
        sys.exit(2)

s.list_agents = lambda: []
srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="off", proxy_headers=False))  # 本体と同じ
th = threading.Thread(target=srv.run, daemon=True)
th.start()
end = time.time() + 10
while not srv.started and time.time() < end:
    time.sleep(0.05)


def inside(path, host=f"host.docker.internal:{PORT}", key=None, method="GET", xff=None):
    """コンテナの中から curl でたたき、HTTP の状態コードを返す（Hermes の利用者で、読むだけ）。"""
    cmd = ["docker", "exec", "-u", "10000:10000", CONTAINER, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "10", "-X", method,
           "-H", f"Host: {host}"] + (["-H", f"X-VL-Key: {key}"] if key else []) + (["-H", f"X-Forwarded-For: {xff}"] if xff else []) + (["-H", "Content-Type: application/json", "-d", "{}"] if method == "POST" else []) \
          + [f"http://host.docker.internal:{PORT}{path}"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()


def outside(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10) as r:
            return str(r.status)
    except urllib.error.HTTPError as e:
        return str(e.code)


SPOOF = f"127.0.0.1:{PORT}"
check("この Mac の上からは、画面用の口を読める", outside("/api/situation"), "200")
check("コンテナから（名前そのまま）: 画面用の口は断る", inside("/api/situation"), "403")
check("コンテナから Host を 127.0.0.1 と偽っても、画面用の口は断る", inside("/api/situation", host=SPOOF), "403")
check("コンテナから Host を偽っても、承認に答える画面の口は断る", inside("/api/answer", host=SPOOF, method="POST"), "403")
check("コンテナから Host を偽っても、覚えていることを足す口は断る", inside("/api/facts", host=SPOOF, method="POST"), "403")
check("コンテナから Host を偽っても、画面そのものは断る", inside("/", host=SPOOF), "403")
check("コンテナから Host を Tailscale の名前に偽っても断る", inside("/api/agents", host="mini.example-tailnet.ts.net"), "403")
check("コンテナから Host を偽り鍵も付けても、画面の口は断る", inside("/api/answer", host=SPOOF, key=KEY, method="POST"), "403")
check("コンテナから Host と X-Forwarded-For（スマホの住所のつもり）を偽っても、画面用の口は断る", inside("/api/situation", host=SPOOF, xff="100.99.99.99"), "403")
check("コンテナから X-Forwarded-For を偽っても、承認に答える画面の口は断る", inside("/api/answer", host=SPOOF, xff="100.99.99.99", method="POST"), "403")
check("コンテナから: 鍵つきの hubctl の口は通す（名前そのまま）", inside("/api/hub/context", key=KEY), "200")
check("コンテナから: 鍵つきの hubctl の口は通す（Host を偽っていても）", inside("/api/hub/context", host=SPOOF, key=KEY), "200")
check("コンテナから: Host を偽っても、鍵なしの hubctl の口は断る", inside("/api/hub/context", host=SPOOF), "403")
check("コンテナから Host を偽っても、提案を送る・見送る画面の口は断る",
      (inside("/api/proposals/send", host=SPOOF, method="POST"), inside("/api/proposals/drop", host=SPOOF, key=KEY, method="POST")), ("403", "403"))
check("コンテナから: 鍵つきなら、提案を読む hubctl の口は通す", inside("/api/hub/proposals", key=KEY), "200")
ws = subprocess.run(["docker", "exec", "-u", "10000:10000", CONTAINER, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "10",
                     "-H", f"Host: {SPOOF}", "-H", "Connection: Upgrade", "-H", "Upgrade: websocket", "-H", "Sec-WebSocket-Version: 13",
                     "-H", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", f"http://host.docker.internal:{PORT}/ws"], capture_output=True, text=True, timeout=30).stdout.strip()
check("コンテナから Host を偽っても、WebSocket（声の会話）は断る", ws, "403")

# 送ってすぐ切る: 本体が接続のもとを調べるころには接続が消えている。断られる（覚えていることが増えない）こと
RAW = ("import socket\n"
       "body = b'{\"text\": \"sent from the container and closed at once\"}'\n"
       "for _ in range(5):\n"
       f"    s = socket.create_connection(('host.docker.internal', {PORT}))\n"
       f"    s.sendall(b'POST /api/facts HTTP/1.1\\r\\nHost: 127.0.0.1:{PORT}\\r\\nContent-Type: application/json\\r\\nContent-Length: ' + str(len(body)).encode() + b'\\r\\nConnection: close\\r\\n\\r\\n' + body)\n"
       "    s.close()\n")
before = len(s.facts()["facts"])
sent = subprocess.run(["docker", "exec", "-u", "10000:10000", CONTAINER, "/opt/hermes/.venv/bin/python", "-c", RAW], capture_output=True, text=True, timeout=30)
time.sleep(2)
check("コンテナの中の台本が動いた（送ってすぐ切る5回）", (sent.returncode, sent.stderr.strip()[-200:]), (0, ""))
check("コンテナから送ってすぐ切っても、画面の口（覚えていることを足す）は通らない（5回）", len(s.facts()["facts"]) - before, 0)

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり（コンテナ {CONTAINER}）")
sys.exit(0 if ok == len(results) else 1)
