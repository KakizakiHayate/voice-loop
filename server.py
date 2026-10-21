# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40"]
# ///
"""voice-loop: トランシーバー式で herdr のエージェントと声で話す中継サーバー。

ブラウザ(タップ→話す→タップ) → whisper-server → herdr agent prompt → セッション記録(JSONL)から返信を読む
→ 語り手(Haiku, stream) → 文ごとに声のエンジン（AivisSpeech か VOICEVOX） → ブラウザで再生

話し相手は個別のセッションか「ハブ」。ハブは hub/ で動く Claude Code セッションで、hubctl 経由で
他の全セッションの状況を読み、指示を送る（/api/hub/*）。
"""
import asyncio
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
import openai
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))
from origin_guard import SameOriginOnly  # noqa: E402  接続元の確かめ

PLUGIN_ID = "u-space.voice-loop"  # herdr-plugin.toml の id と同じ
# 端末ごとの設定と状態は、本体のフォルダ（herdr が入れ直すと置き換わる）の外に置く。場所は herdr がプラグインに用意するフォルダ
CONFIG_DIR = Path(os.environ.get("VL_CONFIG_DIR") or Path.home() / ".config/herdr/plugins/config" / PLUGIN_ID)
STATE_DIR = Path(os.environ.get("VL_STATE_DIR") or Path.home() / ".local/state/herdr/plugins" / PLUGIN_ID)


def _load_config_env():
    """CONFIG_DIR/config.env の「名前=値」を、まだ無い環境変数として読み込む。"""
    try:
        lines = (CONFIG_DIR / "config.env").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        name, sep, value = line.partition("=")
        if sep and name.strip() and not name.lstrip().startswith("#"):
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


_load_config_env()
WHISPER = os.environ.get("VL_WHISPER", "http://127.0.0.1:8080/v1/audio/transcriptions")
# 声のエンジン。どちらも同じ形の API（audio_query → synthesis）で、違うのは呼び先と既定の声だけ
VOICE_ENGINES = {
    "aivis": ("AivisSpeech", "http://127.0.0.1:10101", 888753760),  # まお / ノーマル
    "voicevox": ("VOICEVOX", "http://127.0.0.1:50021", 8),  # 春日部つむぎ / ノーマル。軽い（2026-10-21 実測: 1文 0.9秒・待機 78MB）
}
VOICE = os.environ.get("VL_VOICE", "aivis").strip().lower()
VOICE_NAME, _voice_url, _voice_speaker = VOICE_ENGINES.get(VOICE, VOICE_ENGINES["aivis"])  # 知らない名前なら AivisSpeech で動き、点検で知らせる
# VL_AIVIS は 0.2 までの名前。名前のとおり AivisSpeech のときだけ効かせ、VOICEVOX に替えた人の呼び先を古い値で上書きしない
TTS_URL = os.environ.get("VL_VOICE_URL") or (os.environ.get("VL_AIVIS") if VOICE_NAME == "AivisSpeech" else None) or _voice_url
# 語り手（返答を話し言葉に直す軽いモデル）。既定は NARRATOR_MODEL。narrator_model というファイルに1行書けば、再起動なしで切り替わる
#   名前に "/" が無い … Anthropic 直結。認証は `ant auth login` のプロフィール（~/.config/anthropic/）。API キー不要
#   名前に "/" がある … Vercel AI Gateway（OpenAI 互換）。認証は gateway/ に紐づけた Vercel プロジェクトの OIDC トークン
NARRATOR_MODEL = os.environ.get("VL_NARRATOR_MODEL", "claude-haiku-4-5")
NARRATOR_FILE = CONFIG_DIR / "narrator_model"
narrator_client = anthropic.AsyncAnthropic()
NARRATOR_PROMPT = (HERE / "narrator_prompt.md").read_text()
GATEWAY_URL = "https://ai-gateway.vercel.sh/v1"
GATEWAY_DIR = CONFIG_DIR / "gateway"  # `vercel link` 済みのフォルダ。.env.local の VERCEL_OIDC_TOKEN は12時間で切れる
GATEWAY_PULL = ["npx", "-y", "vercel@63.1.2", "env", "pull", ".env.local", "--yes"]  # チームは紐づけ情報（.vercel/）から決まる
# AivisSpeech が誤読する英字の語（2026-10-12 に読みを実測）。語り手が英字のまま残しても正しく読めるよう、合成の直前に置き換える。
# 利用者の語（プロジェクト名など）は、設定フォルダの readings.txt に「語=よみ」で足す
READINGS = {"herdr": "ハーダー", "launchd": "ローンチディー", "hubctl": "ハブコントロール", "ghostty": "ゴースティ"}


def _load_readings():
    try:
        lines = (CONFIG_DIR / "readings.txt").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        word, sep, reading = line.partition("=")
        if sep and word.strip() and reading.strip() and not word.lstrip().startswith("#"):
            READINGS[word.strip().lower()] = reading.strip()


_load_readings()
READING_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(map(re.escape, sorted(READINGS, key=len, reverse=True))) + r")(?![A-Za-z])", re.I)
PROJECTS = Path.home() / ".claude" / "projects"
HUB_DIR = CONFIG_DIR / "hub"  # ここを作業ディレクトリにしている Claude Code セッションが「ハブ」。中の指示文は利用者が書き換えてよい
HUB_MODEL = os.environ.get("VL_HUB_MODEL", "sonnet")
HUB_WORKSPACE = os.environ.get("VL_HUB_WORKSPACE", "")  # ハブのタブを作る herdr ワークスペースの名前。空なら先頭のワークスペース
HUB_SETTLE = float(os.environ.get("VL_HUB_SETTLE", "30"))  # 起動時、herdr 自身のセッション復元が落ち着くのを待つ秒数
DEFAULT_SPEAKER = int(os.environ.get("VL_DEFAULT_SPEAKER") or _voice_speaker)  # 画面が声を知らせてこないときの声
NOTE_PREFIX = "[voice-loop 通知]"  # ハブへの自動通知の目印。ユーザーの発言と区別する
WATCH_TIMEOUT = 3 * 3600  # 頼んだ件を見張る上限
# herdr 経由の文は、改行があるか約800字を超えると Claude Code が「貼り付けられた内容」として包み、
# 受け手は指示として扱いにくくなる（2026-10-12 実測: 692字は通常、842字は貼り付け扱い）。指示と通知は一行・この字数以内にする
PROMPT_MAX = 700
BG_NOTICE_AFTER = float(os.environ.get("VL_BG_NOTICE_AFTER", "180"))  # 相手の裏の作業が長引いたとき、「まだ動いている」と一度知らせるまでの秒数
# ハブがセッションを立ててよいのは、この直下の既存プロジェクトだけ。未設定なら、セッションの起動は使えない
PROJECTS_DIR = Path(os.environ["VL_PROJECTS_DIR"]).expanduser() if os.environ.get("VL_PROJECTS_DIR") else None
SPAWN_MAX = 3  # ハブが立てたセッションを同時に生かしておける数
SPAWN_MODELS = {"opus", "sonnet", "haiku", "fable"}
SPAWN_PREFIX = "hubjob-"  # ハブが立てたセッションの herdr 上の名前の頭（数を数えるのに使う）
PASTED = re.compile(r'^\s*<pasted_content id="[^"]*">\n(.*)\n</pasted_content id="[^"]*">\s*$', re.S)
# 無音から whisper が生む定番文。これが来たら「聞こえなかった」扱い
HALLUCINATIONS = {"ご視聴ありがとうございました", "次の動画でお会いしましょう", "チャンネル登録お願いします", "おやすみなさい", "ありがとうございました"}
SENTENCE_END = re.compile(r"(?<=[。！？!?\n])")

@asynccontextmanager
async def lifespan(_app):
    tasks = [
        asyncio.create_task(boot_ensure_hub()),  # サーバー起動時（= Mac の再起動後）にハブを立て直す
        asyncio.create_task(keep_gateway_token()),
    ]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(SameOriginOnly)  # ブラウザで開いた別のサイトからの接続を断る


def hlog(msg: str, flush: bool = True):
    """ハブまわりの出来事をログに残す。行頭は [hub]、行末に時刻。"""
    print(f"[hub] {msg}  @{time.strftime('%H:%M:%S')}", flush=flush)


# ---------- herdr ----------
def herdr(*args: str, timeout: float | None = 60) -> dict:
    try:
        p = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"raw": str(e), "code": -1}
    out = p.stdout.strip() or p.stderr.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out, "code": p.returncode}


def read_screen(pane: str, lines: int) -> str:
    """`herdr agent read` はプレーンテキストを返す。"""
    p = subprocess.run(["herdr", "agent", "read", pane, "--source", "recent-unwrapped", "--lines", str(lines)], capture_output=True, text=True, timeout=20)
    return "\n".join(l for l in p.stdout.splitlines() if l.strip())


STATUS_LINE = re.compile(r"^\s*(📂|⏵⏵|⏸|✻)")  # 状態表示の行（費用や経過時間で変わる）
OPTION = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s+(\S.*)$")


def read_prompt(pane: str) -> str:
    """承認・入力待ちで止まっている画面。対話の枠は visible でしか取れない（recent には出ない）。
    罫線と、内容に関係なく変わる状態表示の行は除く。"""
    p = subprocess.run(["herdr", "agent", "read", pane, "--source", "visible", "--lines", "45"], capture_output=True, text=True, timeout=20)
    keep = [l.rstrip() for l in p.stdout.splitlines() if l.strip() and not STATUS_LINE.match(l) and not set(l.strip()) <= set("─╌")]
    return "\n".join(keep[-30:])


def parse_prompt(text: str) -> dict:
    """止まっている画面から、問いの種類と選択肢を読む。末尾の操作案内（Esc to cancel）から上へたどり、1番まで集める
    （それより上にある会話中の番号つきリストを選択肢と取り違えないため）。"""
    lines = text.splitlines()
    foot = next((i for i in range(len(lines) - 1, -1, -1) if "Esc to cancel" in lines[i]), None)
    opts: dict[int, str] = {}
    if foot is not None:
        for l in reversed(lines[:foot]):
            m = OPTION.match(l)
            if m:
                opts[int(m.group(1))] = m.group(2).strip()
                if m.group(1) == "1":
                    break
    if opts.get(1, "").lower() == "yes" and any(v.lower().startswith("no") for v in opts.values()):
        return {"kind": "permission", "options": opts}
    choices = {n: t for n, t in opts.items() if t not in ("Type something.", "Chat about this")}
    return {"kind": "question" if 1 in choices else "unknown", "options": choices}


TRUST_PROMPT = re.compile(r"trust (this|the files in this) folder|Do you trust", re.I)


def visible_screen(pane: str) -> str | None:
    """その pane にいま映っている画面。読めなかったら None（「何も出ていない」と区別する）。"""
    try:
        r = subprocess.run(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "45"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout.strip() else None


def pane_shows_trust(pane: str) -> bool | None:
    """その pane が、Claude Code の「このフォルダを信頼しますか」で止まっているか。新しいフォルダで初めて起動したときだけ出る。
    画面を読めなかったら None。"""
    screen = visible_screen(pane)
    return None if screen is None else bool(TRUST_PROMPT.search(screen))


def _agents() -> list[dict]:
    r = herdr("agent", "list")
    agents = r.get("result", {}).get("agents", [])
    return [
        {
            "pane": a["pane_id"],
            "title": a.get("terminal_title_stripped") or a.get("terminal_title", ""),
            "cwd": a.get("cwd", ""),
            "kind": a.get("agent"),
            "session": (a.get("agent_session") or {}).get("value"),
            "status": a.get("agent_status"),
            "name": a.get("name") or "",
            "hub": a.get("cwd", "") == str(HUB_DIR),
        }
        for a in agents
    ]


def _hub_unstarted(a: dict) -> bool:
    """会話を始める前の画面（フォルダの信頼、ログイン、初回の設定など）で止まっているハブか。
    herdr がセッション ID を持つのは Claude Code が会話を受け付けてからなので、「止まっていて ID が無い」で見分ける。"""
    return a["hub"] and a["status"] == "blocked" and not a["session"]


def list_agents() -> list[dict]:
    # 始まる前の画面で止まっているハブは、まだ話し相手にしない（文字を送ると、その画面に答えてしまう）
    return [a for a in _agents() if not _hub_unstarted(a)]


def agent_key(a: dict) -> str:
    """画面が「同じ相手か」を見分ける鍵。ハブは立て直すとセッション ID が変わるので "hub" で指す。"""
    return "hub" if a["hub"] else (a.get("session") or a["pane"])


_hub_lock = threading.Lock()
# 初期の選択は「No, exit」なので、Enter だけでは進まない
TRUST_TODO = ("herdr の hub タブを開くと、Claude Code がこのフォルダを信頼するか聞いています。↓ で「Yes, I trust this folder」を選び、Enter を押してください"
              "（初回だけ。ハブが ./hubctl を確認なしで実行できる設定を持っているために出る確認です）")
STUCK_TODO = ("herdr の hub タブを開くと、Claude Code が会話を始める前の画面で止まっています。画面の問いに答えてください"
              "（初回は、ログインや設定の確認が出ることがあります）。答えたあと、もう一度 start を実行するか「ハブを起動」を押します")


def hub_stuck() -> dict | None:
    """ハブが会話を始める前の画面で止まっていれば、その pane と、利用者に頼むこと。答えるのは利用者（代わりに答えない）。"""
    for p in herdr("pane", "list").get("result", {}).get("panes") or []:
        if p.get("cwd") == str(HUB_DIR) and pane_shows_trust(p["pane_id"]):
            return {"reason": "trust", "pane": p["pane_id"], "detail": TRUST_TODO}
    hub = next((a for a in _agents() if _hub_unstarted(a)), None)
    return {"reason": "stuck", "pane": hub["pane"], "detail": STUCK_TODO} if hub else None


def ensure_hub() -> dict:
    """ハブが居なければ立てる。Mac の再起動後に残る hub タブ（中身はシェル）があればその中で、無ければタブを作る。"""
    with _hub_lock:
        if not (HUB_DIR / "speaking.md").is_file():
            return {"ok": False, "reason": "hub_dir", "detail": f"ハブの作業フォルダがまだありません（{HUB_DIR}）。プラグインの start を実行してください"}
        stuck = hub_stuck()
        if stuck:
            return {"ok": False, **stuck}
        hub = next((a for a in list_agents() if a["hub"]), None)
        if hub:
            return {"ok": True, "pane": hub["pane"], "started": False}
        panes = herdr("pane", "list").get("result", {}).get("panes")
        if panes is None:
            return {"ok": False, "reason": "herdr が応答しません"}
        pane = next((p["pane_id"] for p in panes if p.get("cwd") == str(HUB_DIR) and not p.get("agent")), None)
        if not pane:
            wss = herdr("workspace", "list").get("result", {}).get("workspaces") or []
            if not wss:
                return {"ok": False, "reason": "herdr にワークスペースがありません"}
            ws = next((w for w in wss if w.get("label") == HUB_WORKSPACE), wss[0])["workspace_id"]
            r = herdr("tab", "create", "--workspace", ws, "--cwd", str(HUB_DIR), "--label", "hub", "--no-focus")
            pane = ((r.get("result") or {}).get("root_pane") or {}).get("pane_id")
            if not pane:
                return {"ok": False, "reason": f"タブを作れませんでした: {json.dumps(r, ensure_ascii=False)[:200]}"}
            time.sleep(2)  # シェルが立ち上がるのを待つ
        # 話し方の決まりはシステムプロンプトにも載せる（CLAUDE.md だけだと、列挙のときに箇条書きへ戻る）
        speaking = " ".join((HUB_DIR / "speaking.md").read_text().split())
        r = herdr("agent", "start", "hub", "--kind", "claude", "--pane", pane, "--timeout", "90000", "--",
                  "--model", HUB_MODEL, "--append-system-prompt", speaking, timeout=120)
        stuck = hub_stuck()
        if stuck:
            return {"ok": False, **stuck}
        if (r.get("error") or {}).get("code") == "agent_name_taken":
            return {"ok": False, "reason": "name_taken", "detail": "herdr に「hub」という名前の別のセッションがあります。そのタブを閉じてから、もう一度 start してください"}
        if "result" not in r:
            return {"ok": False, "reason": f"ハブを起動できませんでした: {json.dumps(r, ensure_ascii=False)[:200]}"}
        return {"ok": True, "pane": pane, "started": True}


async def boot_ensure_hub():
    """サーバー起動時にハブを確かめる。herdr が開かれるまで待ち、復元が落ち着いてから1回だけ行う。"""
    for _ in range(360):  # 最長1時間
        if "result" in await asyncio.to_thread(herdr, "agent", "list"):
            break
        await asyncio.sleep(10)
    else:
        print("[hub] herdr が応答しないため、起動時のハブ確認をあきらめました", flush=True)
        return
    await asyncio.sleep(HUB_SETTLE)
    hlog(f"起動時の確認: {json.dumps(await asyncio.to_thread(ensure_hub), ensure_ascii=False)}", flush=True)


def jsonl_path(agent: dict) -> Path | None:
    """Claude Code のセッション記録の場所。記録は最初の発言で作られるので、まだ無いこともある（呼び出し側で exists を見る）。"""
    if agent.get("kind") != "claude" or not agent.get("session"):
        return None
    enc = re.sub(r"[^A-Za-z0-9]", "-", agent["cwd"])
    p = PROJECTS / enc / f"{agent['session']}.jsonl"
    if p.exists():
        return p
    hits = list(PROJECTS.glob(f"*/{agent['session']}.jsonl"))
    return hits[0] if hits else p


# ユーザー発話として記録されるが、本人の発言ではないもの（ローカルコマンドの出力など）
NOISE_PREFIXES = ("<command-name>", "<command-message>", "<local-command", "<system-reminder>", "<task-notification>", "<bash-input>", "<bash-stdout>", "[Request interrupted")


def iter_events(lines):
    """セッション記録(JSONL)の行から ("user" | "assistant" | "tool", text) を順に返す。"""
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
            continue
        content = (d.get("message") or {}).get("content")
        if d.get("type") == "user":
            if isinstance(content, str):
                m = PASTED.match(content)
                parts = [m.group(1) if m else content]
            else:
                blocks = content or []
                if any(b.get("type") == "tool_result" for b in blocks):
                    yield ("tool", "")
                    continue
                parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
                if any(b.get("type") == "image" for b in blocks):
                    parts.append("[画像]")
            text = "\n".join(p.strip() for p in parts if p.strip() and not p.strip().startswith(NOISE_PREFIXES))
            if text:
                yield ("user", text)
        elif d.get("type") == "assistant":
            for b in content or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    yield ("assistant", b["text"].strip())
                elif b.get("type") == "tool_use":
                    yield ("tool", "")


def read_lines(path: Path, offset: int = 0, tail_bytes: int | None = None) -> list[str]:
    with path.open("rb") as f:
        size = path.stat().st_size
        if tail_bytes is not None and size > tail_bytes:
            f.seek(size - tail_bytes)
            f.readline()  # 途中から始まる行を捨てる
        else:
            f.seek(offset)
        return f.read().decode("utf-8", errors="replace").splitlines()


def history(path: Path, limit: int = 40, tail_bytes: int = 4_000_000) -> list[dict]:
    """画面に出す履歴。1ターン内の assistant の text はひとつの吹き出しにまとめる。"""
    msgs: list[dict] = []
    for kind, text in iter_events(read_lines(path, tail_bytes=tail_bytes)):
        if kind == "tool":
            continue
        if kind == "assistant" and msgs and msgs[-1]["role"] == "ai":
            msgs[-1]["text"] += "\n\n" + text
        elif kind == "user" and text.startswith(NOTE_PREFIX):
            msgs.append({"role": "note", "text": text[len(NOTE_PREFIX):].split("‖", 1)[0].strip()})
        else:
            msgs.append({"role": "me" if kind == "user" else "ai", "text": text})
    return msgs[-limit:]


def last_exchange(lines) -> tuple[str, str]:
    """最後のターンの (ユーザーの発言, 返答)。返答は最後のツール呼び出しより後の text（=結論部）。
    結論部が無ければ（作業の途中など）、そのターンの text 全部。"""
    user = ""
    turn: list[str] = []
    final: list[str] = []
    for kind, text in iter_events(lines):
        if kind == "user":
            user, turn, final = text, [], []
        elif kind == "tool":
            final = []
        else:
            turn.append(text)
            final.append(text)
    return user, "\n\n".join(final or turn)


def last_turn_id(path: Path) -> str | None:
    """最後のユーザー発言の ID。「どのターンか」を見分ける目印（文面が同じでも、別のターンなら別の値になる）。"""
    lines = read_lines(path, tail_bytes=600_000)
    for i in range(len(lines) - 1, -1, -1):
        if any(kind == "user" for kind, _ in iter_events([lines[i]])):
            return json.loads(lines[i]).get("uuid") or f"line-{i}"
    return None


def last_human_turn_id(path: Path | None) -> str | None:
    """ユーザー本人の最後の発言の ID（自動通知は数えない）。「ユーザーが新しく何か言ったか」を見分けるのに使う。"""
    if not (path and path.exists()):
        return None
    lines = read_lines(path, tail_bytes=600_000)
    for i in range(len(lines) - 1, -1, -1):
        if any(kind == "user" and not text.startswith(NOTE_PREFIX) for kind, text in iter_events([lines[i]])):
            return json.loads(lines[i]).get("uuid") or f"line-{i}"
    return None


def reply_since(path: Path, offset: int) -> str:
    """読み上げる本文。offset 以降の最後のターンの返答。"""
    return last_exchange(read_lines(path, offset=offset))[1]


# ---------- 音声 ----------
def _whisper(audio: bytes, filename: str, timeout: float = 120) -> dict:
    boundary = "----vl" + os.urandom(8).hex()
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(WHISPER, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode(), strict=False)


def transcribe(audio: bytes, filename: str) -> str:
    text = _whisper(audio, filename).get("text", "")
    text = re.sub(r"\s+", " ", text).strip()
    if text.replace("。", "").replace(" ", "") in HALLUCINATIONS:
        return ""
    return text


def synth(text: str, speaker: int) -> bytes:
    text = READING_RE.sub(lambda m: READINGS[m.group(1).lower()], text)
    q = urllib.request.urlopen(
        urllib.request.Request(f"{TTS_URL}/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": speaker}), method="POST"),
        timeout=60,
    ).read()
    return urllib.request.urlopen(
        urllib.request.Request(f"{TTS_URL}/synthesis?speaker={speaker}", data=q, headers={"Content-Type": "application/json"}, method="POST"),
        timeout=120,
    ).read()


def narrator_model() -> str:
    """いま使う語り手。narrator_model ファイルがあればその1行、無ければ既定。"""
    try:
        name = NARRATOR_FILE.read_text().strip()
    except OSError:
        name = ""
    return name or NARRATOR_MODEL


def _saved_gateway_token() -> tuple[str, float]:
    """保存済みの OIDC トークンと、切れるまでの秒数。無ければ ("", 0)。"""
    try:
        token = re.search(r'VERCEL_OIDC_TOKEN="?([^"\n]+)', (GATEWAY_DIR / ".env.local").read_text()).group(1)
        payload = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
        return token, exp - time.time()
    except Exception:
        return "", 0.0


_gateway_lock = asyncio.Lock()
_gateway_clients: dict[str, openai.AsyncOpenAI] = {}


async def gateway_token(min_left: float = 600) -> str:
    """AI Gateway に渡すトークン。残りが少なければ Vercel CLI で取り直す（数秒かかる）。"""
    token, left = _saved_gateway_token()
    if left > min_left:
        return token
    async with _gateway_lock:
        token, left = _saved_gateway_token()
        if left > min_left:
            return token
        r = await asyncio.to_thread(subprocess.run, GATEWAY_PULL, cwd=GATEWAY_DIR, capture_output=True, text=True, timeout=90)
        token, left = _saved_gateway_token()
        if left <= 0:
            raise RuntimeError(f"OIDC トークンを取り直せませんでした: {(r.stderr or r.stdout).strip()[-200:]}")
        print(f"[narrator] OIDC トークンを取り直しました（残り {left / 3600:.1f} 時間）", flush=True)
        return token


async def keep_gateway_token():
    """語り手が Gateway 経由のあいだ、トークンが切れる前に取り直しておく（返事の途中で数秒待たせないため）。"""
    while True:
        try:
            if "/" in narrator_model():
                await gateway_token(min_left=3600)
        except Exception as e:
            print(f"[narrator] {e}", flush=True)
        await asyncio.sleep(600)


async def _narrate_stream(model: str, text: str):
    """語り手の出力を、届いた順に少しずつ流す。"""
    user = f"<source>\n{text}\n</source>"  # 原稿であって話しかけではない、と区別できるように包む
    if "/" in model:
        token = await gateway_token()
        if token not in _gateway_clients:  # 接続を使い回す。トークンが替わったら作り直す
            _gateway_clients.clear()
            _gateway_clients[token] = openai.AsyncOpenAI(base_url=GATEWAY_URL, api_key=token)
        # OpenAI のモデルは既定だと考えてから訳すので、最初の文が 3〜4秒遅れる（2026-10-12 実測）。思考は切る
        extra = {"reasoning_effort": "none"} if model.startswith("openai/") else {}
        stream = await _gateway_clients[token].chat.completions.create(
            model=model, stream=True, **extra,
            messages=[{"role": "system", "content": NARRATOR_PROMPT}, {"role": "user", "content": user}])
        async with stream:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
    else:
        async with narrator_client.messages.stream(
            model=model, max_tokens=4000,
            system=[{"type": "text", "text": NARRATOR_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        ) as stream:
            async for delta in stream.text_stream:
                yield delta


async def narrate(text: str, cancel: asyncio.Event):
    """語り手で話し言葉に変換し、文ができるたびに yield する（ストリーム）。"""
    model = narrator_model()
    t0 = time.time(); first = None; buf = ""
    stream = _narrate_stream(model, text)
    try:
        while not cancel.is_set():
            try:
                delta = await anext(stream)
            except StopAsyncIteration:
                break
            except Exception as e:
                if first is not None or model == NARRATOR_MODEL:
                    raise
                # 切り替え先が使えない（トークン切れ・モデル名の誤りなど）。声を止めないよう、既定の語り手で訳し直す
                print(f"[narrator] {model} が使えないので {NARRATOR_MODEL} で訳します: {str(e)[:200]}", flush=True)
                await stream.aclose()
                model = NARRATOR_MODEL
                stream = _narrate_stream(model, text)
                continue
            if first is None:
                first = time.time() - t0
                print(f"[narrator] {model} 最初の出力まで {first:.2f}秒（原稿 {len(text)}字）", flush=True)
            buf += delta
            parts = SENTENCE_END.split(buf)
            for s in parts[:-1]:
                if s.strip():
                    yield s.strip()
            buf = parts[-1]
    finally:
        await stream.aclose()
    if buf.strip() and not cancel.is_set():
        yield buf.strip()


# ---------- HTTP / WS ----------
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    """画面の部品を手元に取っておく仕組み。画面全体を受け持たせるため、/static ではなくここから配る。"""
    return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/api/agents")
def api_agents():
    agents = sorted(list_agents(), key=lambda a: not a["hub"])  # ハブを先頭に
    return JSONResponse([{**a, "key": agent_key(a)} for a in agents])


@app.get("/api/history")
def api_history(pane: str):
    """選んだセッションの会話履歴。正本はそのセッション自身の記録(JSONL)。"""
    agent = next((a for a in list_agents() if a["pane"] == pane), None)
    path = jsonl_path(agent) if agent else None
    if not path:
        return JSONResponse({"source": "none", "messages": []})  # 記録を読めない種類のエージェント
    return JSONResponse({"source": "jsonl", "messages": history(path) if path.exists() else []})


@app.get("/api/speakers")
def api_speakers():
    data = json.loads(urllib.request.urlopen(f"{TTS_URL}/speakers", timeout=10).read())
    return JSONResponse([{"id": st["id"], "name": f"{s['name']} / {st['name']}", "default": st["id"] == DEFAULT_SPEAKER}
                         for s in data for st in s["styles"]])


# ---------- ハブが使う道具（hubctl から呼ばれる） ----------
def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"…（以下 {len(text) - n} 字省略）"


def _clip_ends(text: str, head: int, tail: int) -> str:
    """頭と末尾を残して中を省く。返答の末尾には「残っていること」「お願い」が書かれることが多い。"""
    if len(text) <= head + tail:
        return text
    return text[:head] + f" …（中略 {len(text) - head - tail} 字）… " + text[-tail:]


def _worker(pane: str) -> dict | None:
    return next((a for a in list_agents() if a["pane"] == pane and not a["hub"]), None)


@app.get("/api/hub/sessions")
def hub_sessions():
    """ハブ以外の全セッションの状態と、最後のやり取り。"""
    out = []
    for a in list_agents():
        if a["hub"]:
            continue
        path = jsonl_path(a)
        has = bool(path and path.exists())
        user, reply = last_exchange(read_lines(path, tail_bytes=600_000)) if has else ("", "")
        out.append({
            "pane": a["pane"], "project": a["cwd"].rsplit("/", 1)[-1], "title": a["title"], "status": a["status"], "kind": a["kind"],
            "idle_minutes": round((time.time() - path.stat().st_mtime) / 60) if has else None,
            "last_user": _clip(user, 200), "last_reply": _clip_ends(reply, 200, 500),
        })
    return JSONResponse(out)


@app.get("/api/hub/read")
def hub_read(pane: str, n: int = 6, full: bool = False):
    a = _worker(pane)
    if not a:
        return JSONResponse({"error": f"セッションが見つかりません: {pane}"}, status_code=404)
    path = jsonl_path(a)
    msgs = history(path, limit=n) if path and path.exists() else []
    cap = 8000 if full else 1500
    return JSONResponse({"pane": pane, "project": a["cwd"].rsplit("/", 1)[-1], "title": a["title"], "status": a["status"],
                         "messages": [{"role": m["role"], "text": _clip(m["text"], cap)} for m in msgs]})


@app.get("/api/hub/screen")
def hub_screen(pane: str, lines: int = 30):
    a = _worker(pane)
    if not a:
        return JSONResponse({"error": f"セッションが見つかりません: {pane}"}, status_code=404)
    if a["status"] != "blocked":
        return JSONResponse({"pane": pane, "status": a["status"], "screen": read_screen(pane, lines)})
    text = read_prompt(pane)
    SEEN_PROMPT[pane] = (prompt_fp(text), hub_human_turn())  # ハブがこの問いを見た。答えられるのは、ユーザーの次の発言から
    return JSONResponse({"pane": pane, "status": "blocked", "screen": text, "prompt": parse_prompt(text)})


_probes: dict[str, tuple[float, str]] = {}  # 点検で実際に試した結果: 名前 → (時刻, 不具合の説明。空なら使える)


def _probe(name: str, fn, keep_ok: float = 60, keep_ng: float = 5) -> str:
    """実際に試す点検。画面を開くたびに聞き取りや Anthropic を叩かないよう、結果を少しのあいだ覚えておく。"""
    hit = _probes.get(name)
    if hit and time.time() - hit[0] < (keep_ng if hit[1] else keep_ok):
        return hit[1]
    try:
        problem = fn() or ""
    except Exception as e:
        problem = f"{type(e).__name__}: {str(e)[:160]}"
    _probes[name] = (time.time(), problem)
    return problem


def _whisper_problem() -> str:
    """短い無音を実際に聞き取らせる（同じポートで別のサービスが動いているだけ、を「使える」と数えないため）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\0\0" * 4800)  # 0.3秒
    try:
        ok = "text" in _whisper(buf.getvalue(), "probe.wav", timeout=30)
    except (urllib.error.HTTPError, ValueError):  # 何かは応答したが、whisper の返事の形ではない
        ok = False
    except OSError as e:
        return f"whisper のサーバーが {WHISPER} で動いていません（{str(e)[:80]}）。起動してください。入れ方は README の「準備」にあります"
    return "" if ok else f"{WHISPER} は応答しますが、whisper の返事ではありません。別のサービスが同じポートを使っていないか確かめ、config.env の VL_WHISPER を合わせてください"


def _narrator_problem(model: str) -> str:
    """語り手に実際に届くかを、課金のない問い合わせ（モデルの情報の取得）で確かめる。認証とモデル名の両方が分かる。"""
    try:
        anthropic.Anthropic(timeout=10).models.retrieve(model)
    except anthropic.NotFoundError:
        return f"語り手のモデル名「{model}」が見つかりません。設定フォルダの narrator_model か、config.env の VL_NARRATOR_MODEL を見直してください"
    except anthropic.APIConnectionError as e:
        return f"Anthropic に届きません（{str(e)[:80]}）。ネットワークを確かめてください"
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, TypeError):  # TypeError: 認証の情報がどこにも無い
        return "Anthropic の認証が通りません。`ant auth login` でログインするか、config.env に ANTHROPIC_API_KEY を書いてください"
    except anthropic.AnthropicError as e:
        return f"Anthropic に確かめられませんでした（{type(e).__name__}）。ログインがまだなら `ant auth login`、または config.env に ANTHROPIC_API_KEY を書いてください"
    return ""


def _integration_problem() -> str:
    """herdr の Claude 連携。これが無いと herdr がセッション ID を持てず、返事を記録から読めない。"""
    try:
        out = subprocess.run(["herdr", "integration", "status"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "herdr に聞けませんでした"
    line = next((l for l in out.splitlines() if l.startswith("claude:")), "")
    return "" if line and "not installed" not in line else "`herdr integration install claude` を実行してください（Claude Code のセッションを見分けて、返事を読むのに使います）"


@app.get("/api/doctor")
def doctor():
    """使い始めるのに要るものが揃っているかの点検。ok でない項目に、次にやることを添える。"""
    checks = []

    def add(name: str, ok: bool, todo: str = "", optional: bool = False):
        checks.append({"name": name, "ok": ok, "todo": "" if ok else todo, "optional": optional})

    up = "result" in herdr("agent", "list", timeout=10)
    add("herdr", up, "herdr を起動してください")
    if up:
        problem = _probe("integration", _integration_problem)
        add("herdr の Claude 連携", not problem, problem)
    problem = _probe("whisper", _whisper_problem)
    add("聞き取り（whisper）", not problem, problem)
    speakers = []
    try:
        speakers = [st["id"] for s in json.loads(urllib.request.urlopen(f"{TTS_URL}/speakers", timeout=5).read()) for st in s["styles"]]
    except Exception:
        pass
    if VOICE not in VOICE_ENGINES:
        add("声のエンジンの設定", False, f"config.env の VL_VOICE={VOICE!r} は知らない名前です。{' か '.join(VOICE_ENGINES)} にしてください（いまは AivisSpeech で動いています）")
    add(f"声（{VOICE_NAME}）", bool(speakers), f"{VOICE_NAME} が {TTS_URL} で動いていません。起動してください（Docker なら OrbStack などが動いているかも確認）。入れ方は README の「準備」にあります")
    if speakers:
        add("既定の声", DEFAULT_SPEAKER in speakers, f"既定の声（ID {DEFAULT_SPEAKER}）が {VOICE_NAME} にありません。config.env の VL_DEFAULT_SPEAKER を、入っている声の ID にしてください")
    model = narrator_model()
    if "/" in model:
        linked, left = (GATEWAY_DIR / ".vercel").is_dir(), _saved_gateway_token()[1]
        add(f"語り手（{model}）", linked and left > 0,
            f"{GATEWAY_DIR} で `vercel link` を実行して、Vercel のプロジェクトに紐づけてください" if not linked
            else "Vercel のトークンが切れています。10分以内に自動で取り直します（そのあいだは既定の語り手で訳します）")
    else:
        problem = _probe(f"narrator:{model}", lambda: _narrator_problem(model), keep_ok=300)
        add(f"語り手（{model}）", not problem, problem)
    files = (HUB_DIR / "CLAUDE.md").is_file() and (HUB_DIR / "speaking.md").is_file()
    add("ハブの作業フォルダ", files, "プラグインの start を実行してください（指示文のひな形が置かれます）")
    if files:
        stuck = hub_stuck()
        if stuck:
            add("ハブ", False, stuck["detail"])
        else:
            add("ハブ", any(a["hub"] for a in list_agents()), "プラグインの start を実行するか、画面の「ハブを起動」を押してください")
    add("セッションを立てる場所", bool(PROJECTS_DIR and PROJECTS_DIR.is_dir()),
        "ハブに新しいセッションを立てさせるなら、config.env の VL_PROJECTS_DIR にプロジェクトの置き場所を書いてください", optional=True)
    return JSONResponse({"ok": all(c["ok"] or c["optional"] for c in checks), "checks": checks, "config_dir": str(CONFIG_DIR), "narrator": model})


@app.post("/api/hub/start")
def hub_start():
    """ハブが居なければ立てる（画面の「ハブを起動」と hub-start から呼ばれる）。"""
    r = ensure_hub()
    hlog(f"起動の依頼: {json.dumps(r, ensure_ascii=False)}", flush=True)
    return JSONResponse(r, status_code=200 if r["ok"] else 503)


# ---------- 画面との接続・セッションとの一往復・読み上げ（1対1でも、ハブからの報告でも使う） ----------
class Client:
    """接続中の画面ひとつ。"""

    def __init__(self, sock: WebSocket):
        self.sock = sock
        self.speaker: int | None = None  # この画面で選ばれている声

    async def send(self, **msg):
        await self.sock.send_text(json.dumps(msg, ensure_ascii=False))


CLIENTS: set[Client] = set()
PANE_LOCKS: dict[str, asyncio.Lock] = {}  # ひとつのセッションへの「送って返信を読む」は同時に1件だけ
REPORTING = False  # ハブが自動通知に応答している最中か
LAST_DONE: dict[str, tuple] = {}  # セッションごとの、最後に報告した (ターンの目印, 返答)。同じ返答を二度知らせないため
_tasks: set[asyncio.Task] = set()


def pane_lock(pane: str) -> asyncio.Lock:
    return PANE_LOCKS.setdefault(pane, asyncio.Lock())


async def ask(agent: dict, text: str) -> tuple[str, str]:
    """セッションに送り、返信が出そろうまで待つ。("done", 返信) / ("blocked", 画面の末尾) / ("error", 理由) を返す。"""
    pane = agent["pane"]
    path = jsonl_path(agent)
    offset = path.stat().st_size if path and path.exists() else 0  # 新しいセッションは最初の発言まで記録が無い
    # --wait: 送信後に working を観測してから idle/done/blocked を待つ（送信直後の idle に反応しない）
    r = await asyncio.to_thread(herdr, "agent", "prompt", pane, text, "--wait", "--timeout", "600000", timeout=610)
    err = (r.get("error") or {}).get("code")
    if err == "agent_prompt_stalled":  # 5秒以内に working にならなかった。短い応答で見逃した可能性があるので通常待ちへ
        r = await asyncio.to_thread(herdr, "agent", "wait", pane, "--timeout", "600000", timeout=610)
    elif err or "result" not in r:
        return "error", json.dumps(r, ensure_ascii=False)[:200]
    state = str((r.get("result") or {}).get("agent", {}).get("agent_status") or (r.get("result") or {}).get("agent_status") or "")
    if state == "blocked":
        return "blocked", read_prompt(pane)[-1500:]
    reply = ""
    for _ in range(10):  # JSONL への書き込みは状態変化より少し遅れることがある
        if not (path and path.exists()):
            # 立てたばかりのセッションは、最初の発言まで herdr がセッション ID を持たず記録も無い。ここで引き直す
            agent = next((a for a in list_agents() if a["pane"] == pane), agent)
            path = jsonl_path(agent)
        reply = reply_since(path, offset) if path and path.exists() else ""
        if reply:
            break
        await asyncio.sleep(0.5)
    return "done", reply or read_screen(pane, 40)[-2000:]


async def speak_to(clients: list[Client], text: str, cancel: asyncio.Event):
    """話し言葉に直し、文ごとに合成して画面へ送る。声は画面ごとの選択に従う。切れた画面は飛ばす。"""
    alive = list(clients)

    async def each(fn):
        for c in list(alive):
            try:
                await fn(c)
            except Exception:
                if len(clients) == 1:
                    raise  # 1対1では呼び出し側がエラーとして扱う
                alive.remove(c)

    await each(lambda c: c.send(type="speaking"))  # これ以降の音声がこのターンのもの（止めた後に届く古い音声と区別する）
    n = 0
    async for sentence in narrate(text, cancel):
        voice = {c: DEFAULT_SPEAKER if c.speaker is None else c.speaker for c in alive}  # VOICEVOX では 0 も声の ID
        wavs = {sp: await asyncio.to_thread(synth, sentence, sp) for sp in set(voice.values())}
        if cancel.is_set() or not alive:
            break
        await each(lambda c: c.sock.send_bytes(wavs[voice[c]]) if c in voice else asyncio.sleep(0))
        n += 1
    await each(lambda c: c.send(type="done", sentences=n))


# ---------- ハブ経由で頼んだ件の見張りと報告 ----------
TASK_DONE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
WATCHING: dict[str, int] = {}  # セッションごとの、動いている見張りの数
WATCH_GEN: dict[str, int] = {}  # セッションごとの、見張りの世代。指示を送るたびに進み、古い世代の見張りは引き上げる


def delegation_state(path: Path, offset: int, text: str) -> tuple[bool, str, bool, bool]:
    """offset 以降の記録から、送った指示の行方を読む。
    (指示が読まれたか, 返答, そのあとユーザーの次の発言があったか, 裏の作業が残っているか)。
    相手が自分から再開した分（バックグラウンド作業の完了など）も、次の発言が来るまでは同じ件の返答として数える。
    「裏の作業」は run_in_background で始めたコマンドのうち、完了の知らせ（task-notification）がまだ来ていないもの。"""
    head = " ".join(text.split())[:60]
    found = superseded = False
    turn: list[str] = []
    final: list[str] = []
    background: set[str] = set()
    for line in read_lines(path, offset=offset):
        if found:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                d = {}
            content = (d.get("message") or {}).get("content")
            if d.get("type") == "assistant" and isinstance(content, list):
                background |= {b.get("id") for b in content if b.get("type") == "tool_use" and (b.get("input") or {}).get("run_in_background") is True}
            elif d.get("type") == "user" and isinstance(content, str) and content.lstrip().startswith("<task-notification>"):
                background -= set(TASK_DONE.findall(content))
        for kind, t in iter_events([line]):
            if kind == "user":
                if found:
                    superseded = True
                    break
                found = " ".join(t.split()).startswith(head)
            elif found:
                if kind == "tool":
                    final = []
                else:
                    turn.append(t)
                    final.append(t)
        if superseded:
            break
    return found, "\n\n".join(final or turn), superseded, bool(background)


def start_watch(d: dict):
    task = asyncio.create_task(watch_delegation(d))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def watch_delegation(d: dict):
    """相手のセッションが片づくまで見張り、終わるか、承認・入力待ちで止まったら、ハブに報告させる。"""
    WATCHING[d["pane"]] = WATCHING.get(d["pane"], 0) + 1
    try:
        await _watch_delegation(d)
    except Exception:
        hlog(f"見張りが失敗: {d['project']}｜{d['title']}", flush=True)
        traceback.print_exc()
    finally:
        WATCHING[d["pane"]] -= 1


async def _watch_delegation(d: dict):
    label = f"{d['project']}｜{d['title']}"
    generic = d.get("generic", False)  # 送った指示を記録で追わない見張り（承認に答えたあとなど）
    deadline = time.time() + WATCH_TIMEOUT
    told_blocked, saw_working, quiet = False, d["was_working"], 0
    reported: str | None = None  # 最後に報告した返答。None はまだ一度も「完了」を報告していない
    follow_ups = 0
    bg_since: float | None = None
    told_bg = False
    while time.time() < deadline:
        await asyncio.sleep(3 if reported is None else 5)
        if WATCH_GEN.get(d["pane"]) != d["gen"]:
            return  # 同じセッションに新しい指示が送られた。見張りは最新の1つに任せる（古い件の名前で通知を重ねない）
        a = next((x for x in await asyncio.to_thread(list_agents) if x["pane"] == d["pane"] and x["cwd"] == d["cwd"]), None)
        if not a:
            return await report(d, "gone") if reported is None else None
        if a["status"] == "working":
            saw_working, told_blocked, quiet = True, False, 0
            continue
        path = jsonl_path(a)
        has = bool(path and path.exists())
        read, reply, superseded, pending = delegation_state(path, d["offset"], d["text"]) if has and not generic else (False, "", False, False)
        if reported is not None and superseded:
            return  # 報告のあと、ユーザーが相手に直接次の発言をした。そこから先はユーザー自身の作業なので知らせない
        if a["status"] == "blocked":
            if not told_blocked:
                told_blocked = True
                await report(d, "blocked")
            continue
        quiet += 1
        if reported is not None:
            # 一度報告したあと。相手が自分から再開して続きの返答を出したら、追って知らせる
            if follow_ups >= 5:
                return
            if reply.strip() and reply != reported and not pending:
                follow_ups += 1
                LAST_DONE[d["pane"]] = (last_turn_id(path), reply)
                await report(d, "more")
                reported = reply
            continue
        # 待機に戻っていて、(a) 指示への返答が記録にある、(b) 指示は読まれたが文章の返答が無いまま落ち着いた、
        # (c) 記録では追えないが一度は動いて落ち着いた（作業中に重ねて頼むと相手は1つの返答にまとめる。記録を読めない
        #     種類のセッションもここ）、のどれかなら一区切りとみなす
        if not ((read and reply.strip()) or (read and quiet >= 3) or (not read and saw_working and quiet >= 5)):
            continue
        if pending:
            # ターンは終えたが、裏の作業が残っている（「待機を始めました」など）。ここで「完了」と知らせず、本当の結果を待つ。
            # 長引いたら一度だけ「まだ動いている」と知らせる
            bg_since = bg_since or time.time()
            if not told_bg and time.time() - bg_since >= BG_NOTICE_AFTER:
                told_bg = True
                await report(d, "background")
            continue
        now = (last_turn_id(path), last_exchange(read_lines(path, tail_bytes=600_000))[1]) if has else None
        if now and LAST_DONE.get(d["pane"]) == now:  # 重ねて頼んだ件が、先に報告した返答の中で片づいていた
            hlog(f"報告を省略（先に報告した返答に含まれる）: {label}", flush=True)
            return
        if now:
            LAST_DONE[d["pane"]] = now
        await report(d, "done")
        if not read:
            return  # 記録で追えない件は、続きも見分けられないのでここまで
        reported = reply
    hlog(f"見張りを終了（{WATCH_TIMEOUT // 3600}時間）: {label}", flush=True)


async def report(d: dict, kind: str):
    """ハブに自動通知を送り、ハブの報告を、開いている画面へ声で届ける。画面が無ければ履歴に残るだけ。"""
    global REPORTING
    label = _clip(f"{d['project']}｜{d['title']}", 80)
    hub = next((a for a in await asyncio.to_thread(list_agents) if a["hub"]), None)
    if not hub:
        hlog(f"報告できません（ハブが居ない）: {label} {kind}", flush=True)
        return
    headline, todo = {"done": ("が完了しました", f"hubctl read {d['pane']} で結果を確かめ、"),
                      "more": ("に続きの返答がありました", f"先ほど報告した件の続きです。hubctl read {d['pane']} で確かめ、"),
                      "background": ("は裏で作業を続けています", f"しばらくたっても結果が出ていません。hubctl read {d['pane']} で様子を確かめ、まだ動いていることを一言で、"),
                      "blocked": ("が承認・入力待ちで止まっています", f"hubctl screen {d['pane']} で何を聞かれているか確かめ、"),
                      "gone": ("のセッションが見つからなくなりました", "閉じられた可能性があることを、")}[kind]
    # 一行・短文にする（長文や改行は「貼り付けられた内容」として包まれる）。結果の本文は通知に載せず、ハブが道具で読む。
    # 「‖」より前は画面にも出す見出し、後ろはハブへの依頼
    about = f"あなたが送った指示「{_clip(d['text'], 120)}」" if d["text"] else "ユーザーが答えた承認・質問"
    note = (f"{NOTE_PREFIX} {label} {headline} ‖ {todo}ユーザーに声で報告してください。"
            f"（自動通知です。ユーザーの発言ではありません。{about}についての知らせです。"
            "この通知をきっかけに、新しい指示を送ったり承認に答えたりはしません）")
    async with pane_lock(hub["pane"]):
        if kind == "blocked":  # 順番を待つ間にユーザーが答えていたら、もう止まっていない。古い知らせは送らない
            now = next((a for a in await asyncio.to_thread(list_agents) if a["pane"] == d["pane"]), None)
            if not now or now["status"] != "blocked":
                hlog(f"報告を省略（もう止まっていない）: {label}")
                return
        REPORTING = True
        try:
            state, reply = await ask(hub, note)
        finally:
            REPORTING = False
        hlog(f"報告 {kind}: {label} → {state}（画面 {len(CLIENTS)}）", flush=True)
        if state != "done" or not reply.strip():
            return
        clients = list(CLIENTS)
        for c in clients:
            try:
                await c.send(type="reply", pane=hub["pane"], text=reply, report=True, about=label)
            except Exception:
                clients.remove(c)
        if clients:
            await speak_to(clients, reply, asyncio.Event())


async def dispatch(a: dict, text: str) -> dict:
    """セッションに指示を送り、片づくまでの見張りを始める。"""
    pane, project = a["pane"], a["cwd"].rsplit("/", 1)[-1]
    path = jsonl_path(a)
    offset = path.stat().st_size if path and path.exists() else 0
    r = await asyncio.to_thread(herdr, "agent", "prompt", pane, text, timeout=30)
    err = (r.get("error") or {}).get("code") or (None if "result" in r else "herdr が応答しません")
    hlog(f"send → {pane} ({project}): {text[:120]!r} {'ERR ' + err if err else 'ok'}", flush=True)
    if err:
        return {"ok": False, "reason": err}
    WATCH_GEN[pane] = WATCH_GEN.get(pane, 0) + 1  # このセッションの、これより前の見張りは引き上げさせる
    start_watch({"pane": pane, "cwd": a["cwd"], "project": project, "title": a["title"], "text": text, "offset": offset,
                 "was_working": a["status"] == "working", "gen": WATCH_GEN[pane]})
    return {"ok": True, "project": project, "title": a["title"], "was_working": a["status"] == "working"}


@app.post("/api/hub/send")
async def hub_send(body: dict):
    """指示を送る。完了は待たず、片づいたら（または止まったら）ハブに通知して報告させる。"""
    if REPORTING:  # 通知への応答の中から、次の指示を出させない（ユーザーの知らないところで連鎖するのを防ぐ）
        return JSONResponse({"ok": False, "reason": "reporting"})
    pane, text = str(body.get("pane", "")), " ".join(str(body.get("text", "")).split())
    a = await asyncio.to_thread(_worker, pane)
    if not a:
        return JSONResponse({"ok": False, "reason": f"セッションが見つかりません: {pane}"}, status_code=404)
    if not text:
        return JSONResponse({"ok": False, "reason": "指示が空です"}, status_code=400)
    if len(text) > PROMPT_MAX:
        return JSONResponse({"ok": False, "reason": "too_long", "length": len(text), "max": PROMPT_MAX})
    if a["status"] == "blocked":  # 承認や入力を待っている。herdr も送信を拒否する
        return JSONResponse({"ok": False, "reason": "blocked", "screen": read_prompt(pane)[-1200:]})
    return JSONResponse(await dispatch(a, text))


# ---------- 承認・質問への回答（ハブは決めない。ユーザーの答えを相手に伝えるだけ） ----------
# 取り消しにくい操作に見える語。当たったら、ユーザーにもう一度確かめてからでないと「はい」を通さない
RISKY = re.compile(r"(\brm\b|\btrash\b|delete|\bdrop\b|truncate|\bpush\b|deploy|publish|release|\bsend|mail|merge|reset --hard|force|terraform|\baws\b|gcloud|kubectl|migrat|prod|本番|削除|送信|公開|配信|反映|支払|振込|請求)", re.I)
SEEN_PROMPT: dict[str, tuple[str, str | None]] = {}  # ハブが見た、止まっている画面: pane → (画面の指紋, そのときのユーザーの最後の発言)
CONFIRMING: dict[str, tuple[str, str | None]] = {}  # 取り消しにくい操作の聞き返し中: pane → (画面の指紋, そのときのユーザーの最後の発言)


def asked_part(text: str) -> str:
    """画面のうち、いま聞かれている部分（最後のツール呼び出しの行から下）。それより上の会話は判定に使わない。"""
    lines = text.splitlines()
    start = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].lstrip().startswith("⏺")), max(0, len(lines) - 15))
    return "\n".join(lines[start:])


def prompt_fp(text: str) -> str:
    return hashlib.sha1(" ".join(text.split()).encode()).hexdigest()


def hub_human_turn() -> str | None:
    hub = next((a for a in list_agents() if a["hub"]), None)
    return last_human_turn_id(jsonl_path(hub)) if hub else None


@app.post("/api/hub/answer")
async def hub_answer(body: dict):
    """止まっているセッションの問いに、ユーザーの答えを伝える。
    守ること: ハブが見てユーザーに伝えた問いにだけ答える（見たのと同じ画面で、かつ見たあとにユーザーが発言している）。
    取り消しにくい操作の「はい」は、聞き返してユーザーがもう一度発言してから。「今後は聞かずに許可」は選ばない。"""
    if REPORTING:
        return JSONResponse({"ok": False, "reason": "reporting"})
    pane, answer, confirmed = str(body.get("pane", "")), str(body.get("answer", "")).strip().lower(), bool(body.get("confirmed"))
    a = await asyncio.to_thread(_worker, pane)
    if not a:
        return JSONResponse({"ok": False, "reason": f"セッションが見つかりません: {pane}"}, status_code=404)
    if a["status"] != "blocked":
        return JSONResponse({"ok": False, "reason": "not_blocked", "status": a["status"]})
    trust = await asyncio.to_thread(pane_shows_trust, pane)
    if trust is None:  # 何を聞かれているか読めないまま、答えを送らない
        return JSONResponse({"ok": False, "reason": "unreadable"})
    if trust:  # フォルダを信頼するかは、利用者が端末で答える
        return JSONResponse({"ok": False, "reason": "trust"})
    text = await asyncio.to_thread(read_prompt, pane)
    fp, prompt, turn = prompt_fp(text), parse_prompt(text), await asyncio.to_thread(hub_human_turn)
    if answer in ("no", "n"):
        key, said = "esc", "いいえ（取り消し）"
    else:
        seen = SEEN_PROMPT.get(pane)
        if not seen or seen[0] != fp:  # 見ていない、または見たあとで問いが変わった
            SEEN_PROMPT[pane] = (fp, turn)
            return JSONResponse({"ok": False, "reason": "unseen", "screen": text})
        if seen[1] == turn:  # 見たのと同じ発言の中で答えようとしている = ユーザーはまだ中身を聞いていない
            return JSONResponse({"ok": False, "reason": "same_turn", "screen": text})
        if answer in ("yes", "y"):
            if prompt["kind"] != "permission":
                return JSONResponse({"ok": False, "reason": "not_permission", "options": prompt["options"]})
            if RISKY.search(asked_part(text)):
                held = CONFIRMING.get(pane)
                if not (confirmed and held and held[0] == fp and held[1] != turn):
                    CONFIRMING[pane] = (fp, turn) if not (held and held[0] == fp) else held
                    return JSONResponse({"ok": False, "reason": "confirm", "screen": text})
            key, said = "1", "はい（今回だけ許可）"
        elif answer.isdigit():
            n = int(answer)
            if prompt["kind"] != "question" or n not in prompt["options"]:
                return JSONResponse({"ok": False, "reason": "bad_choice", "kind": prompt["kind"], "options": prompt["options"]})
            key, said = str(n), f"{n}番「{prompt['options'][n]}」"
        else:
            return JSONResponse({"ok": False, "reason": "bad_answer"})
    r = await asyncio.to_thread(herdr, "agent", "send-keys", pane, key)
    project = a["cwd"].rsplit("/", 1)[-1]
    hlog(f"answer → {pane} ({project}): {said} {'ok' if 'result' in r else 'ERR'}", flush=True)
    if "result" not in r:
        return JSONResponse({"ok": False, "reason": "herdr が応答しません"})
    SEEN_PROMPT.pop(pane, None)
    CONFIRMING.pop(pane, None)
    if not WATCHING.get(pane):  # ハブ経由で頼んだ件でなければ見張りが無いので、答えたあとの結果を知らせるために付ける
        path = jsonl_path(a)
        start_watch({"pane": pane, "cwd": a["cwd"], "project": project, "title": a["title"], "text": "", "generic": True,
                     "offset": path.stat().st_size if path and path.exists() else 0, "was_working": True,
                     "gen": WATCH_GEN.setdefault(pane, 0)})
    return JSONResponse({"ok": True, "project": project, "title": a["title"], "said": said})


# ---------- 画面のボタンからの返答（利用者がタップで答える） ----------
def _asked(pane: str, key: str) -> tuple[dict | None, dict]:
    """画面が指している相手の、いま止まっている問い。(相手, 問い)。問いの kind は
    permission（許可・断る）/ question（選択式。断るだけ）/ trust（端末で答える）/ unknown / none（止まっていない）/ unreadable / gone。"""
    a = next((a for a in list_agents() if a["pane"] == pane), None)
    if not a or (key and agent_key(a) != key):  # 開きっぱなしの画面の一覧が古く、同じ場所に別のセッションが居る
        return None, {"kind": "gone"}
    if a["status"] != "blocked":
        return a, {"kind": "none"}
    trust = pane_shows_trust(pane)
    if trust is None or trust:
        return a, {"kind": "unreadable" if trust is None else "trust"}
    text = read_prompt(pane)
    asked = asked_part(text)
    return a, {"kind": parse_prompt(text)["kind"], "text": _clip(asked, 1200), "fp": prompt_fp(text), "risky": bool(RISKY.search(asked))}


@app.get("/api/prompt")
def api_prompt(pane: str, key: str = ""):
    return JSONResponse(_asked(pane, key)[1])


@app.post("/api/answer")
async def api_answer(body: dict):
    """画面のボタンで、止まっているセッションの問いに答える。
    守ること: 画面に出したのと同じ問いにだけ答える（指紋で確かめる）。許可は「今回だけ」（1）で、「今後は聞かずに許可」は選ばない。
    取り消しにくい操作に見えるときは、もう一度押してから（confirmed）。フォルダの信頼の確認には答えない。"""
    pane, key, fp = str(body.get("pane", "")), str(body.get("key", "")), str(body.get("fp", ""))
    choice, confirmed = str(body.get("choice", "")), bool(body.get("confirmed"))
    a, asked = await asyncio.to_thread(_asked, pane, key)
    if asked["kind"] in ("gone", "none", "unreadable", "trust"):
        return JSONResponse({"ok": False, "reason": asked["kind"]})
    if asked["fp"] != fp:  # 画面に出したあとで問いが変わった
        return JSONResponse({"ok": False, "reason": "changed", "prompt": asked})
    if choice == "allow" and asked["kind"] == "permission":
        if asked["risky"] and not confirmed:
            return JSONResponse({"ok": False, "reason": "confirm"})
        keys, said = "1", "許可（今回だけ）"
    elif choice == "deny" and asked["kind"] in ("permission", "question"):
        keys, said = "esc", "断る"
    else:
        return JSONResponse({"ok": False, "reason": "bad_choice", "kind": asked["kind"]})
    r = await asyncio.to_thread(herdr, "agent", "send-keys", pane, keys)
    hlog(f"answer(画面) → {pane} ({a['cwd'].rsplit('/', 1)[-1]}): {said} {'ok' if 'result' in r else 'ERR'}", flush=True)
    return JSONResponse({"ok": True, "said": said} if "result" in r else {"ok": False, "reason": "herdr"})


# ---------- セッションの起動 ----------
def spawn_session(directory: Path, project: str, model: str) -> dict:
    """プロジェクトのディレクトリで Claude Code を立てる。同名のワークスペースがあればその中のタブに、無ければ作る。"""
    wss = herdr("workspace", "list").get("result", {}).get("workspaces")
    if wss is None:
        return {"ok": False, "reason": "herdr が応答しません"}
    ws = next((w for w in wss if w.get("label") == project), None)
    if ws:
        r = herdr("tab", "create", "--workspace", ws["workspace_id"], "--cwd", str(directory), "--label", "ハブが起動", "--no-focus")
    else:
        r = herdr("workspace", "create", "--cwd", str(directory), "--label", project, "--no-focus")
    found = re.search(r'"pane_id":\s*"([^"]+)"', json.dumps(r))
    if not found:
        return {"ok": False, "reason": f"タブを作れませんでした: {json.dumps(r, ensure_ascii=False)[:200]}"}
    pane = found.group(1)
    time.sleep(2)  # シェルが立ち上がるのを待つ
    name = f"{SPAWN_PREFIX}{project}-{int(time.time()) % 100000}"
    r = herdr("agent", "start", name, "--kind", "claude", "--pane", pane, "--timeout", "90000", "--", "--model", model, timeout=120)
    if pane_shows_trust(pane):
        return {"ok": False, "reason": "trust", "pane": pane}
    if "result" not in r:
        return {"ok": False, "reason": f"セッションを起動できませんでした: {json.dumps(r, ensure_ascii=False)[:200]}", "pane": pane}
    return {"ok": True, "pane": pane, "name": name}


@app.post("/api/hub/spawn")
async def hub_spawn(body: dict):
    """PROJECTS_DIR 直下の既存プロジェクトに新しいセッションを立て、最初の指示があれば送る。"""
    if REPORTING:
        return JSONResponse({"ok": False, "reason": "reporting"})
    if not PROJECTS_DIR or not PROJECTS_DIR.is_dir():
        return JSONResponse({"ok": False, "reason": "no_projects_dir"})
    project = str(body.get("project", "")).strip().strip("/")
    model = str(body.get("model") or "opus").strip().lower()
    text = " ".join(str(body.get("text", "")).split())
    directory = PROJECTS_DIR / project
    if not project or "/" in project or project.startswith(".") or not directory.is_dir():
        names = sorted(d.name for d in PROJECTS_DIR.iterdir() if d.is_dir() and not d.name.startswith("."))
        return JSONResponse({"ok": False, "reason": "no_project", "projects": names})
    if model not in SPAWN_MODELS:
        return JSONResponse({"ok": False, "reason": "bad_model", "models": sorted(SPAWN_MODELS)})
    if len(text) > PROMPT_MAX:
        return JSONResponse({"ok": False, "reason": "too_long", "length": len(text), "max": PROMPT_MAX})
    live = [a for a in await asyncio.to_thread(list_agents) if a["name"].startswith(SPAWN_PREFIX)]
    if len(live) >= SPAWN_MAX:
        return JSONResponse({"ok": False, "reason": "limit", "max": SPAWN_MAX, "live": [f"{a['cwd'].rsplit('/', 1)[-1]}｜{a['title']}" for a in live]})
    r = await asyncio.to_thread(spawn_session, directory, project, model)
    hlog(f"spawn → {project} ({model}): {json.dumps(r, ensure_ascii=False)[:160]}", flush=True)
    if not r["ok"]:
        return JSONResponse(r)
    out = {"ok": True, "pane": r["pane"], "project": project, "model": model, "sent": False}
    if text:
        a = next((x for x in await asyncio.to_thread(list_agents) if x["pane"] == r["pane"]), None)
        sent = await dispatch(a, text) if a else {"ok": False, "reason": "起動したセッションが見つかりません"}
        out["sent"], out["send_error"] = sent["ok"], None if sent["ok"] else sent.get("reason")
    return JSONResponse(out)


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    client = Client(sock)
    CLIENTS.add(client)
    cancel = asyncio.Event()  # 進行中のターンを止める合図。ターンごとに作り直す
    current: asyncio.Task | None = None
    send = client.send

    async def turn(text: str, pane: str, cancel: asyncio.Event, key: str | None):
        try:
            await run_turn(text, pane, cancel, key)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 語り手・音声合成・herdr の失敗を黙って止めず、画面とログに出す
            traceback.print_exc()
            await send(type="error", text=f"{type(e).__name__}: {e}"[:300])

    async def run_turn(text: str, pane: str, cancel: asyncio.Event, key: str | None):
        agent = next((a for a in list_agents() if a["pane"] == pane), None)
        if not agent:
            await send(type="error", gone=True, text="話し相手のセッションが見つかりません。選び直してください。")
            return
        if key and agent_key(agent) != key:  # 開きっぱなしの画面の一覧が古く、同じ場所に別のセッションが居る
            await send(type="error", gone=True, text="話し相手が入れ替わっています。送らずに止めました。選び直してください。")
            return
        await send(type="status", text="エージェントに送りました。考え中…")
        async with pane_lock(pane):  # ハブが自動通知に応答している最中なら、それが終わってから送る
            state, reply = await ask(agent, text)
            if cancel.is_set():
                return
            if state == "error":
                await send(type="error", text=f"送信できませんでした: {reply}")
            elif state == "blocked":
                await send(type="status", text="承認待ちです")
                await send(type="blocked", pane=pane)  # 画面に、許可・断るのボタンを出させる
                await speak_to([client], "【承認待ちの画面】\n" + reply, cancel)
            elif not reply.strip():
                await send(type="error", text="回答を読めませんでした")
            else:
                await send(type="reply", pane=pane, text=reply)
                await speak_to([client], reply, cancel)

    def start_turn(text: str, pane: str, speaker: int, key: str | None):
        nonlocal cancel, current
        client.speaker = speaker
        cancel.set()  # 前のターンの読み上げが残っていれば止める
        if current and not current.done():
            current.cancel()
        cancel = asyncio.Event()
        current = asyncio.create_task(turn(text, pane, cancel, key))

    pending: dict = {}
    try:
        while True:
            msg = await sock.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("text") is not None:
                d = json.loads(msg["text"])
                if d["type"] == "hello":  # 画面が選んでいる声（ハブからの報告を、その声で届けるため）
                    client.speaker = None if d.get("speaker") in (None, "") else int(d["speaker"])
                    continue
                if d["type"] == "stop":
                    cancel.set()
                    if current:
                        current.cancel()
                    await send(type="stopped")
                    continue
                if d["type"] == "clientlog":  # 端末側（特にスマホ）の再生まわりの出来事を残す
                    print(f"[client] {str(d.get('msg'))[:300]}", flush=True)
                    continue
                pending = d  # {"type":"turn", pane, speaker, filename} → 次のバイナリが音声
                if d["type"] == "text":
                    start_turn(d["text"], d["pane"], int(d["speaker"]), d.get("key"))
                continue
            if msg.get("bytes") is not None and pending.get("type") == "turn":
                await send(type="status", text="聞き取り中…")
                text = await asyncio.to_thread(transcribe, msg["bytes"], pending.get("filename", "audio.webm"))
                if not text:
                    await send(type="error", text="聞き取れませんでした")
                    continue
                await send(type="transcript", pane=pending["pane"], text=text)
                start_turn(text, pending["pane"], int(pending["speaker"]), pending.get("key"))
    except WebSocketDisconnect:
        pass
    finally:
        CLIENTS.discard(client)
        cancel.set()
        if current:
            current.cancel()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("VL_HOST", "127.0.0.1"), port=int(os.environ.get("VL_PORT", "8790")))
