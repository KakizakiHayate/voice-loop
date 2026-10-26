# /// script
# requires-python = ">=3.11"
# ///
"""接続元の確かめ（scripts/origin_guard.py）の判定の試験。通すべき接続と、断るべき接続を並べて確かめる。
使い方: uv run spike/origin_guard_test.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import subprocess  # noqa: E402

import origin_guard  # noqa: E402
from origin_guard import is_container, peer_program, problem  # noqa: E402

os.environ["VL_HERMES_KEY"] = "k1"  # コンテナからの接続に要る鍵（試験用の値）

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
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/hub/sessions"},  # コンテナ（Hermes）の hubctl、鍵つき
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/hub/read"},
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/hub/screen"},
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/hub/send", "_method": "POST"},
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/hub/answer", "_method": "POST"},
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/hub/spawn", "_method": "POST"},
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/hub/close", "_method": "POST"},
    # 接続のもとがコンテナの中継（OrbStack など）でも、Host を偽っていても、鍵つきの hubctl の口は通す
    {"host": "127.0.0.1:8790", "x-vl-key": "k1", "_path": "/api/hub/sessions", "_container": True},
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
    {"host": "host.docker.internal:8790", "_path": "/api/hub/sessions"},  # コンテナから鍵なし
    {"host": "host.docker.internal:8790", "x-vl-key": "wrong", "_path": "/api/hub/send", "_method": "POST"},  # 鍵違い
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/hub/start", "_method": "POST"},  # hubctl の口の外
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/hub/sessions", "_method": "POST"},  # 口は合っていても方法が違う
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/agents"},  # 画面用の API
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/answer", "_method": "POST"},
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/"},  # 画面
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/ws", "_method": ""},  # WebSocket
    {"host": "host.docker.internal:8790", "x-vl-key": "k1", "_path": "/api/hub/sessions", "origin": "http://host.docker.internal:8790"},  # コンテナ経由のブラウザ
    {"host": "host.docker.internal.evil.example:8790", "_path": "/api/hub/sessions"},
    # コンテナの中から Host を偽る（接続のもとは OrbStack など）。Host に関係なく、コンテナとして扱う
    {"host": "127.0.0.1:8790", "_container": True, "_path": "/api/situation"},  # 画面用の API
    {"host": "127.0.0.1:8790", "_container": True, "_path": "/api/answer", "_method": "POST"},  # 承認に答える（画面の口）
    {"host": "127.0.0.1:8790", "_container": True, "_path": "/api/facts", "_method": "POST"},
    {"host": "127.0.0.1:8790", "_container": True, "x-vl-key": "k1", "_path": "/api/hermes/session/new", "_method": "POST"},  # ハブの会話を新しくする（画面の口）
    {"host": "127.0.0.1:8790", "_container": True, "_path": "/"},  # 画面
    {"host": "127.0.0.1:8790", "_container": True, "_path": "/ws", "_method": ""},  # WebSocket
    {"host": "127.0.0.1:8790", "_container": True, "_path": "/api/hub/sessions"},  # hubctl の口でも鍵なし
    {"host": TS, "_container": True, "x-forwarded-host": TS, "_path": "/api/agents"},  # Tailscale の名前を偽る
    {"host": "localhost:8790", "_container": True, "x-vl-key": "k1", "_path": "/api/answer", "_method": "POST"},  # 鍵があっても画面の口は断る
]


def check(case: dict) -> str | None:
    os.environ["VL_ALLOWED_HOSTS"] = case.get("_env", "")
    return problem({k: v for k, v in case.items() if not k.startswith("_")}, case.get("_method", "GET"), case.get("_path", "/"), case.get("_container", False))


bad = [f"通すはずが断った: {c} → {check(c)}" for c in PASS if check(c)]
bad += [f"断るはずが通った: {c}" for c in BLOCK if check(c) is None]

# 接続のもとのプログラムの読み取り（lsof の出力を差し替える）
ME = str(os.getpid())
LSOF = {
    "OrbStack": (0, f"p1361\ncOrbStack Helper\nf94\nn127.0.0.1:63228->127.0.0.1:8790\np{ME}\ncpython3.12\nf15\nn127.0.0.1:8790->127.0.0.1:63228\n", ""),
    "browser": (0, f"p222\ncGoogle Chrome Helper\nf30\nn127.0.0.1:63228->127.0.0.1:8790\np{ME}\ncpython3.12\nf15\nn127.0.0.1:8790->127.0.0.1:63228\n", ""),
    "own_only": (0, f"p{ME}\ncpython3.12\nf15\nn127.0.0.1:8790->127.0.0.1:63228\n", ""),  # 相手が root のプログラム（見えない）。本体の側はつながっている
    "self": (0, f"p{ME}\ncpython3.12\nf14\nn127.0.0.1:63228->127.0.0.1:8790\nf15\nn127.0.0.1:8790->127.0.0.1:63228\n", ""),  # 本体が自分につないだ
    "closed": (1, "", ""),  # 相手がもう切った（本体の側も ESTABLISHED でない）
    "other_port": (0, f"p{ME}\ncpython3.12\nf15\nn127.0.0.1:9999->127.0.0.1:63228\n", ""),  # 本体の別の口の接続（この接続ではない）
    "error": (1, "", "lsof: status error"),
    "crash": (2, "", ""),
}
real_run = subprocess.run
for name, want in [("OrbStack", "OrbStack Helper"), ("browser", "Google Chrome Helper"), ("own_only", ""), ("self", "python3.12"),
                   ("closed", None), ("other_port", None), ("error", None), ("crash", None)]:
    code, out, err = LSOF[name]
    origin_guard.subprocess.run = lambda *a, code=code, out=out, err=err, **k: subprocess.CompletedProcess(a[0], code, out, err)
    got = peer_program(("127.0.0.1", 63228), ("127.0.0.1", 8790))
    if got != want:
        bad.append(f"接続のもとの読み取り（{name}）: {got!r}（期待 {want!r}）")


def boom(*a, **k):
    raise subprocess.TimeoutExpired("lsof", 3)


origin_guard.subprocess.run = boom
if peer_program(("127.0.0.1", 63228)) is not None:
    bad.append("lsof が時間切れなら None（調べられない）")
if peer_program(("100.64.0.5", 63228)) != "" or peer_program(None) != "":
    bad.append("この Mac の上でない接続・接続元の無いものは調べない（\"\"）")
if peer_program(("127.0.0.1", 0)) is not None:
    bad.append("接続元が中継の値で書き換えられている（ポート 0）なら None（本当の相手が分からない）")

# Tailscale Serve を通ってきた接続の、中継が付けた接続元（X-Forwarded-For の最後）がこの Mac 自身か
real_local = origin_guard.local_addresses
origin_guard.local_addresses = lambda: {"127.0.0.1", "::1", "100.101.102.103", "fd7a:115c:a1e0::1234", "192.168.0.10"}
for xff, want in [("100.101.102.103", True), ("100.99.99.99, 100.101.102.103", True), ("fd7a:115c:a1e0::1234", True),
                  ("100.64.1.2", False), ("100.101.102.103, 100.64.1.2", False), ("", False)]:
    got = origin_guard.forwarded_from_self({"x-forwarded-for": xff} if xff else {})
    if got != want:
        bad.append(f"中継の接続元がこの Mac 自身か: {xff!r} → {got}（期待 {want}）")
origin_guard.local_addresses = lambda: None
if not origin_guard.forwarded_from_self({"x-forwarded-for": "100.64.1.2"}):
    bad.append("この Mac の住所が分からないときは、中継を通った接続をコンテナとして扱う（通さない側）")

# 住所を取り直せないときは、古い住所を使わない（None）
origin_guard.local_addresses = real_local
origin_guard._ADDRS = (0.0, {"127.0.0.1", "::1", "100.1.1.1"})  # 期限切れの古い住所
origin_guard.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "ifconfig: error")
if origin_guard.local_addresses() is not None:
    bad.append("ifconfig が失敗したら、期限切れの住所は使わずに None")
origin_guard.subprocess.run = boom
if origin_guard.local_addresses() is not None:
    bad.append("ifconfig が時間切れなら None")
origin_guard.subprocess.run = real_run
if "127.0.0.1" not in (origin_guard.local_addresses() or set()):
    bad.append("本物の ifconfig から、この Mac の住所が読める")
origin_guard.subprocess.run = real_run
for program, want in [("OrbStack Helper", True), ("OrbStack", True), ("com.docker.backend", True), ("vpnkit-bridge", True), (None, True),
                      ("python3.12", False), ("Google Chrome Helper", False), ("", False), ("tailscaled", False)]:
    if is_container(program) != want:
        bad.append(f"コンテナの中継の判定: {program!r} → {is_container(program)}（期待 {want}）")
os.environ["VL_CONTAINER_PROGRAMS"] = "podman, colima"
if not (is_container("podman-machine") and is_container("Colima")):
    bad.append("VL_CONTAINER_PROGRAMS で中継のプログラムを足せる")
print("\n".join(bad) or f"通す {len(PASS)} 例・断る {len(BLOCK)} 例、接続のもとの読み取り・判定もすべて期待どおり")
sys.exit(1 if bad else 0)
