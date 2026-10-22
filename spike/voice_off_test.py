# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40"]
# ///
"""読み上げを切った画面（🔇）に音声を送らないことと、聞く画面が無いときは語り手も合成も呼ばないことの試験。
語り手（または語り手の API のストリームだけ）・合成・セッションへの送信は差し替え、画面は作り物の接続で受ける。
「その場で閉じる」は、speak_to が戻った直後に見る（イベントループの後始末で閉じられた分と区別するため）。使い方: uv run spike/voice_off_test.py"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state")  # 本物の設定フォルダを読まない
os.environ.pop("VL_HUB", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402

SENTENCES = ["一文目です。", "二文目です。", "三文目です。"]
narr = {"calls": 0, "yielded": 0, "closed": 0}
stream = {"closed": 0}
hooks = {}
REAL_NARRATE = s.narrate  # 本物の語り手。下の API のストリームだけ差し替えて、閉じ方まで確かめる


async def fake_stream(model, text):
    """語り手の API の代わり。1文ずつ流し、閉じられたら数える。"""
    try:
        for i, sentence in enumerate(SENTENCES):
            yield sentence
            if "after" in hooks:
                hooks["after"](i)
    finally:
        stream["closed"] += 1


s._narrate_stream = fake_stream


async def fake_narrate(text, cancel):
    narr["calls"] += 1
    try:
        for i, sentence in enumerate(SENTENCES):
            narr["yielded"] += 1
            yield sentence
            if "after" in hooks:  # i 文目を送り終えて、次の文を求められたところ
                hooks["after"](i)
    finally:
        narr["closed"] += 1


s.narrate = fake_narrate
s.synth = lambda text, speaker: f"{speaker}:{text}".encode()


class Sock:
    """作り物の画面の接続。inbox の順に届け、関数なら待ってから次へ進む。尽きたら切断。"""

    def __init__(self, inbox=()):
        self.texts, self.wavs, self.inbox = [], [], list(inbox)

    async def accept(self):
        pass

    async def send_text(self, t):
        self.texts.append(json.loads(t))

    async def send_bytes(self, b):
        self.wavs.append(b)

    async def receive(self):
        while self.inbox:
            m = self.inbox.pop(0)
            if not callable(m):
                return {"type": "websocket.receive", "text": json.dumps(m)}
            await m()
        return {"type": "websocket.disconnect"}

    def types(self):
        return [t["type"] for t in self.texts]


def client(voice=True, speaker=None):
    c = s.Client(Sock())
    c.voice, c.speaker = voice, speaker
    return c


def reset():
    narr.update(calls=0, yielded=0, closed=0)
    stream.update(closed=0)
    hooks.clear()


def speak_now(clients, cancel=None, real=False):
    """speak_to を走らせ、戻った直後の（イベントループの後始末で閉じられる前の）語り手と API のストリームの閉じた数を返す。"""
    async def run():
        await s.speak_to(clients, "返答です", cancel or asyncio.Event())
        return narr["closed"], stream["closed"]
    s.narrate = REAL_NARRATE if real else fake_narrate
    try:
        return asyncio.run(run())
    finally:
        s.narrate = fake_narrate


results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def speak(clients, text="返答です"):
    reset()
    asyncio.run(s.speak_to(clients, text, asyncio.Event()))


c = client()
speak([c])
check("読み上げる画面には、文ごとに音声を送る", (c.sock.types(), len(c.sock.wavs), narr["calls"]), (["speaking", "done"], 3, 1))
check("送り終えたら done に文の数", c.sock.texts[-1], {"type": "done", "sentences": 3})

c = client(voice=False)
speak([c])
check("切った画面には音声を送らず、語り手も呼ばない", (c.sock.types(), c.sock.wavs, narr["calls"]), (["speaking", "done"], [], 0))
check("切った画面の done は 0 文", c.sock.texts[-1], {"type": "done", "sentences": 0})

on, off = client(speaker=5), client(voice=False, speaker=7)
speak([on, off])
check("2画面で片方だけ切ったら、語り手は1回、音声は読み上げる画面だけ", (narr["calls"], len(on.sock.wavs), off.sock.wavs), (1, 3, []))
check("音声は、読み上げる画面の声で作る", on.sock.wavs[0], "5:一文目です。".encode())
check("切った画面にも、終わりの知らせは届く", (on.sock.types(), off.sock.types()), (["speaking", "done"], ["speaking", "done"]))

c = client()
reset()
hooks["after"] = lambda i: setattr(c, "voice", False) if i == 0 else None  # 1文目を聞いたところで 🔇
closed_now = speak_now([c])
check("読み上げの途中で切ったら、次の文から送らない", (len(c.sock.wavs), c.sock.texts[-1]), (1, {"type": "done", "sentences": 1}))
check("途中で切ったら、語り手の生成もその場で閉じる（speak_to が戻る前に）", (narr["yielded"], closed_now[0]), (2, 1))

c = client()
reset()
hooks["after"] = lambda i: setattr(c, "voice", False) if i == 0 else None
closed_now = speak_now([c], real=True)
check("本物の語り手でも、途中で切ったら API のストリームまでその場で閉じる", (len(c.sock.wavs), closed_now[1]), (1, 1))
c = client()
reset()
closed_now = speak_now([c], real=True)
check("本物の語り手で最後まで読んだときも、ストリームは閉じている", (len(c.sock.wavs), closed_now[1]), (3, 1))

on, off = client(), client()
reset()
hooks["after"] = lambda i: setattr(off, "voice", False) if i == 0 else None
asyncio.run(s.speak_to([on, off], "返答です", asyncio.Event()))
check("2画面で片方だけ途中で切っても、もう片方は最後まで読み上げる", (len(on.sock.wavs), len(off.sock.wavs), narr["yielded"]), (3, 1, 3))

c = client()
reset()
cancel = asyncio.Event()
hooks["after"] = lambda i: cancel.set() if i == 0 else None  # 「停止」は前と同じく効く
closed_now = speak_now([c], cancel)
check("「停止」で止めたときも、語り手をその場で閉じる", (len(c.sock.wavs), closed_now[0]), (1, 1))

# 画面からの hello（🔇 を押すたびにも届く）
seen = []


async def snapshot():
    seen.append(next((c.voice, c.speaker) for c in s.CLIENTS))


for hello, want in (({"type": "hello", "speaker": "5", "voice": False}, (False, 5)),
                    ({"type": "hello", "speaker": "5", "voice": True}, (True, 5)),
                    ({"type": "hello", "speaker": "5"}, (True, 5))):  # voice を送らない古い画面
    seen.clear()
    asyncio.run(s.ws(Sock([hello, snapshot])))
    check(f"hello {json.dumps({k: v for k, v in hello.items() if k != 'type'})} → 読み上げ・声", seen, [want])
seen.clear()
asyncio.run(s.ws(Sock([{"type": "hello", "speaker": "5", "voice": False}, snapshot, {"type": "hello", "speaker": "5", "voice": True}, snapshot])))
check("切ったあとに戻せる", seen, [(False, 5), (True, 5)])

# 画面からの1往復（文字で送る）: 切っていれば返答の文字だけが届く
AGENT = {"pane": "w9:p1", "title": "テスト", "cwd": "/x/proj", "kind": "claude", "session": "sess-1", "status": "idle", "name": "", "hub": False}
s.list_agents = lambda: [dict(AGENT)]
asked = []


async def fake_ask(agent, text):
    asked.append(text)
    return "done", "返答です。二文目です。"
s.ask = fake_ask


def turn(voice):
    reset()
    sock = Sock([{"type": "hello", "speaker": "5", "voice": voice},
                 {"type": "text", "text": "こんにちは", "pane": "w9:p1", "speaker": "5"}, lambda: asyncio.sleep(0.5)])
    asyncio.run(s.ws(sock))
    return sock


sock = turn(False)
check("切った画面で文字を送ると、返答の文字が届き、音声は無い", (sock.types()[1:], [t.get("text") for t in sock.texts if t["type"] == "reply"], sock.wavs, narr["calls"]),
      (["reply", "speaking", "done"], ["返答です。二文目です。"], [], 0))
check("切っていても、セッションには送る", asked[-1], "こんにちは")
sock = turn(True)
check("読み上げる画面では、これまでどおり音声も届く", (sock.types()[1:], len(sock.wavs), narr["calls"]), (["reply", "speaking", "done"], 3, 1))

# ハブからの報告: 開いている画面がどれも切っていれば、語り手を呼ばず、文字だけ届く
HUB = {"pane": "w1:p1", "title": "hub", "cwd": str(s.HUB_DIR), "kind": "claude", "session": "hub-sess", "status": "idle", "name": "hub", "hub": True}
s.list_agents = lambda: [dict(HUB), dict(AGENT)]
d = {"pane": "w9:p1", "project": "proj", "title": "テスト", "text": "README を直して"}


def report(*clients):
    reset()
    s.CLIENTS.clear()
    s.CLIENTS.update(clients)
    asyncio.run(s.report(d, "done"))
    s.CLIENTS.clear()


a, b = client(voice=False), client(voice=False)
report(a, b)
check("報告: どの画面も切っていれば、語り手を呼ばず、文字だけ届く",
      (narr["calls"], a.sock.types(), b.sock.types(), a.sock.wavs + b.sock.wavs), (0, ["reply", "speaking", "done"], ["reply", "speaking", "done"], []))
check("報告の文字には、何についての報告かが付く", (a.sock.texts[0]["report"], a.sock.texts[0]["about"]), (True, "proj｜テスト"))
a, b = client(), client(voice=False)
report(a, b)
check("報告: 読み上げる画面だけに音声", (narr["calls"], len(a.sock.wavs), b.sock.wavs), (1, 3, []))

bad = [f"{name}: {g!r}（期待 {w!r}）" for name, ok, g, w in results if not ok]
print("\n".join(bad) or f"{len(results)} 例、すべて期待どおり")
sys.exit(1 if bad else 0)
