# /// script
# dependencies = ["websockets>=12"]
# ///
"""相手が作業中のうちに2つ目を頼む。相手が1つの返答にまとめたら、報告も1回で済むことを見る（音は鳴らさない）。"""
import asyncio, json, time, urllib.request
import websockets
hub = next(a for a in json.loads(urllib.request.urlopen("http://127.0.0.1:8790/api/agents").read()) if a["hub"])
QS = ["ボイスループのテスト用のセッションに 二十秒待ってから 一つ目完了 とだけ答えるように頼んで",
      "同じテスト用のセッションに 追加で 二つ目完了 とだけ答えるように頼んで"]

async def main():
    t0 = time.time(); log = lambda s: print(f"[{time.time()-t0:6.1f}s] {s}", flush=True)
    async with websockets.connect("ws://127.0.0.1:8790/ws", max_size=None) as ws:
        await ws.send(json.dumps({"type": "hello", "speaker": 1878365376}))
        qi = reports = 0; is_report = False
        async def ask():
            nonlocal qi
            log("▶ 発言: " + QS[qi]); await ws.send(json.dumps({"type": "text", "text": QS[qi], "pane": hub["pane"], "key": "hub", "speaker": 1878365376})); qi += 1
        await ask()
        last = None
        while True:  # 最初の報告のあと45秒待って、同じ結果の報告が重ねて来ないことを見る
            if last and time.time() - last > 45: break
            try: msg = await asyncio.wait_for(ws.recv(), timeout=5 if last else 600)
            except asyncio.TimeoutError: continue
            if isinstance(msg, bytes): continue
            d = json.loads(msg)
            if d["type"] == "reply":
                is_report = bool(d.get("report")); log(("【報告】" if is_report else "【その場の返答】") + d["text"].replace("\n", " ")[:230])
            elif d["type"] == "error": log("error: " + d.get("text", ""))
            elif d["type"] == "done":
                if is_report: reports += 1; last = time.time()
                elif qi < len(QS): await ask()
    print(f'報告の数: {reports}')
asyncio.run(main())
