# /// script
# dependencies = ["pillow>=10"]
# ///
"""voice-loop のアイコンを描いて、各サイズで static/icons/ に書き出す。
図柄: 黒地に、緑の輪（ループ）と白い声の波形。自分の画像に替えるなら、正方形の PNG を引数に渡す。
使い方: uv run spike/make_icon.py [元にする画像.png]"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"
S = 2048  # 大きく描いてから縮める（縁をなめらかにする）


def draw() -> Image.Image:
    im = Image.new("RGB", (S, S), "#141414")
    d = ImageDraw.Draw(im)
    c, r, w = S // 2, int(S * 0.335), int(S * 0.066)
    # 輪: 右上に切れ目のある弧。端は丸める
    d.arc([c - r, c - r, c + r, c + r], start=-25, end=275, fill="#3ea66b", width=w)
    import math
    for deg in (-25, 275):
        x, y = c + (r - w / 2) * math.cos(math.radians(deg)), c + (r - w / 2) * math.sin(math.radians(deg))
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill="#3ea66b")
    # 波形: 5本の丸い棒
    bw, gap = int(S * 0.058), int(S * 0.036)
    heights = [0.13, 0.25, 0.37, 0.25, 0.13]
    x0 = c - (5 * bw + 4 * gap) // 2
    for i, h in enumerate(heights):
        hh = int(S * h)
        x = x0 + i * (bw + gap)
        d.rounded_rectangle([x, c - hh // 2, x + bw, c + hh // 2], radius=bw // 2, fill="#f2f2f2")
    return im


src = Image.open(sys.argv[1]).convert("RGB") if len(sys.argv) > 1 else draw()
side = min(src.size)
src = src.crop(((src.width - side) // 2, (src.height - side) // 2, (src.width + side) // 2, (src.height + side) // 2))
for name, size in [("icon-512.png", 512), ("icon-192.png", 192), ("apple-touch-icon.png", 180), ("favicon-32.png", 32)]:
    src.resize((size, size), Image.LANCZOS).save(OUT / name)
    print("wrote", OUT / name)
