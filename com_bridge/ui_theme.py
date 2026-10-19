# -*- coding: utf-8 -*-
"""UI 设计系统 —— 统一的配色、字体与控件样式(纯 tkinter/ttk 实现)。

## 为什么要单独一层
之前 UI 是"毛坯": 直接用 ttk 默认主题 + 各处零散的 foreground="#666"。
结果风格不一致(有的地方灰、有的地方黑)、间距随意、没有层级感。
把设计要素集中到这里, 才能做到:
  * 全程序一致的色板与字号
  * 换主题只改一处
  * 各构建函数只关心结构, 不关心"用几号灰"

## 设计取向
深色卡片 + 浅灰背景的双层结构, 接近现代 Windows 11 工具的观感:
  * 背景: 浅灰 (#F3F4F6), 卡片: 白 (#FFFFFF), 边框: #E5E7EB
  * 主色: 蓝 (#2563EB), 成功: 绿 (#16A34A), 危险: 红 (#DC2626)
  * 文字: 主 #111827, 次 #6B7280, 弱 #9CA3AF
  * 字体: 微软雅黑 10pt(中文清晰), 等宽用 Consolas
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# --- 色板 -------------------------------------------------------------
BG = "#F3F4F6"          # 窗口背景
CARD = "#FFFFFF"        # 卡片背景
CARD_ALT = "#FAFAFA"    # 次级卡片
BORDER = "#E5E7EB"      # 分隔线/边框
TEXT = "#111827"        # 主文字
TEXT_MUTED = "#6B7280"  # 次要文字
TEXT_FAINT = "#9CA3AF"  # 更弱(提示)
PRIMARY = "#2563EB"     # 主色(按钮/选中)
PRIMARY_DARK = "#1D4ED8"
SUCCESS = "#16A34A"     # 运行中
DANGER = "#DC2626"      # 错误/停止
WARNING = "#D97706"
LOG_BG = "#111827"      # 日志深色背景
LOG_FG = "#D1D5DB"

# --- 字体 -------------------------------------------------------------
FONT_UI = ("Microsoft YaHei UI", 10)
FONT_UI_SMALL = ("Microsoft YaHei UI", 9)
FONT_UI_TINY = ("Microsoft YaHei UI", 8)
FONT_TITLE = ("Microsoft YaHei UI", 14, "bold")
FONT_SUBTITLE = ("Microsoft YaHei UI", 10, "bold")
FONT_MONO = ("Consolas", 9)

# --- 间距(4 的倍数, 保持节奏一致) ------------------------------------
PAD_XS, PAD_S, PAD_M, PAD_L, PAD_XL = 4, 8, 12, 16, 24


def apply_theme(root: tk.Misc) -> ttk.Style:
    """把统一主题应用到 ttk 控件, 返回 Style 供进一步微调。"""
    style = ttk.Style(root)
    try:
        # 'clam' 比默认 'vista' 更容易做平铺配色(后者很多属性被系统主题锁死)
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(background=BG)

    style.configure(".", font=FONT_UI, background=BG, foreground=TEXT)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=TEXT, font=FONT_UI)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=TEXT_MUTED,
                    font=FONT_UI_SMALL)
    style.configure("CardMuted.TLabel", background=CARD,
                    foreground=TEXT_MUTED, font=FONT_UI_SMALL)
    style.configure("Faint.TLabel", background=CARD, foreground=TEXT_FAINT,
                    font=FONT_UI_TINY)
    style.configure("Title.TLabel", background=BG, foreground=TEXT,
                    font=FONT_TITLE)
    style.configure("Subtitle.TLabel", background=CARD, foreground=TEXT,
                    font=FONT_SUBTITLE)

    # 主按钮(填充蓝)/次按钮(描边)
    style.configure("Primary.TButton", font=FONT_UI, padding=(14, 7),
                    background=PRIMARY, foreground="#FFFFFF",
                    borderwidth=0, focuscolor=PRIMARY)
    style.map("Primary.TButton",
              background=[("active", PRIMARY_DARK), ("disabled", "#BFDBFE")],
              foreground=[("disabled", "#FFFFFF")])
    style.configure("Secondary.TButton", font=FONT_UI, padding=(14, 7),
                    background=CARD, foreground=TEXT, borderwidth=1,
                    relief="solid")
    style.map("Secondary.TButton",
              background=[("active", "#F9FAFB"), ("disabled", "#F3F4F6")],
              foreground=[("disabled", TEXT_FAINT)])
    style.configure("Danger.TButton", font=FONT_UI, padding=(14, 7),
                    background=DANGER, foreground="#FFFFFF", borderwidth=0)
    style.map("Danger.TButton",
              background=[("active", "#B91C1C"), ("disabled", "#FECACA")])

    # 输入框
    style.configure("TEntry", padding=6, fieldbackground=CARD,
                    borderwidth=1, relief="solid")
    style.configure("TCombobox", padding=4)

    # 单选(角色卡片用)
    style.configure("Card.TRadiobutton", background=CARD, font=FONT_UI,
                    foreground=TEXT)
    style.map("Card.TRadiobutton", background=[("active", CARD)])

    # 分隔线
    style.configure("TSeparator", background=BORDER)

    # 树/表(状态窗口)
    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=TEXT, rowheight=26, borderwidth=0,
                    font=FONT_UI_SMALL)
    style.configure("Treeview.Heading", background=CARD_ALT,
                    foreground=TEXT_MUTED, font=FONT_UI_SMALL,
                    borderwidth=0)

    return style


def card(parent: tk.Misc, title: str | None = None,
         padding: int = PAD_L) -> tuple[tk.Frame, tk.Frame]:
    """创建一张卡片, 返回 (卡片外框, 内容区)。

    用 Frame + 高亮边框模拟卡片(ttk.LabelFrame 在 clam 下边框很难看)。
    """
    outer = tk.Frame(parent, background=CARD,
                     highlightbackground=BORDER, highlightthickness=1,
                     bd=0)
    inner = tk.Frame(outer, background=CARD, padx=padding, pady=padding)
    inner.pack(fill="both", expand=True)
    if title:
        tk.Label(inner, text=title, background=CARD, foreground=TEXT,
                 font=FONT_SUBTITLE, anchor="w").pack(fill="x", pady=(0, PAD_S))
    return outer, inner


def heading(parent: tk.Misc, text: str,
            sub: str | None = None) -> tk.Frame:
    """区块标题(主标题 + 可选副标题)。"""
    f = tk.Frame(parent, background=BACKGROUND_OF(parent))
    tk.Label(f, text=text, background=BACKGROUND_OF(parent),
             foreground=TEXT, font=FONT_TITLE, anchor="w").pack(fill="x")
    if sub:
        tk.Label(f, text=sub, background=BACKGROUND_OF(parent),
                 foreground=TEXT_MUTED, font=FONT_UI_SMALL,
                 anchor="w", justify="left").pack(fill="x", pady=(2, 0))
    return f


def BACKGROUND_OF(widget: tk.Misc) -> str:
    """探测控件的背景色, 让子控件融进去(卡片内/窗口背景自适应)。"""
    try:
        bg = widget.cget("background")
        if bg:
            return str(bg)
    except Exception:  # noqa: BLE001
        pass
    return BG


def status_dot(parent: tk.Misc, color: str = TEXT_FAINT) -> tk.Label:
    """用一个小圆点表示状态(● 字符 + 颜色), 返回可改色的 Label。"""
    bg = BACKGROUND_OF(parent)
    lbl = tk.Label(parent, text="●", foreground=color, background=bg,
                   font=("Segoe UI Symbol", 12))
    return lbl
