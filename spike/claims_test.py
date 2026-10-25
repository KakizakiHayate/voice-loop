# /// script
# requires-python = ">=3.11"
# ///
"""README の「守り」の表（約束と、確かめる試験）の点検。どの約束にも試験が付いていて、その試験のファイルが実在すること。
試験の無い約束を書かないための決まりを、機械でも確かめる。
使い方: uv run spike/claims_test.py"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
readme = (ROOT / "README.md").read_text()
m = re.search(r"^## 守り（誰から何を守るか）\n(.*?)(?=^## )", readme, re.S | re.M)
bad = []
if not m:
    bad.append("README に「## 守り（誰から何を守るか）」の節がありません")
rows = [line for line in (m.group(1).splitlines() if m else []) if line.startswith("|") and not line.startswith(("| 約束", "|---"))]
if not rows:
    bad.append("「守り」の節に、約束と試験の表がありません")
for row in rows:
    cells = [c.strip() for c in row.strip("|").split("|")]
    claim, tests = cells[0], re.findall(r"`(spike/[\w./-]+\.py)`", cells[-1])
    if not tests:
        bad.append(f"試験の付いていない約束: {claim}")
    bad += [f"表にある試験のファイルがありません: {t}（{claim[:30]}…）" for t in tests if not (ROOT / t).is_file()]
print("\n".join(bad) or f"守りの約束 {len(rows)} 件、どれにも試験が付いていて、試験のファイルも実在します")
sys.exit(1 if bad else 0)
