# /// script
# dependencies = ["websockets>=12"]
# ///
"""E2E: 合成音声を「マイク入力」として WebSocket に流し、往復の時刻と返答音声を記録する。"""
import asyncio, json, sys, time, urllib.parse, urllib.request, subprocess, os
import websockets

PANE = sys.argv[1]
QUESTION = sys.argv[2] if len(sys.argv) > 2 else "トランシーバーの試験です。あなたは何のプロジェクトのディレクトリにいますか。二文で答えてください。"
SPEAKER = 1878365376  # コハク
AIVIS = "http://127.0.0.1:10101"
OUT = os.environ.get("TMPDIR", "/tmp")


def synth(text, speaker=SPEAKER):
    q = urllib.request.urlopen(urllib.request.Request(f"{AIVIS}/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": speaker}), method="POST")).read()
    return urllib.request.urlopen(urllib.request.Request(f"{AIVIS}/synthesis?speaker={speaker}", data=q, headers={"Content-Type": "application/json"}, method="POST")).read()


async def main():
    audio = synth(QUESTION)
    t0 = time.time()
    log = lambda s: print(f"[{time.time()-t0:6.1f}s] {s}", flush=True)
    async with websockets.connect("ws://127.0.0.1:8790/ws", max_size=None) as ws:
        await ws.send(json.dumps({"type": "turn", "pane": PANE, "speaker": SPEAKER, "filename": "q.wav"}))
        await ws.send(audio)
        log(f"sent {len(audio)} bytes of question audio")
        n = 0
        wavs = []
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=600)
            if isinstance(msg, bytes):
                n += 1
                p = f"{OUT}/vl_reply_{n}.wav"
                open(p, "wb").write(msg)
                wavs.append(p)
                log(f"audio #{n}: {len(msg)/2/44100:.1f}s")
                continue
            d = json.loads(msg)
            log(f"{d['type']}: {str(d.get('text', d))[:160]}")
            if d["type"] in ("done", "error", "stopped"):
                break
    for p in wavs:
        subprocess.run(["afplay", p])


asyncio.run(main())
