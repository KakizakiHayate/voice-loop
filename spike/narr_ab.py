# /// script
# dependencies = ["anthropic", "openai"]
# ///
"""語り手のモデル比較。server.py の narrate() と同じ呼び方で、同じ原稿を各モデルに渡し、
最初の文が出るまでの時間・全体の時間・トークン数・決まりの守り具合を記録する。
モデル名に "/" があれば Vercel AI Gateway 経由（設定フォルダの gateway/.env.local の VERCEL_OIDC_TOKEN。12時間で切れるので
そのフォルダで `npx vercel env pull .env.local --yes` を打って取り直す）。
使い方: uv run spike/narr_ab.py <結果を書く JSON> [繰り返し回数] [語り手の指示ファイル] [モデル,モデル]
       NARR_EFFORT=low を付けると、Gateway 経由のモデルに思考の深さを指定する"""
import json, os, pathlib, re, sys, time
import anthropic, openai

HERE = pathlib.Path(__file__).resolve().parent.parent
PROMPT = pathlib.Path(sys.argv[3]).read_text() if len(sys.argv) > 3 else (HERE / "narrator_prompt.md").read_text()
SENTENCE_END = re.compile(r"(?<=[。！？!?\n])")
MODELS = {  # 100万トークンあたりのドル: 入力, キャッシュ書き込み, キャッシュ読み出し, 出力
    "claude-haiku-4-5": (1.0, 1.25, 0.10, 5.0), "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "anthropic/claude-haiku-5.5": (0.10, 0.125, 0.01, 0.50),
    "openai/gpt-6-luna": (0.10, 0.10, 0.01, 0.50), "openai/gpt-6-luna-fast": (0.20, 0.20, 0.02, 1.00),
}
REPEAT = int(sys.argv[2]) if len(sys.argv) > 2 else 2
MODELS = {m: MODELS[m] for m in (sys.argv[4].split(",") if len(sys.argv) > 4 else ["claude-haiku-4-5", "claude-haiku-5-5"])}
EFFORT = os.environ.get("NARR_EFFORT")

SOURCES = {
    "短い報告（引用つき）": "テスト用セッションから返事が来ました。「今日は2026年10月12日の月曜日です。」とのことです。正しく答えられています。",
    "送った指示の報告": "ボイスループのテスト用セッションに、「今日の曜日を一文で答えてください。」と送りました。相手は待機中だったので、すぐ読まれるはずです。終わったら知らせます。",
    "全体の状況": "八つ開いていて、止まっているものはありません。あなたの対応待ちは三つで、blog の PR 135 のレビューとマージ、同じく更新履歴の PR 134 のレビュー、それとネットショップの定例準備で「決済は外部のサービスを使う」と言い切ってよいかの判断です。予約サイトの「同じお知らせが二回届いた方」の件も、相手にはまだ伝わっていません。ほかは急ぎはありません。",
    "短い質問": "どのセッションに送りますか？",
    "表と記号のある長い返答": """C で配れます。herdr のプラグインは「設定ファイル1枚と、好きな言語のコマンド」という軽い仕組みです。

## 仕様の要点

フォルダに `herdr-plugin.toml` を置き、herdr に起動させるコマンドを書くだけです。専用の SDK はなく、`herdr` コマンドがそのまま API です。

| 宣言できるもの | いつ動くか | voice-loop での使い道 |
|---|---|---|
| build | `herdr plugin install` のときに1回 | 依存の取得 |
| startup | herdr がセッションを復元し終えた直後に1回 | サーバーとハブが居るかの確認 |
| actions | 利用者が呼んだとき | 起動・停止・状態・URL 表示 |

- **入れ方**：`herdr plugin install 持ち主/リポジトリ` です。GitHub から取得し、確認を取ってから登録します。
- **常駐はプラグインの役目ではありません**：本体は launchd で常駐させます。
- **更新の命令はありません**：入れ直すか、自前の更新命令を持ちます。

進めるなら、まず公開せずに、この Mac でプラグインとして動く形にするのがよいと考えます。進めますか。""",
}
QUOTES = {"短い報告（引用つき）": "今日は2026年10月12日の月曜日です。", "送った指示の報告": "今日の曜日を一文で答えてください。"}

direct = anthropic.Anthropic()
gateway = None
if any("/" in m for m in MODELS):
    token = re.search(r'VERCEL_OIDC_TOKEN="?([^"\n]+)', (pathlib.Path.home() / ".config/herdr/plugins/config/u-space.voice-loop/gateway/.env.local").read_text()).group(1)
    gateway = openai.OpenAI(base_url="https://ai-gateway.vercel.sh/v1", api_key=token)


def run(model: str, src: str):
    """1回訳させて (最初の文までの秒, 全体の秒, 訳文, トークン数) を返す。"""
    t0 = time.time(); first = None; buf = ""
    user = f"<source>\n{src}\n</source>"

    def feed(delta: str):
        nonlocal first, buf
        buf += delta
        if first is None and len(SENTENCE_END.split(buf)) > 1:
            first = time.time() - t0

    if "/" in model:
        u = {"in": 0, "write": 0, "read": 0, "out": 0, "reasoning": 0}
        stream = gateway.chat.completions.create(
            model=model, stream=True, stream_options={"include_usage": True},
            messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": user}],
            **({"reasoning_effort": EFFORT} if EFFORT else {}))
        for ch in stream:
            if ch.choices and ch.choices[0].delta.content:
                feed(ch.choices[0].delta.content)
            if ch.usage:
                d = ch.usage
                read = (d.prompt_tokens_details.cached_tokens if d.prompt_tokens_details else 0) or 0
                u = {"in": d.prompt_tokens - read, "write": 0, "read": read, "out": d.completion_tokens,
                     "reasoning": (d.completion_tokens_details.reasoning_tokens if d.completion_tokens_details else 0) or 0}
    else:
        with direct.messages.stream(
            model=model, max_tokens=4000,
            system=[{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        ) as stream:
            for delta in stream.text_stream:
                feed(delta)
            m = stream.get_final_message().usage
        u = {"in": m.input_tokens, "write": m.cache_creation_input_tokens or 0, "read": m.cache_read_input_tokens or 0, "out": m.output_tokens, "reasoning": 0}
    total = time.time() - t0
    return (first if first is not None else total), total, buf, u


rows = []
for name, src in SOURCES.items():
    for model, (pin, pwrite, pread, pout) in MODELS.items():
        for i in range(REPEAT):
            first, total, buf, u = run(model, src)
            sents = [s for s in SENTENCE_END.split(buf) if s.strip()]
            rows.append({
                "source": name, "model": model, "run": i + 1, "src_chars": len(src), "out_chars": len(buf),
                "first_sentence_s": round(first, 2), "total_s": round(total, 2), "tokens": u,
                "usd": round((u["in"] * pin + u["write"] * pwrite + u["read"] * pread + u["out"] * pout) / 1e6, 6),
                "symbols": sorted(set(re.findall(r"[*#|`→]|^\s*-\s", buf, re.M))),
                "ends_with_maru": all(s.strip().endswith(("。", "？")) for s in sents),
                "quote_kept": (QUOTES[name] in buf) if name in QUOTES else None,
                "text": buf,
            })
            r = rows[-1]
            print(f"{name} | {model} #{i+1}: 最初の文 {r['first_sentence_s']}秒 / 全体 {r['total_s']}秒 / {u} / ${r['usd']}", flush=True)
json.dump(rows, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
