# -*- coding: utf-8 -*-
"""UI 设计系统 v6 —— 双主题(深色 A / 浅色渐变 B), 卡片 + 图标徽章。

## 由来(2026-10-21)

v4(浅色卡片)被否, v5(白纸细线极简)被否得更彻底("还不如上一版")。
复盘: 用户要的不是"更少的装饰", 而是**有层次、有色彩、有质感**。
于是出了三个方向稿(A 深色 / B 柔和渐变 / C 蓝色主视觉), 用户回答
"A 和 B 都很好" —— 所以 v6 把两者做成**同一套布局的两套配色**。

## 结构(两套配色共用)

    ┌ 标题栏(系统) ─────────────────────────────┐
    │  ●标记  Com Bridge            [状态] [日志] │  ← header
    │ ┌ 卡片1: 状态 ─────────────────────────┐ │
    │ │ ●运行中                       61 ms  │ │
    │ │ 192.168.10.12 → 192.168.10.201       │ │
    │ └──────────────────────────────────────┘ │
    │ ┌ 卡片2: 设置 ─────────────────────────┐ │
    │ │ [icon] 这台电脑的角色   [主电脑|副电脑] │ │
    │ │ [icon] 副电脑方位       [左|右|上|下]   │ │
    │ │ [icon] 延迟目标              [60] ms   │ │
    │ │ [icon] 开机自启                (开关)   │ │
    │ └──────────────────────────────────────┘ │
    │ [        启动服务        ]   ← 渐变+光晕  │
    │          停止服务                          │
    └──────────────────────────────────────────┘

## 关键实现约定

* 所有"带圆角/渐变/阴影"的东西都由 ui_render 渲染成位图; **纯文字标签**
  才用原生 Label。短文字控件(分段胶囊/幽灵按钮/键帽/状态胶囊)连文字一起
  画进位图 —— 因为 Label 的矩形底色会压掉圆角与渐变。
* 卡片阴影需要在布局里预留 CARD_M 边距, 见 ui_render.CARD_M。
* `set_mode()` 改写本模块的**模块级颜色常量**; 主窗口在切换主题时整体重建,
  所以常量在构建期始终自洽。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import ui_render as R

# --- 配色表 -------------------------------------------------------------
# 两套配色的键必须完全一致(切换主题靠同一套代码)。
_DARK = {
    "BG": "#0F1115",
    "BG_GRAD": None,
    "CARD": "#151A22",
    "CARD_BORDER": "#252C39",
    "CARD_BORDER_ALPHA": 255,
    "SHADOW_ALPHA": 0,
    "TEXT": "#E6E8EC",
    "TEXT_STRONG": "#F2F4F8",
    "TEXT_MUTED": "#8B93A1",
    "TEXT_FAINT": "#6D7684",
    "ACCENT": "#2563EB",
    "ACCENT_DARK": "#1D4ED8",
    "ACCENT_SOFT": "#7DA8F5",
    "BTN_TOP": "#3B82F6",
    "BTN_BOTTOM": "#2563EB",
    "BTN_GLOW": "#2563EB",
    "BTN_OFF_BG": "#232A36",
    "BTN_OFF_FG": "#5F6A7B",
    "BTN_STOP_FG": "#A8B4D8",
    "GHOST_BG": "#171B22",
    "GHOST_BORDER": "#252C39",
    "GHOST_FG": "#8B93A1",
    "SEG_TRACK": "#0F131A",
    "SEG_BORDER": "#242B37",
    "SEG_PILL": "#24303F",
    "SEG_PILL_SHADOW": True,
    "SEG_FG": "#8B93A1",
    "SEG_FG_ON": "#E6E8EC",
    "FIELD_BG": "#0F131A",
    "FIELD_BORDER": "#242B37",
    "FIELD_FG": "#E6E8EC",
    "HAIR": "#1C222C",
    "BADGE_STYLES": [("#1C2432", "#7DA8F5")] * 4,
    "TOGGLE_ON": "#22C55E",
    "TOGGLE_OFF": "#39414F",
    "RUN_BG": "#17301F", "RUN_BG_BORDER": "#2A5A3A",
    "RUN_FG": "#4ADE80", "RUN_DOT": "#22C55E",
    "WAIT_BG": "#33260F", "WAIT_BG_BORDER": "#5C4718",
    "WAIT_FG": "#FBBF24", "WAIT_DOT": "#F59E0B",
    "IDLE_BG": "#1B2029", "IDLE_BG_BORDER": "#2A3140",
    "IDLE_FG": "#8B93A1", "IDLE_DOT": "#5A6472",
    "KEYCAP_BG": "#171B22", "KEYCAP_BORDER": "#2A3140", "KEYCAP_FG": "#C9D0DB",
    "DANGER": "#F87171", "SUCCESS": "#22C55E", "WARNING": "#F59E0B",
    "LOG_BG": "#0F1115", "LOG_FG": "#C9D0DB",
    "PRIMARY_BG": "#1B2434",
}

_LIGHT = {
    "BG": "#F2F5FB",
    "BG_GRAD": None,
    "CARD": "#FFFFFF",
    "CARD_BORDER": "#101828",
    "CARD_BORDER_ALPHA": 18,
    "SHADOW_ALPHA": 34,
    "TEXT": "#101828",
    "TEXT_STRONG": "#101828",
    "TEXT_MUTED": "#667085",
    "TEXT_FAINT": "#98A2B3",
    "ACCENT": "#4F46E5",
    "ACCENT_DARK": "#4338CA",
    "ACCENT_SOFT": "#6366F1",
    "BTN_TOP": "#4F46E5",
    "BTN_BOTTOM": "#4338CA",
    "BTN_GLOW": "#4338CA",
    "BTN_OFF_BG": "#EDEEFB",
    "BTN_OFF_FG": "#B2B6D6",
    "BTN_STOP_FG": "#4F46E5",
    "GHOST_BG": "#FFFFFF",
    "GHOST_BORDER": "#101828",
    "GHOST_FG": "#5B6478",
    "SEG_TRACK": "#F2F4F7",
    "SEG_BORDER": None,
    "SEG_PILL": "#FFFFFF",
    "SEG_PILL_SHADOW": True,
    "SEG_FG": "#667085",
    "SEG_FG_ON": "#101828",
    "FIELD_BG": "#F2F4F7",
    "FIELD_BORDER": None,
    "FIELD_FG": "#101828",
    "HAIR": "#F2F4F7",
    "BADGE_STYLES": [("#EEF2FF", "#4F46E5"), ("#F5F3FF", "#7C3AED"),
                     ("#FFF4ED", "#EA580C"), ("#ECFDF5", "#059669")],
    "TOGGLE_ON": "#12B76A",
    "TOGGLE_OFF": "#D6DCE5",
    "RUN_BG": "#E9FBF1", "RUN_BG_BORDER": None,
    "RUN_FG": "#0B7A4B", "RUN_DOT": "#12B76A",
    "WAIT_BG": "#FEF6E7", "WAIT_BG_BORDER": None,
    "WAIT_FG": "#B45309", "WAIT_DOT": "#F59E0B",
    "IDLE_BG": "#F2F4F7", "IDLE_BG_BORDER": None,
    "IDLE_FG": "#667085", "IDLE_DOT": "#98A2B3",
    "KEYCAP_BG": "#FFFFFF", "KEYCAP_BORDER": "#DFE4EB", "KEYCAP_FG": "#3A4250",
    "DANGER": "#E5484D", "SUCCESS": "#12B76A", "WARNING": "#F59E0B",
    "LOG_BG": "#FFFFFF", "LOG_FG": "#3A4250",
    "PRIMARY_BG": "#EEF2FF",
}

_PALETTES = {"dark": _DARK, "light": _LIGHT}
_mode = "light"

# 这些常量由 set_mode() 覆盖
BG = "#FFFFFF"
BG_GRAD = None
CARD = "#FFFFFF"
CARD_BORDER = "#101828"
CARD_BORDER_ALPHA = 18
SHADOW_ALPHA = 34
TEXT = "#101828"
TEXT_STRONG = "#101828"
TEXT_MUTED = "#667085"
TEXT_FAINT = "#98A2B3"
ACCENT = "#4F46E5"
ACCENT_DARK = "#4338CA"
ACCENT_SOFT = "#6366F1"
BTN_TOP = "#4F46E5"
BTN_BOTTOM = "#4338CA"
BTN_GLOW = "#4338CA"
BTN_OFF_BG = "#EDEEFB"
BTN_OFF_FG = "#B2B6D6"
BTN_STOP_FG = "#4F46E5"
GHOST_BG = "#FFFFFF"
GHOST_BORDER = "#101828"
GHOST_FG = "#5B6478"
SEG_TRACK = "#F2F4F7"
SEG_BORDER = None
SEG_PILL = "#FFFFFF"
SEG_PILL_SHADOW = True
SEG_FG = "#667085"
SEG_FG_ON = "#101828"
FIELD_BG = "#F2F4F7"
FIELD_BORDER = None
FIELD_FG = "#101828"
HAIR = "#F2F4F7"
BADGE_STYLES = _LIGHT["BADGE_STYLES"]
TOGGLE_ON = "#12B76A"
TOGGLE_OFF = "#D6DCE5"
RUN_BG = "#E9FBF1"
RUN_BG_BORDER = None
RUN_FG = "#0B7A4B"
RUN_DOT = "#12B76A"
WAIT_BG = "#FEF6E7"
WAIT_BG_BORDER = None
WAIT_FG = "#B45309"
WAIT_DOT = "#F59E0B"
IDLE_BG = "#F2F4F7"
IDLE_BG_BORDER = None
IDLE_FG = "#667085"
IDLE_DOT = "#98A2B3"
KEYCAP_BG = "#FFFFFF"
KEYCAP_BORDER = "#DFE4EB"
KEYCAP_FG = "#3A4250"
DANGER = "#E5484D"
SUCCESS = "#12B76A"
WARNING = "#F59E0B"
LOG_BG = "#FFFFFF"
LOG_FG = "#3A4250"
PRIMARY_BG = "#EEF2FF"

# 兼容旧名(gui_windows.py 在用)
BORDER = "#F2F4F7"
BORDER_STRONG = "#E4E8EE"
TEXT_BODY = "#3A4250"
CARD_ALT = "#FAFBFC"
PRIMARY = "#4F46E5"
PRIMARY_DARK = "#4338CA"
PRIMARY_LIGHT = "#6366F1"
SUCCESS_BG = "#E9FBF1"
WARNING_BG = "#FEF6E7"
DANGER_BG = "#FDECEC"
IDLE_COLOR = "#98A2B3"


def set_mode(mode: str) -> str:
    """切换配色(dark / light), 返回实际生效的模式。"""
    global _mode, BORDER, BORDER_STRONG, TEXT_BODY, CARD_ALT
    global PRIMARY, PRIMARY_DARK, PRIMARY_LIGHT
    global SUCCESS_BG, WARNING_BG, DANGER_BG, IDLE_COLOR
    mode = "dark" if str(mode).lower() == "dark" else "light"
    _mode = mode
    globals().update(_PALETTES[mode])
    # 兼容别名的派生
    BORDER = HAIR
    BORDER_STRONG = CARD_BORDER
    TEXT_BODY = TEXT if mode == "dark" else "#3A4250"
    CARD_ALT = "#171B22" if mode == "dark" else "#FAFBFC"
    PRIMARY = ACCENT
    PRIMARY_DARK = ACCENT_DARK
    PRIMARY_LIGHT = ACCENT_SOFT
    SUCCESS_BG = RUN_BG
    WARNING_BG = WAIT_BG
    DANGER_BG = "#2A1719" if mode == "dark" else "#FDECEC"
    IDLE_COLOR = IDLE_DOT
    return _mode


def current_mode() -> str:
    return _mode


def palette() -> dict:
    return dict(_PALETTES[_mode])


def is_dark() -> bool:
    return _mode == "dark"


def system_prefers_dark() -> bool:
    """Windows 是否处于深色模式(读注册表 AppsUseLightTheme)。

    读不到就按浅色处理 —— 不要因为探测失败把界面搞成深色。
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(val) == 0
        finally:
            winreg.CloseKey(key)
    except Exception:  # noqa: BLE001
        return False


# --- 字体(Tk 用磅值; 1pt ≈ 1.333px @96dpi) -----------------------------
FONT_NAME = ("Microsoft YaHei UI", 10, "bold")       # 13px 品牌名
FONT_STATE = ("Microsoft YaHei UI", 20, "bold")      # 27px 大状态/大数字
FONT_UI = ("Microsoft YaHei UI", 10)                 # 13px 行标签
FONT_UI_SMALL = ("Microsoft YaHei UI", 8)            # 11px 说明
FONT_UI_TINY = ("Microsoft YaHei UI", 7)             # 9px 极小字
FONT_SUBTITLE = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 13, "bold")
FONT_BTN = ("Microsoft YaHei UI", 11, "bold")        # 15px
FONT_BRAND = ("Microsoft YaHei UI", 10, "bold")
FONT_MONO = ("Consolas", 9)
FONT_MONO_SMALL = ("Consolas", 8)
FONT_KEYCAP = ("Microsoft YaHei UI", 8)

# --- 间距 / 圆角 --------------------------------------------------------
PAD_XS, PAD_S, PAD_M, PAD_L, PAD_XL = 4, 8, 12, 16, 24
WINDOW_W = 620
WINDOW_H = 660
PAGE_PAD = 20          # 页面左右留白(卡片可见边缘)
CARD_M = R.CARD_M      # 卡片阴影预留边(与 R.card 一致)
CARD_PAD_X = 18        # 卡片内每行左右留白
RADIUS_CARD = 16
RADIUS_BTN = 14
RADIUS_SEG = 10
RADIUS_FIELD = 9


def BACKGROUND_OF(widget: tk.Misc) -> str:
    try:
        bg = widget.cget("background")
        if bg:
            return str(bg)
    except Exception:  # noqa: BLE001
        return BG


def apply_theme(root: tk.Misc) -> ttk.Style:
    """应用 ttk 主题(次级窗口里零星的 ttk 控件用)。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(background=BG)
    style.configure(".", font=FONT_UI, background=BG, foreground=TEXT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT, font=FONT_UI)
    style.configure("TEntry", padding=6, fieldbackground=FIELD_BG,
                    background=FIELD_BG, bordercolor=BORDER_STRONG,
                    borderwidth=1, relief="solid", insertcolor=TEXT)
    style.configure("TSeparator", background=HAIR)
    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=TEXT, rowheight=26, borderwidth=0,
                    font=FONT_UI_SMALL)
    style.configure("Treeview.Heading", background=CARD,
                    foreground=TEXT_MUTED, font=FONT_UI_SMALL,
                    borderwidth=0)
    return style


# --- 卡片 ---------------------------------------------------------------

class Card(tk.Frame):
    """圆角 16 + 1px 极淡描边 + 高斯阴影。内容放在 ``.body`` 里。

    ★ 布局箱比可见卡片大 CARD_M(阴影边), 所以外部间距要按
      "可见间距 - 2*CARD_M" 来给, 见 gui._build。
    """

    def __init__(self, parent: tk.Misc, *, radius: int = RADIUS_CARD,
                 bg: str | None = None, **kw):
        super().__init__(parent, background=bg or BG, **kw)
        self._radius = radius
        self._photo = None
        self._last = None
        self._bg_lbl = tk.Label(self, background=bg or BG, bd=0)
        self._bg_lbl.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, background=CARD)
        m = CARD_M
        self.body.pack(fill="both", expand=True, padx=m, pady=m)
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, ev) -> None:
        w, h = int(ev.width), int(ev.height)
        if w < 12 or h < 12 or (w, h) == self._last:
            return
        self._last = (w, h)
        vw = max(4, w - CARD_M * 2)
        vh = max(4, h - CARD_M * 2)
        try:
            self._photo = R.card(
                vw, vh, self._radius, self,
                fill=CARD, border=CARD_BORDER,
                border_alpha=CARD_BORDER_ALPHA,
                shadow_alpha=SHADOW_ALPHA,
                blur=7, dy=2)
            self._bg_lbl.configure(image=self._photo)
        except Exception:  # noqa: BLE001
            pass

    def set_fill(self, fill: str) -> None:
        """兼容旧接口(卡片底色由主题决定, 这里只改内容层底色)。"""
        try:
            self.body.configure(background=fill)
        except tk.TclError:
            pass


# --- 分段控件 -----------------------------------------------------------

class Segmented(tk.Label):
    """互斥分段选择器。整条是**一张位图**(含文字), 点击按 x 命中。"""

    def __init__(self, parent: tk.Misc, options, value: str, command=None, *,
                 height: int = 34, text_px: int = 12, bg: str | None = None):
        base = bg or BACKGROUND_OF(parent)
        super().__init__(parent, background=base, bd=0, cursor="hand2")
        self._opts = list(options)
        self._value = value
        self._command = command
        self._height = height
        self._text_px = text_px
        self._basis = base
        self._photo = None
        self._geom: list[tuple[int, int, str]] = []
        self._render()
        self.bind("<Button-1>", self._on_click)

    def _render(self) -> None:
        items = [(lbl, v == self._value) for v, lbl in self._opts]
        photo, widths = R.segmented(
            items, self, height=self._height, text_px=self._text_px,
            track=SEG_TRACK, track_border=SEG_BORDER,
            pill=SEG_PILL, pill_shadow=SEG_PILL_SHADOW,
            fg=SEG_FG, fg_on=SEG_FG_ON)
        self._photo = photo
        self._geom = []
        x = 3
        for (v, _), cw in zip(self._opts, widths):
            self._geom.append((x, cw, v))
            x += cw + 2
        self.configure(image=photo)

    def _on_click(self, ev) -> None:
        for x, cw, v in self._geom:
            if x <= ev.x < x + cw:
                if v != self._value:
                    self.set_value(v)
                    if self._command is not None:
                        try:
                            self._command(v)
                        except Exception:  # noqa: BLE001
                            pass
                return

    def get(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        if value != self._value:
            self._value = value
            self._render()


# --- 图标徽章 -----------------------------------------------------------

class Badge(tk.Label):
    """28~30px 圆角方块 + 线性图标。"""

    def __init__(self, parent: tk.Misc, kind: str, *, size: int = 30,
                 index: int = 0, bg: str | None = None):
        base = bg or BACKGROUND_OF(parent)
        super().__init__(parent, background=base, bd=0)
        styles = BADGE_STYLES or [("#EEF2FF", "#4F46E5")]
        b, f = styles[index % len(styles)]
        self._photo = R.badge(size, b, f, kind, self)
        self.configure(image=self._photo)


# --- 按钮 ---------------------------------------------------------------

class MediaButton(tk.Label):
    """纯图形播放 / 停止按钮(无底色)。

    ★★★ 2026-10-21 用户定稿:
      * "按钮不能有底色, 而是纯粹的黑色播放三角和黑色正方形停止按键"
      * "播放在左, 停止在右"
      * 颜色随状态变:
            未启动      -> 播放 黑, 停止 **红**
            等待连接    -> 播放 **黄**
            连接成功    -> 播放 **绿**
        停止按钮只在"程序停止"时变红; 其它状态下保持黑色
        (用户只说了"当程序停止时, 停止按钮由黑变红", 没有别的规则)。

    ★ 颜色在这里是**唯一的状态表达**: 没有圆底、没有文字, 一旦颜色不对
      用户就完全看不出状态, 所以 `set_state()` 而不是 `set_enabled()`
      作为主入口。
    """

    # 图形颜色按状态取(用户指定的四种)
    _PLAY_COLOR = {"idle": "#0D1117", "wait": "#F59E0B", "run": "#22C55E"}
    _STOP_COLOR = {"idle": "#E5484D", "wait": "#0D1117", "run": "#0D1117"}

    def __init__(self, parent: tk.Misc, kind: str, command=None, *,
                 size: int = 30, bg: str | None = None):
        base = bg or BACKGROUND_OF(parent)
        super().__init__(parent, background=base, bd=0, cursor="hand2")
        self._size = int(size)
        self._kind = kind
        self._command = command
        self._enabled = True
        self._state = "idle"
        self._photo = None
        self._render()
        self.bind("<Button-1>", self._on_click)

    def _color(self) -> str:
        """当前该用的图形颜色。

        ★★★ 2026-10-21 修正: 早先"不可点就压成浅灰", 结果**未启动时
          停止方块是灰的而不是红的** —— 而用户明确要求"程序停止时停止
          按钮由黑变红", 也就是说那个红色恰恰出现在"停止按钮不可点"的
          状态。所以颜色**一律由状态决定**, 不再因禁用而灰掉。
          是否可点靠鼠标指针形状区分(hand2 / arrow)。
        """
        table = self._PLAY_COLOR if self._kind == "play" else self._STOP_COLOR
        return table.get(self._state, "#0D1117")

    def _render(self) -> None:
        self._photo = R.media_button(self._size, self._kind, self,
                                     fg=self._color())
        self.configure(image=self._photo)

    def _set_hover(self, on: bool) -> None:
        """悬停只改指针形状, 不换色 —— 颜色完全留给状态表达。"""
        try:
            self.configure(cursor="hand2" if self._enabled else "arrow")
        except tk.TclError:
            pass

    def _on_click(self, _e=None) -> None:
        if self._enabled and self._command is not None:
            try:
                self._command()
            except Exception:  # noqa: BLE001
                pass

    def set_enabled(self, on: bool) -> None:
        on = bool(on)
        if on == self._enabled:
            return
        self._enabled = on
        self._render()

    def set_state(self, state: str) -> None:
        """按运行状态重算颜色: idle / wait / run。"""
        if state != self._state:
            self._state = state
            self._render()

    def bind_click(self) -> None:
        """兼容旧调用(点击绑定已在 __init__ 里完成)。"""


class BigButton(tk.Label):
    """主按钮: 渐变 + 下方光晕(整张位图, 含文字)。"""

    def __init__(self, parent: tk.Misc, text: str, command=None, *,
                 width: int = 580, height: int = 50, radius: int = RADIUS_BTN,
                 bg: str | None = None):
        base = bg or BACKGROUND_OF(parent)
        super().__init__(parent, background=base, bd=0, cursor="hand2")
        self._bw, self._bh = int(width), int(height)
        self._radius = radius
        self._text = text
        self._command = command
        self._enabled = True
        self._render()

    def _render(self) -> None:
        if self._enabled:
            self._photo = R.button(
                self._bw, self._bh, self._radius, BTN_TOP, BTN_BOTTOM, self,
                glow=BTN_GLOW, glow_alpha=110, glow_blur=12, glow_dy=6,
                text=self._text, fg="#FFFFFF", text_px=15, bold=True)
        else:
            # 禁用: 平面褪色块, 不再有光晕(光晕会让人以为还能点)
            self._photo = R.label_rect(
                self, text=self._text, height=self._bh, radius=self._radius,
                fill=BTN_OFF_BG, fg=BTN_OFF_FG, text_px=15, bold=True,
                width=self._bw)[0]
        self.configure(image=self._photo)

    def _on_click(self, _e=None) -> None:
        if self._enabled and self._command is not None:
            try:
                self._command()
            except Exception:  # noqa: BLE001
                pass

    def set_enabled(self, on: bool) -> None:
        on = bool(on)
        if on == self._enabled:
            self._relayout_cursor()
            return
        self._enabled = on
        self._render()
        self._relayout_cursor()

    def _relayout_cursor(self) -> None:
        try:
            self.configure(cursor="hand2" if self._enabled else "arrow")
        except tk.TclError:
            pass

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self._render()

    def bind_click(self) -> None:
        self.bind("<Button-1>", self._on_click)


class PillButton(tk.Label):
    """幽灵按钮(顶部"状态/日志")。"""

    def __init__(self, parent: tk.Misc, text: str, command=None, *,
                 height: int = 28, bg: str | None = None):
        base = bg or BACKGROUND_OF(parent)
        super().__init__(parent, background=base, bd=0, cursor="hand2")
        photo, _w = R.label_rect(
            self, text=text, height=height, radius=8, fill=GHOST_BG,
            border=GHOST_BORDER, border_alpha=30, fg=GHOST_FG,
            text_px=12, pad_x=10)
        self._photo = photo
        self._command = command
        self.configure(image=photo)
        self.bind("<Button-1>", self._on_click)

    def _on_click(self, _e=None) -> None:
        if self._command is not None:
            try:
                self._command()
            except Exception:  # noqa: BLE001
                pass


class TextLink(tk.Label):
    """纯文字链接(次级操作"停止服务")。"""

    def __init__(self, parent: tk.Misc, text: str, command=None, *,
                 bg: str | None = None, font=None, fg: str | None = None):
        base = bg or BACKGROUND_OF(parent)
        super().__init__(parent, text=text, background=base, bd=0,
                         cursor="hand2", font=font or FONT_UI_SMALL,
                         foreground=fg or TEXT_MUTED, padx=10, pady=5)
        self._command = command
        self._enabled = True
        self._fg = fg or TEXT_MUTED
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))

    def _hover(self, on: bool) -> None:
        if not self._enabled:
            return
        try:
            self.configure(foreground=TEXT if on else self._fg)
        except tk.TclError:
            pass

    def _on_click(self, _e=None) -> None:
        if self._enabled and self._command is not None:
            try:
                self._command()
            except Exception:  # noqa: BLE001
                pass

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        try:
            self.configure(cursor="hand2" if on else "arrow",
                           foreground=self._fg if on else TEXT_FAINT)
        except tk.TclError:
            pass


# --- 状态胶囊 -----------------------------------------------------------

_STATES = {
    "run": ("RUN_BG", "RUN_FG", "RUN_DOT", "运行中"),
    "wait": ("WAIT_BG", "WAIT_FG", "WAIT_DOT", "等待主电脑"),
    "idle": ("IDLE_BG", "IDLE_FG", "IDLE_DOT", "未启动"),
}


class StatusBadge(tk.Label):
    """三态状态胶囊(圆点 + 文字), 一张位图。"""

    def __init__(self, parent: tk.Misc, state: str = "idle", *,
                 bg: str | None = None):
        base = bg or BACKGROUND_OF(parent)
        super().__init__(parent, background=base, bd=0)
        self._state = state
        self._photo = None
        self.set_state(state)

    def set_state(self, state: str, text: str | None = None) -> None:
        """state: run/wait/idle; text 覆盖胶囊文字(用于"启动失败"等异常态)。"""
        self._state = state
        bgn, fgn, dotn, tx = _STATES.get(state, _STATES["idle"])
        if text:
            tx = text
        photo, _w = R.status_pill(
            tx, self, bg=globals()[bgn], fg=globals()[fgn],
            dot=globals()[dotn], text_px=12, height=26,
            border=globals().get(bgn.replace("_BG", "_BG_BORDER")))
        self._photo = photo
        try:
            self.configure(image=photo)
        except tk.TclError:
            pass

    def set_state_text(self, state: str, text: str) -> None:
        self.set_state(state, text)


class Tag(tk.Label):
    """小标签(状态卡里的"键盘鼠标 / 音频同步")。"""

    def __init__(self, parent: tk.Misc, text: str, *, bg: str | None = None):
        base = bg or BACKGROUND_OF(parent)
        super().__init__(parent, background=base, bd=0)
        photo, _w = R.label_rect(
            self, text=text, height=22, radius=6, fill=FIELD_BG,
            fg=TEXT_MUTED, text_px=11, pad_x=8)
        self._photo = photo
        self.configure(image=photo)


# --- 开关 / 键帽 / 输入框 ----------------------------------------------

def toggle(parent: tk.Misc, on: bool = False, width: int = 44,
           height: int = 25, bg: str | None = None, command=None) -> tk.Label:
    """胶囊开关。返回带 set_on / is_on 的 Label。"""
    base = bg or BACKGROUND_OF(parent)
    lbl = tk.Label(parent, background=base, bd=0, cursor="hand2")
    state = {"on": bool(on)}

    def redraw() -> None:
        ph = R.toggle_image(width, height, state["on"], lbl,
                            on_color=TOGGLE_ON, off_color=TOGGLE_OFF)
        lbl.configure(image=ph)
        lbl._sw_photo = ph
        state["photo"] = ph

    def set_on(v: bool) -> None:
        state["on"] = bool(v)
        redraw()

    def on_click(_e=None):
        state["on"] = not state["on"]
        redraw()
        if command is not None:
            try:
                command(state["on"])
            except Exception:  # noqa: BLE001
                pass

    lbl.set_on = set_on               # type: ignore[attr-defined]
    lbl.is_on = lambda: state["on"]   # type: ignore[attr-defined]
    lbl.bind("<Button-1>", on_click)
    redraw()
    return lbl


class Keycap(tk.Label):
    """键帽(整张位图, 含文字)。"""

    def __init__(self, parent: tk.Misc, text: str, *, bg: str | None = None):
        base = bg or BACKGROUND_OF(parent)
        super().__init__(parent, background=base, bd=0)
        w = R.text_w(text, 11, True) + 14
        self._photo = R.keycap(w, 21, self, fill=KEYCAP_BG,
                               border=KEYCAP_BORDER, fg=KEYCAP_FG,
                               text=text, text_px=11)
        self.configure(image=self._photo)


class Field(tk.Frame):
    """圆角输入框(1px/无边框 + 聚焦高亮)。"""

    def __init__(self, parent: tk.Misc, textvariable: tk.Variable, *,
                 width: int = 70, height: int = 32, bg: str | None = None,
                 justify: str = "center", suffix: str | None = None):
        base = bg or BACKGROUND_OF(parent)
        super().__init__(parent, background=base, bd=0, highlightthickness=0,
                         width=width, height=height)
        self.pack_propagate(False)
        self._bw, self._bh = int(width), int(height)
        self._base = base
        self._focused = False
        self._photo = None
        self._bg_lbl = tk.Label(self, background=base, bd=0)
        self._bg_lbl.place(x=0, y=0, relwidth=1, relheight=1)
        self.entry = tk.Entry(
            self, textvariable=textvariable, bd=0, relief="flat",
            background=FIELD_BG, foreground=FIELD_FG,
            insertbackground=FIELD_FG, font=FONT_UI, justify=justify,
            highlightthickness=0, selectbackground=PRIMARY_BG,
            selectforeground=FIELD_FG)
        dw = 30 if suffix else 12
        self.entry.place(x=4, y=4, width=max(10, self._bw - dw - 6),
                         height=self._bh - 8)
        self._suffix = None
        if suffix:
            # ★★★ 2026-10-21 用户反馈: "ms 这两个字符过于贴框体的边"。
            #   根因: 单位原来贴在 x = 宽 - 18 处, 右端只剩 18-字符宽 的余量,
            #   视觉上就顶在边框上。现在给单位一个**独立右内边距**:
            #   右侧恒留 8px, 单位宽度按实际文字量出来。
            try:
                from PIL import ImageFont
                _f = ImageFont.truetype("msyh.ttc", 11)
                uw = int(_f.getbbox(suffix)[2]) + 1
            except Exception:  # noqa: BLE001
                uw = 16
            self._suffix = tk.Label(self, text=suffix, background=FIELD_BG,
                                    foreground=TEXT_FAINT, font=FONT_UI_SMALL,
                                    bd=0)
            self._suffix.place(x=self._bw - uw - 8, y=4,
                               width=uw, height=self._bh - 8)
        self.entry.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self._render()

    def _set_focus(self, on: bool) -> None:
        if on != self._focused:
            self._focused = on
            self._render()

    def _render(self) -> None:
        border = ACCENT if self._focused else FIELD_BORDER
        self._photo = R.label_rect(
            self, height=self._bh, radius=RADIUS_FIELD, fill=FIELD_BG,
            border=border, border_alpha=255, width=self._bw)[0]
        self._bg_lbl.configure(image=self._photo)

    def bind_change(self, fn) -> None:
        self.entry.bind("<Return>", lambda _e: fn())
        self.entry.bind("<FocusOut>", lambda _e: fn(), add="+")


# --- 细线 ---------------------------------------------------------------

def _canvas(parent: tk.Misc, w: int, h: int, bg: str) -> tk.Canvas:
    return tk.Canvas(parent, width=w, height=h, background=bg,
                     highlightthickness=0, bd=0)


def hairline(parent: tk.Misc, *, bg: str | None = None) -> tk.Frame:
    return tk.Frame(parent, background=bg or HAIR, height=1, bd=0,
                    highlightthickness=0)


def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    fill = kw.get("fill", CARD)
    w = max(1, int(round(x2 - x1)))
    h = max(1, int(round(y2 - y1)))
    ph = R.rounded_rect(w, h, int(r), fill, cv)
    cv._rr_photos = getattr(cv, "_rr_photos", [])
    cv._rr_photos.append(ph)
    return [cv.create_image(int(x1), int(y1), image=ph, anchor="nw")]


def status_dot(parent: tk.Misc, color: str = TEXT_FAINT, size: int = 9,
               glow: bool = False) -> tk.Label:
    base = BACKGROUND_OF(parent)
    lbl = tk.Label(parent, background=base, bd=0)
    state = {"color": color, "glow": glow}

    def redraw() -> None:
        ph = R.status_dot(size, state["color"], lbl, glow=state["glow"])
        lbl.configure(image=ph)
        lbl._dot_photo = ph

    def set_color(c: str, glow=None) -> None:
        # ★ 参数名必须叫 glow: gui_windows 是按 glow= 调用的。
        #   改名会抛 TypeError, 而状态窗口的定时刷新把它吞掉了 ——
        #   表现就是"状态窗口一行数据都读不出来"(实测踩到)。保留旧名。
        state["color"] = c
        if glow is not None:
            state["glow"] = glow
        redraw()

    lbl.set_color = set_color          # type: ignore[attr-defined]
    redraw()
    return lbl


def brand_mark(parent: tk.Misc, size: int = 26, bg: str | None = None) -> tk.Label:
    base = bg or BACKGROUND_OF(parent)
    lbl = tk.Label(parent, background=base, bd=0)
    ph = R.brand_icon(size, lbl, top=ACCENT_SOFT, bottom=ACCENT_DARK)
    lbl.configure(image=ph)
    lbl._brand_photo = ph
    return lbl


def level_bars(parent: tk.Misc, values, width: int = 110, height: int = 18,
               bg: str | None = None) -> tk.Label:
    """电平条(次级窗口用, 简化实现)。"""
    base = bg or BACKGROUND_OF(parent)
    lbl = tk.Label(parent, background=base, bd=0)
    n = max(1, len(values))
    bar_w = max(2, (width - 2 * (n - 1)) // n)
    lbl.update_bars = lambda _v: None   # type: ignore[attr-defined]
    lbl._bars_photo = R.label_rect(
        parent, text="", height=height, width=width,
        fill=CARD, pad_x=0)[0]
    lbl.configure(image=lbl._bars_photo)
    _ = bar_w
    return lbl


def card(parent: tk.Misc, title: str | None = None,
         padding: int = PAD_L) -> tuple[tk.Frame, tk.Frame]:
    """兼容旧接口(次级窗口用): 白底 + 细边。"""
    outer = tk.Frame(parent, background=CARD,
                     highlightbackground=CARD_BORDER,
                     highlightthickness=1, bd=0)
    inner = tk.Frame(outer, background=CARD, padx=padding, pady=padding)
    inner.pack(fill="both", expand=True)
    if title:
        tk.Label(inner, text=title, background=CARD, foreground=TEXT,
                 font=FONT_SUBTITLE, anchor="w").pack(fill="x",
                                                      pady=(0, PAD_S))
    return outer, inner


def heading(parent: tk.Misc, text: str, sub: str | None = None) -> tk.Frame:
    bg = BACKGROUND_OF(parent)
    f = tk.Frame(parent, background=bg)
    tk.Label(f, text=text, background=bg, foreground=TEXT,
             font=FONT_TITLE, anchor="w").pack(fill="x")
    if sub:
        tk.Label(f, text=sub, background=bg, foreground=TEXT_MUTED,
                 font=FONT_UI_SMALL, anchor="w").pack(fill="x", pady=(2, 0))
    return f


# 启动时按默认浅色初始化
set_mode("light")
