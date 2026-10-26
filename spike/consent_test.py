# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40"]
# ///
"""承認に答える・閉じるの守りの、利用者の発言の文法（scripts/consent.py）と、Claude Code のハブのときの「直前に聞いた返答」（heard）の試験。
Hermes のハブのときは spike/hermes_hub_test.py。herdr には触れない（差し替える）。使い方: uv run spike/consent_test.py"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
import consent as c  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


# ---------- 文法 ----------
CLOSE_YES = ["はい", "うん、お願い", "OKです", "閉じちゃって", "いいよ", "ええ、どうぞ", "yes", "はい、今度こそ閉じてください", "今度は閉じていいよ",
             "proj は閉じていいよ", "proj のセッションは閉じちゃって", "問題ないです", "よろしくお願いします", "ok", "じゃあ閉じて", "うんうん"]
CLOSE_NO = ["いい天気", "うん、いい天気", "天気がいいね", "お願いがあるんだけど", "はい、承認はしません", "はい、閉じるのは拒否します", "閉じないで", "いいよ、今度で",
            "いいですか", "閉じていいのかな", "ええと、どうしようかな", "閉じるな", "許可するなよ", "頼むな", "README を見せて", "いいえ", "いや、やめておいて", "うーん",
            "待って", "はい、でもまだ残して", "ううん", "うん、閉じるのは後でお願い", "今度にして", "うん、また今度お願い", "もういいよ", "ok no", "not yet",
            "いいな", "はい、それと天気も教えて", ""]
check("閉じる: 許す例", [t for t in CLOSE_YES if not c.affirms(t, "close", "proj")], [])
check("閉じる: 断る例（打ち消し・保留・問い返し・禁止形・ほかのことも言っている・関係のない文）", [t for t in CLOSE_NO if c.affirms(t, "close", "proj")], [])
REFUSE = ["いいえ", "はい、承認はしません", "閉じないで", "閉じなくていいよ", "いいよ、今度で", "ちょっと待って", "まだ", "大丈夫じゃない", "拒否します", "キャンセルで",
          "ok no", "not yet", "stop", "閉じるな", "許可するなよ", "頼むな", "いいですか", "閉じていいのかな", "2番かな"]
NOT_REFUSE = ["はい", "構わないです", "問題ないです", "差し支えないです", "notes は閉じていいよ", "いいよ", "2番で", "OKです", "大丈夫"]
check("断る（文法より先に、発言全体で見る。打ち消し・保留・禁止形・問い返し）", [t for t in REFUSE if not c.refuses(t)], [])
check("断らない（構わない・問題ない などの「ない」、語の中の no は数えない）", [t for t in NOT_REFUSE if c.refuses(t)], [])
check("英語の打ち消しは語の境目で見る（notes の no は数えない）", (c.affirms("notes は閉じていいよ", "close", "notes"), c.affirms("no", "close", "notes")), (True, False))
check("閉じる句は答えるでは使えず、許可の句は閉じるでは使えない", (c.affirms("閉じて", "answer", "proj"), c.affirms("許可して", "close", "proj")), (False, False))
ANS_YES = ["はい、許可して", "許可", "はい、許可していい", "通して", "うん、実行して", "どうぞ", "はい"]
check("答える（はい）: 許す例", [t for t in ANS_YES if not c.affirms(t, "answer", "proj")], [])
OPTS = {1: "Markdown", 2: "HTML", 3: "東京"}
PICK2 = ["2番で", "2番でお願いします", "じゃあ2番", "うん、2番で", "二番目にして", "HTML で", "html にして", "２番", "2"]
NOPICK = ["2時間かかる", "一番いい天気だね", "二度と実行するな", "2番にすると何が起きる？", "東京の天気を教えて", "2番じゃなくて3番", "2番で、3番も",
          "1番と2番", "2番かな", "HTML の話を聞かせて", ""]
check("選ぶ: 2 番を選ぶ例", [t for t in PICK2 if not c.chooses(t, 2, OPTS, "proj")], [])
check("選ぶ: どの番号も選ばない例", [(t, n) for t in NOPICK for n in (1, 2, 3) if c.chooses(t, n, OPTS, "proj")], [])
check("選ぶ: 番号と文言が別の選択肢を指すときは選ばない（一つに決まらない）",
      (c.chooses("1番で", 1, {1: "高速", 2: "1番"}), c.chooses("1番で", 2, {1: "高速", 2: "1番"}), c.chooses("高速で", 1, {1: "高速", 2: "1番"})), (False, False, True))
check("選ぶ: 数字のプロジェクト名でも、番号を取り違えない（「22番」を「2＋2番」と読まない。a2a 実装 1 回目の再現）",
      (c.chooses("22番", 2, {2: "低速", 22: "高速"}, "2"), c.chooses("22番", 22, {2: "低速", 22: "高速"}, "2"), c.chooses("22番", 2, {1: "a1", 2: "b2"}, "2")), (False, True, False))
check("選ぶ: 選択肢に無い番号を言ったら、どの番号も選ばない", [k for k in (1, 2, 3) if c.chooses("5番で", k, OPTS, "proj")], [])
check("選ぶ: 一字の文言では選ばない（取り違えやすい）", c.chooses("Aで", 1, {1: "A", 2: "B"}), False)
ASKS = {"proj の「テスト」が、ls の実行の許可を求めています。許可しますか？": True, "proj で、本当に消していいですか": True,
        "**PROJ** の「テスト」を閉じていいですか**": True, "proj の資料です。": False, "許可しますか？": False, "": False}
check("聞いた文: プロジェクト名（大文字小文字を問わない）と問いの形", {t: c.asks(t, "proj") for t in ASKS}, ASKS)

# ---------- Claude Code のハブ: 直前に聞いた返答（記録の、いまの発言の行より前だけ） ----------
HUB_LOG = Path(TMP) / "hub.jsonl"
HUB = {"pane": "w1:p1", "title": "hub", "cwd": str(s.HUB_DIR), "kind": "claude", "session": "hub-sess", "status": "idle", "name": "hub", "hub": True}
TARGET = {"pane": "w9:p1", "title": "テスト", "cwd": "/x/proj", "kind": "claude", "session": "sess-1", "status": "blocked", "name": "", "hub": False}
PERMISSION = """⏺ Bash(ls -la)
  ⎿  Running…
 Bash command
   ls -la
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for ls commands in /Users/me/proj
   3. No, and tell Claude what to do differently (esc)
 Esc to cancel · Tab to amend"""
keys: list[str] = []
s.list_agents = lambda: [dict(HUB), dict(TARGET)]
s.jsonl_path = lambda a: HUB_LOG if a["hub"] else None
s.pane_shows_trust = lambda pane: False
s.read_prompt = lambda pane: PERMISSION
s.herdr = lambda *a, **k: (keys.append(a[-1]) if a[:2] == ("agent", "send-keys") else None, {"result": {}})[1]
s.start_watch = lambda *a, **k: None
n = 0


def line(d):
    with HUB_LOG.open("a") as f:
        f.write(json.dumps(d, ensure_ascii=False) + "\n")


def user(text, note=False):
    global n
    n += 1
    line({"type": "user", "uuid": f"u{n}", "message": {"content": (s.NOTE_PREFIX + " " if note else "") + text}})
    return f"u{n}"


def tool():
    line({"type": "assistant", "message": {"content": [{"type": "tool_use", "input": {}}]}})
    line({"type": "user", "uuid": f"r{n}", "message": {"content": [{"type": "tool_result", "content": "x"}]}})


def text(*parts):
    line({"type": "assistant", "message": {"content": [{"type": "text", "text": p} for p in parts]}})


def answer(**body):
    keys.clear()
    r = json.loads(asyncio.run(s.hub_answer({"pane": "w9:p1", "answer": "yes", **body})).body)
    return r.get("reason", "ok"), list(keys)


HUB_LOG.write_text("")
t1 = user("テストの承認どうなってる？")
tool()
check("見る（1回目は unseen で、ハブが見たことにする）", answer()[0], "unseen")
text("確かめました。", "proj の「テスト」が ls の実行の許可を求めています。", "許可しますか？")
t2 = user("はい、許可して")
check("返答が複数の文に分かれていても、返答全体で聞いた文を取る", s.heard_before(HUB_LOG, t2), "確かめました。\n\nproj の「テスト」が ls の実行の許可を求めています。\n\n許可しますか？")
text("proj の「テスト」の承認を、いま許可しますか？")  # いまの発言の中でハブが書いた文（利用者はまだ聞いていない）
check("いまの発言の行より後の文は、聞いた文に使わない", s.heard_before(HUB_LOG, t2).endswith("許可しますか？") and "いま許可しますか" not in s.heard_before(HUB_LOG, t2), True)
check("聞いた文と発言が合えば 1 を送る", answer(), ("ok", ["1"]))

s.SEEN_PROMPT.clear()
user("別の話")
tool()
answer()  # 見る
text("blog の記事を書きました。")
user("テストが承認待ちで止まっています", note=True)  # 自動通知
tool()
text("proj の「テスト」が承認待ちです。ls の実行を許可しますか？")
t3 = user("はい")
check("あいだの自動通知への報告が、直前に聞いた文になる", s.heard_before(HUB_LOG, t3), "proj の「テスト」が承認待ちです。ls の実行を許可しますか？")
check("…その報告のあとの「はい」で 1 を送る", answer(), ("ok", ["1"]))

s.SEEN_PROMPT.clear()
user("承認見て")
tool()
answer()
text("proj の「テスト」が ls の実行の許可を求めています。許可しますか？")
user("テストの件が終わりました", note=True)
tool()
text("blog の記事が完了しました。")
t4 = user("はい")
check("問いのあとに別件の報告をはさむと、その報告が直前に聞いた文で、「はい」は送らない", answer(), ("not_asked", []))
text("proj の「テスト」の ls、許可しますか？")
user("いいえ、まだ")
check("発言が断りなら「はい」は送らない", answer(), ("not_said", []))
check("記録に無い発言 ID の聞いた文は空", s.heard_before(HUB_LOG, "nope"), "")

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
