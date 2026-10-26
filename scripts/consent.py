"""利用者の発言が、許す（承認の「はい」・閉じる）か選ぶ（選択肢の番号）の答えになっているか。ハブの「承認に答える」「閉じる」の守りで使う（server.py）。

ハブは利用者の発言を作れないが、発言のどこかに「いい」があれば通る、のような緩い判定だと、関係のない発言や断る発言を答えに使える。
そこで、発言全体がこの文法に収まるときだけ答えとして読む（README「守り」）。保証は文法との照合まで（何への返事かは見ない）。
  - 断る（先に見る）: 打ち消し・保留の語（構わない・問題ない などを除いてから）、禁止形（閉じるな）、問いの形で終わる発言
  - 許す: すべての節が「前置き＊ ＋（許す句＋結び＊）＋」
  - 選ぶ: すべての節が「選ぶ節」か「許す節」で、選びうる選択肢がちょうど一つ
"""
import re
import unicodedata

KEEP_NAI = ("構わない", "かまわない", "問題ない", "問題なし", "差し支えない", "差支えない")  # 「ない」を含むが、許す語
NEG_JA = re.compile(r"いいえ|ううん|いや|だめ|ダメ|駄目|やめ|止め|待って|まって|ない|なく|ません|拒否|却下|キャンセル|取り消|取消|残して|まだ|後で|あとで"
                    r"|今度に|今度で|また今度|違う|ちがう|別の")
NEG_EN = re.compile(r"(?<![a-z])(?:no|not|don't|dont|stop|wait|later|cancel)(?![a-z])")  # 語の境目で見る（notes の no は数えない）
PROHIBIT = re.compile(r"[るうくぐすつぬぶむ]な[よーっ]*$")  # 禁止形（閉じるな・許可するなよ・頼むな）。節ごとに見る
ASKING = re.compile(r"(?:[?？]|か|かな|かね|っけ)[。．.!！\s]*$")
QUESTION = re.compile(r"[?？]|か(?:[。」』）)\s*]|$)", re.M)  # 聞いた文が問いの形か

PRE = ["じゃあ", "じゃ", "では", "それでは", "それじゃあ", "それじゃ", "あー", "あ", "えっと", "ええと", "うーん", "んー", "まあ", "そうですね", "そうだね", "そうね",
       "今度は", "今度こそ", "今は", "それは", "これは"]  # 「もう」は入れない（「もういいよ」は「やめて」の意味にもなる）
YES = ["はい", "うん", "ええ", "いい", "オーケー", "オッケー", "okay", "ok", "yes", "sure", "どうぞ", "お願い", "おねがい", "大丈夫", "構わない", "かまわない",
       "問題ない", "問題なし", "了解", "りょうかい", "承知", "よろしく", "頼みます", "頼む", "そうして", "それで"]
YES_FOR = {"close": ["閉じちゃってもいい", "閉じちゃっていい", "閉じてもいい", "閉じていい", "閉じちゃって", "閉じて", "とじて", "閉じる"],
           "answer": ["許可して", "許可する", "許可", "通して", "進めて", "実行して"]}
TAIL = ["してください", "ください", "下さい", "します", "です", "ます", "して", "だよ", "よ", "ね", "わ", "ぞ", "ー", "っ", "〜", "~"]  # 「な」は入れない（禁止形）
PICK = ["を選んで", "を選ぶ", "にします", "にする", "にして", "でお願い", "でおねがい", "がいい", "でいい", "で", "を"]
KANJI = {c: i for i, c in enumerate("〇一二三四五六七八九")}


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", _norm(text))


def _alt(words) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


def clauses(text: str) -> list[str]:
    """節（「、。,.!！?？」と改行で分け、節の中の空白は除く）。"""
    return [re.sub(r"\s+", "", c) for c in re.split(r"[、。,.!！?？\n]+", _norm(text)) if c.strip()]


def refuses(text: str) -> bool:
    """断る・保留する・問い返す発言か（許す・選ぶより先に見る）。"""
    flat = _flat(text)
    for k in KEEP_NAI:
        flat = flat.replace(k, "")
    if NEG_JA.search(flat) or NEG_EN.search(_norm(text)) or ASKING.search(_norm(text).strip()):
        return True
    return any(PROHIBIT.search(c) for c in clauses(text))


def _pre(project: str) -> str:
    p = _flat(project)
    return f"(?:{_alt(PRE)}" + (f"|{re.escape(p)}(?:のセッション)?(?:は|も|を)?" if p else "") + ")*"


def _yes_clause(action: str | None, project: str):
    yes = _alt(YES + (YES_FOR[action] if action else []))
    return re.compile(f"{_pre(project)}(?:(?:{yes})(?:{_alt(TAIL)})*)+")


def affirms(text: str, action: str, project: str = "") -> bool:
    """発言全体が「許す」か（action は close か answer）。"""
    if refuses(text):
        return False
    cs, clause = clauses(text), _yes_clause(action, project)
    return bool(cs) and all(clause.fullmatch(c) for c in cs)


def chooses(text: str, n: int, options: dict, project: str = "") -> bool:
    """発言全体が、選択肢 n を選ぶ答えか。選びうる選択肢（選ぶ節の番号と、文言が合う選択肢）がちょうど {n} のときだけ。"""
    if refuses(text):
        return False
    pre, tail, pick = _pre(project), f"(?:{_alt(TAIL)})*", f"(?:{_alt(PICK)})?"
    by_number = re.compile(f"{pre}(?P<n>[0-9]+|[〇一二三四五六七八九])番?目?{pick}{tail}")
    by_label = {k: re.compile(f"{pre}{re.escape(lab)}{pick}{tail}") for k, v in options.items() if len(lab := _flat(str(v))) >= 2}
    plain = _yes_clause(None, project)
    picked: set[int] = set()
    cs = clauses(text)
    for c in cs:
        hit = {k for k, rx in by_label.items() if rx.fullmatch(c)}
        m = by_number.fullmatch(c)
        if m:
            v = m["n"]
            hit.add(int(v) if v.isdigit() else KANJI[v])
        if hit:
            picked |= hit
        elif not plain.fullmatch(c):
            return False
    return bool(cs) and picked == {n}


def asks(heard: str, project: str) -> bool:
    """利用者が聞いたハブの返答が、プロジェクト名（フォルダ名）を挙げた問いか。"""
    return bool(project) and _flat(project) in _flat(heard) and bool(QUESTION.search(heard or ""))
