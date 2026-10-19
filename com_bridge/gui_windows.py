# -*- coding: utf-8 -*-
"""次级窗口: 实时状态 / 日志。

## 为什么从主界面移出来(用户要求)
"UI 实时状态和日志从主 UI 上移除, 归入次级窗口。通过按钮打开后
 弹出窗口显示实时状态或日志。"

主界面的职责应该只有一件事: **告诉我现在是什么状态、让我启动/停止**。
把五行实时数据和一大块日志塞在主界面里, 会让核心操作(启动/停止)
被淹没 —— 那正是"毛坯感"的主要来源。

## 设计
* 两个独立 Toplevel: 状态窗 / 日志窗, 可同时打开, 各自可最小化
* 状态窗用"指标行 + 状态圆点"的排布, 比纯文本行更易扫读
* 日志窗保留深色背景(等宽字体), 支持自动滚动与"暂停跟随"
* 窗口关闭只是隐藏(不销毁), 下次打开保持原位置与内容
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

from . import ui_theme as T


class _BaseWindow:
    """次级窗口基类: 单例 + 关闭只隐藏。"""

    title = "Com Bridge"
    geometry = "640x420"

    def __init__(self, master: tk.Misc, app) -> None:
        self.app = app
        self.master = master
        self.win: tk.Toplevel | None = None
        self._pos: str | None = None

    # -- 生命周期 ------------------------------------------------------

    def show(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            try:
                self.win.focus_force()
            except tk.TclError:
                pass
            # ★★★ 重开窗口必须重启刷新定时器(用户实测 BUG:
            #   "实时状态在运行一段时间后会卡死不再更新")。
            #   hide() 会取消 tick 链(hide_children), 而 show() 原来
            #   只做 deiconify —— 定时器一旦取消就永远没人再排,
            #   窗口重开后界面冻结在旧值。子类实现 on_reopen()
            #   以恢复自己的循环; 无循环的子类不实现即可。
            self.on_reopen()
            return
        self._create()

    def on_reopen(self) -> None:
        """窗口从隐藏中重新显示时恢复刷新循环(子类按需覆盖)。"""

    def _create(self) -> None:
        w = tk.Toplevel(self.master)
        w.title(self.title)
        w.geometry(self._pos or self.geometry)
        w.minsize(480, 320)
        w.configure(background=T.BG)
        w.protocol("WM_DELETE_WINDOW", self.hide)
        self.win = w
        self.build(w)
        # 记住窗口位置, 下次打开还原
        w.bind("<Configure>", self._remember_pos)

    def _remember_pos(self, _ev=None) -> None:
        try:
            if self.win is not None and not self.win.state() == "iconic":
                self._pos = self.win.geometry()
        except tk.TclError:
            pass

    def hide(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.hide_children()
            self.win.withdraw()

    def hide_children(self) -> None:
        """子类可覆盖: 关闭时停掉自己的定时器。"""

    def destroy(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.hide_children()
            self.win.destroy()
        self.win = None

    @property
    def visible(self) -> bool:
        try:
            return (self.win is not None and self.win.winfo_exists()
                    and self.win.state() != "withdrawn")
        except tk.TclError:
            return False

    def build(self, w: tk.Toplevel) -> None:
        raise NotImplementedError


class StatusWindow(_BaseWindow):
    """实时状态窗口: 一行一个指标, 带状态圆点。"""

    title = "Com Bridge · 实时状态"
    geometry = "680x460"

    # (key, 分组标题, 显示名)
    FIELDS = (
        ("connection", "连接", "连接"),
        ("audio", "音频链路", "音频"),
        ("buffer", "音频链路", "缓冲/延迟"),
        ("input", "输入链路", "键鼠"),
        ("clock", "输入链路", "时钟"),
        ("hook", "输入链路", "键盘钩子"),
    )

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, app)
        self._rows: dict[str, tk.Label] = {}
        self._dots: dict[str, tk.Label] = {}
        self._timer: str | None = None

    def build(self, w: tk.Toplevel) -> None:
        outer = tk.Frame(w, background=T.BG, padx=T.PAD_L, pady=T.PAD_L)
        outer.pack(fill="both", expand=True)

        # 顶栏: 标题 + 刷新提示
        top = tk.Frame(outer, background=T.BG)
        top.pack(fill="x", pady=(0, T.PAD_M))
        tk.Label(top, text="实时状态", background=T.BG, foreground=T.TEXT,
                 font=T.FONT_TITLE).pack(side="left")
        self._updated = tk.Label(top, text="", background=T.BG,
                                 foreground=T.TEXT_FAINT,
                                 font=T.FONT_UI_TINY)
        self._updated.pack(side="right")

        # 卡片: 按分组排布
        current_group = None
        group_inner = None
        for key, group, label in self.FIELDS:
            if group != current_group:
                current_group = group
                card, group_inner = T.card(outer, title=group)
                card.pack(fill="x", pady=(0, T.PAD_S))
            row = tk.Frame(group_inner, background=T.CARD)
            row.pack(fill="x", pady=2)
            dot = tk.Label(row, text="●", background=T.CARD,
                           foreground=T.TEXT_FAINT,
                           font=("Segoe UI Symbol", 11))
            dot.pack(side="left", padx=(0, T.PAD_S))
            tk.Label(row, text=label, background=T.CARD,
                     foreground=T.TEXT_MUTED, font=T.FONT_UI_SMALL,
                     width=10, anchor="w").pack(side="left")
            val = tk.Label(row, text="-", background=T.CARD,
                           foreground=T.TEXT, font=T.FONT_UI_SMALL,
                           anchor="w", justify="left")
            val.pack(side="left", fill="x", expand=True)
            self._rows[key] = val
            self._dots[key] = dot

        # 底部按钮
        btns = tk.Frame(outer, background=T.BG)
        btns.pack(fill="x", pady=(T.PAD_S, 0))
        ttk.Button(btns, text="关闭", style="Secondary.TButton",
                   command=self.hide).pack(side="right")

        self._tick()

    def _tick(self) -> None:
        if not self.visible:
            self._timer = None
            return
        try:
            self.refresh()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._timer = self.win.after(500, self._tick)
        except tk.TclError:
            self._timer = None

    def hide_children(self) -> None:
        if self._timer is not None and self.win is not None:
            try:
                self.win.after_cancel(self._timer)
            except tk.TclError:
                pass
        self._timer = None

    def on_reopen(self) -> None:
        """重开时重启刷新循环(BUG 修复: hide 取消后无人再排)。"""
        if self._timer is None:
            self._tick()

    def refresh(self) -> None:
        """从 app 拉取最新状态并刷新显示。"""
        data = self.app.collect_status()
        for key, _group, _label in self.FIELDS:
            item = data.get(key)
            if item is None:
                continue
            text, level = item if isinstance(item, tuple) else (str(item), 0)
            lbl = self._rows.get(key)
            if lbl is not None:
                lbl.configure(text=text)
            dot = self._dots.get(key)
            if dot is not None:
                color = {0: T.TEXT_FAINT, 1: T.SUCCESS,
                         2: T.WARNING, 3: T.DANGER}.get(level, T.TEXT_FAINT)
                dot.configure(foreground=color)
        try:
            self._updated.configure(
                text="更新于 " + time.strftime("%H:%M:%S"))
        except tk.TclError:
            pass


class LogWindow(_BaseWindow):
    """日志窗口: 深色等宽 + 自动跟随。"""

    title = "Com Bridge · 日志"
    geometry = "820x520"

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, app)
        self._text: tk.Text | None = None
        self._follow = tk.BooleanVar(value=True)
        self._timer: str | None = None
        self._pending: list[str] = []

    def build(self, w: tk.Toplevel) -> None:
        outer = tk.Frame(w, background=T.BG, padx=T.PAD_L, pady=T.PAD_L)
        outer.pack(fill="both", expand=True)

        top = tk.Frame(outer, background=T.BG)
        top.pack(fill="x", pady=(0, T.PAD_S))
        tk.Label(top, text="运行日志", background=T.BG, foreground=T.TEXT,
                 font=T.FONT_TITLE).pack(side="left")
        ttk.Checkbutton(top, text="自动跟随", variable=self._follow,
                        style="Card.TCheckbutton"
                        if "Card.TCheckbutton" in
                        ttk.Style(w).map("TCheckbutton")
                        else "TCheckbutton").pack(side="right")
        ttk.Button(top, text="清空", style="Secondary.TButton",
                   command=self.clear).pack(side="right", padx=T.PAD_S)

        frame = tk.Frame(outer, background=T.LOG_BG,
                         highlightbackground=T.BORDER, highlightthickness=1)
        frame.pack(fill="both", expand=True)
        self._text = tk.Text(frame, wrap="none", state="disabled",
                             font=T.FONT_MONO, background=T.LOG_BG,
                             foreground=T.LOG_FG, insertbackground=T.LOG_FG,
                             borderwidth=0, padx=10, pady=8)
        sb = ttk.Scrollbar(frame, command=self._text.yview)
        self._text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)

        btns = tk.Frame(outer, background=T.BG)
        btns.pack(fill="x", pady=(T.PAD_S, 0))
        ttk.Button(btns, text="关闭", style="Secondary.TButton",
                   command=self.hide).pack(side="right")

        self._tick()

    @property
    def text_widget(self) -> tk.Text | None:
        return self._text

    def append(self, line: str) -> None:
        """由主窗口投递一行日志(窗口不可见时缓存, 打开后补上)。"""
        if self._text is None or not self.visible:
            self._pending.append(line)
            if len(self._pending) > 4000:
                del self._pending[:2000]
            return
        self._insert(line)

    def _insert(self, line: str) -> None:
        t = self._text
        if t is None:
            return
        try:
            t.configure(state="normal")
            t.insert("end", line + "\n")
            # 限制总行数, 防内存无限增长
            if int(t.index("end-1c").split(".")[0]) > 5000:
                t.delete("1.0", "1000.0")
            if self._follow.get():
                t.see("end")
            t.configure(state="disabled")
        except tk.TclError:
            pass

    def clear(self) -> None:
        self._pending.clear()
        if self._text is not None:
            try:
                self._text.configure(state="normal")
                self._text.delete("1.0", "end")
                self._text.configure(state="disabled")
            except tk.TclError:
                pass

    def _tick(self) -> None:
        if not self.visible:
            self._timer = None
            return
        # 补上窗口关闭期间缓存的日志
        if self._pending:
            batch = self._pending
            self._pending = []
            for line in batch[-1500:]:
                self._insert(line)
        try:
            self._timer = self.win.after(300, self._tick)
        except tk.TclError:
            self._timer = None

    def hide_children(self) -> None:
        if self._timer is not None and self.win is not None:
            try:
                self.win.after_cancel(self._timer)
            except tk.TclError:
                pass
        self._timer = None

    def on_reopen(self) -> None:
        """重开时重启补日志循环(hide 取消后无人再排, 同 StatusWindow)。"""
        if self._timer is None:
            self._tick()
