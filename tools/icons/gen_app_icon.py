# -*- coding: utf-8 -*-
"""应用图标(exe / 标题栏 / 任务栏 / 开始菜单) —— 蓝底 + 白色「H」。

## 母题(2026-10-21 用户定稿)

用户提供并确认的图形: 两根竖直圆角柱 + 中间一条短横杠 = 「H」。
两根柱子 = 两台电脑, 中间横杠 = 连接链路, 与产品本体(双机键鼠越界 +
音频同步)一一对应。

几何定义在 `com_bridge/brand.py` —— **唯一来源**。托盘图标、窗口图标、
exe 图标、主界面品牌标记全部调它, 不再各写一套(此前三处各画一套,
改一次要改四处, 且实测会逐渐漂移)。

## 两套规格(Windows 的标准做法)

    大图标(16~256px): 蓝渐变圆角底板 + 白色 H   <- 本模块
    托盘(16~32px):    无底板, 单色剪影 + 状态点   <- tray.py 调 brand.tray_rects

## 为什么细部要按整数像素

见 brand.py 的模块说明(教训1 占比不均 / 教训2 用户看到"两道线")。
这里的实现是**两级合成**:

  1. 底板(圆角 + 渐变)走超采样 -> 大面积需要平滑边缘
  2. H 的三个矩形在**目标尺寸**上按整数像素直接画 -> 绝不被重采样摊薄

细线要硬边对齐、大块要抗锯齿, 这是像素图标的通行做法。
"""
from __future__ import annotations

import base64
import io
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..")))

from com_bridge import brand  # noqa: E402

TOP = brand.TOP
BOT = brand.BOT
FG = brand.FG

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256, 512, 1024)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
EMBED_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _lerp(a, b, t: float):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def render(size: int) -> Image.Image:
    """渲染应用图标: 蓝渐变圆角底板 + 白色 H。"""
    ss = 8 if size <= 64 else 4
    W = size * ss
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    # --- 底板: 超采样(圆角边缘需要平滑) ---
    grad = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(W):
        gd.line([(0, y), (W, y)],
                fill=_lerp(TOP, BOT, y / max(1, W - 1)) + (255,))
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, W - 1, W - 1], radius=int(round(0.22 * W)), fill=255)
    img.paste(grad, (0, 0), mask)
    plate = img.resize((size, size), Image.LANCZOS)

    # --- H: 在目标尺寸上按整数像素画(硬边, 不重采样) ---
    d = ImageDraw.Draw(plate)
    radius = brand.bar_radius(size)
    for (x0, y0, x1, y1) in brand.bars_for(size):
        if radius > 0:
            d.rounded_rectangle([x0, y0, x1, y1], radius=radius,
                                fill=FG + (255,))
        else:
            d.rectangle([x0, y0, x1, y1], fill=FG + (255,))
    return plate


# --- 量化自检 -----------------------------------------------------------

def _fg_rows(img: Image.Image, thr: int = 200) -> list[int]:
    """返回被前景(H)覆盖超过 15% 宽度的行。"""
    px = img.load()
    s = img.width
    out = []
    for y in range(s):
        n = sum(1 for x in range(s)
                if px[x, y][3] >= 128 and px[x, y][0] > thr
                and px[x, y][1] > thr and px[x, y][2] > thr)
        if n >= max(2, s * 0.15):
            out.append(y)
    return out


def _fg_cols(img: Image.Image, thr: int = 200) -> list[int]:
    px = img.load()
    s = img.width
    out = []
    for x in range(s):
        n = sum(1 for y in range(s)
                if px[x, y][3] >= 128 and px[x, y][0] > thr
                and px[x, y][1] > thr and px[x, y][2] > thr)
        if n >= max(2, s * 0.15):
            out.append(x)
    return out


def _groups(vals: list[int]) -> list[int]:
    """把连续编号分成组, 返回每组的长度。"""
    out, cur = [], 0
    prev = None
    for v in sorted(vals):
        if prev is not None and v != prev + 1:
            out.append(cur)
            cur = 0
        cur += 1
        prev = v
    if cur:
        out.append(cur)
    return out


def ink(img: Image.Image) -> float:
    a = img.getchannel("A").tobytes()
    return sum(1 for v in a if v >= 128) / (img.width * img.height)


def white_ratio(img: Image.Image) -> float:
    px = img.load()
    n_white = n_solid = 0
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if a < 128:
                continue
            n_solid += 1
            if r > 200 and g > 200 and b > 200:
                n_white += 1
    return n_white / max(1, n_solid)


def contrast(a, b) -> float:
    def lum(c):
        def ch(v):
            v = v / 255.0
            return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ascii_preview(img: Image.Image, label: str = "") -> None:
    """把图标打成 ASCII(# = 前景 H, + = 底板, . = 透明)。"""
    px = img.load()
    print(f"  {label}")
    for y in range(img.height):
        row = ""
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if a < 100:
                row += "."
            elif r > 200 and g > 200 and b > 200:
                row += "#"
            else:
                row += "+"
        print("    " + row)


def _row_segments(img: Image.Image, y: int, thr: int = 200) -> list[int]:
    """返回某一行上前景色像素的**分段宽度**列表。

    ★ 这是 "H" 的正确判据: 一行上有几段前景, 直接对应图形结构:
      普通行(柱身) -> 2 段(左柱 / 右柱)
      横杠行       -> 3 段(左柱 / 横杠 / 右柱)
    """
    px = img.load()
    s = img.width
    segs, cur = [], 0
    for x in range(s):
        r, g, b, a = px[x, y]
        fg = (a >= 128 and r > thr and g > thr and b > thr)
        if fg:
            cur += 1
        elif cur:
            segs.append(cur)
            cur = 0
    if cur:
        segs.append(cur)
    return segs


def check_geometry(size: int) -> tuple[bool, str]:
    """验证某个尺寸下 H 的形状正确。返回 (是否通过, 说明)。

    判据(就是 "H" 的定义本身):
      * 柱身行: 恰好 **2 段**(左柱 / 右柱), 且两段宽度相等
      * 横杠行: 恰好 **3 段**(左柱 / 横杠 / 右柱)
    只要出现 "柱身行有 3 段" 或 "横杠行有 1 段", 就说明细部糊在一起了。
    """
    im = render(size)
    g = brand.layout_for(size)
    arm_y = g["arm_y"] + g["arm_h"] // 2      # 横杠正中那一行
    # ★ 柱身取样必须在**中段**: 大尺寸有圆头, 最顶上那行会被圆角收窄,
    #   量到的是收窄后的宽度(实测 32px 在 top 行只有 2px, 而真实柱宽是 6px)。
    body_y = g["top"] + g["bar_h"] // 3
    body = _row_segments(im, body_y)
    arm = _row_segments(im, arm_y)
    if len(body) != 2:
        return False, f"柱身行应为 2 段, 实际 {body}"
    if abs(body[0] - body[1]) > 0:
        return False, f"两柱宽度不等 {body}"
    if len(arm) != 3:
        return False, f"横杠行应为 3 段, 实际 {arm}"
    return True, f"柱身{body} 横杠{arm}"


def _check() -> int:
    print("=" * 70)
    print("应用图标 — 蓝底 + 白色「H」(两柱一杠)")
    print("=" * 70)
    for s in (16, 20, 24, 32):
        ascii_preview(render(s), f"{s}px")
        print()

    print("=== 形状校验(列必须分成 3 段才是 H) ===")
    ok = True
    for s in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        good, detail = check_geometry(s)
        ok = ok and good
        print(f"{s:>6}px  {'OK  ' if good else 'FAIL'}  {detail}")

    print("\n=== 量化 ===")
    print(f"{'尺寸':>6} {'不透明占比':>10} {'前景占比':>9} {'对比度':>7}  判定")
    for s in (16, 20, 24, 32, 48, 64, 256):
        im = render(s)
        ir = ink(im) * 100
        wr = white_ratio(im) * 100
        ct = contrast(FG, BOT)
        # 判据说明:
        #   ir >= 70  -> 底板几乎铺满(圆角会略少), 否则任务栏上会看到缝隙
        #   5..40%    -> H 占底板的比例。太细会糊、太粗会变成色块。
        #                上限从 34 放宽到 40: 16px 下 H 天然占比偏高
        #                (柱子只有 3px 宽, 为了可读必须粗一点), 而紫色底
        #                对比度更高(7.90), 占比高反而更清楚。
        #   ct >= 3.0 -> WCAG 图形门槛
        good = (ir >= 70 and 5 <= wr <= 40 and ct >= 3.0)
        ok = ok and good
        print(f"{s:>6} {ir:>9.1f}% {wr:>8.1f}% {ct:>7.2f}  "
              f"{'OK' if good else 'FAIL'}")
    print("\n=> " + ("全部通过" if ok else "有项目未通过"))
    return 0 if ok else 1


def _png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _write_module(out_py: str) -> None:
    """生成 com_bridge/appicon.py —— 图标以 base64 内嵌进源码。

    ★ 内嵌而不是读文件: 单文件 exe 解包后资源路径会变(见 tray.py 的历史
      注释)。内嵌零路径依赖, 解码几个小 PNG 是毫秒级。
    ★ 必须内嵌**每一个**常用尺寸: Windows 需要 32px(标题栏/任务栏)时若只
      有 256/48, 就只能缩放; 而 H 的 2~3px 细部一缩放就糊。
    """
    lines = [f'PNG_{s} = "{_png_b64(render(s))}"' for s in EMBED_SIZES]
    consts = "\n".join(lines)
    order = ", ".join(f"PNG_{s}" for s in EMBED_SIZES)
    src = f'''# -*- coding: utf-8 -*-
"""应用图标(窗口/任务栏/Alt+Tab) —— base64 内嵌, 零路径依赖。

由 tools/icons/gen_app_icon.py 自动生成, **请勿手改**。

图形是「H」(两柱一杠 = 两台电脑 + 一条链路), 几何定义在
com_bridge/brand.py —— 托盘/窗口/exe/主界面品牌标记共用同一份定义。

为什么内嵌: 单文件 exe 解包后资源路径会变(见 tray.py 的历史注释),
内嵌后 `apply()` 在源码运行与打包 exe 两种方式下都能工作。

为什么内嵌 {len(EMBED_SIZES)} 个尺寸: Windows 在标题栏/任务栏/Alt+Tab/
资源管理器各处要的尺寸不同。只给少数几档的话, 缺的那档只能缩放,
而 H 的 2~3px 细部一缩放就会被抗锯齿摊薄(实测从"两柱一杠"变成
"两块方块")。全部内嵌 = 永远拿到原生尺寸。
"""
from __future__ import annotations

import base64

{consts}

# 全部尺寸(iconphoto 会据此建多个 HICON)
ALL_SIZES = ({order},)


def apply(root) -> bool:
    """把应用图标设到窗口上(标题栏 + 任务栏 + Alt+Tab)。返回是否成功。

    ★ 必须**保留 PhotoImage 的引用**: tkinter 不持有它, 被 GC 回收后
      图标会变回 Tk 默认的羽毛。这里挂在 root 上。
    """
    try:
        import tkinter as tk
        photos = []
        for data in ALL_SIZES:
            photos.append(tk.PhotoImage(data=data, master=root))
        root.iconphoto(True, *photos)
        root._app_icon_photos = photos     # 防 GC
        return True
    except Exception:  # noqa: BLE001
        return False
'''
    with open(out_py, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"  -> {out_py} ({os.path.getsize(out_py)} bytes, "
          f"{len(EMBED_SIZES)} 个尺寸)")


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", ".."))
    out_dir = os.path.join(here, "_app_icon")
    os.makedirs(out_dir, exist_ok=True)

    if "--check" in sys.argv:
        return _check()

    for s in SIZES:
        render(s).save(os.path.join(out_dir, f"app_{s}.png"))
    print(f"已生成 {len(SIZES)} 个 PNG -> {out_dir}")

    # 多尺寸 .ico(exe 用)
    imgs = [render(s) for s in ICO_SIZES]
    ico = os.path.join(root, "com-bridge.ico")
    imgs[-1].save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES],
                  append_images=imgs[:-1])
    print(f"  -> {ico} ({os.path.getsize(ico)} bytes)")

    _write_module(os.path.join(root, "com_bridge", "appicon.py"))

    # 最后跑一遍形状自检, 不合格就非零退出(构建脚本可直接拦)
    print()
    return _check()


if __name__ == "__main__":
    sys.exit(main())
