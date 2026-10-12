# /// script
# dependencies = ["websockets>=12"]
# ///
"""ハブのモデル比較用。同じ発言を順に送り、返答が出るまでの時間と返答の長さを記録する（音は鳴らさない）。
使い方: uv run spike/model_ab.py <ラベル> <結果を書く JSON> [先頭から何問だけ送るか]"""
import asyncio, json, sys, time, urllib.request
import websockets

LABEL, OUT = sys.argv[1], sys.argv[2]
STEPS = [
    ("全体の状況", "全体の状況を教えて"),
    ("1件の中身", "ユービーウェブの件 いま何待ち"),
    ("振り分け", "ボイスループのテスト用のセッションに 今日の日付を一文で答えるように頼んで"),
]
if len(sys.argv) > 3: STEPS = STEPS[:int(sys.argv[3])]
hub = next(a for a in json.loads(urllib.request.urlopen("http://127.0.0.1:8790/api/agents").read()) if a["hub"])


async def main():
    rows = []
    async with websockets.connect("ws://127.0.0.1:8790/ws", max_size=None) as ws:
        await ws.send(json.dumps({"type": "hello", "speaker": 1878365376}))

        async def wait_reply(report: bool, timeout=300):
            """reply が届いた時刻（=ハブが答え終えた時刻）と本文、読み上げの秒数を返す。"""
            end = time.time() + timeout; cur = None; t_reply = None; secs = 0.0
            while time.time() < end:
                try: msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                except asyncio.TimeoutError: break
                if isinstance(msg, bytes):
                    if cur: secs += len(msg) / 2 / 44100
                    continue
                d = json.loads(msg)
                if d["type"] == "reply" and bool(d.get("report")) == report: cur, t_reply, secs = d, time.time(), 0.0
                elif d["type"] == "done" and cur: return t_reply, cur["text"], secs
            return None, "", 0.0

        for name, text in STEPS:
            t0 = time.time()
            await ws.send(json.dumps({"type": "text", "text": text, "pane": hub["pane"], "key": "hub", "speaker": 1878365376}))
            t, reply, secs = await wait_reply(False)
            rows.append({"step": name, "seconds": round(t - t0, 1) if t else None, "chars": len(reply), "speech_seconds": round(secs), "reply": reply})
            print(f"{LABEL} {name}: {rows[-1]['seconds']}秒 / {len(reply)}字 / 読み上げ {round(secs)}秒", flush=True)
            if name == "振り分け":
                t, reply, secs = await wait_reply(True)
                rows.append({"step": "完了の報告", "seconds": round(t - t0, 1) if t else None, "chars": len(reply), "speech_seconds": round(secs), "reply": reply})
                print(f"{LABEL} 完了の報告: 依頼から {rows[-1]['seconds']}秒 / {len(reply)}字 / 読み上げ {round(secs)}秒", flush=True)
    json.dump({"label": LABEL, "rows": rows}, open(OUT, "w"), ensure_ascii=False, indent=1)

asyncio.run(main())
