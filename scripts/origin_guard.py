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
  鍵は Hermes の API サーバーの鍵と同じ値で、本体とコンテナのあいだを双方向に守る（コンテナ側には hub_key として読み取り専用で置く）。"""
import os
import urllib.parse

LOCAL_NAMES = {"127.0.0.1", "localhost", "::1"}
DOCKER_NAME = "host.docker.internal"  # コンテナから見た、この Mac の名前
DOCKER_ALLOWED = {("GET", "/api/hub/sessions"), ("GET", "/api/hub/read"), ("GET", "/api/hub/screen"), ("GET", "/api/hub/tasks"),
                  ("POST", "/api/hub/send"), ("POST", "/api/hub/answer"), ("POST", "/api/hub/spawn"), ("POST", "/api/hub/close"),
                  ("GET", "/api/hub/context"), ("POST", "/api/hub/remember"), ("POST", "/api/hub/forget")}


def _allowed_name(name: str) -> bool:
    extra = {h.strip().lower() for h in os.environ.get("VL_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return name in LOCAL_NAMES or name.endswith(".ts.net") or name in extra


def problem(headers: dict[str, str], method: str = "GET", path: str = "") -> str | None:
    """断る理由。通してよければ None。headers の名前は小文字。method と path はコンテナからの接続の判定にだけ使う。"""
    host = headers.get("host", "").strip().lower()
    try:
        name = urllib.parse.urlsplit("//" + host).hostname or ""
        origin = urllib.parse.urlsplit(headers["origin"].strip().lower()) if "origin" in headers else None
    except ValueError:
        return f"Host か Origin の形が読めません（Host {host!r}）"
    if name == DOCKER_NAME:
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
            why = problem(headers, scope.get("method", ""), scope.get("path", ""))  # WebSocket は method 無し → コンテナからは断られる
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
