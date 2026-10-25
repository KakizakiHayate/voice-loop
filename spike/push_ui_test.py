# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "playwright==1.63.0"]  # 手元に入っているブラウザ（chromium 1243・webkit 2359）に合わせる
# ///
"""画面の 🔔（通知）と 📋（頼んだ件）の通し。本体の写しをこの試験の中で 127.0.0.1:8791 に立て、本物のブラウザで開く（常駐の 8790 には触れない）。
- Chromium: ブラウザの購読の部品（PushManager）を差し替えて、🔔 の入・切・送り直し・断られたとき、見えているかの知らせ、
  sw.js が通知を出すこと、📋 の一覧。配送サービスには送らない（requests.post を差し替える）
- WebKit（iPhone の名乗り・ホーム画面の外）: 🔔 がホーム画面への追加を案内すること、📋 の一覧
iPhone の実機の代わりにはならない（ホーム画面に置いたときの許可と、実際の通知の届き方は、実機で確かめる）。
使い方: uv run spike/push_ui_test.py"""
import base64
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="claude")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import requests  # noqa: E402
import uvicorn  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import server as s  # noqa: E402

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Resp:
    status_code, reason, text, headers = 201, "Created", "", {}


posted: list[str] = []
requests.post = lambda url, **k: (posted.append(url), Resp())[1]


async def nothing(*a, **k):
    return None


s.boot_ensure_hub = nothing
s.keep_gateway_token = nothing
s.watch_blocked = nothing
HUB = {"pane": "w1:p1", "cwd": str(s.HUB_DIR), "kind": "claude", "session": "h", "status": "idle", "title": "hub", "name": "", "hub": True}
s.list_agents = lambda: [HUB]
s.jsonl_path = lambda a: None
now = time.time()
s.TASKS.update({
    "a": {"id": "a", "kind": "send", "pane": "w2:p1", "project": "blog", "title": "記事", "text": "下書きを直して", "started": now - 600, "updated": now - 60, "status": "watching", "note": ""},
    "b": {"id": "b", "kind": "answer", "pane": "w3:p1", "project": "shop", "title": "注文", "text": "", "started": now - 7200, "updated": now - 3600, "status": "lost",
          "note": "本体が止まったので、見張りが切れました"},
    "c": {"id": "c", "kind": "send", "pane": "w4:p1", "project": "x", "title": "<b>y</b>", "text": "<img src=x onerror=\"window.__xss=1\">", "started": now - 30, "updated": now - 30,
          "status": "done", "note": ""},
})

key = ec.generate_private_key(ec.SECP256R1())
P256 = b64(key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))
AUTH = b64(os.urandom(16))
# ブラウザの購読の部品の代わり。subscribe で許可を聞いたことにして、購読を返す。window.__push で様子を見る・断らせる
FAKE_PUSH = """
(() => {
  const st = { sub: null, calls: [], deny: false, hidden: false };
  window.__push = st;
  const mk = () => ({ endpoint: 'https://fcm.googleapis.com/fcm/send/ui-test',
    toJSON() { return { endpoint: this.endpoint, keys: { p256dh: '%s', auth: '%s' } }; },
    async unsubscribe() { st.calls.push('unsubscribe'); st.sub = null; localStorage.removeItem('__fakeSub'); return true; } });
  if (localStorage.getItem('__fakeSub')) st.sub = mk(); // 購読はブラウザが持つので、開き直しても残る
  class FakePM {
    async getSubscription() { return st.sub; }
    async subscribe(o) { st.calls.push('subscribe:' + (o.userVisibleOnly ? 'visible' : 'silent') + ':' + o.applicationServerKey.length);
      if (st.deny) { st.denied = true; throw new DOMException('denied', 'NotAllowedError'); } st.sub = st.sub || mk(); localStorage.setItem('__fakeSub', '1'); return st.sub; }
  }
  window.PushManager = FakePM;
  Object.defineProperty(ServiceWorkerRegistration.prototype, 'pushManager', { get() { return this.__pm || (this.__pm = new FakePM()); } });
  Object.defineProperty(Notification, 'permission', { get() { return st.denied ? 'denied' : 'default'; } });
  Object.defineProperty(document, 'hidden', { get() { return st.hidden; } });
  // 状態の欄の文字と sys() の行を集める
})();
""" % (P256, AUTH)

srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=PORT, log_level="warning"))
th = threading.Thread(target=srv.run, daemon=True)
th.start()
for _ in range(200):
    if srv.started:
        break
    time.sleep(0.05)


def wait(fn, limit=8.0):
    end = time.time() + limit
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def sys_lines(page):
    return page.eval_on_selector_all(".sys", "els => els.map(e => e.textContent)")


def tasks_view(page, name_prefix):
    page.click("#tasksBtn")
    check(f"{name_prefix} 📋 で一覧が開く", page.is_visible("#tasks"), True)
    page.wait_for_selector("#taskList .task")
    items = page.eval_on_selector_all("#taskList .task", "els => els.map(e => [e.className, e.textContent])")
    check(f"{name_prefix} 3件が新しい順", [i[1].split("・")[0] for i in items], ["完了（報告済み）", "見張り中", "見張りが切れた（本体が止まった）"])
    check(f"{name_prefix} 色分け（動いている・完了・切れた）", [i[0] for i in items], ["task done", "task active", "task bad"])
    check(f"{name_prefix} 頼んだことと種類", "指示：下書きを直して" in items[1][1], True)
    check(f"{name_prefix} 答えた件は種類だけ・理由のメモ", ("承認・質問への答え" in items[2][1], "本体が止まったので、見張りが切れました" in items[2][1]), (True, True))
    check(f"{name_prefix} 中身は文字として出す（タグを解釈しない）", ("<img src=x" in items[0][1], "<b>y</b>" in items[0][1], page.evaluate("window.__xss || 0")), (True, True, 0))
    check(f"{name_prefix} 📋 は開いている間、色がつく", page.get_attribute("#tasksBtn", "aria-pressed"), "true")
    page.click("#tasksClose")
    check(f"{name_prefix} ✕ で閉じる", (page.is_visible("#tasks"), page.get_attribute("#tasksBtn", "aria-pressed")), (False, "false"))


with sync_playwright() as pw:
    # ---------------- Chromium ----------------
    b = pw.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    ctx.grant_permissions(["notifications"], origin=BASE)
    ctx.add_init_script(FAKE_PUSH)
    page = ctx.new_page()
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    check("Chromium: 🔔 が出る（切）", (page.is_visible("#bell"), page.get_attribute("#bell", "aria-pressed")), (True, "false"))
    check("Chromium: 開いた画面は「見えている」と本体に伝わる", wait(lambda: any(c.visible for c in s.CLIENTS)), True)
    page.evaluate("window.__push.hidden = true; document.dispatchEvent(new Event('visibilitychange'))")
    check("Chromium: 隠れると「見えていない」と伝わる", wait(lambda: s.CLIENTS and not any(c.visible for c in s.CLIENTS)), True)
    page.evaluate("window.__push.hidden = false; document.dispatchEvent(new Event('visibilitychange'))")
    wait(lambda: any(c.visible for c in s.CLIENTS))

    wait(lambda: page.evaluate("!!document.getElementById('bell') && typeof swReg !== 'undefined' && !!swReg && !!pushKey"))
    page.click("#bell")
    check("Chromium: 🔔 で購読（userVisibleOnly・鍵は65バイト）", wait(lambda: "subscribe:visible:65" in page.evaluate("window.__push.calls")), True)
    check("Chromium: 本体に登録される", wait(lambda: [x["endpoint"] for x in s.push_subs()] == ["https://fcm.googleapis.com/fcm/send/ui-test"]), True)
    check("Chromium: 試しの通知を送る（見えていても、押した端末には送る）", wait(lambda: posted == ["https://fcm.googleapis.com/fcm/send/ui-test"]), True)
    check("Chromium: 案内が出る", wait(lambda: "通知を受け取れるようにしました。試しの通知を送りました" in sys_lines(page)), True)
    check("Chromium: 🔔 に色がつく（入）", page.get_attribute("#bell", "aria-pressed"), "true")

    s.push_drop(["https://fcm.googleapis.com/fcm/send/ui-test"])  # 本体が登録を失った（状態フォルダを消した等）
    page.reload()
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    check("Chromium: 開き直すと購読を本体に送り直す（本体が登録を失っていても戻る・試しは送らない）",
          (wait(lambda: len(s.push_subs()) == 1), time.sleep(1.0) or len(posted)), (True, 1))
    check("Chromium: 開き直しても 🔔 は入のまま", wait(lambda: page.get_attribute("#bell", "aria-pressed") == "true"), True)

    # sw.js: 通知を出す
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
    sw.evaluate("""() => { self.__shown = []; ServiceWorkerRegistration.prototype.showNotification = function (t, o) {
      self.__shown.push([t, o.body, o.tag, o.renotify, o.data && o.data.url]); return Promise.resolve(); }; }""")
    cdp = ctx.new_cdp_session(page)
    regs = []
    cdp.on("ServiceWorker.workerRegistrationUpdated", lambda e: regs.extend(e["registrations"]))
    cdp.send("ServiceWorker.enable")
    for _ in range(100):  # 知らせは、Playwright の待ちの中で届く
        if regs:
            break
        page.wait_for_timeout(50)
    cdp.send("ServiceWorker.deliverPushMessage", {"origin": BASE + "/", "registrationId": regs[0]["registrationId"],
                                                  "data": '{"title": "blog｜記事", "body": "完了しました", "tag": "report-w2:p1", "url": "/"}'})
    wait(lambda: sw.evaluate("self.__shown.length") > 0)
    check("Chromium: sw.js が届いた見出しで通知を出す（同じ件は置き換えて鳴らし直す）", sw.evaluate("self.__shown"),
          [["blog｜記事", "完了しました", "report-w2:p1", True, "/"]])

    page.click("#bell")
    check("Chromium: もう一度押すと切れる（端末の購読をやめる）", wait(lambda: "unsubscribe" in page.evaluate("window.__push.calls")), True)
    check("Chromium: 本体の登録も消える", wait(lambda: s.push_subs() == []), True)
    check("Chromium: 切った案内と 🔔（切）", (wait(lambda: "通知を切りました" in sys_lines(page)), page.get_attribute("#bell", "aria-pressed")), (True, "false"))

    page.evaluate("window.__push.deny = true")
    page.click("#bell")
    check("Chromium: 許可されなかったときは、端末の設定を案内", wait(lambda: any("端末の設定（通知 → voice-loop）で許可" in t for t in sys_lines(page))), True)
    check("Chromium: 許可されなかったときは登録しない", s.push_subs(), [])

    tasks_view(page, "Chromium:")
    b.close()

    # ---------------- WebKit（iPhone の名乗り・ホーム画面の外） ----------------
    b = pw.webkit.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
    ctx.add_init_script("delete window.PushManager;")  # iPhone の Safari のタブには購読の部品が無い
    page = ctx.new_page()
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    check("WebKit: iPhone のブラウザでも 🔔 は出る（案内のため）", page.is_visible("#bell"), True)
    page.click("#bell")
    check("WebKit: 押すとホーム画面への追加を案内", wait(lambda: any("ホーム画面に追加した voice-loop で受け取れます" in t for t in sys_lines(page))), True)
    tasks_view(page, "WebKit:")
    b.close()

    # ---------------- 連絡先の設定が誤っているとき ----------------
    s.PUSH_SUBJECT = "https://example.com/some/path"
    b = pw.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    ctx.add_init_script(FAKE_PUSH)
    page = ctx.new_page()
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.wait_for_timeout(800)
    page.click("#bell")
    check("連絡先（VL_PUSH_SUBJECT）の誤りは、🔔 を押したときに理由を出す", wait(lambda: any("VL_PUSH_SUBJECT" in t for t in sys_lines(page))), True)
    check("連絡先が誤っている間は購読しない", page.evaluate("window.__push.calls"), [])
    b.close()
    s.PUSH_SUBJECT = "mailto:voice-loop@localhost"

    # ---------------- 購読の部品が無い、iPhone でもないブラウザ ----------------
    b = pw.chromium.launch()
    ctx = b.new_context()
    ctx.add_init_script("delete window.PushManager;")
    page = ctx.new_page()
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    check("通知を受け取れないブラウザでは 🔔 を出さない", page.is_visible("#bell"), False)
    b.close()

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
