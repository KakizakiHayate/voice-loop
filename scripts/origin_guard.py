"""本体への接続元の確かめ。ブラウザで開いた別のサイトから、本体を操作されないようにする。

本体は 127.0.0.1 でだけ待ち受け、スマホからは Tailscale Serve を通して使う。その外から届くブラウザの接続を断る。
- Host … この Mac の名前（127.0.0.1・localhost・::1）か、Tailscale の名前（〜.ts.net）か、VL_ALLOWED_HOSTS に書いた名前だけ。
  別のドメインを 127.0.0.1 に向ける攻撃（DNS リバインディング）を断つため
- Origin … 付いていれば、開いている画面と同じ場所（Host か、Tailscale Serve が渡す X-Forwarded-Host）だけ。
  ブラウザは別のサイトからの接続に必ず Origin を付け、ページの側では書き換えられない
Origin の無い接続（hubctl、curl、画面そのものの読み込み）は通す。ブラウザの外からここへ届くのは、
この Mac の上のプログラムと tailnet の端末だけで、そこは信頼する範囲として扱う（README「接続できる範囲」）。
- コンテナ … Docker（OrbStack など）の中からは host.docker.internal という名前で届く。そこからは、Hermes の hubctl が使う
  /api/hub/ の口だけを、X-VL-Key が VL_HERMES_KEY と一致するときに通す。画面（/）・WebSocket・画面用の API は断る。
  鍵は Hermes の API サーバーの鍵と同じ値で、本体とコンテナのあいだを双方向に守る（コンテナ側には hub_key として読み取り専用で置く）。
  コンテナの中から Host を 127.0.0.1 と偽ることはできる（接続元もこの Mac の 127.0.0.1 に見える）。そこで、この Mac の上からの
  接続は、もとのプログラムを lsof で調べ、コンテナの中継のプログラム（OrbStack・Docker）なら、Host に関係なくコンテナとして扱う。
  調べられなかったときもコンテナとして扱う（通さない側に倒す）。接続元は本当の接続の相手で見る（本体は X-Forwarded-For で
  接続元を書き換えない。uvicorn の proxy_headers を切る）。
  コンテナからは、この Mac の Tailscale の名前（〜.ts.net）でも Tailscale Serve を通って届く。そのときの接続のもとは root で動く
  Tailscale のネットワーク拡張で見えないが、Tailscale Serve が X-Forwarded-For の最後に付ける接続元が、この Mac 自身の住所になる。
  そうした接続（コンテナか、この Mac の上で Tailscale の名前で開いたもの）もコンテナとして扱う。この Mac の上では 127.0.0.1 で開く。"""
import asyncio
import os
import re
import subprocess
import time
import urllib.parse

LOCAL_NAMES = {"127.0.0.1", "localhost", "::1"}
DOCKER_NAME = "host.docker.internal"  # コンテナから見た、この Mac の名前
DOCKER_ALLOWED = {("GET", "/api/hub/sessions"), ("GET", "/api/hub/read"), ("GET", "/api/hub/screen"), ("GET", "/api/hub/tasks"),
                  ("POST", "/api/hub/send"), ("POST", "/api/hub/answer"), ("POST", "/api/hub/spawn"), ("POST", "/api/hub/close"),
                  ("GET", "/api/hub/context"), ("POST", "/api/hub/remember"), ("POST", "/api/hub/forget")}


# コンテナから Mac へ届く接続を中継するプログラムの名前（頭の部分。大文字小文字は問わない）。VL_CONTAINER_PROGRAMS で足せる
CONTAINER_PROGRAMS = ("orbstack", "com.docker", "docker", "vpnkit")


def peer_program(client, server=None) -> str | None:
    """この Mac の上からの接続（127.0.0.1・::1）の、もとのプログラムの名前。
    本体の側の接続がまだつながっていて、相手がこの利用者のプログラムに見つからなければ ""（root で動く Tailscale のネットワーク拡張など。
    コンテナの中継は利用者のプログラムなので見える）。本体の側の接続も見つからない（相手がもう切った）・調べられないときは None。"""
    if not client or client[0] not in ("127.0.0.1", "::1") or not isinstance(client[1], int):
        return ""
    port = client[1]
    if port == 0:  # 接続元が中継の値で書き換えられている（本当の相手が分からない）
        return None
    mine = str(server[1]) if server and isinstance(server[1], int) else None
    try:
        p = subprocess.run(["lsof", "+c", "0", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED", "-Fpcn"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode not in (0, 1) or (p.returncode == 1 and p.stderr.strip()):  # 1 で出力なし = 見つからない
        return None
    pid, cmd, peer, alive = None, "", None, False
    for line in p.stdout.splitlines():
        if line.startswith("p"):
            pid, cmd = line[1:], ""
        elif line.startswith("c"):
            cmd = line[1:]
        elif line.startswith("n"):
            local, _, remote = line[1:].partition("->")
            if local.endswith(f":{port}") and peer is None:
                peer = cmd  # 手元の側が、この接続の相手のポートになっているもの = 接続のもと（この本体が自分につないだときも）
            elif pid == str(os.getpid()) and remote.endswith(f":{port}") and (mine is None or local.endswith(f":{mine}")):
                alive = True  # 本体の側の、この接続
    if peer is not None:
        return peer
    return "" if alive else None  # 本体の側も見えない = 相手がもう切った（誰だったか分からない）


_ADDRS: tuple[float, set[str] | None] = (0.0, None)  # (調べた時刻, この Mac の住所)


def local_addresses() -> set[str] | None:
    """この Mac の住所（ifconfig の inet・inet6。Tailscale の住所も入る）。1 分覚える。調べられなければ None。"""
    global _ADDRS
    at, addrs = _ADDRS
    if addrs is not None and time.time() - at < 60:
        return addrs
    try:
        r = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None  # 古い住所は使わない（住所が変わっていると、この Mac 自身を別の端末と取り違える）
    found = {a.split("%")[0].lower() for a in re.findall(r"^\s*inet6?\s+([0-9a-fA-F:.%a-z0-9]+)", r.stdout, re.M)}
    if r.returncode != 0 or not found:
        return None
    _ADDRS = (time.time(), found | {"127.0.0.1", "::1"})
    return _ADDRS[1]


def forwarded_from_self(headers: dict[str, str]) -> bool:
    """中継（Tailscale Serve）が X-Forwarded-For の最後に付けた接続元が、この Mac 自身か。住所が分からないときも True（通さない側）。"""
    xff = headers.get("x-forwarded-for", "")
    if not xff.strip():
        return False
    last = xff.split(",")[-1].strip().strip("[]").lower()
    addrs = local_addresses()
    return addrs is None or last in addrs


def is_container(program: str | None) -> bool:
    """もとのプログラムがコンテナの中継か。調べられなかった（None）ときもコンテナとして扱う。"""
    if program is None:
        return True
    extra = tuple(p.strip().lower() for p in os.environ.get("VL_CONTAINER_PROGRAMS", "").split(",") if p.strip())
    return program.strip().lower().startswith(CONTAINER_PROGRAMS + extra) if program else False


def _allowed_name(name: str) -> bool:
    extra = {h.strip().lower() for h in os.environ.get("VL_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return name in LOCAL_NAMES or name.endswith(".ts.net") or name in extra


def problem(headers: dict[str, str], method: str = "GET", path: str = "", container: bool = False) -> str | None:
    """断る理由。通してよければ None。headers の名前は小文字。method と path はコンテナからの接続の判定にだけ使う。
    container: 接続のもとがコンテナの中継のプログラムだった（Host に関係なく、コンテナからの接続として扱う）。"""
    host = headers.get("host", "").strip().lower()
    try:
        name = urllib.parse.urlsplit("//" + host).hostname or ""
        origin = urllib.parse.urlsplit(headers["origin"].strip().lower()) if "origin" in headers else None
    except ValueError:
        return f"Host か Origin の形が読めません（Host {host!r}）"
    if name == DOCKER_NAME or container:
        if origin is not None:
            return f"コンテナ経由のブラウザからは使えません（Origin {headers['origin']!r}）"
        if (method, path) not in DOCKER_ALLOWED:
            return f"コンテナからは hubctl の口以外は使えません（{method} {path}）"
        key = os.environ.get("VL_HERMES_KEY", "")
        if not key or headers.get("x-vl-key", "") != key:
            return "コンテナからの接続に、正しい鍵（X-VL-Key）が付いていません"
        return None
    if not _allowed_name(name):
        return f"Host {host!r} はこの Mac の名前ではありません"
    if origin is None:
        return None
    same = {host, headers.get("x-forwarded-host", "").strip().lower()} - {""}
    if origin.scheme in ("http", "https") and origin.netloc in same:
        return None
    return f"Origin {headers['origin']!r} は開いている画面と別の場所です（Host {host!r}）"


class SameOriginOnly:
    """ASGI のミドルウェア。HTTP と WebSocket の両方で problem() を確かめ、断るときは 403 を返す。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            program = await asyncio.to_thread(peer_program, scope.get("client"), scope.get("server"))  # Host は偽れるので、接続のもとのプログラムも見る
            # もとが見えない接続（root の Tailscale の中継など）は、中継が付けた接続元がこの Mac 自身なら、コンテナとして扱う
            container = is_container(program) or (program == "" and await asyncio.to_thread(forwarded_from_self, headers))
            why = problem(headers, scope.get("method", ""), scope.get("path", ""), container)  # WebSocket は method 無し → コンテナからは断られる
            if why:
                print(f"[guard] 断りました: {scope['type']} {scope.get('path')} … {why}", flush=True)
                if scope["type"] == "websocket":  # 受け入れる前に閉じると、サーバーが 403 を返す
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await send({"type": "http.response.start", "status": 403,
                                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                    await send({"type": "http.response.body", "body": "この接続元からは使えません。\n".encode()})
                return
        await self.app(scope, receive, send)
