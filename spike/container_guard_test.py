# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0"]
# ///
"""コンテナから Host を偽っても、画面の口に届かないことを、本物のコンテナで確かめる。
本体の口は app.routes から全部たどる（口を足すと、試験を書き足さなくても確かめの対象に入る）。コンテナに開くのは
origin_guard.DOCKER_ALLOWED の口だけで、それも鍵があるときだけ。
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

# 開いている口を鍵つきで呼んでも、本物の herdr やセッションに何も起きないように差し替える
s.list_agents = lambda: []
s.herdr = lambda *a, **k: {"error": {"code": "test"}}
s.spawn_session = lambda *a, **k: {"ok": False, "reason": "test", "created": False}
s.hub_turns = lambda n=1: []
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

# ---------- 本体の口を全部たどる ----------
import json  # noqa: E402

from starlette.routing import Mount, WebSocketRoute  # noqa: E402

import origin_guard  # noqa: E402  （本体が scripts を読み込み先に足している）

ROUTES = []
for r in s.app.routes:
    if isinstance(r, Mount):
        ROUTES.append(("GET", r.path + "/index.html"))  # 静的なファイル
    elif isinstance(r, WebSocketRoute):
        ROUTES.append(("WS", r.path))
    else:
        ROUTES += [(m, r.path) for m in sorted(r.methods - {"HEAD"})]
# 試験の側で独立に持つ「コンテナに開いてよい口」。本体の一覧（DOCKER_ALLOWED）を変えたら、ここも意図して直す
# （本体の一覧を期待値に使うと、誤って画面の口を足しても試験が気づかない）
HUB_OPEN = {("GET", "/api/hub/sessions"), ("GET", "/api/hub/read"), ("GET", "/api/hub/screen"), ("GET", "/api/hub/tasks"),
            ("POST", "/api/hub/send"), ("POST", "/api/hub/answer"), ("POST", "/api/hub/spawn"), ("POST", "/api/hub/close"),
            ("GET", "/api/hub/context"), ("POST", "/api/hub/remember"), ("POST", "/api/hub/forget")}
check("本体の「コンテナに開く口」の一覧が、試験の側の一覧と同じ", (sorted(origin_guard.DOCKER_ALLOWED - HUB_OPEN), sorted(HUB_OPEN - origin_guard.DOCKER_ALLOWED)), ([], []))
check("コンテナに開く口は、どれも本体に実在する", sorted(HUB_OPEN - set(ROUTES)), [])
# 1 回の docker exec で、口ごとに 3 通り（名前を偽る・鍵なし／名前そのまま・鍵つき／名前を偽る・鍵つき）を呼ぶ
WALK = r"""
import http.client, json, sys
port, key, routes = int(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
out = []
for method, path in routes:
    for host, with_key in ((f"127.0.0.1:{port}", False), (f"host.docker.internal:{port}", True), (f"127.0.0.1:{port}", True)):
        h = {"Host": host, **({"X-VL-Key": key} if with_key else {})}
        body = None
        if method == "WS":
            h.update({"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
        elif method in ("POST", "PUT", "PATCH", "DELETE"):
            h["Content-Type"], body = "application/json", "{}"
        c = http.client.HTTPConnection("host.docker.internal", port, timeout=20)
        try:
            c.request("GET" if method == "WS" else method, path, body=body, headers=h)
            code = c.getresponse().status
        except Exception as e:
            code = type(e).__name__
        finally:
            c.close()
        out.append([method, path, host.split(":")[0], with_key, code])
print(json.dumps(out))
"""
walk = subprocess.run(["docker", "exec", "-u", "10000:10000", CONTAINER, "/opt/hermes/.venv/bin/python", "-c", WALK, str(PORT), KEY, json.dumps(ROUTES)],
                      capture_output=True, text=True, timeout=300)
rows = json.loads(walk.stdout or "[]")
check("全部の口をたどれた（口の数 × 3 通り）", (len(rows), walk.stderr.strip()[-200:]), (len(ROUTES) * 3, ""))
open_wrong = [r for r in rows if (r[0], r[1]) not in HUB_OPEN and r[4] != 403]
check(f"ハブの道具の口以外（{len(ROUTES) - len(HUB_OPEN)} 個）は、コンテナからはどの呼び方でも断る", open_wrong, [])
hub_nokey = [r for r in rows if (r[0], r[1]) in HUB_OPEN and not r[3] and r[4] != 403]
check("ハブの道具の口も、鍵なしなら断る（名前を偽っても）", hub_nokey, [])
# 届く = 守りを通って本体の処理が答えた（403・5xx・通信の失敗は、届いたとみなさない）
# 届く = この Mac の上から同じ呼び方をしたときと、同じ答え（状態コード）が返る（守りが間に入っていない）
import http.client  # noqa: E402


def local_code(method, path):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    try:
        c.request(method, path, body="{}" if method == "POST" else None, headers={"Content-Type": "application/json"} if method == "POST" else {})
        return c.getresponse().status
    finally:
        c.close()


expect = {(m, path): local_code(m, path) for m, path in sorted(HUB_OPEN)}
check("この Mac の上からは、ハブの道具の口がどれも 403・5xx 以外で答える（比べる元）", sorted(k for k, v in expect.items() if v == 403 or v >= 500), [])
hub_key = [r for r in rows if (r[0], r[1]) in HUB_OPEN and r[3] and r[4] != expect[(r[0], r[1])]]
check(f"ハブの道具の口（{len(HUB_OPEN)} 個）は、鍵つきなら、この Mac の上からと同じ答えが返る（名前そのまま・偽っても）", hub_key, [])

# ---------- Tailscale の名前から入る道（常駐に読むだけ。VL_TEST_TS_URL を付けたときだけ） ----------
# Tailscale Serve は常駐の 8790 にだけつながっているので、この道は試験用の本体では確かめられない。
# 例: VL_TEST_TS_URL=https://<この Mac>.<tailnet>.ts.net:8790 uv run spike/container_guard_test.py
TS_URL = os.environ.get("VL_TEST_TS_URL", "").rstrip("/")
if TS_URL:
    def via_ts(path, ws=False, key=False, xff=None):
        """コンテナの中から常駐へ、Tailscale の名前で読むだけの要求を送る。key: コンテナの hub_key を付ける（中身は表に出さない）。"""
        hdr = ['-H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="' if ws else "",
               '-H "X-VL-Key: $(cat /opt/voice-loop/hub_key)"' if key else "", f'-H "X-Forwarded-For: {xff}"' if xff else ""]
        cmd = f'curl -s -o /dev/null -w "%{{http_code}}" -m 15 {" ".join(hdr)} "{TS_URL}{path}"'
        return subprocess.run(["docker", "exec", "-u", "10000:10000", CONTAINER, "sh", "-c", cmd], capture_output=True, text=True, timeout=60).stdout.strip()
    check("常駐: コンテナから Tailscale の名前で、画面用の口（読むだけ）は断る", via_ts("/api/situation"), "403")
    check("常駐: コンテナから Tailscale の名前で、鍵なしのハブの口（読むだけ）は断る", via_ts("/api/hub/context"), "403")
    check("常駐: コンテナから Tailscale の名前で、WebSocket は断る", via_ts("/ws", ws=True), "403")
    check("常駐: Tailscale の名前で鍵を付けても、画面用の口と WebSocket は断る", (via_ts("/api/situation", key=True), via_ts("/ws", ws=True, key=True)), ("403", "403"))
    check("常駐: Tailscale の名前で接続元（X-Forwarded-For）を偽っても、画面用の口は断る", via_ts("/api/situation", xff="100.99.99.99"), "403")
    check("常駐: Tailscale の名前でも、鍵つきならハブの道具の口（読むだけ）は届く", via_ts("/api/hub/context", key=True), "200")
else:
    print("（Tailscale の名前から入る道は確かめていません。VL_TEST_TS_URL を付けると、常駐に読むだけで確かめます）")

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり（コンテナ {CONTAINER}）")
sys.exit(0 if ok == len(results) else 1)
