# voice-loop

口と耳だけで、herdr の上の Claude Code と話すためのプラグインです。スマホやブラウザでタップして話し、もう一度タップして送ると、返事が声で返ってきます（トランシーバー式）。

話し相手は2種類あります。

- **個別のセッション**：herdr で動いている Claude Code のセッションをひとつ選んで話します。
- **ハブ**：全セッションを見渡す役のセッションです。「全体どうなってる？」に答え、「〇〇に△△を頼んで」で指示を送り、頼んだ件が片づくと声で知らせます。

## しくみ

```
ブラウザ（タップして話す）
  → 聞き取り（whisper）        声を文字にする
  → herdr → Claude Code         セッションに送り、返事を記録から読む
  → 語り手（軽いモデル）        返事を話し言葉に直す
  → 声（AivisSpeech か VOICEVOX）読み上げる
  → ブラウザで再生
```

本体（`server.py`）は launchd の常駐サービスとして動きます。プラグインは、その上げ下ろしと初回の準備を受け持つだけです。herdr を閉じても動き続け、Mac を再起動したあとは launchd が上げ直す作りです。

## 準備

macOS 専用です。次のものが要ります。足りないものは、あとで `doctor` が教えます。

| 要るもの | 何に使うか |
|---|---|
| herdr 0.9.3 以上 | セッションの一覧・送信・画面の読み取り |
| Claude Code と、herdr の Claude 連携 | 話し相手。連携が無いと返事を記録から読めません |
| uv | 本体とハブの道具の実行 |
| whisper のサーバー | 聞き取り |
| AivisSpeech Engine（または VOICEVOX Engine） | 声 |
| Anthropic の API の認証 | 語り手（返事を話し言葉に直すモデル） |
| Tailscale（任意） | スマホから使うとき |

### herdr と Claude Code

voice-loop は、herdr と Claude Code が動いている Mac に入れます。セッションの記録と herdr を、同じ Mac の上で読むためです。Claude Code はログインを済ませておき、herdr の Claude 連携を入れます。

```sh
herdr integration install claude
```

### uv

```sh
brew install uv
```

### 聞き取り（whisper）

`127.0.0.1:8080` で、OpenAI 互換の `/v1/audio/transcriptions` に応えるサーバーを立てます。whisper.cpp の例です（モデルは約1.6GB）。

```sh
brew install whisper.cpp ffmpeg
mkdir -p ~/.local/share/whisper-models ~/.local/state/whisper-work
curl -L -o ~/.local/share/whisper-models/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
cd ~/.local/state/whisper-work && whisper-server \
  -m ~/.local/share/whisper-models/ggml-large-v3-turbo.bin \
  --host 127.0.0.1 --port 8080 --inference-path /v1/audio/transcriptions -l ja --convert
```

最後の `whisper-server` は、止めるまで動き続けます。別のタブで実行して、開いたままにしてください。

`--convert` は、ブラウザの録音を ffmpeg で変換するために要ります。変換は作業フォルダに一時ファイルを書くので、書き込めるフォルダで起動してください。launchd に登録して常駐させるときは、`WorkingDirectory` を忘れると変換に失敗します。

### 声（AivisSpeech）

`127.0.0.1:10101` で AivisSpeech Engine を動かします。Docker の例です（イメージは約3.7GB）。Docker を動かすもの（OrbStack や Docker Desktop）が要ります。OrbStack なら `brew install --cask orbstack` で入れ、一度アプリを開いて初期設定を済ませます。

```sh
docker run -d --name aivis --restart unless-stopped \
  -p 127.0.0.1:10101:10101 ghcr.io/aivis-project/aivisspeech-engine:cpu-latest
```

### 軽い声にするなら（VOICEVOX）

AivisSpeech は CPU だけで計算し（Mac の GPU には対応していません）、待機中もメモリを約1.9GB使います。メモリの少ない Mac では、代わりに VOICEVOX Engine を使えます。声の顔ぶれは変わります。

作者の Mac mini（M4 Pro）で、約6秒の文を作るのにかかった時間です。

| | 1文の合成 | 待機中のメモリ | イメージ |
|---|---|---|---|
| AivisSpeech | 2.0〜3.1 秒 | 1.86GB | 3.7GB |
| VOICEVOX | 0.8〜1.0 秒 | 78MB | 2.3GB |

```sh
docker run -d --name voicevox --restart unless-stopped \
  -p 127.0.0.1:50021:50021 voicevox/voicevox_engine:cpu-latest
```

設定フォルダの `config.env` に `VL_VOICE=voicevox` と書き、`restart` します。既定の声は春日部つむぎで、画面でほかの声を選べます。`config.env` で `VL_DEFAULT_SPEAKER` を書いているなら、その行を消すか、VOICEVOX の声の ID に直してください。AivisSpeech の声の ID のままだと、点検が「既定の声」で知らせます。

VOICEVOX の[利用規約](https://voicevox.hiroshiba.jp/term/)では、使うときに VOICEVOX を使ったことがわかるクレジットの表記（「VOICEVOX:春日部つむぎ」など）が要ります。声ごとの規約にも従います。

### 語り手の認証

語り手は Anthropic の API を呼びます。Claude のサブスクリプションとは別に、API の利用料がかかります（既定の Claude Haiku 4.5 で、200字ほどの返事1回あたり約0.3円。作者の環境での実測）。認証は次のどちらかです。

- Anthropic の CLI でログインする：`brew install anthropics/tap/ant` のあと `ant auth login`
- API キーを使う：設定フォルダの `config.env` に `ANTHROPIC_API_KEY=...` を書く

## 入れ方

```sh
herdr plugin install u-space-inc/voice-loop
herdr plugin action invoke u-space.voice-loop.start
```

手元に置いたフォルダから使うなら、`herdr plugin link <フォルダ>` で登録します。

`start` は、設定フォルダにひな形を置き、常駐サービスを登録して上げ、ハブを立て、最後に点検の結果を出します。手でやることが残っていれば、番号つきで並びます。

`herdr plugin action invoke` は、受け付けたことを JSON で返すだけです。結果は次の命令で読みます。1行目が `start running` なら、まだ準備の途中です。初回は必要なものの取得で数分かかることがあるので、少し待って打ち直してください。

```sh
herdr plugin log list --plugin u-space.voice-loop | python3 -c '
import json, sys
logs = json.load(sys.stdin)["result"]["logs"]
if not logs: sys.exit("記録なし")
last = logs[-1]
print(last["action_id"], last["status"])
print(last.get("stdout") or "", last.get("stderr") or "")'
```

「記録なし」は、プラグインが入っていないときにも出ます。入っているかどうかは `herdr plugin list` で確かめます。

## 初回に、手で答えること

ハブを初めて立てると、herdr の `hub` タブで Claude Code が「このフォルダを信頼しますか」と聞いて止まります。**↓ で「Yes, I trust this folder」を選び、Enter を押してください。** 初めの選択は「No, exit」なので、Enter だけでは進みません。

この確認が出るのは、ハブの作業フォルダが次の設定を持っているためです。

- `./hubctl`（ハブの道具）は、確認なしで実行できる
- ファイルを書く道具（Edit / Write）は使えない
- Bash で実行できるのは `./hubctl` だけ（それ以外は見張りが止める）

答えたあと、もう一度 `start` を実行するか、画面の「ハブを起動」を押します。ログインなど、ほかの画面で止まっているときも同じで、端末で答えてから `start` です。voice-loop がこれらの画面に代わりに答えることはありません。

ハブに新しいセッションを立てさせたとき、そのプロジェクトで Claude Code を初めて起動するなら、同じ確認がそのタブに出ます。これも手で答えます。

## 使い方

`start` が出す URL を開きます。スマホから使うなら、Tailscale で https にして開きます（スマホのマイクは https でないと使えません）。

```sh
tailscale serve --bg --https=8790 http://127.0.0.1:8790
```

話し相手を選び、「タップして話す」を押して話し、もう一度押して送ります。初めて開いたときの話し相手はハブです。準備が残っていると、画面にも同じ案内が出ます。

どれかのセッションが承認で止まると、話し相手の画面のまま、画面の上にそのセッションの名前と、何を聞かれているかと、「許可（今回だけ）」「断る」のボタンが出ます。いくつ止まっていても、すべて並びます。許可は今回だけで、「今後は聞かずに許可」は選びません。削除や送信など取り消しにくい操作に見えるときは、もう一度押すと許可します。押すと、送る直前に同じ問いのままかを確かめ、変わっていれば送らずに出し直します。

ボタンが出るのは、承認の画面と見分けられたときだけです。選択式の質問、フォルダを信頼するかの確認、読み取れない画面は、名前と中身だけを出します。答えは端末か、ハブに話して伝えます。

つながらないときも、一度開いたことのある端末なら画面は開き、「接続できません。接続を確かめてください」と出ます。そのあいだは送れません（打ちかけの文は残ります）。つながると、読み込み直さずにその場で戻ります。

更新したあとは、状態の欄の右端の「↻」で最新の画面に読み込み直せます（ホーム画面に追加したアプリには、ブラウザの読み込み直しがないため）。手元に取っておいた画面ではなく本体から取り直し、取れないときは今の画面のまま「接続できません。接続を確かめてください」と出ます。

## 接続できる範囲

本体は `127.0.0.1` でだけ待ち受けます。使えるのは、この Mac の上のプログラムと、Tailscale Serve を通した tailnet の端末です。

- この範囲の中からなら、パスワードなしでセッションへ指示を送れます。tailnet をほかの人と共有しているときは、Tailscale のアクセス制御で、この Mac の 8790 番を自分の端末だけに絞ってください。
- `tailscale funnel` でインターネットに公開しないでください。
- ブラウザで開いたほかのサイトからの接続は、本体が断ります。接続元（Origin）と宛先の名前（Host）を確かめています。断ったときは、ログに `[guard]` で始まる行が残ります。
- この Mac の名前と Tailscale の名前（〜.ts.net）以外で画面を開くときは、`config.env` の `VL_ALLOWED_HOSTS` にその名前を書きます。
- Docker のコンテナからは `host.docker.internal` という名前で届きます。そこからは、Hermes がセッションを読むための 2 本（`/api/hub/sessions` と `/api/hub/read`）だけを通し、指示を送る・承認に答える・セッションを立てる・画面を開くは断ります。

## 命令

どれも `herdr plugin action invoke u-space.voice-loop.<名前>` で呼びます。出力は `herdr plugin log list --plugin u-space.voice-loop` で読めます。

| 名前 | すること |
|---|---|
| `start` | 初回の準備と起動。何度実行してもかまいません |
| `stop` | 止める（Mac を再起動すると、また上がります） |
| `restart` | 再起動。設定を変えたあとに |
| `status` | サービス・ハブ・語り手の状態と URL |
| `url` | 開く URL |
| `doctor` | 点検。足りないものと、次にやること |
| `uninstall` | 常駐の登録を外す（設定は残ります） |

## 設定

端末ごとのものは、本体の外の設定フォルダ（`~/.config/herdr/plugins/config/u-space.voice-loop/`）にあります。プラグインを入れ直しても消えません。

| ファイル | 中身 |
|---|---|
| `config.env` | 設定。行頭の `#` を外すと有効になります。変えたら `restart` |
| `readings.txt` | 読み上げで読み間違える語の読み替え（`語=よみ`）。変えたら `restart` |
| `hub/CLAUDE.md` | ハブへの指示文。呼ばれたい名前やプロジェクトの呼び方を足せます。変えたらハブのタブを閉じて `start` |
| `hub/speaking.md` | ハブの話し方の決まり（長さ、箇条書きにしない、など） |
| `narrator_model` | 語り手のモデル名を1行。書くと再起動なしで切り替わり、消すと既定に戻ります |

ハブに新しいセッションを立てさせるには、`config.env` の `VL_PROJECTS_DIR` にプロジェクトの置き場所を書きます。立てられるのは、その直下にある既存のフォルダだけです。

ログは `~/.local/state/herdr/plugins/u-space.voice-loop/server.log` です。

## ハブがすること・しないこと

ハブはほかのセッションの記録を読むので、そこに紛れた指示に乗せられないよう、できることを絞ってあります。

- 指示は、確認を挟まずに送ります。そのかわり、何をどこへ送ったかを必ず声で伝えます。
- 承認や質問で止まっているセッションには、あなたの答えを伝えるだけです。中身を声で伝えたあと、あなたの次の発言で答えを聞きます。本番への反映、送信、削除など取り消しにくい操作は、もう一度聞き返します。
- 「今後は聞かずに許可」にあたる選択肢は選びません。フォルダの信頼の確認にも答えません。
- セッションを閉じることはしません。立てられるのは同時に3つまでです。
- 自分ではファイルを書かず、`./hubctl` 以外のコマンドも実行しません。

## Hermes を話し相手にする（試験中）

[Hermes Agent](https://hermes-agent.nousresearch.com/)（Nous Research の自律エージェント）を、話し相手の 1 人として足せます。Hermes の仕事は「全体どうなってる？」に答えることだけです。セッションを読む道具（`hubread`）しか持たず、指示を送る・承認に答える・セッションを立てることはできません（本体の見張りが、コンテナからの接続を読む 2 本に絞っています）。

ハブと違い、Hermes への指示文（`hermes/AGENTS.md`）には仕事と道具しか書いてありません。話し方の決まりは、声で直したことを Hermes が自分のスキルとして書き残すかを見るために、わざと書いていません。ハブの `CLAUDE.md` にある決まりが答え合わせの正本です。

```
ブラウザ → 本体 → Hermes の API サーバー（Docker の中、同じ会話を続ける）
                       └→ hubread → 本体の /api/hub/sessions・/api/hub/read（host.docker.internal 経由、読むだけ）
```

### 用意

Hermes は公式の Docker イメージで動かします（Docker を動かすものが要ります）。設定は `~/.hermes` に残ります。

```sh
mkdir -p ~/.hermes
docker pull nousresearch/hermes-agent:latest
# 作業フォルダ（指示文の置き場）、スキルの書き込みは承認してから、API サーバーの鍵
docker run --rm -v ~/.hermes:/opt/data nousresearch/hermes-agent config set terminal.cwd /opt/voice-loop
docker run --rm -v ~/.hermes:/opt/data nousresearch/hermes-agent config set skills.write_approval true
docker run --rm -v ~/.hermes:/opt/data nousresearch/hermes-agent config set API_SERVER_ENABLED true
docker run --rm -v ~/.hermes:/opt/data nousresearch/hermes-agent config set API_SERVER_HOST 0.0.0.0
docker run --rm -v ~/.hermes:/opt/data nousresearch/hermes-agent config set API_SERVER_KEY "$(openssl rand -hex 32)"
```

モデルと認証は、対話の設定で選びます（Anthropic の API キー、Nous Portal など。Claude Pro の OAuth は使えません）。

```sh
docker run -it --rm -v ~/.hermes:/opt/data nousresearch/hermes-agent model
```

`config.env` に、同じ鍵を書きます（`~/.hermes/.env` の `API_SERVER_KEY` と同じ値）。書いたら `restart` します。`start` か `restart` で、設定フォルダに `hermes/AGENTS.md` と `hermes/hubread` が置かれます。

```
VL_HERMES_KEY=（同じ鍵）
```

コンテナを常駐させます。設定フォルダの `hermes/` を `/opt/voice-loop` として読み取り専用で見せ、API サーバーはこの Mac の上からだけ使えるようにします。

```sh
docker run -d --name hermes --restart unless-stopped \
  -v ~/.hermes:/opt/data \
  -v ~/.config/herdr/plugins/config/u-space.voice-loop/hermes:/opt/voice-loop:ro \
  -p 127.0.0.1:8642:8642 \
  nousresearch/hermes-agent gateway run
```

話し相手の一覧に「Hermes｜読むだけの話し相手（試験中）」が出ます。`doctor` に Hermes の項目が足されます。

### 使い方と、スキルの確かめ方

- 声で「全体どうなってる？」と聞き、気に入らなければ声で直します（「予定は要らない」「件数から言って」など）。会話は `VL_HERMES_SESSION`（既定 `voice-loop`）の名前で続くので、直したことは次の返事に効きます
- Hermes がスキルを書こうとすると、承認待ちになります（`skills.write_approval`）。確かめて採用します。`~/.hermes/skills` を git に入れておくと、何がいつ変わったかが残ります

```sh
docker exec -it hermes hermes   # 対話の画面で
/skills pending                  # 保留中のスキルの書き込み
/skills diff <id>                # 差分
/skills approve <id>             # 採用（reject で捨てる）
```

- 一問一答（`hermes chat -q`）では Hermes はスキルを書きません（公式ドキュメント: one-shot では `skill_manage` を外す）。本体が API サーバー経由で同じ会話を続けているのはそのためです
- Hermes は `hubread` が返す内容を読みます。そこに紛れた指示には従わないよう指示文に書いてありますが、確実ではありません。Hermes に渡しているのは読む道具だけなので、乗せられても起きるのは「変な報告」までです

### Hermes との会話をターミナルで見る（2026-10-21 に確かめた範囲）

voice-loop が API で続けている会話は、Hermes 側ではふつうのセッション（ID は `VL_HERMES_SESSION` の値）として残る。目で見たいときは、任意のターミナルで次を打つと、その時点までの履歴が開く。

```
docker exec -it hermes hermes --resume voice-loop
```

- 生中継ではない。繋いだあとに voice-loop から送った発言は画面に流れてこない。見直すときは `/quit` で閉じて繋ぎ直す
- `hermes --tui --resume` は、他の端末が開いている会話には繋げない（0.22.2 の Docker 版で「この build では使えない」と出る）
- ターミナルを閉じるときは必ず `/quit`。ウィンドウを消すだけだと、コンテナの中に CLI が残り、そのセッションが「別の端末で開いている」扱いのまま繋ぎ直せなくなる（voice-loop からの発言は影響なく通る）。残ったときは `docker exec hermes sh -c 'pkill -f "resume voice-loop"'`
- Web ダッシュボード（`HERMES_DASHBOARD=1` と `-p 127.0.0.1:9119:9119` を付けてコンテナを作り直す）は未検証

## 更新と、外し方

herdr に更新の命令は無いので、入れ直します。設定フォルダはそのまま残ります。本体の場所や版が変わっていれば、次に herdr を起動したとき（または `start` のとき）に、常駐サービスが新しい本体に切り替わります。

外すときは、`uninstall` で常駐の登録を外してから、herdr でプラグインを外します。設定フォルダは残るので、要らなければ手で消します。

## 確かめてある範囲

作者の Mac mini 1台（macOS、herdr 0.9.3、Claude Code 2.1）で動かしています。次のことは、まだ確かめていません。

- ほかの Mac への導入
- GitHub からの `herdr plugin install`（手元のフォルダを `herdr plugin link` で登録する形だけ確認済み）
- Mac を再起動したあとの復帰
- VOICEVOX の声での会話の通し（合成・声の一覧・点検・画面の既定の声までは確認済み）
- iPhone のホーム画面のアプリで、Mac に届かない状態から開いて戻るまで（パソコンのブラウザでは確認済み）

## ライセンス

MIT です（`LICENSE`）。`static/vendor/` に同梱している次のライブラリは、それぞれのライセンスに従います。本文は同じフォルダにあります。

| ライブラリ | ライセンス | 本文 |
|---|---|---|
| marked v12.0.2 | MIT | `static/vendor/marked.LICENSE.md` |
| DOMPurify 3.1.6 | Apache-2.0 または MPL-2.0 | `static/vendor/purify.LICENSE` |
