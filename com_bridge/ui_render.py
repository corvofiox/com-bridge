# -*- coding: utf-8 -*-
"""UI 渲染层 —— PIL 超采样 / 高斯模糊 / 渐变, 输出 PhotoImage 交给 tkinter。

## v6(2026-10-21 用户选定 A 深色 / B 浅色渐变卡片)

v5 的"白纸 + 细线"被否了("还不如上一版")。复盘: 用户要的是**有层次、
有色彩、有质感**的界面, 不是极简。v6 按用户逐张选中的设计稿还原:

  * 卡片: 圆角 16 + 1px 极淡描边 + **真实高斯阴影**(不是多层叠加近似)
  * 主按钮: 竖向渐变 + 下方柔和光晕
  * 分段控件: **整条自绘成一张图**(含文字), 避免 tkinter 逐段贴图时
    矩形底色压掉圆角
  * 图标: 28~30px 圆角方块底 + 线性描边图形, 全部 PIL 画

## 为什么阴影用 ImageFilter.GaussianBlur
之前是"画 N 层递减 alpha 的圆角矩形"近似, 结果要么灰脏、要么硬边。
真正的 CSS box-shadow 是"轮廓模糊", 用高斯模糊才能得到同样柔和的衰减。

## 尺寸约定
所有渲染函数的 (w, h) 都是**逻辑像素**, 内部乘 SS 超采样后再缩回,
因此边缘有真实过渡像素。返回值尺寸会在文档里写明。
"""
from __future__ import annotations

import tkinter as tk

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

# 超采样倍数
SS = 4

# 卡片阴影预留边: 与 gui 的布局间距配套(见 gui._build 的 CARD_PAD)
CARD_M = 6


# --- 基础 ---------------------------------------------------------------

def dpi_scale(widget: tk.Misc) -> float:
    try:
        return max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except Exception:  # noqa: BLE001
        return 1.0


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _rgba(c: str | None, a: int = 255):
    if c is None:
        return None
    return _hex(c) + (a,)


def _finish(img: Image.Image, w: int, h: int, master=None):
    out = img.resize((max(1, int(w)), max(1, int(h))), Image.LANCZOS)
    return ImageTk.PhotoImage(out, master=master)


def _resize(img: Image.Image, w: int, h: int) -> Image.Image:
    """降到最终分辨率(文字要在这一步**之后**才写)。"""
    return img.resize((max(1, int(w)), max(1, int(h))), Image.LANCZOS)


def _draw_text(img: Image.Image, xy, text: str, px: int, color: str,
               *, bold: bool = False, anchor: str = "mm") -> None:
    """在**已经降到最终分辨率**的图上写字。

    ★★★ 为什么不能和图形一起在 SS 倍画布上写:
      4x 画布上的文字降到 1x 时, 细笔画被 LANCZOS 平均掉 —— 白字变灰、
      深字变浅, 整体发虚(实测: 白字落在蓝底上没有任何像素超过 245)。
      先降采样图形, 再让 FreeType 在 1x 上做字形抗锯齿, 才是清晰的。
    """
    if not text:
        return
    ImageDraw.Draw(img).text(xy, text, font=font(px, bold),
                             fill=_rgba(color), anchor=anchor)


_FONT_CACHE: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def font(px: int, bold: bool = False):
    """取中文字体(PIL 用)。px 是像素高度, 不是磅值。"""
    key = (int(px), bool(bold))
    hit = _FONT_CACHE.get(key)
    if hit is not None:
        return hit
    path = "msyhbd.ttc" if bold else "msyh.ttc"
    try:
        f = ImageFont.truetype(path, int(px))
    except OSError:
        try:
            f = ImageFont.truetype("segoeui.ttf", int(px))
        except OSError:
            f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def text_w(text: str, px: int, bold: bool = False) -> int:
    try:
        return int(font(px, bold).getbbox(text)[2]) + 1
    except Exception:  # noqa: BLE001
        return int(len(text) * px * 0.6)


# --- 渐变 ---------------------------------------------------------------

def _lerp_stops(stops, t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    prev_p, prev_c = stops[0]
    for p, c in stops[1:]:
        if t <= p:
            span = max(1e-6, p - prev_p)
            k = (t - prev_p) / span
            a, b = _hex(prev_c), _hex(c)
            return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))
        prev_p, prev_c = p, c
    return _hex(stops[-1][1])


def linear_gradient(w: int, h: int, stops, master=None):
    """竖向多色渐变(纯色块, 不需要超采样)。stops=[(0.0,"#aa"),(1.0,"#bb")]。"""
    w, h = max(1, int(w)), max(1, int(h))
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=_lerp_stops(stops, y / max(1, h - 1)))
    return ImageTk.PhotoImage(img, master=master)


def gradient_fill(w: int, h: int, radius: int, top: str, bottom: str,
                  master=None, *, border: str | None = None,
                  border_alpha: int = 255):
    """圆角 + 竖向渐变的实心块(按钮 / 深色卡片光辉用)。"""
    sw, sh = int(w * SS), int(h * SS)
    grad = Image.new("RGB", (sw, sh))
    gd = ImageDraw.Draw(grad)
    t, b = _hex(top), _hex(bottom)
    for y in range(sh):
        k = y / max(1, sh - 1)
        gd.line([(0, y), (sw, y)],
                fill=tuple(int(t[i] + (b[i] - t[i]) * k) for i in range(3)))
    mask = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, sw - 1, sh - 1], radius=int(radius * SS), fill=255)
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    if border is not None:
        ImageDraw.Draw(img).rounded_rectangle(
            [0, 0, sw - 1, sh - 1], radius=int(radius * SS),
            outline=_rgba(border, border_alpha), width=SS)
    return _finish(img, w, h, master)


# --- 卡片(圆角 + 真实高斯阴影) -----------------------------------------

def card(w: int, h: int, radius: int, master=None, *, fill: str = "#FFFFFF",
         gradient=None, border: str | None = None,
         border_alpha: int = 26, shadow_alpha: int = 30,
         blur: int = 7, dy: int = 2):
    """渲染卡片, 尺寸 = (w + 2*CARD_M, h + 2*CARD_M)。

    ★ 阴影用高斯模糊: 先做圆角轮廓 mask, 再整体向下偏移 dy、模糊 blur,
      最后以低 alpha 压到卡片下面 —— 与 CSS box-shadow 的观感一致。
    ★ CARD_M 只有 6px, 是因为 tkinter 的 pack 间距不含阴影, 阴影边留太
      大会把卡片"挤小"(见 gui._build 里 padx/pady 的算法)。
    """
    sw, sh = int(w * SS), int(h * SS)
    m = int(CARD_M * SS)
    full = Image.new("RGBA", (sw + m * 2, sh + m * 2), (0, 0, 0, 0))

    # 1) 阴影层
    if shadow_alpha > 0:
        sh_mask = Image.new("L", (sw + m * 2, sh + m * 2), 0)
        ImageDraw.Draw(sh_mask).rounded_rectangle(
            [m, m + int(dy * SS), m + sw - 1, m + sh - 1 + int(dy * SS)],
            radius=int(radius * SS), fill=shadow_alpha)
        sh_mask = sh_mask.filter(ImageFilter.GaussianBlur(int(blur * SS)))
        # ★ 用 putalpha 把轮廓当 alpha 通道塞进一张纯色图 —— 比逐像素
        #   写 alpha 快两个数量级(4x 超采样下后者要几百万次 Python 循环)。
        r_, g_, b_ = _hex("#0B1220")
        shadow = Image.new("RGBA", full.size, (r_, g_, b_, 0))
        shadow.putalpha(sh_mask)
        full = Image.alpha_composite(full, shadow)

    # 2) 卡片本体
    body = Image.new("RGBA", (sw + m * 2, sh + m * 2), (0, 0, 0, 0))
    if gradient is not None:
        grad = Image.new("RGB", (sw, sh))
        gd = ImageDraw.Draw(grad)
        top, bot = gradient
        t, b = _hex(top), _hex(bot)
        for y in range(sh):
            k = y / max(1, sh - 1)
            gd.line([(0, y), (sw, y)],
                    fill=tuple(int(t[i] + (b[i] - t[i]) * k) for i in range(3)))
        mask = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, sw - 1, sh - 1], radius=int(radius * SS), fill=255)
        inner = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        inner.paste(grad, (0, 0), mask)
        body.paste(inner, (m, m), inner)
    else:
        ImageDraw.Draw(body).rounded_rectangle(
            [m, m, m + sw - 1, m + sh - 1], radius=int(radius * SS),
            fill=_rgba(fill))
    if border is not None:
        ImageDraw.Draw(body).rounded_rectangle(
            [m, m, m + sw - 1, m + sh - 1], radius=int(radius * SS),
            outline=_rgba(border, border_alpha), width=SS)
    full = Image.alpha_composite(full, body)
    return _finish(full, w + CARD_M * 2, h + CARD_M * 2, master)


# --- 分段控件(整条一张图, 含文字) --------------------------------------

def segmented(items, master=None, *, height: int = 34, pad: int = 3,
              text_px: int = 12, track: str = "#F2F4F7",
              track_border: str | None = None,
              pill: str = "#FFFFFF", pill_shadow: bool = True,
              fg: str = "#667085", fg_on: str = "#101828",
              radius: int = 10):
    """items = [(文字, 是否选中)]。返回 PhotoImage(宽自适应)。

    ★ 文字用 PIL 画在同一个位图里: tkinter 若用子 Label 叠字, 每段的矩形
      底色会把相邻胶囊的圆角压成直角(实测过)。整条一张图彻底避免该问题。
    """
    pads = 12
    widths = [max(26, text_w(t, text_px, sel) + pads * 2) for t, sel in items]
    w = sum(widths) + pad * 2 + (len(items) - 1) * 2
    h = height
    sw, sh = w * SS, h * SS
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 轨道
    d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=radius * SS,
                        fill=_rgba(track))
    if track_border is not None:
        d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=radius * SS,
                            outline=_rgba(track_border), width=SS)

    x = pad
    for (label, sel), cw in zip(items, widths):
        if sel:
            if pill_shadow:
                msk = Image.new("L", (sw, sh), 0)
                ImageDraw.Draw(msk).rounded_rectangle(
                    [x * SS, pad * SS + SS, (x + cw) * SS - 1,
                     (h - pad) * SS - 1 + SS],
                    radius=(radius - 3) * SS, fill=70)
                msk = msk.filter(ImageFilter.GaussianBlur(2 * SS))
                lay = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
                lay.putalpha(msk)
                img = Image.alpha_composite(img, lay)
            d = ImageDraw.Draw(img)
            d.rounded_rectangle(
                [x * SS, pad * SS, (x + cw) * SS - 1, (h - pad) * SS - 1],
                radius=(radius - 3) * SS, fill=_rgba(pill))
        x += cw + 2
    out = _resize(img, w, h)
    x = pad
    for (label, sel), cw in zip(items, widths):
        _draw_text(out, (x + cw / 2, h / 2), label, text_px,
                   fg_on if sel else fg, bold=sel)
        x += cw + 2
    return ImageTk.PhotoImage(out, master=master), widths


# --- 图标徽章 -----------------------------------------------------------

def _draw_icon(d: ImageDraw.ImageDraw, kind: str, box, color, wid: int) -> None:
    """在 box=(x0,y0,x1,y1) 内画线性图标。"""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    s = min(x1 - x0, y1 - y0)
    c = _rgba(color)

    if kind == "screen":                       # 一台显示器
        r = s * 0.14
        d.rounded_rectangle([x0, y0 + s * 0.14, x1, y1 - s * 0.28], radius=r,
                            outline=c, width=wid)
        d.line([(cx, y1 - s * 0.26), (cx, y1 - s * 0.04)], fill=c, width=wid)
        d.line([(cx - s * 0.22, y1 - s * 0.04), (cx + s * 0.22, y1 - s * 0.04)],
               fill=c, width=wid)
    elif kind == "layout":                     # 两块屏幕并排(方位)
        d.rounded_rectangle([x0, y0 + s * 0.20, x0 + s * 0.46, y1 - s * 0.12],
                            radius=s * 0.10, outline=c, width=wid)
        d.rounded_rectangle([x0 + s * 0.56, y0 + s * 0.06,
                             x1, y1 - s * 0.26],
                            radius=s * 0.10, outline=c, width=wid)
    elif kind == "latency":                    # 双向箭头
        d.line([(x0 + s * 0.14, cy), (x1 - s * 0.14, cy)], fill=c, width=wid)
        d.line([(x0 + s * 0.36, cy - s * 0.20),
                (x0 + s * 0.12, cy),
                (x0 + s * 0.36, cy + s * 0.20)], fill=c, width=wid,
               joint="curve")
        d.line([(x1 - s * 0.36, cy - s * 0.20),
                (x1 - s * 0.12, cy),
                (x1 - s * 0.36, cy + s * 0.20)], fill=c, width=wid,
               joint="curve")
    else:                                      # power(开机自启)
        rr = s * 0.42
        d.arc([cx - rr, cy - rr + s * 0.06, cx + rr, cy + rr + s * 0.06],
              start=-58, end=238, fill=c, width=wid)
        d.line([(cx, y0 + s * 0.10), (cx, cy + s * 0.02)], fill=c, width=wid)


def badge(size: int, bg: str, fg: str, kind: str, master=None, *,
          radius: int = 9, icon_ratio: float = 0.5):
    """图标徽章: 圆角方块底 + 居中线性图标。"""
    sw = size * SS
    img = Image.new("RGBA", (sw, sw), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, sw - 1, sw - 1], radius=radius * SS,
                        fill=_rgba(bg))
    s = sw * icon_ratio
    x0 = (sw - s) / 2
    y0 = (sw - s) / 2
    _draw_icon(d, kind, (x0, y0, x0 + s, y0 + s), fg, max(1, int(1.55 * SS)))
    return _finish(img, size, size, master)


# --- 主按钮(渐变 + 光晕) ----------------------------------------------

def button(w: int, h: int, radius: int, top: str, bottom: str, master=None, *,
           glow: str | None = None, glow_alpha: int = 110, glow_blur: int = 12,
           glow_dy: int = 6, text: str = "", fg: str = "#FFFFFF",
           text_px: int = 15, bold: bool = True):
    """主按钮, 返回尺寸 (w, h + glow_dy + glow_blur) 的图(下方留光晕)。

    ★ 文字也画在图里, 且**按按钮本体(h)居中对齐** —— 图比按钮本体高,
      若交给 Label 的 compound 居中会整体下沉半个光晕高度。
    """
    extra = (glow_dy + glow_blur) if glow else 0
    sw, sh = int(w * SS), int((h + extra) * SS)
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))

    if glow:
        msk = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(msk).rounded_rectangle(
            [0, int(glow_dy * SS), sw - 1, int((h + glow_dy) * SS)],
            radius=radius * SS, fill=glow_alpha)
        msk = msk.filter(ImageFilter.GaussianBlur(int(glow_blur * SS * 0.5)))
        lay = Image.new("RGBA", (sw, sh), _hex(glow) + (255,))
        lay.putalpha(msk)
        img = Image.alpha_composite(img, lay)

    body = Image.new("RGBA", (sw, int(h * SS)), (0, 0, 0, 0))
    grad = Image.new("RGB", (sw, int(h * SS)))
    gd = ImageDraw.Draw(grad)
    t, b = _hex(top), _hex(bottom)
    for y in range(int(h * SS)):
        k = y / max(1, int(h * SS) - 1)
        gd.line([(0, y), (sw, y)],
                fill=tuple(int(t[i] + (b[i] - t[i]) * k) for i in range(3)))
    mask = Image.new("L", (sw, int(h * SS)), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, sw - 1, int(h * SS) - 1], radius=radius * SS, fill=255)
    body.paste(grad, (0, 0), mask)
    img.alpha_composite(body, (0, 0))
    out = _resize(img, w, h + extra)
    _draw_text(out, (w / 2, h / 2), text, text_px, fg, bold=bold)
    return ImageTk.PhotoImage(out, master=master)


def status_pill(text: str, master=None, *, bg: str, fg: str, dot: str,
                text_px: int = 12, height: int = 26,
                border: str | None = None, border_alpha: int = 255,
                dot_glow: bool = True):
    """状态胶囊: 圆点 + 文字, 一张图。返回 (PhotoImage, w)。"""
    pad_l, pad_r, gap = 11, 12, 7
    dot_d = 7
    tw = text_w(text, text_px, True)
    w = pad_l + dot_d + gap + tw + pad_r
    sw, sh = w * SS, height * SS
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=int(height * SS / 2),
                        fill=_rgba(bg))
    if border is not None:
        d.rounded_rectangle([0, 0, sw - 1, sh - 1],
                            radius=int(height * SS / 2),
                            outline=_rgba(border, border_alpha), width=SS)
    cx = int((pad_l + dot_d / 2) * SS)
    cy = int(sh / 2)
    dr = dot_d * SS / 2
    if dot_glow:
        msk = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(msk).ellipse(
            [cx - dr - 2 * SS, cy - dr - 2 * SS,
             cx + dr + 2 * SS, cy + dr + 2 * SS], fill=120)
        msk = msk.filter(ImageFilter.GaussianBlur(2 * SS))
        lay = Image.new("RGBA", (sw, sh), _hex(dot) + (255,))
        lay.putalpha(msk)
        img = Image.alpha_composite(img, lay)
        d = ImageDraw.Draw(img)
    d.ellipse([cx - dr, cy - dr, cx + dr, cy + dr], fill=_rgba(dot))
    out = _resize(img, w, height)
    _draw_text(out, (pad_l + dot_d + gap, height / 2), text, text_px, fg,
               bold=True, anchor="lm")
    return ImageTk.PhotoImage(out, master=master), w


# --- 胶囊 / 圆点 / 开关 -------------------------------------------------

def pill(w: int, h: int, fill: str, master=None, *, border: str | None = None,
         border_alpha: int = 255):
    """状态胶囊的底(文字由 Label compound="center" 叠上去)。"""
    sw, sh = w * SS, h * SS
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=sh // 2,
                        fill=_rgba(fill))
    if border is not None:
        d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=sh // 2,
                            outline=_rgba(border, border_alpha), width=SS)
    return _finish(img, w, h, master)


def status_dot(size: int, color: str, master=None, *, glow: bool = False):
    """抗锯齿圆点, 可选外圈光晕。"""
    pad = size * 0.62 if glow else 1
    total = int(size + pad * 2)
    sw = int(total * SS)
    img = Image.new("RGBA", (sw, sw), (0, 0, 0, 0))
    cx = cy = sw / 2.0
    dot_r = size * SS / 2.0
    rgb = _hex(color)

    if glow:
        msk = Image.new("L", (sw, sw), 0)
        rr = sw / 2.0 - SS
        ImageDraw.Draw(msk).ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                                    fill=150)
        msk = msk.filter(ImageFilter.GaussianBlur(3 * SS))
        lay = Image.new("RGBA", (sw, sw), rgb + (255,))
        lay.putalpha(msk)
        img = Image.alpha_composite(img, lay)

    ImageDraw.Draw(img).ellipse(
        [cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=rgb + (255,))
    return _finish(img, total, total, master)


def toggle_image(width: int, height: int, on: bool, master=None, *,
                 on_color: str = "#12B76A", off_color: str = "#D6DCE5",
                 knob_shadow: bool = True):
    """胶囊开关。"""
    sw, sh = int(width * SS), int(height * SS)
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = sh / 2.0
    d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=r,
                        fill=_rgba(on_color if on else off_color))
    knob_r = r - 2.0 * SS
    cy = sh / 2.0
    cx = (sw - r) if on else r
    if knob_shadow:
        msk = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(msk).ellipse(
            [cx - knob_r, cy - knob_r + SS, cx + knob_r, cy + knob_r + SS],
            fill=90)
        msk = msk.filter(ImageFilter.GaussianBlur(1.2 * SS))
        lay = Image.new("RGBA", (sw, sh), (0, 0, 0, 255))
        lay.putalpha(msk)
        img = Image.alpha_composite(img, lay)
    ImageDraw.Draw(img).ellipse(
        [cx - knob_r, cy - knob_r, cx + knob_r, cy + knob_r],
        fill=(255, 255, 255, 255))
    return _finish(img, width, height, master)


# --- 键帽 / 输入框 / 圆角矩形 ------------------------------------------

def media_button(size: int, kind: str, master=None, *, fg: str):
    """播放 / 停止的**纯图形**按钮(无底色、无圆圈)。

    kind = "play"  -> 右向实心三角
    kind = "stop"  -> 实心圆角方块

    ★★★ 2026-10-21 用户要求: "按钮不能有底色, 而是纯粹的黑色播放三角和
      黑色正方形停止按键"。所以这里**只画图形本身**, 其余像素全透明 ——
      Label 的底色(卡片色)会透出来, 视觉上就是"裸图形浮在卡片上"。

    ★ 透明底 + 单独上色: 图形颜色由调用方按运行状态给(黑/红/黄/绿),
      这也是"状态用颜色表达"的唯一载体, 不能再靠圆底。

    ★ 两个几何修正(都是实测出来的):
      * 三角形按几何中心居中会"陷在左边" -> 右移约 1/16 边长
      * 顶点/边长取整 -> 否则尖端和方角被抗锯齿摊掉一像素, 发虚
    """
    s = int(size)
    ss = SS
    sw = s * ss
    img = Image.new("RGBA", (sw, sw), (0, 0, 0, 0))
    plate_src = img
    # 先在超采样画布上铺几何(三角形斜边需要平滑), 再降采样
    p = ImageDraw.Draw(plate_src)
    if kind == "play":
        hh = s * 0.30
        dx = s * 0.055
        left = (s * 0.28 + dx) * ss
        right = (s * 0.74 + dx) * ss
        cy = (s / 2.0) * ss
        p.polygon([(left, cy - hh * ss), (right, cy), (left, cy + hh * ss)],
                  fill=_rgba(fg))
    else:
        half = s * 0.30
        cx = cy = (s / 2.0) * ss
        p.rounded_rectangle([cx - half * ss, cy - half * ss,
                             cx + half * ss, cy + half * ss],
                            radius=max(1.0, s * 0.06 * ss), fill=_rgba(fg))
    return _finish(plate_src, s, s, master)


def keycap(w: int, h: int, master=None, *, fill: str = "#FFFFFF",
           border: str = "#DFE4EB", fg: str = "#3A4250", text: str = "",
           text_px: int = 11):
    """键帽(一张图, 含文字)。"""
    sw, sh = w * SS, h * SS
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=5 * SS,
                        fill=_rgba(fill))
    d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=5 * SS,
                        outline=_rgba(border), width=SS)
    out = _resize(img, w, h)
    _draw_text(out, (w / 2, h / 2), text, text_px, fg, bold=True)
    return ImageTk.PhotoImage(out, master=master)


def rounded_rect(w: int, h: int, radius: int, fill: str | None = None,
                 master=None, *, border: str | None = None,
                 border_w: int = 1, gradient=None):
    """通用圆角矩形(fill=None 表示只描边)。"""
    sw, sh = int(w * SS), int(h * SS)
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = max(0, int(radius * SS))
    if gradient is not None:
        top, bot = gradient
        grad = Image.new("RGB", (sw, sh))
        gd = ImageDraw.Draw(grad)
        t, b = _hex(top), _hex(bot)
        for y in range(sh):
            k = y / max(1, sh - 1)
            gd.line([(0, y), (sw, y)],
                    fill=tuple(int(t[i] + (b[i] - t[i]) * k) for i in range(3)))
        mask = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, sw - 1, sh - 1], radius=r, fill=255)
        img.paste(grad, (0, 0), mask)
        d = ImageDraw.Draw(img)
    elif fill is not None:
        d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=r, fill=_rgba(fill))
    if border is not None:
        d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=r,
                            outline=_rgba(border), width=max(1, int(border_w * SS)))
    return _finish(img, w, h, master)


def feather_rect(w: int, h: int, radius: int, fill: str, master=None, *,
                 alpha: int = 255):
    """带透明度的圆角块(状态胶囊/徽章底等复用)。"""
    sw, sh = int(w * SS), int(h * SS)
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle(
        [0, 0, sw - 1, sh - 1], radius=radius * SS, fill=_rgba(fill, alpha))
    return _finish(img, w, h, master)


def brand_icon(size: int, master=None, *, top: str = "#5B9CFF",
               bottom: str = "#2563EB", fg: str = "#FFFFFF"):
    """主界面品牌标记: 蓝渐变圆角底板 + 白色「H」。

    ★ H 的几何来自 `com_bridge.brand`(与窗口图标 / exe 图标 / 托盘同源)。
      早先这里写死比例(sw*0.22 等), 与另外三处对不上 —— 用户要的是
      同一个图形, 不是四个"差不多"的图形。
    ★ 底板走超采样(圆角需要平滑), H 在**降采样之后**按整数像素画
      (细部一被重采样就糊, 见 brand.py 的教训2)。
    """
    from . import brand
    sw = size * SS
    img = Image.new("RGBA", (sw, sw), (0, 0, 0, 0))
    grad = Image.new("RGB", (sw, sw))
    gd = ImageDraw.Draw(grad)
    t, b = _hex(top), _hex(bottom)
    for y in range(sw):
        k = y / max(1, sw - 1)
        gd.line([(0, y), (sw, y)],
                fill=tuple(int(t[i] + (b[i] - t[i]) * k) for i in range(3)))
    mask = Image.new("L", (sw, sw), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, sw - 1, sw - 1], radius=int(sw * 0.30), fill=255)
    img.paste(grad, (0, 0), mask)
    plate = _resize(img, size, size)

    d = ImageDraw.Draw(plate)
    radius = brand.bar_radius(size)
    for (x0, y0, x1, y1) in brand.bars_for(size):
        if radius > 0:
            d.rounded_rectangle([x0, y0, x1, y1], radius=radius,
                                fill=_rgba(fg))
        else:
            d.rectangle([x0, y0, x1, y1], fill=_rgba(fg))
    return ImageTk.PhotoImage(plate, master=master)


def label_rect(master=None, *, text: str = "", height: int = 30,
               radius: int = 8, fill: str | None = None,
               border: str | None = None, border_alpha: int = 255,
               text_px: int = 12, fg: str = "#000000", bold: bool = False,
               pad_x: int = 12, min_width: int = 0, width: int | None = None,
               track_w: int | None = None):
    """圆角底 + 居中文字, **一张图**(含文字)。

    ★ 为什么文字也用 PIL 画: tkinter 的 Label compound 叠字需要 Label 的
      矩形背景色与底层图完全一致, 一旦底色是渐变/半透明就必然露出方块边。
      短文字控件(分段胶囊 / 幽灵按钮 / 键帽)整张图渲染最稳。

    返回 (PhotoImage, 实际宽度)。
    """
    tw = text_w(text, text_px, bold)
    w = int(width) if width else max(int(min_width), tw + pad_x * 2)
    h = int(height)
    sw, sh = w * SS, h * SS
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(radius * SS)
    if fill is not None:
        d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=r, fill=_rgba(fill))
    if border is not None:
        d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=r,
                            outline=_rgba(border, border_alpha), width=SS)
    out = _resize(img, w, h)
    _draw_text(out, (w / 2, h / 2), text, text_px, fg, bold=bold)
    return ImageTk.PhotoImage(out, master=master), w
