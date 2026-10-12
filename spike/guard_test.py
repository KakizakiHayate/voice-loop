# /// script
# dependencies = ["playwright>=1.48"]
# ///
"""話し相手の取り違え防止の検証（スマホ幅・WebKit）。

選んでいた相手が消えても別のセッションへ自動で切り替わらないこと、ハブが居ないときに画面から立て直せること、
古い画面から別の相手へ送ろうとするとサーバーが止めることを確かめる。試験用のセッションとハブのタブを作り直す。
使い方: uv run spike/guard_test.py <残す試験用ペイン> [画像の保存先]
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

KEEP_PANE = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else "."
BASE = "http://127.0.0.1:8790"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def herdr(*args):
    return json.loads(subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=150).stdout or "{}")


def agents():
    return json.loads(urllib.request.urlopen(f"{BASE}/api/agents").read())


def state(page):
    return page.evaluate("""() => ({ value: document.getElementById('agent').value,
        label: (document.getElementById('agent').selectedOptions[0] || {}).textContent || '',
        talk: !document.getElementById('talk').disabled, send: !document.getElementById('send').disabled, typed: !document.getElementById('typed').disabled,
        hubBtn: getComputedStyle(document.getElementById('hubStart')).display !== 'none',
        log: document.getElementById('log').innerText, status: document.getElementById('statusText').innerText,
        want: localStorage.getItem('vl.agentKey3') })""")


def refresh(page):
    page.evaluate("async () => { agentsSig = ''; await loadAgents(); }")
    page.wait_for_timeout(600)


with sync_playwright() as p:
    browser = p.webkit.launch()
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    page = ctx.new_page()
    errors, sent = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("websocket", lambda ws: ws.on("framesent", lambda f: sent.append(f) if isinstance(f, str) and '"type":"text"' in f.replace(" ", "") else None))

    # 1) 初めて開いたときはハブ
    page.goto(BASE)
    page.wait_for_function("document.querySelectorAll('#agent option').length > 1 && document.getElementById('conn').classList.contains('on')")
    page.wait_for_timeout(800)
    s = state(page)
    check("初回はハブが選ばれ、話せる", s["label"].startswith("ハブ") and s["talk"] and s["send"] and not s["hubBtn"] and s["want"] == "hub", s["label"])

    # 2) 個別のセッションを選んだあと、そのセッションが消える
    tab = herdr("tab", "create", "--workspace", KEEP_PANE.split(":")[0], "--cwd", str(Path(__file__).resolve().parent.parent), "--label", "tmp-guard", "--no-focus")["result"]
    tmp_pane, tmp_tab = tab["root_pane"]["pane_id"], tab["tab"]["tab_id"]
    time.sleep(2)
    herdr("agent", "start", "voice-tmp", "--kind", "claude", "--pane", tmp_pane, "--timeout", "90000", "--", "--model", "sonnet")
    time.sleep(2)
    refresh(page)
    page.select_option("#agent", tmp_pane)
    page.wait_for_timeout(800)
    s = state(page)
    tmp_key = next(a["key"] for a in agents() if a["pane"] == tmp_pane)
    check("個別のセッションを選べる", s["value"] == tmp_pane and s["talk"] and s["want"] == tmp_key, s["label"])
    herdr("tab", "close", tmp_tab)
    page.wait_for_function("document.getElementById('agent').value === ''", timeout=30000)  # 20秒ごとの自動更新で気づく
    s = state(page)
    check("相手が消えたら「選び直し」になり、別の相手に切り替わらない", s["value"] == "" and s["label"] == "話し相手を選んでください", s["label"])
    check("選び直すまで話せない（話す・送る・入力が無効）", not s["talk"] and not s["send"] and not s["typed"])
    check("理由が画面に出る", "見つかりません" in s["log"] and s["status"] == "話し相手を選んでください", s["log"][:40])
    page.evaluate("() => { document.getElementById('typed').disabled = false; document.getElementById('typed').value = '送られてはいけない'; document.getElementById('typedForm').requestSubmit(); }")
    page.wait_for_timeout(500)
    check("無効を外して送信しても、何も送られない", len(sent) == 0, f"送信 {len(sent)} 件")
    page.screenshot(path=f"{OUT}/guard_missing.png")

    # 3) 開き直しても、勝手にハブや先頭のセッションへは切り替わらない
    page.reload()
    page.wait_for_function("document.querySelectorAll('#agent option').length > 1")
    page.wait_for_timeout(800)
    s = state(page)
    check("開き直しても自動では切り替わらない", s["value"] == "" and not s["talk"], s["label"])

    # 4) 自分で選び直せば話せる
    hub_pane = next(a["pane"] for a in agents() if a["hub"])
    page.select_option("#agent", hub_pane)
    page.wait_for_timeout(800)
    s = state(page)
    check("ハブを選び直せば話せる", s["label"].startswith("ハブ") and s["talk"] and s["want"] == "hub")

    # 5) ハブが居ないとき: 切り替えずに知らせ、「ハブを起動」で立て直せる
    hub_tab = next(a["tab_id"] for a in herdr("agent", "list")["result"]["agents"] if a["cwd"].endswith("u-space.voice-loop/hub"))
    herdr("tab", "close", hub_tab)
    time.sleep(2)
    refresh(page)
    s = state(page)
    check("ハブが居ないと「選び直し」になり、ボタンが出る", s["value"] == "" and not s["talk"] and s["hubBtn"] and "ハブが起動していません" in s["log"], s["log"][:30])
    page.screenshot(path=f"{OUT}/guard_nohub.png")
    t0 = time.time()
    page.click("#hubStart")
    page.wait_for_function("(document.getElementById('agent').selectedOptions[0] || {textContent: ''}).textContent.startsWith('ハブ')", timeout=150000)
    page.wait_for_timeout(500)
    s = state(page)
    check("「ハブを起動」でハブが立ち、選ばれて話せる", s["talk"] and not s["hubBtn"], f"{time.time() - t0:.0f}秒")

    # 6) 古い画面が別の相手へ送ろうとしたら、サーバーが止める
    before = json.loads(urllib.request.urlopen(f"{BASE}/api/history?pane={KEEP_PANE}").read())["messages"]
    r = page.evaluate("""(pane) => new Promise(res => { const w = new WebSocket('ws://' + location.host + '/ws');
        w.onopen = () => w.send(JSON.stringify({type: 'text', text: '届いてはいけない指示', pane, key: 'stale-key', speaker: 888753760}));
        w.onmessage = e => { if (typeof e.data === 'string') { const m = JSON.parse(e.data); if (m.type === 'error' || m.type === 'reply') { w.close(); res(m); } } };
        setTimeout(() => res({type: 'timeout'}), 20000); })""", KEEP_PANE)
    time.sleep(3)
    after = json.loads(urllib.request.urlopen(f"{BASE}/api/history?pane={KEEP_PANE}").read())["messages"]
    check("相手の鍵が合わない指示はサーバーが止める", r.get("type") == "error" and r.get("gone") is True and len(after) == len(before), f"{r.get('text', r)} / 相手の履歴 {len(before)} → {len(after)}")

    check("ページのエラーなし", not errors, "; ".join(errors)[:200])
    browser.close()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
