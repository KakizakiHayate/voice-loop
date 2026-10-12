# /// script
# dependencies = ["websockets>=12"]
# ///
"""ハブと何往復か話す（文字入力・音は鳴らさない）。引数を順に実行する:
  "発言"      … ハブに送り、その場の返答を待つ
  @report    … ハブからの自発的な報告を1つ待つ
  @quiet:N   … N 秒待ち、その間に報告が来ないことを確かめる
使い方: uv run spike/talk.py "〜を頼んで" @report "許可して" @report"""
import asyncio, json, sys, time, urllib.request
import websockets

hub = next(a for a in json.loads(urllib.request.urlopen("http://127.0.0.1:8790/api/agents").read()) if a["hub"])


async def main():
    t0 = time.time(); log = lambda s: print(f"[{time.time()-t0:6.1f}s] {s}", flush=True)
    async with websockets.connect("ws://127.0.0.1:8790/ws", max_size=None) as ws:
        await ws.send(json.dumps({"type": "hello", "speaker": 1878365376}))
        backlog = []  # まだ使っていない報告

        async def next_reply(want_report: bool, timeout: float):
            """次の返答（その場の返答 or 報告）を、読み上げの送出完了まで待って返す。"""
            if want_report and backlog: return backlog.pop(0)
            end = time.time() + timeout; cur = None
            while time.time() < end:
                try: msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                except asyncio.TimeoutError: return None
                if isinstance(msg, bytes): continue
                d = json.loads(msg)
                if d["type"] == "reply": cur = d
                elif d["type"] == "error": log("error: " + d.get("text", ""))
                elif d["type"] == "done" and cur:
                    if bool(cur.get("report")) == want_report: return cur
                    if cur.get("report"): backlog.append(cur)  # その場の返答を待つ間に届いた報告は取っておく
                    cur = None
            return None

        for step in sys.argv[1:]:
            if step == "@report":
                r = await next_reply(True, 600); log("【報告】" + (r["text"].replace("\n", " ")[:300] if r else "（来なかった）"))
            elif step.startswith("@quiet:"):
                r = await next_reply(True, float(step.split(":")[1])); log("【静かに待った】" + ("報告なし" if not r else "報告あり: " + r["text"].replace("\n", " ")[:200]))
            else:
                log("▶ " + step)
                await ws.send(json.dumps({"type": "text", "text": step, "pane": hub["pane"], "key": "hub", "speaker": 1878365376}))
                r = await next_reply(False, 600); log("【返答】" + (r["text"].replace("\n", " ")[:300] if r else "（来なかった）"))

asyncio.run(main())
