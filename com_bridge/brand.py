# -*- coding: utf-8 -*-
"""Com Bridge 品牌图标几何 —— **唯一来源**。

## 母题(2026-10-21 用户定稿)

用户给出的图形是「H」: 两根竖直圆角柱 + 中间一条短横杠。
含义直读: 两台电脑(两根柱子)由一条链路(横杠)连起来 —— 与
"双机键鼠越界 + 音频同步"这个产品本体完全对应。

## 为什么抽成独立模块

同一图形要出现在 **四个地方**, 各有各的尺寸与配色要求:

    ┌ 托盘(16~32px, 单色, 叠加黄/绿状态点)
    ├ 窗口标题栏 / 任务栏(16~256px, 蓝底白字)
    ├ .exe 文件图标(多尺寸 .ico)
    └ 主界面左上角的品牌标记(26px, 蓝底白字)

以前每处各写一套几何, 改一次要改四处, 而且实测会**逐渐漂移**
(托盘那套三条横线改了三次才和三处对齐)。这里只定义一次,
所有渲染器都调 `bars_for(size)` / `layout_for(size)`。

## 几何怎么定(为什么按像素而不是比例)

这是两次真实教训的直接产物:

  教训1(占比不均): 按比例缩放时 16px -> 2 行, 20px -> 4 行, 24px -> 5 行,
    同一个图标"越大线越粗"。
  教训2(用户看到"两道线"): 细节落在**半像素**上时, 超采样降采样会把
    最细的那条摊掉 —— 三条粗细不一致, 缩到标题栏尺寸后最细的先消失。

结论: 细节尺寸必须**吸附到整数物理像素**, 且余量平均分给上下边距。
本模块的 `layout_for()` 就是这么算的。
"""
from __future__ import annotations

# 品牌色: 靛紫渐变底 + 白字
# ★★★ 2026-10-21 用户反馈: "颜色不同, 原本的是紫色, 你这个是蓝色"。
#   根因: 这里误用了更早"三条横线"时代的蓝色(#5B9CFF/#2F6BE0), 而用户
#   选定的 B 稿(浅色)里品牌标记是 **#6366F1 -> #4338CA 靛紫**。
#   以设计稿为准, 不要再改回蓝色。
TOP = (0x63, 0x66, 0xF1)
BOT = (0x43, 0x38, 0xCA)
FG = (0xFF, 0xFF, 0xFF)

# 单色版(托盘 / 无底板场合): 主体随任务栏明暗反转
INK_LIGHT = (0x00, 0x00, 0x00)
INK_DARK = (0xFF, 0xFF, 0xFF)


def layout_for(size: int) -> dict:
    """返回布尔型布局(全部是**整数像素**)。

    键:
        pad      左右留边
        bar_w    两根竖柱的宽度
        bar_h    竖柱高度
        top      竖柱顶端 y
        arm_h    中间横杠的厚度
        arm_pad  横杠两端相对竖柱内侧再内缩的距离

    ★ 为什么横杠比竖柱窄: H 的横杠如果与竖柱等宽, 在小尺寸下会和
      竖柱融成一个"工"字块, 辨识度反而下降; 略窄 + 居中留空, 三条
      结构才分得开。
    """
    s = int(size)
    if s <= 20:
        pad, bar_w, bar_h, arm_h = 2, 3, 12, 2
    elif s <= 28:
        pad, bar_w, bar_h, arm_h = 3, 4, 16, 3
    elif s <= 48:
        pad, bar_w, bar_h, arm_h = 4, 6, 24, 4
    elif s <= 96:
        pad, bar_w, bar_h, arm_h = 6, 9, 36, 6
    else:
        pad = max(6, round(s * 0.09))
        bar_w = max(6, round(s * 0.135))
        bar_h = max(6, round(s * 0.50))
        arm_h = max(5, round(s * 0.09))

     # 超小尺寸保底: 保证视觉重心仍有"两根柱子"
    bar_w = max(2, min(bar_w, s // 4))
    arm_h = max(2, min(arm_h, bar_h // 2))
    # 竖直居中, 且必须落在整数像素上(见模块说明的教训2)
    top = max(1, (s - bar_h) // 2)
    # 水平: 两根柱子贴左右留边, 横杠居中且**左右对称**留出空隙
    # ★★★ 这里踩过一个"差一根像素"的坑: 早先 arm_x1 取 right_x - arm_pad,
    #   于是横杠右端与右柱**相邻**(连成一片), 左端却空出 arm_pad ——
    #   看起来像"左柱 | 长横杠", 而不是居中的短杠。
    #   右端必须是 right_x - arm_pad - 1(坐标是含端点的整数像素)。
    arm_pad = max(1, round(bar_w * 0.45)) if s > 32 else 1
    return {
        "pad": pad,
        "bar_w": bar_w,
        "bar_h": bar_h,
        "top": top,
        "arm_h": arm_h,
        "arm_pad": arm_pad,
        "left_x": pad,
        "right_x": s - pad - bar_w,
        # ★★★ 坐标必须**左右对称**。做过一个错误修正: 起初只在 arm_x0
        #   上减 1(想收紧左端), 结果横杠贴住左柱、右侧却留空, 行分段
        #   变成 [3, 8](一边连通一边断开)。正确做法是两端都用"含端点"
        #   的同一套公式, 让左右各留同样宽的空隙。
        "arm_x0": pad + bar_w + arm_pad,
        "arm_x1": s - pad - bar_w - arm_pad - 1,
        "arm_y": top + (bar_h - arm_h) // 2,
    }


def bars_for(size: int) -> list[tuple[int, int, int, int]]:
    """返回要填的矩形列表 [(x0, y0, x1, y1), ...](含端点, 整数像素)。

    顺序: 左柱 / 右柱 / 中横杠。渲染器只需把每个矩形填成前景色。
    """
    g = layout_for(size)
    return [
        (g["left_x"], g["top"], g["left_x"] + g["bar_w"] - 1,
         g["top"] + g["bar_h"] - 1),
        (g["right_x"], g["top"], g["right_x"] + g["bar_w"] - 1,
         g["top"] + g["bar_h"] - 1),
        (g["arm_x0"], g["arm_y"], max(g["arm_x0"], g["arm_x1"]),
         g["arm_y"] + g["arm_h"] - 1),
    ]


def bar_radius(size: int) -> int:
    """竖柱的圆角半径(px)。

    ★ 只在大尺寸用圆头: 16px 下半径 1.5 的圆角会把 3px 宽的柱子吃掉一半,
      反而像"缩进一格"。小尺寸用直角 —— 这是像素图标的通行做法。
    """
    if size <= 24:
        return 0
    return max(1, round(layout_for(size)["bar_w"] * 0.5))


# --- 归一化版本(托盘 GDI 那条路径按 16px 基准采样) --------------------

def tray_rects(base: float = 16.0):
    """托盘图标的三个矩形(浮点), 直接由 `layout_for(base)` **派生**。

    ★★★ 2026-10-21 修正: 早先这里另写了一套常量(_TR_PAD/_TR_W...),
      结果是"同一个图标, 托盘那版和窗口那版差一个像素" —— 左柱落在
      x=1 还是 x=2、右柱宽度几 px 全对不上。用户要的是**同一个图形**,
      所以必须同源: 这里只做"整数像素 -> 浮点矩形"的换算, 不再自定义尺寸。

    返回 [(x0, y0, x1, y1), ...], 坐标为浮点, 右/下边界是**开区间**
    (x1, y1 表示外沿), 供超采样逐子像素判定 x0 <= x < x1。
    """
    s = int(round(float(base)))
    g = layout_for(s)
    k = float(base) / float(s)
    # 整数像素的左闭右开坐标 -> 浮点(右/下乘 k 时用 +1 表示外沿)
    lx = g["left_x"] * k
    rx = g["right_x"] * k
    bw = g["bar_w"] * k
    top = g["top"] * k
    bh = g["bar_h"] * k
    arm_y = g["arm_y"] * k
    arm_h = g["arm_h"] * k
    ax0 = g["arm_x0"] * k
    ax1 = (g["arm_x1"] + 1) * k
    return [
        (lx, top, lx + bw, top + bh),
        (rx, top, rx + bw, top + bh),
        (ax0, arm_y, max(ax0, ax1), arm_y + arm_h),
    ]
