# -*- coding: utf-8 -*-
"""把四个状态的截图拼成一张对比图(交付/评审用)。

用法:
    python tools/compose_states.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

STATES = [
    ("gui_idle.png", "未启动"),
    ("gui_run.png", "运行中 · 主电脑"),
    ("gui_wait.png", "等待主电脑"),
    ("gui_agent.png", "未启动 · 副电脑"),
]

GAP = 16
TOP = 34
BG = "#F4F6F8"
LABEL_FG = "#5B636E"


def _font(size: int):
    for name in ("msyh.ttc", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> int:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_ui_shots")
    imgs = []
    for fn, label in STATES:
        path = os.path.join(base, fn)
        if not os.path.exists(path):
            print("missing", path)
            continue
        imgs.append((Image.open(path).convert("RGB"), label))
    if not imgs:
        return 1

    w = sum(im.width for im, _ in imgs) + GAP * (len(imgs) + 1)
    h = max(im.height for im, _ in imgs) + TOP + GAP
    canvas = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(canvas)
    f = _font(13)

    x = GAP
    for im, label in imgs:
        d.text((x + 2, 10), label, font=f, fill=LABEL_FG)
        canvas.paste(im, (x, TOP))
        x += im.width + GAP

    out = os.path.join(base, "gui_v5_all.png")
    canvas.save(out)
    print(out, canvas.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
