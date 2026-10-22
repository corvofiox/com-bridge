# -*- coding: utf-8 -*-
"""次级窗口: 实时状态 / 日志。

## 为什么从主界面移出来(用户要求)
"UI 实时状态和日志从主 UI 上移除, 归入次级窗口。通过按钮打开后弹出窗口
显示实时状态或日志。" 主界面的职责只有两件: 告诉我现在的状态、让我启动
或停止。把五行实时数据和一大块日志塞进主界面, 核心操作会被淹没。

## v6 重做(2026-10-21)
早先这两个窗口是"逐行 Label + 系统 ttk 按钮", 与主界面 v6 的卡片语言
完全脱节 —— 用户直接指出"次级菜单不仅没变化"。现在统一到主界面的
一套原语: 圆角卡片 / 图标徽章 / 分段与胶囊控件 / 细线分隔。

★ 两个窗口都**按打开时的主题**构建。主窗口切换主题时会先把它们销毁
  (见 gui._rebuild_ui), 下次打开就是新配色 —— 不做"运行时重着色",
  因为 tkinter 里逐控件改色反而是最容易漏掉一半的地方。
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

from . import ui_theme as T


class _BaseWindow:
    """次级窗口基类: 单例 + 关闭只隐藏。"""

    title = "Com Bridge"
    geometry = "640x420"      # 设计稿尺寸, 实例化时按界面缩放换算

    @staticmethod
    def _scale_geometry(spec: str) -> str:
        """把 "640x420" 按界面缩放换算成实际像素。"""
        try:
            from . import ui_theme as T
            w, h = spec.lower().split("x")
            return f"{T.sp(int(w))}x{T.sp(int(h))}"
        except Exception:  # noqa: BLE001
            return spec

    def __init__(self, master: tk.Misc, app) -> None:
        self.app = app
        self.master = master
        self.win: tk.Toplevel | None = None
        self._pos: str | None = None

    # -- 生命周期 ------------------------------------------------------

    def show(self) -> None:
        try:
            alive = self.win is not None and bool(self.win.winfo_exists())
        except tk.TclError:
            alive = False
        if alive:
            self.win.deiconify()
            self.win.lift()
            try:
                self.win.focus_force()
            except tk.TclError:
                pass
            # ★★★ 重开窗口必须重启刷新定时器(用户实测 BUG:
            #   "实时状态在运行一段时间后会卡死不再更新")。
            #   hide() 会取消 tick 链, 而 show() 原来只做 deiconify ——
            #   定时器一旦取消就永远没人再排, 窗口重开后冻结在旧值。
            self.on_reopen()
            return
        self._create()

    def on_reopen(self) -> None:
        """窗口从隐藏中重新显示时恢复刷新循环(子类按需覆盖)。"""

    def _create(self) -> None:
        w = tk.Toplevel(self.master)
        w.title(self.title)
        w.geometry(self._pos or self._scale_geometry(self.geometry))
        w.minsize(480, 320)
        w.configure(background=T.BG)
        w.protocol("WM_DELETE_WINDOW", self.hide)
        self.win = w
        self.build(w)
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
        try:
            if self.win is not None and self.win.winfo_exists():
                self.hide_children()
                self.win.destroy()
        except tk.TclError:
            pass
        self.win = None

    @property
    def visible(self) -> bool:
        try:
            return (self.win is not None and bool(self.win.winfo_exists())
                    and self.win.state() != "withdrawn")
        except tk.TclError:
            return False

    def build(self, w: tk.Toplevel) -> None:
        raise NotImplementedError


class StatusWindow(_BaseWindow):
    """实时状态窗口: 分组卡片 + 一行一个指标(圆点 + 名称 + 数值)。"""

    title = "Com Bridge · 实时状态"
    geometry = "720x560"

    # (key, 分组标题, 显示名)
    FIELDS = (
        ("connection", "连接", "连接"),
        ("audio", "音频链路", "音频"),
        ("buffer", "音频链路", "缓冲 / 延迟"),
        ("input", "输入链路", "键鼠"),
        ("clock", "输入链路", "时钟"),
        ("hook", "输入链路", "键盘钩子"),
    )

    _LEVEL_COLOR = {0: "#98A2B3", 1: "#12B76A", 2: "#F59E0B", 3: "#E5484D"}

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, app)
        self._rows: dict[str, tk.Label] = {}
        self._dots: dict[str, tk.Label] = {}
        self._timer: str | None = None

    def build(self, w: tk.Toplevel) -> None:
        page = tk.Frame(w, background=T.BG, padx=T.PAGE_PAD, pady=T.sp(16))
        page.pack(fill="both", expand=True)

        # ── 标题栏 ─────────────────────────────────────────────────
        top = tk.Frame(page, background=T.BG)
        top.pack(fill="x", pady=(0, T.sp(14)))
        title_box = tk.Frame(top, background=T.BG)
        title_box.pack(side="left")
        tk.Label(title_box, text="实时状态", background=T.BG,
                 foreground=T.TEXT, font=T.FONT_TITLE,
                 padx=0).pack(anchor="w")
        self._updated = tk.Label(title_box, text="正在读取…", background=T.BG,
                                 foreground=T.TEXT_FAINT,
                                 font=T.FONT_UI_SMALL, padx=0)
        self._updated.pack(anchor="w", pady=(2, 0))
        T.PillButton(top, "关闭", self.hide, bg=T.BG).pack(side="right")

        # ── 分组卡片 ───────────────────────────────────────────────
        current_group = None
        group_body = None
        first_in_group = False
        for key, group, label in self.FIELDS:
            if group != current_group:
                current_group = group
                first_in_group = True
                wrap = tk.Frame(page, background=T.BG)
                wrap.pack(fill="x", pady=(0, T.sp(6)))
                card = T.Card(wrap, bg=T.BG)
                card.pack(fill="x")
                group_body = card.body
                head = tk.Frame(group_body, background=T.CARD)
                head.pack(fill="x", padx=T.CARD_PAD_X, pady=(T.sp(12), T.sp(6)))
                tk.Label(head, text=group, background=T.CARD,
                         foreground=T.TEXT_FAINT, font=T.FONT_UI_SMALL,
                         padx=0).pack(side="left")
            row = tk.Frame(group_body, background=T.CARD, height=T.sp(38))
            row.pack(fill="x")
            row.pack_propagate(False)
            if not first_in_group:
                tk.Frame(row, background=T.HAIR, height=1,
                         bd=0).pack(fill="x")
            first_in_group = False
            holder = tk.Frame(row, background=T.CARD)
            holder.pack(fill="both", expand=True, padx=T.CARD_PAD_X)

            dot = T.status_dot(holder, color=T.TEXT_FAINT, size=7,
                               glow=False)
            dot.pack(side="left", padx=(0, T.sp(10)))
            # ★ 固定宽度的名称列: 用 Frame + pack_propagate(False) 撑出
            #   像素级宽度, 中文用 Label 的 width(字符数) 不可靠。
            name_box = tk.Frame(holder, background=T.CARD, width=T.sp(96),
                                height=T.sp(18))
            name_box.pack(side="left")
            name_box.pack_propagate(False)
            tk.Label(name_box, text=label, background=T.CARD,
                     foreground=T.TEXT_MUTED, font=T.FONT_UI_SMALL,
                     anchor="w", padx=0).pack(fill="both", expand=True)
            val = tk.Label(holder, text="—", background=T.CARD,
                           foreground=T.TEXT, font=T.FONT_UI_SMALL,
                           anchor="w", justify="left", padx=0)
            val.pack(side="left", fill="x", expand=True)
            self._rows[key] = val
            self._dots[key] = dot

        # ── 底部说明 ───────────────────────────────────────────────
        foot = tk.Frame(page, background=T.BG)
        foot.pack(fill="x", pady=(6, 0))
        tk.Label(foot, text="绿色=正常 · 琥珀=需注意 · 红色=异常",
                 background=T.BG, foreground=T.TEXT_FAINT,
                 font=T.FONT_UI_TINY, padx=0).pack(side="left")

        self._tick()

    def _tick(self) -> None:
        if not self.visible:
            self._timer = None
            return
        try:
            self.refresh()
        except Exception:  # noqa: BLE001
            # ★ 这里**不能**静默吞: 曾经因为 set_color 签名不匹配抛
            #   TypeError, 被这个 except 吃掉, 界面表现为"一行数据都没有"。
            #   现在把异常写进日志区(用户能在日志窗口看到)。
            import traceback
            try:
                self.app._log_q.put_nowait(
                    "!! 状态窗口刷新失败: "
                    + traceback.format_exc(limit=2).replace("\n", " | "))
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
        """重开时重启刷新循环(hide 取消后无人再排)。"""
        if self._timer is None:
            self._tick()

    def refresh(self) -> None:
        """从 app 拉取最新状态并刷新显示。"""
        data = self.app.collect_status()
        for key, _group, _label in self.FIELDS:
            item = data.get(key)
            lbl = self._rows.get(key)
            dot = self._dots.get(key)
            if item is None:
                if lbl is not None:
                    lbl.configure(text="—", foreground=T.TEXT_FAINT)
                if dot is not None and hasattr(dot, "set_color"):
                    dot.set_color("#C6CFDA", glow=False)
                continue
            text, level = item if isinstance(item, tuple) else (str(item), 0)
            if lbl is not None:
                lbl.configure(text=text)
            if dot is not None and hasattr(dot, "set_color"):
                dot.set_color(self._LEVEL_COLOR.get(level, "#98A2B3"),
                              glow=(level in (1, 2, 3)))
        try:
            self._updated.configure(
                text="更新于 " + time.strftime("%H:%M:%S") + " · 每 0.5 秒刷新")
        except tk.TclError:
            pass


class LogWindow(_BaseWindow):
    """日志窗口: 等宽字体 + 级别着色 + 自动跟随。"""

    title = "Com Bridge · 日志"
    geometry = "880x600"

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, app)
        self._text: tk.Text | None = None
        self._follow = tk.BooleanVar(value=True)
        self._timer: str | None = None
        self._pending: list[str] = []

    def build(self, w: tk.Toplevel) -> None:
        page = tk.Frame(w, background=T.BG, padx=T.PAGE_PAD, pady=16)
        page.pack(fill="both", expand=True)

        # ── 标题栏 ─────────────────────────────────────────────────
        top = tk.Frame(page, background=T.BG)
        top.pack(fill="x", pady=(0, 12))
        title_box = tk.Frame(top, background=T.BG)
        title_box.pack(side="left")
        tk.Label(title_box, text="运行日志", background=T.BG,
                 foreground=T.TEXT, font=T.FONT_TITLE,
                 padx=0).pack(anchor="w")
        self._hint = tk.Label(title_box, text="", background=T.BG,
                              foreground=T.TEXT_FAINT,
                              font=T.FONT_UI_SMALL, padx=0)
        self._hint.pack(anchor="w", pady=(2, 0))

        T.PillButton(top, "关闭", self.hide, bg=T.BG).pack(side="right")
        T.PillButton(top, "清空", self.clear,
                     bg=T.BG).pack(side="right", padx=(6, 0))
        follow = tk.Frame(top, background=T.BG)
        follow.pack(side="right", padx=(0, 10))
        tk.Label(follow, text="自动跟随", background=T.BG,
                 foreground=T.TEXT_MUTED, font=T.FONT_UI_SMALL).pack(
            side="left", padx=(0, 8))
        T.toggle(follow, on=True, width=38, height=22, bg=T.BG,
                 command=lambda on: self._follow.set(bool(on))).pack(
            side="left")

        # ── 日志卡片 ───────────────────────────────────────────────
        wrap = tk.Frame(page, background=T.BG)
        wrap.pack(fill="both", expand=True)
        card = T.Card(wrap, bg=T.BG)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card.body, background=T.CARD)
        inner.pack(fill="both", expand=True, padx=12, pady=10)

        self._text = tk.Text(
            inner, wrap="none", state="disabled", font=T.FONT_MONO,
            background=T.CARD, foreground=T.TEXT,
            insertbackground=T.TEXT, borderwidth=0, highlightthickness=0,
            padx=2, pady=2, selectbackground=T.PRIMARY_BG,
            selectforeground=T.TEXT)
        # ★ 按级别着色: 用 Text 的 tag, 这样一行出错不影响其它行
        self._text.tag_configure("err", foreground=T.DANGER)
        self._text.tag_configure("warn", foreground=T.WARNING)
        self._text.tag_configure("ok", foreground=T.SUCCESS)
        self._text.tag_configure("dim", foreground=T.TEXT_FAINT)
        sb = ttk.Scrollbar(inner, command=self._text.yview)
        self._text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)

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
        """插入一行日志并按内容着色。"""
        t = self._text
        if t is None:
            return
        try:
            tag = None
            if any(k in line for k in ("!!", "失败", "错误", "异常", "Error")):
                tag = "err"
            elif any(k in line for k in ("警告", "超时", "断口", "重连", "WARN")):
                tag = "warn"
            elif any(k in line for k in ("通过", "就绪", "完成", "已连接", "OK")):
                tag = "ok"
            t.configure(state="normal")
            if tag:
                t.insert("end", line + "\n", tag)
            else:
                t.insert("end", line + "\n")
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
        if self._pending:
            batch = self._pending
            self._pending = []
            for line in batch[-1500:]:
                self._insert(line)
        try:
            n = 0
            if self._text is not None:
                n = int(self._text.index("end-1c").split(".")[0])
            self._hint.configure(text=f"共 {n} 行 · 超过 5000 行自动丢弃最早部分")
        except tk.TclError:
            pass
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
        """重开时重启补日志循环(hide 取消后无人再排)。"""
        if self._timer is None:
            self._tick()
