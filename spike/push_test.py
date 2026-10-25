# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "httpx>=0.27"]
# ///
"""プッシュ通知（画面の 🔔）の試験。鍵と購読の保存、配送先の確かめ、暗号化した中身、見えている画面があるときは送らないこと、
配送先が購読を消したときの片づけ、頼んだ件の報告と承認待ちの知らせ。
配送サービスには送らない（requests.post を差し替え、受け取った中身を端末の鍵で復号して確かめる）。herdr・ハブ・語り手には触れない。
使い方: uv run spike/push_test.py"""
import asyncio
import base64
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="claude")
os.environ.pop("VL_PUSH_SUBJECT", None)
os.environ.pop("VL_PUSH_HOSTS", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import http_ece  # noqa: E402
import requests  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

import server as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(t: str) -> bytes:
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


class Device:
    """通知を受ける端末の代わり。購読の鍵を持ち、届いた中身を復号する。"""

    def __init__(self, endpoint):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.sub = {"endpoint": endpoint, "keys": {"p256dh": b64(self.key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)), "auth": b64(self.auth)}}

    def open(self, body: bytes) -> dict:
        return json.loads(http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"))


class Resp:
    def __init__(self, code):
        self.status_code, self.reason, self.text, self.headers = code, "x", "", {}


sent: list[dict] = []  # 配送サービスに届いたもの
answer: dict[str, object] = {}  # endpoint → 返す状態コードか例外


def fake_post(url, data=None, headers=None, timeout=None, **_):
    sent.append({"url": url, "data": data, "headers": headers or {}, "at": time.time()})
    out = answer.get(url, 201)
    if isinstance(out, BaseException):
        raise out
    return Resp(out)


requests.post = fake_post


def jwt_claims(headers) -> dict:
    t = headers["Authorization"].split("t=", 1)[1].split(",", 1)[0]
    return json.loads(unb64(t.split(".")[1]))


async def nothing(*a, **k):
    return None


s.boot_ensure_hub = nothing
s.keep_gateway_token = nothing

# ---------------- 鍵と公開鍵 ----------------
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    s.list_agents = lambda: []
    r = c.get("/api/push").json()
    check("公開鍵を返す", r["ok"], True)
    check("公開鍵は非圧縮の点（65バイト・先頭 0x04）", (len(unb64(r["key"])), unb64(r["key"])[0]), (65, 4))
    check("鍵のファイルは本人だけが読める（0600）", stat.S_IMODE(s.PUSH_KEY_FILE.stat().st_mode), 0o600)
    check("鍵のフォルダも本人だけ（0700）", stat.S_IMODE(s.PUSH_KEY_FILE.parent.stat().st_mode), 0o700)
    check("二度目も同じ鍵（作り直さない）", c.get("/api/push").json()["key"], r["key"])
    check("登録はまだ0件", r["count"], 0)
    s.PUSH_SUBJECT = "https://github.com/someone/repo"
    bad_sub = c.get("/api/push").json()
    check("連絡先の形が使えないときは、押した時点で理由を返す", (bad_sub["ok"], "VL_PUSH_SUBJECT" in bad_sub["reason"]), (False, True))
    s.PUSH_SUBJECT = "mailto:voice-loop@localhost"

    # ---------------- 購読の形と配送先の確かめ ----------------
    apple = Device("https://web.push.apple.com/QGuQyavXut")
    bad = [
        ("https でない", {**apple.sub, "endpoint": "http://web.push.apple.com/x"}),
        ("知らない配送先（この Mac から任意の場所へ送る口にしない）", {**apple.sub, "endpoint": "https://evil.example.com/x"}),
        ("配送先に似せた名前", {**apple.sub, "endpoint": "https://web.push.apple.com.evil.example/x"}),
        ("内向きの名前", {**apple.sub, "endpoint": "https://127.0.0.1/x"}),
        ("\\ と @ で宛先の読み方を食い違わせる（内向き）", {**apple.sub, "endpoint": "https://127.0.0.1\\@web.push.apple.com/x"}),
        ("\\ と @ で宛先の読み方を食い違わせる（外向き）", {**apple.sub, "endpoint": "https://evil.example\\@web.push.apple.com/x"}),
        ("利用者名つき", {**apple.sub, "endpoint": "https://user@web.push.apple.com/x"}),
        ("443 以外の番号", {**apple.sub, "endpoint": "https://web.push.apple.com:8443/x"}),
        ("改行入り", {**apple.sub, "endpoint": "https://web.push.apple.com/x\n"}),
        ("空白入り", {**apple.sub, "endpoint": "https://web.push.apple.com/x y"}),
        ("# つき", {**apple.sub, "endpoint": "https://web.push.apple.com/x#y"}),
        ("鍵が無い", {"endpoint": apple.sub["endpoint"], "keys": {"p256dh": "x"}}),
        ("鍵が長すぎる", {"endpoint": apple.sub["endpoint"], "keys": {"p256dh": "x" * 300, "auth": "y"}}),
        ("形が違う", "https://web.push.apple.com/x"),
    ]
    for name, sub in bad:
        rr = c.post("/api/push/subscribe", json={"subscription": sub, "test": True})
        check(f"断る: {name}", (rr.status_code, rr.json()["ok"]), (400, False))
    check("断ったものは保存しない・送らない", (s.push_subs(), sent), ([], []))
    real_forms = ["https://fcm.googleapis.com/fcm/send/dXk2:APA91bH-abc_def", "https://wns2-par02p.notify.windows.com/w/?token=BQYAAAB%2bXyz%3d",
                  "https://updates.push.services.mozilla.com/wpush/v2/gAAAAABk-x_y", "https://web.push.apple.com:443/QGuQ"]
    check("実際の配送サービスの URL の形は通す", [s.push_problem({**apple.sub, "endpoint": e}) for e in real_forms], [None] * 4)

    # ---------------- 登録と試しの通知（暗号化した中身） ----------------
    rr = c.post("/api/push/subscribe", json={"subscription": apple.sub, "test": True}).json()
    check("登録して試しの通知を送る", rr, {"ok": True, "test": "sent"})
    check("配送サービスに1件", [x["url"] for x in sent], [apple.sub["endpoint"]])
    msg = apple.open(sent[0]["data"])
    check("中身は端末の鍵で復号できる（見出しだけ）", msg, {"title": "voice-loop", "body": "通知を受け取れるようになりました", "tag": "test", "url": "/"})
    check("中身は暗号化されている（平文の見出しが載っていない）", "通知" in sent[0]["data"].decode("latin-1"), False)
    claims = jwt_claims(sent[0]["headers"])
    check("VAPID の aud は配送先のオリジン", claims["aud"], "https://web.push.apple.com")
    check("VAPID の sub は既定で届かない宛先（利用者の連絡先を勝手に名乗らない）", claims["sub"], "mailto:voice-loop@localhost")
    check("TTL は6時間", sent[0]["headers"].get("ttl"), str(6 * 3600))
    check("購読のファイルは本人だけが読める（0600）", stat.S_IMODE(s.PUSH_SUBS.stat().st_mode), 0o600)
    c.post("/api/push/subscribe", json={"subscription": apple.sub})
    check("同じ端末の送り直しは重ねない（test なしは送らない）", (len(s.push_subs()), len(sent)), (1, 1))

    # ---------------- 見えている画面があるときは送らない ----------------
    google = Device("https://fcm.googleapis.com/fcm/send/abc")
    c.post("/api/push/subscribe", json={"subscription": google.sub})
    sent.clear()
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        w.send_json({"type": "hello", "speaker": "0", "voice": True, "visible": True})
        w.send_json({"type": "clientlog", "msg": "sync"})  # hello を読み終えたことを、次のメッセージの処理で確かめる
        time.sleep(0.3)
        check("見えている画面があると送らない", asyncio.run(s.push_all("p｜t", "完了しました", "report-x")), "visible")
        w.send_json({"type": "hello", "speaker": "0", "voice": True, "visible": False})
        time.sleep(0.3)
        check("画面が隠れると送る", asyncio.run(s.push_all("p｜t", "完了しました", "report-x")), "sent")
        w.send_json({"type": "hello", "speaker": "0", "voice": True})
        time.sleep(0.3)
        check("visible を送らない古い画面は、見えていないものとして扱う", asyncio.run(s.push_all("p｜t", "完了しました", "report-x")), "sent")
        s.VISIBLE_TTL = 0.5
        w.send_json({"type": "hello", "speaker": "0", "voice": True, "visible": True})
        time.sleep(0.2)
        check("見えている知らせの直後は送らない", asyncio.run(s.push_all("p｜t", "完了しました", "report-x")), "visible")
        time.sleep(0.6)
        check("見えている知らせが途切れたら（裏に回ったことが届かなくても）送る", asyncio.run(s.push_all("p｜t", "完了しました", "report-x")), "sent")
        s.VISIBLE_TTL = 45
    check("登録したすべての端末に送る", sorted(x["url"] for x in sent), sorted([apple.sub["endpoint"], google.sub["endpoint"]] * 3))
    auds = {x["url"]: jwt_claims(x["headers"])["aud"] for x in sent}
    check("aud は送り先ごと（使い回さない）", auds[google.sub["endpoint"]], "https://fcm.googleapis.com")
    check("Google 側の中身も復号できる", google.open(next(x["data"] for x in sent if x["url"] == google.sub["endpoint"]))["body"], "完了しました")

    # ---------------- 配送サービスの答えによる片づけ ----------------
    sent.clear()
    answer[apple.sub["endpoint"]] = 410
    answer[google.sub["endpoint"]] = 500
    check("どこにも届かなければ failed", asyncio.run(s.push_all("p｜t", "x", "t")), "failed")
    check("410（購読が切れた）の端末は消す・500 の端末は残す", [x["endpoint"] for x in s.push_subs()], [google.sub["endpoint"]])
    answer[google.sub["endpoint"]] = requests.ConnectionError("down")
    third = Device("https://updates.push.services.mozilla.com/wpush/v2/xyz")
    c.post("/api/push/subscribe", json={"subscription": third.sub})
    sent.clear()
    check("通信の失敗があっても、ほかの端末には送る", asyncio.run(s.push_all("p｜t", "x", "t")), "sent")
    check("通信の失敗した端末も残す", len(s.push_subs()), 2)
    answer.clear()

    # ---------------- 切る・上限 ----------------
    c.post("/api/push/unsubscribe", json={"endpoint": google.sub["endpoint"]})
    check("切った端末は消える", [x["endpoint"] for x in s.push_subs()], [third.sub["endpoint"]])
    for i in range(12):
        c.post("/api/push/subscribe", json={"subscription": Device(f"https://web.push.apple.com/n{i}").sub})
    subs = [x["endpoint"] for x in s.push_subs()]
    check("登録は最大10件（古いものから外す）", (len(subs), subs[-1], third.sub["endpoint"] in subs), (10, "https://web.push.apple.com/n11", False))
    for e in subs:
        c.post("/api/push/unsubscribe", json={"endpoint": e})
    check("全部切ると0件", s.push_subs(), [])
    sent.clear()
    check("登録が0件なら何も送らない", asyncio.run(s.push_all("p｜t", "x", "t")), "none")

    # ---------------- コンテナ（Hermes）からは使えない ----------------
    key = {"Host": "host.docker.internal:8790", "X-VL-Key": "k"}
    os.environ["VL_HERMES_KEY"] = "k"
    check("コンテナからの登録は断る", c.post("/api/push/subscribe", json={"subscription": apple.sub}, headers=key).status_code, 403)
    check("コンテナからの公開鍵の取得も断る", c.get("/api/push", headers=key).status_code, 403)
    os.environ.pop("VL_HERMES_KEY")

# 壊れた購読のファイルでも止まらない
s.PUSH_SUBS.write_text("{壊れている")
check("購読のファイルが壊れていても、空として読む", s.push_subs(), [])
s.PUSH_SUBS.write_text(json.dumps([{"endpoint": "https://evil.example.com/x", "keys": {"p256dh": "a", "auth": "b"}},
                                   {**apple.sub, "endpoint": "https://127.0.0.1\\@web.push.apple.com/x"}, apple.sub]))
check("ファイルに紛れた知らない配送先・読み方の食い違う届け先は使わない", [x["endpoint"] for x in s.push_subs()], [apple.sub["endpoint"]])
sent.clear()
asyncio.run(s.push_all("p｜t", "x", "t"))
check("送るのも、確かめた届け先だけ", [x["url"] for x in sent], [apple.sub["endpoint"]])
s.PUSH_SUBS.write_text(json.dumps([apple.sub]))

# ---------------- 頼んだ件の報告と通知 ----------------
D = {"pane": "w9:p1", "cwd": "/x/proj", "project": "proj", "title": "テスト", "text": "テストして", "offset": 0, "was_working": False, "gen": 1}
HUB = {"pane": "w1:p1", "cwd": str(s.HUB_DIR), "kind": "claude", "session": "h", "status": "idle", "title": "hub", "name": "", "hub": True}
spoken: list[float] = []


async def slow_speak(clients, text, cancel):
    await asyncio.sleep(1.0)
    spoken.append(time.time())


s.speak_to = slow_speak


async def run_report(kind, hub_answer=("done", "片づきました"), agents=None):
    sent.clear()
    spoken.clear()
    s.list_agents = (lambda: agents) if agents is not None else (lambda: [HUB])

    async def fake_ask(agent, text):
        return hub_answer

    s.ask = fake_ask
    t0 = time.time()
    try:
        await s.report(dict(D), kind)
    finally:  # 報告が例外で終わっても、裏で送る通知は最後まで待つ（本物の本体ではループが続くので送られる）
        await asyncio.gather(*list(s._tasks), return_exceptions=True)
    return t0


def bodies():
    return [apple.open(x["data"])["body"] for x in sent]


class Fake:  # 画面の代わり（読み上げを試すため、隠れた画面を1つつないでおく）
    visible, voice, speaker = False, True, None

    async def send(self, **m):
        pass


s.CLIENTS.add(Fake())
asyncio.run(run_report("done"))
check("完了: 通知を送る", bodies(), ["完了しました"])
check("完了: 通知の見出しは 案件｜件名、tag は report-<pane>", (apple.open(sent[0]["data"])["title"], apple.open(sent[0]["data"])["tag"]), ("proj｜テスト", "report-w9:p1"))
check("完了: 通知は読み上げが終わるのを待たずに送る", bool(sent and spoken and sent[0]["at"] < spoken[0]), True)
asyncio.run(run_report("more"))
check("続きの返答: 通知を送る", bodies(), ["続きの返答がありました"])
asyncio.run(run_report("background"))
check("裏で作業中: 通知を送る", bodies(), ["裏で作業を続けています"])
asyncio.run(run_report("gone"))
check("見失った: 通知を送る", bodies(), ["セッションが見つからなくなりました"])
asyncio.run(run_report("blocked", agents=[HUB, {**HUB, "pane": "w9:p1", "cwd": "/x/proj", "hub": False, "status": "blocked"}]))
check("承認待ち: report からは送らない（watch_blocked が送る）", bodies(), [])
asyncio.run(run_report("done", hub_answer=("error", "Hermes に届きません")))
check("ハブから報告を受け取れなくても、通知は送る", bodies(), ["完了しました（ハブから報告を受け取れませんでした）"])
asyncio.run(run_report("done", agents=[]))
check("ハブが居なくても、通知は送る", bodies(), ["完了しました（ハブが居ないので、声の報告はありません）"])


async def raising_report(d, kind, notify=None):
    raise TimeoutError("ハブの画面を読めませんでした")


real_report = s._report
s._report = raising_report
try:
    asyncio.run(run_report("done"))
    raised = False
except TimeoutError:
    raised = True
check("ハブへの報告が例外で終わっても、通知は1回送る（例外は見張りに返す）", (bodies(), raised), (["完了しました（ハブから報告を受け取れませんでした）"], True))
s._report = real_report
s.REPORT_PUSH_WAIT = 0.2


async def slow_hub():
    sent.clear()
    s.list_agents = lambda: [HUB]

    async def slow_ask(agent, text):
        await asyncio.sleep(1.0)
        return "done", "片づきました"

    s.ask = slow_ask
    t0 = time.time()
    await s.report(dict(D), "done")
    await asyncio.gather(*list(s._tasks), return_exceptions=True)
    return t0


t0 = asyncio.run(slow_hub())
check("ハブの報告が遅いときは、待ちきらずに通知する（1回だけ）", (bodies(), round(sent[0]["at"] - t0, 1) < 0.9 if sent else None),
      (["完了しました（ハブの報告は、このあと届きます）"], True))


class Screen:  # 見えている画面の代わり（hello を受けた時刻と、見えているか）
    def __init__(self):
        self.visible, self.voice, self.speaker, self.seen = True, True, None, time.time()

    async def send(self, **m):
        pass


async def hide_later(screen, after):
    await asyncio.sleep(after)
    screen.visible = False


async def visible_then_hidden(hide_at):
    """30秒の時点（ここでは0.2秒）では画面が見えていて、hide_at 秒で隠れ、ハブの報告は1.0秒でできる。"""
    sent.clear()
    screen = Screen()
    s.CLIENTS.add(screen)
    s.list_agents = lambda: [HUB]

    async def slow_ask(agent, text):
        await asyncio.sleep(1.0)
        return "done", "片づきました"

    s.ask = slow_ask
    hider = asyncio.create_task(hide_later(screen, hide_at)) if hide_at is not None else None
    await s.report(dict(D), "done")
    await asyncio.gather(*list(s._tasks), return_exceptions=True)
    if hider:
        hider.cancel()
    s.CLIENTS.discard(screen)


asyncio.run(visible_then_hidden(0.4))
check("30秒の時点で見えていても、ハブの報告の前に隠れたら、報告ができた時点で送る（1回）", bodies(), ["完了しました"])
asyncio.run(visible_then_hidden(None))
check("ずっと見えていれば送らない（その画面に声と文字で届く）", bodies(), [])
s.REPORT_PUSH_WAIT = 30

# 報告済みの件は、その後の続きの作業で止まっても・見失っても、台帳では完了のまま
tid = s.task_new({**D, "text": "x"}, "send")
s.task_set(tid, "done")
for k in ("blocked", "background", "gone"):
    asyncio.run(s.report({**D, "task": tid}, k))
check("報告済みの件は、そのあと止まっても・裏で作業しても・見失っても完了のまま", s.TASKS[tid]["status"], "done")
tid2 = s.task_new({**D, "text": "y"}, "send")
asyncio.run(s.report({**D, "task": tid2}, "blocked"))
check("報告の前に止まった件は「止まっている」", s.TASKS[tid2]["status"], "blocked")
s.CLIENTS.clear()

# ---------------- 承認・入力待ち（watch_blocked） ----------------
s.BLOCKED_POLL = 0.05
s.BLOCKED_NOTIFY_AFTER = 0
A = {"pane": "w2:p1", "cwd": "/x/a", "kind": "claude", "session": "sa", "status": "blocked", "title": "A", "name": "", "hub": False}
B = {**A, "pane": "w3:p1", "cwd": "/x/b", "session": "sb", "title": "B", "status": "idle"}
frames = [
    [A, B],                                            # 起動したときにもう止まっていた A は知らせない
    [{**A, "status": "working"}, {**B, "status": "blocked"}],  # B が止まった → 1回
    [{**A, "status": "working"}, {**B, "status": "blocked"}],  # B は止まったまま → 重ねない
    [{**A, "status": "blocked"}, {**B, "status": "blocked"}],  # A が止まった → 1回
    [{**A, "status": "idle"}, {**B, "status": "idle"}],
    [{**A, "status": "idle"}, {**B, "status": "blocked"}],     # B がまた止まった → もう1回
    [{**A, "session": "sa2", "status": "blocked"}, {**B, "status": "blocked"}],  # 同じ場所に別のセッションが止まって居る → 1回
    [{**A, "session": "sa2", "status": "blocked"}, {**B, "status": "blocked"}, {**HUB, "status": "blocked"}],  # ハブが止まった → 1回
]


async def run_blocked():
    sent.clear()
    i = {"n": 0}

    def agents():
        f = frames[min(i["n"], len(frames) - 1)]
        i["n"] += 1
        return f

    s.list_agents = agents
    task = asyncio.create_task(s.watch_blocked())
    while i["n"] < len(frames) + 2:
        await asyncio.sleep(0.02)
    task.cancel()


asyncio.run(run_blocked())
got = [(apple.open(x["data"])["title"], apple.open(x["data"])["tag"]) for x in sent]
check("止まったときに1回ずつ（起動時からのものは除く・止まったままは重ねない）", got,
      [("b｜B", "blocked-w3:p1"), ("a｜A", "blocked-w2:p1"), ("b｜B", "blocked-w3:p1"), ("a｜A", "blocked-w2:p1"), ("ハブ", "blocked-w1:p1")])
check("承認待ちは急ぎ（Urgency: high）", {x["headers"].get("Urgency") for x in sent}, {"high"})


async def run_blocked_error():
    calls = {"n": 0}

    def agents():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("herdr が応答しません")
        return [B]

    s.list_agents = agents
    task = asyncio.create_task(s.watch_blocked())
    while calls["n"] < 5:
        await asyncio.sleep(0.02)
    task.cancel()
    return calls["n"]


check("一覧を読めない回があっても、見張りは続く", asyncio.run(run_blocked_error()) >= 5, True)

# herdr に一時的に届かず一覧が空になっても、止まったままのものを知らせ直さない
frames = [[{**B, "status": "idle"}], [{**B, "status": "blocked"}], [], [], [{**B, "status": "blocked"}], [{**B, "status": "blocked"}]]
asyncio.run(run_blocked())
check("herdr に届かない回をはさんでも、止まったままなら重ねない", len(sent), 1)

# 止まってすぐ答えたもの（Mac の前で答えた承認）は鳴らさない。待つ秒数を過ぎても止まったままなら1回
s.BLOCKED_NOTIFY_AFTER = 0.4


async def run_threshold():
    sent.clear()
    t0 = time.time()
    calls = {"n": 0}

    def agents():
        calls["n"] += 1
        t = time.time() - t0
        if t < 0.2:
            return [{**B, "status": "idle"}]
        if t < 0.4:
            return [{**B, "status": "blocked"}]   # 0.2秒だけ止まって、すぐ答えた
        if t < 0.6:
            return [{**B, "status": "working"}]
        return [{**B, "status": "blocked"}]       # 0.6秒から止まったまま

    s.list_agents = agents
    task = asyncio.create_task(s.watch_blocked())
    await asyncio.sleep(0.9)
    early = len(sent)
    await asyncio.sleep(0.6)
    task.cancel()
    return early, len(sent)


early, late = asyncio.run(run_threshold())
check("すぐ答えた承認は鳴らさない（待つ秒数の前）", early, 0)
check("待つ秒数を過ぎても止まったままなら1回だけ", late, 1)


async def run_no_subs():
    calls = {"n": 0}

    def agents():
        calls["n"] += 1
        return [B]

    s.list_agents = agents
    task = asyncio.create_task(s.watch_blocked())
    await asyncio.sleep(0.4)
    task.cancel()
    return calls["n"]


saved_subs = s.PUSH_SUBS.read_text()
s.PUSH_SUBS.write_text("[]")
check("届け先が無い間は herdr を見に行かない", asyncio.run(run_no_subs()), 0)
s.PUSH_SUBS.write_text(saved_subs)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
