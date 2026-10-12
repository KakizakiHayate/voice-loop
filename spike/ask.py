# /// script
# dependencies = ["websockets>=12"]
# ///
"""文字で1往復して、経過時刻・返信・音声の数を出す（音は鳴らさない）。使い方: uv run spike/ask.py <pane> <質問>"""
import asyncio, json, sys, time
import websockets

PANE, QUESTION = sys.argv[1], sys.argv[2]
SPEAKER = 1878365376  # コハク


async def main():
    t0 = time.time()
    log = lambda s: print(f"[{time.time()-t0:6.1f}s] {s}", flush=True)
    async with websockets.connect("ws://127.0.0.1:8790/ws", max_size=None) as ws:
        await ws.send(json.dumps({"type": "text", "text": QUESTION, "pane": PANE, "speaker": SPEAKER}))
        n = secs = 0
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=600)
            if isinstance(msg, bytes):
                n += 1; secs += len(msg) / 2 / 44100
                if n == 1: log("最初の音声が届いた")
                continue
            d = json.loads(msg)
            if d["type"] == "reply": log("返信:\n" + d["text"] + "\n")
            elif d["type"] in ("error", "status"): log(f"{d['type']}: {d.get('text')}")
            if d["type"] in ("done", "error", "stopped"):
                log(f"終了: 音声 {n} 文 / 計 {secs:.0f} 秒ぶん"); break

asyncio.run(main())
