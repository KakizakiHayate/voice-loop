# /// script
# dependencies = ["websockets>=12"]
# ///
"""ハブに指示の振り分けを頼み、その場の返答と、あとから届く「頼んだ件の報告」を受け取る（音は鳴らさない）。
使い方: uv run spike/report_test.py <ハブへの発言> [待つ報告の数=1]"""
import asyncio, json, sys, time, urllib.request
import websockets

QUESTION = sys.argv[1]
WANT = int(sys.argv[2]) if len(sys.argv) > 2 else 1
hub = next(a for a in json.loads(urllib.request.urlopen("http://127.0.0.1:8790/api/agents").read()) if a["hub"])


async def main():
    t0 = time.time()
    log = lambda s: print(f"[{time.time()-t0:6.1f}s] {s}", flush=True)
    async with websockets.connect("ws://127.0.0.1:8790/ws", max_size=None) as ws:
        await ws.send(json.dumps({"type": "hello", "speaker": 1878365376}))
        await ws.send(json.dumps({"type": "text", "text": QUESTION, "pane": hub["pane"], "key": "hub", "speaker": 1878365376}))
        reports = n = 0; secs = 0.0
        while reports < WANT:
            msg = await asyncio.wait_for(ws.recv(), timeout=600)
            if isinstance(msg, bytes):
                n += 1; secs += len(msg) / 2 / 44100; continue
            d = json.loads(msg)
            if d["type"] == "reply":
                log(("【報告】" if d.get("report") else "【その場の返答】") + (f"（{d.get('about')}）" if d.get("report") else "") + "\n" + d["text"] + "\n")
                is_report = bool(d.get("report"))
            elif d["type"] == "error": log("error: " + d.get("text", ""))
            elif d["type"] == "done":
                log(f"読み上げ送出: {n} 文 / {secs:.0f} 秒ぶん"); n = 0; secs = 0.0
                if is_report: reports += 1

asyncio.run(main())
