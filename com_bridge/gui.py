"""GUI 界面(tkinter)。

为什么用 tkinter: 内置于 Python, 不新增重依赖, 单文件 exe 只增 ~6MB;
PySide6 会让 exe 涨到 100MB+。定位是"配置 + 状态监视"窗口, 不追求炫。

线程模型:
  * tkinter 必须占主线程;
  * 角色对象(Server/Agent)在后台线程启动/停止 —— Agent.start() 里有
    发现(最多 discover_timeout_s)与对时(约 1s)的阻塞调用, 放主线程会
    冻住界面;
  * 状态刷新用 after() 轮询 role.stats, 日志用 stdout 重定向到队列。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import ttk

from .agent import Agent
from .clock import now_us
from .server import Server

SIDES = ["left", "right", "top", "bottom"]
SIDE_ZH = {"left": "左侧", "right": "右侧", "top": "上方", "bottom": "下方"}
_ROLE_ZH = {"server": "主电脑", "agent": "副电脑"}


class _NullWriter:
    """窗口化 exe(PyInstaller --noconsole)里 sys.stdout / sys.stderr 是 None。

    此时任何 print() 都会抛 AttributeError。而 print 散布在 server/agent/audio
    各处, 逐个加判断不现实, 所以启动 GUI 时先把 None 换成这个空写入器。
    """

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def _ensure_std_streams() -> None:
    """把 None 的 stdout/stderr 换成空写入器。

    必须在创建 _TeeStdout 之前调用, 否则 tee 会拿到 None 并在第一次 print
    时抛 AttributeError, 进而让后台启动线程静默死亡、界面永远停在"启动中"。
    """
    if sys.stdout is None:
        sys.stdout = _NullWriter()
    if sys.stderr is None:
        sys.stderr = _NullWriter()


class _TeeStdout:
    """把 print 同时写到原 stdout 与队列, 供 GUI 日志区显示。"""

    def __init__(self, original, sink: "queue.Queue[str]"):
        self._original = original
        self._sink = sink
        self._buf = ""

    def write(self, text: str) -> int:
        # original 可能为 None(窗口化 exe), 这里必须容错
        if self._original is not None:
            try:
                self._original.write(text)
            except (AttributeError, ValueError, OSError):
                pass
        self._buf += text
        while "\n" in self._buf:
            line, _, self._buf = self._buf.partition("\n")
            if line.strip():
                try:
                    self._sink.put_nowait(line)
                except queue.Full:
                    pass
        return len(text)

    def flush(self) -> None:
        if self._original is not None:
            try:
                self._original.flush()
            except (AttributeError, ValueError, OSError):
                pass

    def isatty(self) -> bool:  # pragma: no cover
        return False


class ComBridgeGui:
    def __init__(self) -> None:
        # 必须先补好 stdout/stderr: 窗口化 exe 里它们是 None, 后面 tee 与
        # 角色代码里的 print 都依赖它
        _ensure_std_streams()
        self.root = tk.Tk()
        self.root.title("Com Bridge — 双机键鼠/音频同步")
        self.root.geometry("640x480")
        self.root.minsize(560, 420)

        self._role: Server | Agent | None = None
        self._role_thread: threading.Thread | None = None
        self._stop_thread: threading.Thread | None = None
        # 看门狗: 记录"启动中/停止中"开始的时刻。后台线程若因任何原因
        # 静默死亡(未捕获异常、卡死), 界面不该永久停在过渡态。
        self._pending_since: float | None = None
        self._pending_kind: str | None = None
        self._log_q: "queue.Queue[str]" = queue.Queue(maxsize=2000)
        self._tee = _TeeStdout(sys.stdout, self._log_q)
        self._orig_stdout = sys.stdout
        self._ui_queue: "queue.Queue[tuple]" = queue.Queue()
        # 焦点监控/窗口样式(见 _release_focus_for_hook): 窗口在前台时
        # Windows 不会把键盘事件交给本进程的低级键盘钩子 -> 必须保证
        # 本窗口永远不是前台窗口。
        self._closing = False
        self._focus_watch_started = False
        self._noactivate_applied = False
        self._top_hwnd = 0
        # 用户主动操作窗口后的"焦点宽限期"(见 _grant_focus_grace)。
        # 0 表示没有宽限, 监控按正常规则交还焦点。
        self._focus_grace_until = 0.0

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # ★ 用户主动操作窗口时给焦点宽限期(见 _grant_focus_grace):
        #   否则监控会在 700ms 内把他的交互打断 —— 实测表现为
        #   "点最小化后窗口无法恢复"、"点了界面没反应"。
        for seq in ("<Button-1>", "<Key>", "<FocusIn>", "<Map>"):
            try:
                self.root.bind_all(seq, self._on_user_activity, add="+")
            except Exception:  # noqa: BLE001
                pass
        # 托盘(创建失败不影响主功能, 只是退化为"点 X 即退出")
        self._tray = None
        self._hide_hinted = False
        # ★ 日志缓冲: 日志窗口没打开时也要留住日志
        #   (否则托盘/启动报错的关键信息会被丢弃, 见 _poll 的说明)
        self._log_backlog: list[str] = []
        self._init_tray()
        # ★ 恢复上次配置 + 按"上次是否运行中"决定自动重连
        self.root.after(300, self._restore_and_maybe_autostart)
        self.root.after(100, self._poll)

    def _restore_and_maybe_autostart(self) -> None:
        """启动时恢复配置; 若上次是"运行状态下退出", 自动重连。

        用户要求: "如果程序是从启动状态退出的, 而非停止状态退出的,
                    下次程序启动时自动重连上次连接的主机IP。"

        ★ 判定完全依赖 config.was_running:
            - 用户点了"停止"再退出 -> was_running=False -> 不自动连
            - 用户直接关窗口/托盘退出/关机 -> was_running=True -> 自动连
        """
        cfg = {}
        try:
            from . import config as _cfg
            cfg = _cfg.load()
        except Exception:  # noqa: BLE001
            return
        # 恢复角色与配置项(不改配置区控件的值, 只用于决定自动启动)
        role = str(cfg.get("role") or "server")
        try:
            self._select_role(role, fire=True)
        except Exception:  # noqa: BLE001
            pass
        if not cfg.get("was_running"):
            return
        # 自动重连
        host = str(cfg.get("server_host") or "")
        if role == "agent" and not host:
            # 副机没记过 IP -> 无法自动连, 明确提示而不是静默失败
            self._log_q.put("!! 上次是在运行状态下退出的, 但没记录主电脑 IP,"
                            " 请填写后点启动")
            return
        # 先让界面把配置区建好, 再触发启动
        self.root.after(400, self._autostart_now)

    def _autostart_now(self) -> None:
        """执行自动重连(延迟一拍, 确保配置区已构建、控件可用)。"""
        try:
            from . import config as _cfg
            cfg = _cfg.load()
        except Exception:  # noqa: BLE001
            return
        if not cfg.get("was_running") or self._role is not None:
            return
        # 把记忆值写回控件, 保证 _on_start 读到的是正确参数
        try:
            if cfg.get("server_host") and getattr(self, "_host_var", None):
                self._host_var.set(str(cfg["server_host"]))
            if cfg.get("jitter_ms") and getattr(self, "_jitter_var", None):
                self._jitter_var.set(str(cfg["jitter_ms"]))
            if cfg.get("side") and getattr(self, "_side_var", None):
                self._side_var.set(str(cfg["side"]))
        except Exception:  # noqa: BLE001
            pass
        self._log_q.put("[gui] 上次为运行状态 -> 自动重连…")
        self._on_start()

    # -- 布局 ----------------------------------------------------------

    def _build(self) -> None:
        """主界面布局(现代化重设计, 2026-10-19)。

        ★ 设计原则:
          1. **主界面只做三件事**: 选角色 / 填配置 / 启动停止。
             实时状态与日志移到次级窗口(用户要求) —— 它们信息密度高,
             塞在主界面会把核心操作淹没, 那正是"毛坯感"的来源。
          2. 卡片式分区 + 统一色板(见 ui_theme), 间距用 4 的倍数。
          3. 顶部一条**状态横幅**: 圆点 + 一句话说明当前状态,
             用户一眼看到"现在到底在不在跑"(托盘图标同色同步)。
          4. 配置区用网格, 长提示放控件下方, 不参与争宽
             (此前实测: 提示变长会把 Entry 挤到只剩几像素)。
          5. 主机/副机配置不同 -> 切换角色时**重建配置区**。
        """
        from . import ui_theme as T

        self.root.title("Com Bridge")
        self.root.geometry("620x640")
        self.root.minsize(560, 580)
        T.apply_theme(self.root)

        root_bg = T.BG
        self.root.columnconfigure(0, weight=1)
        # 只让"卡片容器"占满高度, 底部固定
        self.root.rowconfigure(2, weight=1)

        # ── 顶部标题栏 ──────────────────────────────────────────────
        top = tk.Frame(self.root, background=root_bg)
        top.grid(row=0, column=0, sticky="ew", padx=T.PAD_L,
                 pady=(T.PAD_L, T.PAD_S))
        top.columnconfigure(0, weight=1)
        tk.Label(top, text="Com Bridge", background=root_bg,
                 foreground=T.TEXT, font=T.FONT_TITLE).grid(
            row=0, column=0, sticky="w")
        tk.Label(top, text="双机键鼠越界 · 音频同步", background=root_bg,
                 foreground=T.TEXT_MUTED, font=T.FONT_UI_SMALL).grid(
            row=1, column=0, sticky="w", pady=(2, 0))
        # 右上角: 状态窗口/日志窗口入口(用户要求放到按钮打开的次级窗口)
        btns_top = tk.Frame(top, background=root_bg)
        btns_top.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Button(btns_top, text="实时状态", style="Secondary.TButton",
                   command=self._open_status_window).pack(side="left",
                                                          padx=(0, T.PAD_S))
        ttk.Button(btns_top, text="日志", style="Secondary.TButton",
                   command=self._open_log_window).pack(side="left")

        # ── 状态横幅(圆点 + 文案 + 次要操作) ────────────────────────
        banner, bi = T.card(self.root, padding=T.PAD_M)
        banner.grid(row=1, column=0, sticky="ew", padx=T.PAD_L,
                    pady=(0, T.PAD_S))
        row = tk.Frame(bi, background=T.CARD)
        row.pack(fill="x")
        self._dot = tk.Label(row, text="●", background=T.CARD,
                             foreground=T.TEXT_FAINT,
                             font=("Segoe UI Symbol", 14))
        self._dot.pack(side="left", padx=(0, T.PAD_S))
        self._state_var = tk.StringVar(value="已停止")
        tk.Label(row, textvariable=self._state_var, background=T.CARD,
                 foreground=T.TEXT, font=T.FONT_UI,
                 anchor="w").pack(side="left")
        # 开机自启(静默, 不需要管理员)
        # ★ 从"管理员自启(计划任务+RunLevel Highest)"改为 HKCU Run:
        #   用户明确要求"不能加入需要用户确认的过程, 整个过程必须静默",
        #   而创建最高权限计划任务必然弹 UAC(系统安全边界)。
        self._elev_var = tk.StringVar(value="检测中…")
        self._elev_btn = ttk.Button(row, text="开机自启",
                                    style="Secondary.TButton",
                                    command=self._on_elev_toggle)
        self._elev_btn.pack(side="right")
        tk.Label(bi, textvariable=self._elev_var, background=T.CARD,
                 foreground=T.TEXT_FAINT, font=T.FONT_UI_TINY,
                 anchor="w").pack(fill="x", pady=(T.PAD_XS, 0))
        self.root.after(200, self._refresh_elev_state)

        # ── 中间可滚动区: 角色 + 配置 ───────────────────────────────
        mid = tk.Frame(self.root, background=root_bg)
        mid.grid(row=2, column=0, sticky="nsew", padx=T.PAD_L)
        mid.columnconfigure(0, weight=1)
        # 配置行不参与拉伸: 多余空间留在最底部, 卡片自然靠上排布
        mid.rowconfigure(1, weight=0)
        mid.rowconfigure(2, weight=1)

        # 角色选择卡片(两个并排, 选中态用蓝色高亮)
        role_card, role_inner = T.card(mid, title="这台电脑的角色")
        role_card.grid(row=0, column=0, sticky="ew", pady=(0, T.PAD_S))
        self._role_var = tk.StringVar(value="server")
        self._role_boxes: dict[str, tk.Frame] = {}
        self._role_labels: dict[str, tuple[tk.Label, tk.Label]] = {}
        rrow = tk.Frame(role_inner, background=T.CARD)
        rrow.pack(fill="x")
        for col, (value, title, desc) in enumerate((
            ("server", "主电脑", "接键鼠与音箱\n输出到本机声卡"),
            ("agent", "副电脑", "本机扬声器静音\n把声音送到主电脑"),
        )):
            rrow.columnconfigure(col, weight=1)
            box = tk.Frame(rrow, background=T.CARD_ALT,
                           highlightbackground=T.BORDER,
                           highlightthickness=2, cursor="hand2")
            box.grid(row=0, column=col, sticky="nsew",
                     padx=(0, T.PAD_S) if col == 0 else (T.PAD_S, 0))
            inner = tk.Frame(box, background=T.CARD_ALT,
                             padx=T.PAD_M, pady=T.PAD_M)
            inner.pack(fill="both", expand=True)
            lbl_t = tk.Label(inner, text=title, background=T.CARD_ALT,
                             foreground=T.TEXT, font=T.FONT_SUBTITLE,
                             anchor="w")
            lbl_t.pack(fill="x")
            lbl_d = tk.Label(inner, text=desc, background=T.CARD_ALT,
                             foreground=T.TEXT_MUTED, font=T.FONT_UI_TINY,
                             anchor="w", justify="left")
            lbl_d.pack(fill="x", pady=(2, 0))
            for widget in (box, inner, lbl_t, lbl_d):
                widget.bind("<Button-1>",
                            lambda _e, v=value: self._select_role(v))
            self._role_boxes[value] = box
            self._role_labels[value] = (lbl_t, lbl_d)

        # 配置区(随角色重建)
        self._cfgf = tk.Frame(mid, background=root_bg)
        self._cfgf.grid(row=1, column=0, sticky="new")
        self._cfgf.columnconfigure(0, weight=1)
        self._build_config_server()
        self._select_role(self._role_var.get(), fire=False)

        # ── 底部操作条 ──────────────────────────────────────────────
        bottom = tk.Frame(self.root, background=root_bg)
        bottom.grid(row=3, column=0, sticky="ew", padx=T.PAD_L,
                    pady=(T.PAD_S, T.PAD_L))
        bottom.columnconfigure(0, weight=1)
        self._start_btn = ttk.Button(bottom, text="启动服务",
                                     style="Primary.TButton",
                                     command=self._on_start)
        self._start_btn.grid(row=0, column=0, sticky="ew")
        self._stop_btn = ttk.Button(bottom, text="停止服务",
                                    style="Danger.TButton",
                                    command=self._on_stop, state="disabled")
        self._stop_btn.grid(row=0, column=1, sticky="e", padx=(T.PAD_S, 0))

        # 日志/状态窗口对象(首次点击时创建)
        self._status_win = None
        self._log_win = None

    # -- 角色选择(卡片高亮) ---------------------------------------------

    def _select_role(self, value: str, fire: bool = True) -> None:
        """选中角色卡片: 高亮选中项并重建配置区。"""
        from . import ui_theme as T
        self._role_var.set(value)
        for v, box in self._role_boxes.items():
            selected = (v == value)
            bg = T.PRIMARY if selected else T.CARD_ALT
            border = T.PRIMARY if selected else T.BORDER
            box.configure(background=bg, highlightbackground=border)
            for child in box.winfo_children():
                child.configure(background=bg)
                for sub in child.winfo_children():
                    try:
                        sub.configure(background=bg)
                    except tk.TclError:
                        pass
            lbl_t, lbl_d = self._role_labels[v]
            lbl_t.configure(foreground="#FFFFFF" if selected else T.TEXT)
            lbl_d.configure(foreground="#DBEAFE" if selected
                            else T.TEXT_MUTED)
        if fire:
            self._on_role_change()
        else:
            self._rebuild_config_for(value)

    def _rebuild_config_for(self, value: str) -> None:
        from . import ui_theme as T
        for w in self._cfgf.winfo_children():
            w.destroy()
        self._cfgf.columnconfigure(0, weight=1)
        self._cfgf.rowconfigure(0, weight=1)
        if value == "agent":
            self._build_config_agent()
        else:
            self._build_config_server()

    # -- 配置区构建(按角色) ---------------------------------------------

    def _clear_config(self) -> None:
        for w in self._cfgf.winfo_children():
            w.destroy()
        # 旧控件的引用必须清掉, 否则 configure 已销毁的控件会抛 TclError
        self._side_box = None
        self._side_hint = None
        self._host_entry = None
        self._jitter_entry = None

    def _build_config_server(self) -> None:
        """主电脑配置: 副电脑方位 + 延迟目标(现代化卡片版)。"""
        from . import ui_theme as T
        parent = self._cfgf
        card, inner = T.card(parent, title="主电脑配置")
        card.pack(fill="x")
        self._side_var = tk.StringVar(value="right")
        self._build_field(
            inner, row=0, label="副电脑方位",
            hint="副机自动采用这里下发的方位, 无需在副机上重复设置",
            widget_factory=lambda wrap: ttk.Combobox(
                wrap, textvariable=self._side_var, values=SIDES,
                state="readonly", width=10))
        self._side_box = self._last_field_widget
        self._jitter_var = tk.StringVar(value="60")
        self._build_field(
            inner, row=1, label="延迟目标 (ms)",
            hint="音频抖动缓冲: 越大越稳, 但延迟越高",
            widget_factory=lambda wrap: ttk.Entry(wrap, textvariable=self._jitter_var,
                                     width=10))
        self._jitter_entry = self._last_field_widget

    def _build_config_agent(self) -> None:
        """副电脑配置: 主电脑 IP + 延迟目标(现代化卡片版)。"""
        from . import ui_theme as T
        card, inner = T.card(self._cfgf, title="副电脑配置")
        card.pack(fill="x")
        inner.columnconfigure(1, weight=1)
        # ★ IP 会从配置里读回(用户要求"记忆 IP"), 所以这里取当前记忆值
        remembered = ""
        try:
            from . import config as _cfg
            remembered = str(_cfg.load().get("server_host") or "")
        except Exception:  # noqa: BLE001
            remembered = ""
        self._host_var = tk.StringVar(value=remembered)
        self._build_field(
            inner, row=0, label="主电脑 IP",
            hint="填主电脑的 IP(在主电脑上运行 ipconfig 查看); 会自动记住",
            widget_factory=lambda wrap: ttk.Entry(wrap, textvariable=self._host_var),
            wide=True)
        self._host_entry = inner.winfo_children()[-1]
        self._jitter_var = tk.StringVar(value="60")
        try:
            from . import config as _cfg
            self._jitter_var.set(str(_cfg.load().get("jitter_ms") or 60))
        except Exception:  # noqa: BLE001
            pass
        self._build_field(
            inner, row=1, label="延迟目标 (ms)",
            hint="音频抖动缓冲; 方位无需填写 —— 自动采用主电脑下发的值",
            widget_factory=lambda wrap: ttk.Entry(wrap, textvariable=self._jitter_var,
                                     width=10))
        self._jitter_entry = inner.winfo_children()[-1]

    def _build_field(self, parent, row: int, label: str, hint: str,
                     widget_factory, wide: bool = False) -> None:
        """统一的"标签 + 控件 + 下方提示"结构。

        ★ 提示必须放**控件下方独立一行**: 曾把它塞在同一行,
          提示变长时 Entry 被挤到只剩几像素(实测踩到)。
        ★ 内部套一层 Frame 并用 grid 排版, 且**控件必须建在这个 Frame 里**
          —— 不能建在 card 的 inner 上(inner 已用 pack 放标题,
          tkinter 不允许同一容器混用 pack 与 grid, 实测 TclError)。
        """
        from . import ui_theme as T
        wrap = tk.Frame(parent, background=T.CARD)
        wrap.pack(fill="x")
        wrap.columnconfigure(1, weight=1)
        tk.Label(wrap, text=label, background=T.CARD,
                 foreground=T.TEXT_MUTED, font=T.FONT_UI_SMALL,
                 anchor="w").grid(row=0, column=0, sticky="w",
                                  padx=(0, T.PAD_M), pady=(T.PAD_S, 2))
        w = widget_factory(wrap)
        if wide:
            w.grid(row=0, column=1, sticky="ew", pady=(T.PAD_S, 2))
        else:
            w.grid(row=0, column=1, sticky="w", pady=(T.PAD_S, 2))
        tk.Label(wrap, text=hint, background=T.CARD,
                 foreground=T.TEXT_FAINT, font=T.FONT_UI_TINY,
                 anchor="w", justify="left").grid(
            row=1, column=1, sticky="w", pady=(0, T.PAD_XS))
        self._last_field_widget = w

    # -- 角色切换 ------------------------------------------------------

    def _on_role_change(self) -> None:
        """角色切换: **重建**配置区(而不是改禁用状态)。

        主机需要填"方位+延迟", 副机需要填"IP+延迟" —— 需要的输入完全
        不同。各自只显示自己需要的东西, 用户就不会在副机上纠结方位框
        (它对副机毫无意义, 之前展示它直接导致配置填反的事故)。
        """
        self._rebuild_config_for(self._role_var.get())
        # ★ 记忆角色(用户切到副机后再开机, 应该还是副机)
        try:
            from . import config as _cfg
            _cfg.update(role=self._role_var.get())
        except Exception:  # noqa: BLE001
            pass

    # -- 启动/停止 -----------------------------------------------------

    def _on_start(self) -> None:
        if self._role is not None:
            return
        role_kind = self._role_var.get()
        is_agent = role_kind == "agent"
        try:
            jitter = float(self._jitter_var.get())
        except ValueError:
            self._log_q.put("!! 延迟目标必须是数字")
            return
        # ★ 方位只在主电脑上配置(它会被下发给副机); 副机的配置区里根本
        #   没有这个框(见 _build_config_agent) —— 从根上杜绝"副机填了
        #   反向的方位导致回传边算反"的配置事故。
        side = self._side_var.get() if not is_agent else None
        host = self._host_var.get().strip() if is_agent else None
        if is_agent and not host:
            self._log_q.put("!! 请先填写主电脑的 IP(在主电脑上用 ipconfig 查看)")
            self._set_state("缺少主电脑 IP")
            return
        # 切换 stdout 重定向, 让角色的 print 进日志区
        sys.stdout = self._tee
        self._set_state("启动中...")
        self._mark_pending("start")
        self._start_btn.configure(state="disabled")

        # ★ 记住本次配置(含"运行中"标志) —— 这是开机自动重连的依据。
        #   在**启动前**写: 即使启动失败, 下次也照用户意图再试一次
        #   (用户填好 IP 点启动, 就该记住这个 IP)。
        try:
            from . import config as _cfg
            _cfg.mark_running(role_kind, side or "right", jitter,
                              host or "")
        except Exception:  # noqa: BLE001
            pass

        def worker() -> None:
            role: Server | Agent | None = None
            try:
                if role_kind == "server":
                    role = Server(name="server", side=side, jitter_ms=jitter)
                else:
                    role = Agent(server_host=host, name="agent",
                                 mute_local=True, discover_timeout_s=3.0,
                                 side=side or "right")
                role.start()
            except BaseException as exc:  # noqa: BLE001
                # 这里必须用 BaseException 并保证通知一定会发出:
                # 窗口化 exe 下任何未被捕获的异常都会让本线程静默死亡,
                # 界面就永久停在"启动中..."。异常信息也要兜住(不能只靠
                # traceback.print_exc, 它在 stdout 异常时会二次抛错)。
                detail = f"{type(exc).__name__}: {exc}"
                try:
                    traceback.print_exc()
                except BaseException:
                    pass
                try:
                    self._log_q.put_nowait("!! 启动失败: " + detail)
                except queue.Full:
                    pass
                # 半启动的角色要收拾干净, 否则端口/COM 对象会残留
                if role is not None:
                    try:
                        role.stop()
                    except BaseException:
                        pass
                self._ui_queue.put(("error", detail))
                return
            self._role = role
            self._ui_queue.put(("started", role_kind))

        self._role_thread = threading.Thread(target=worker, daemon=True,
                                             name="RoleStarter")
        self._role_thread.start()

    def _release_focus_for_hook(self) -> None:
        """★★★ 让 GUI 窗口**主动放弃焦点** —— 这是键盘钩子能工作的前提。

        实测根因(tools/exp_window_vs_hook_precise.py, 100% 稳定复现):
            无窗口                -> kbd_calls+8  ✅
            窗口隐藏              -> kbd_calls+8  ✅
            窗口显示(无焦点)      -> kbd_calls+8  ✅
            窗口获得焦点          -> kbd_calls+0  ❌ 回调永久失效
            焦点转移给别的窗口    -> kbd_calls+6  ✅ 立刻恢复
            有焦点时重装钩子      -> kbd_calls+0  ❌ 重装无效

        机制: 当**本进程的窗口是前台窗口**时, Windows 不会把键盘事件交给
        本进程的低级键盘钩子(前台窗口自己会收到键, 钩子被视为多余)。
        后果正是用户报的"在副机打字, 主机也同步输入"。
        这也解释了为什么 Deskflow 不受影响: 它没有窗口, 是后台服务。

        ★★★ 用户实测确认(2026-10-19 13:4x):
            "主机程序在前台, 副机打字会发送到主机的程序窗口。
             主机程序在后台, 副机打字没有发送到主机。"
           —— 焦点就是唯一开关。所以必须**持续**保证窗口不在前台,
           而不是只在启动时交一次(用户随时会点回窗口)。

        修法(三层):
          1. 把前台交给 shell(桌面/任务栏) —— 启动时先让出焦点
          2. 周期性监控: 一旦发现自己是前台, 立刻再交出去
          3. **不加 WS_EX_NOACTIVATE**(见 _apply_noactivate 的说明)

        ★★★ 为什么最终放弃了 WS_EX_NOACTIVATE(用户实测回归):
          加了该样式后, 窗口**永远无法被激活** —— 用户点最小化后
          既不能从任务栏恢复, 点窗口也拿不到焦点, 表现正是
          "GUI 完全从桌面消失、任务栏里也消失、无法再恢复"。
          实测(tools/exp_minimize_vanishing.py): 带该样式的窗口
          最小化后 ShowWindow(RESTORE) 能把 iconic 清掉, 但
          GetForegroundWindow 永远不是它 -> 用户感知为"弹不出来"。
          代价远大于收益, 因此改为"只靠监控让出焦点",
          窗口本身保持普通窗口的全部正常行为(可激活/可最小化/可恢复)。
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # 记录顶层 hwnd(监控与恢复都要用)
            if not getattr(self, "_top_hwnd", 0):
                hwnd = int(self.root.winfo_id())
                self._top_hwnd = user32.GetAncestor(hwnd, 2) or hwnd
            # ① 把前台交给 shell(桌面/任务栏), 它一定存在且不吞键
            shell = user32.GetShellWindow()
            if shell:
                user32.SetForegroundWindow(shell)
            # ② 启动周期性监控(只启动一次)
            if not getattr(self, "_focus_watch_started", False):
                self._focus_watch_started = True
                self.root.after(700, self._focus_watch_tick)
        except Exception as exc:  # noqa: BLE001
            try:
                self._log_q.put_nowait(f"[gui] 放弃焦点失败: {exc}")
            except queue.Full:
                pass

    def _apply_noactivate(self) -> None:
        """【已废弃】曾经给窗口加 WS_EX_NOACTIVATE, 现不再使用。

        ★ 为什么废弃(用户实测回归): "点最小化后 GUI 完全从桌面消失,
          任务栏中也消失, 无法再恢复"。
          实测(tools/exp_minimize_vanishing.py):
            * 普通窗口:    最小化 -> restore -> iconic=False, 正常
            * 带 NOACTIVATE: 最小化 -> restore -> iconic 清掉了, 但
              GetForegroundWindow 永远不是它 -> **用户点不出来**
          该样式让窗口彻底无法激活, 代价(无法恢复)远大于收益。
          让出焦点改由 _focus_watch_tick 在**窗口可见且非最小化**时
          主动完成, 窗口自身保持普通窗口的全部正常行为。

        保留本方法(空实现)是为了不破坏既有测试与调用点; 新代码不应调用。
        """
        return

    def _focus_watch_tick(self) -> None:
        """周期监控: 自己是前台就立刻交出去(用户可能刚点过 GUI)。

        ★ 为什么必须持续监控(用户实测): "主机程序在前台, 副机打字会发送
          到主机的程序窗口。主机程序在后台, 副机打字没有发送到主机。"
          => 只要窗口抢到前台, 钩子立刻失效。而用户随时会点 GUI 窗口,
          所以必须每 700ms 检查一次并纠正。

        ★★★ 两条**必须跳过**的情形(都是用户实测的回归):
          1. 窗口最小化 / 不可见 —— 此时它本来就不是前台, 钩子不受影响;
             若仍去抢前台, 用户点任务栏恢复时会被立刻夺走焦点,
             表现为"点了没反应/闪一下又没了"。
          2. 用户刚主动操作过窗口(点击/恢复)之后的**宽限期** ——
             用户点开 GUI 是为了用它, 这时强行把焦点交出去等于抢他的操作。
             宽限期过后再恢复保护。
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
            top = getattr(self, "_top_hwnd", 0) or int(self.root.winfo_id())
            # ① 最小化 / 不可见 -> 不干预
            if not user32.IsWindowVisible(top) or user32.IsIconic(top):
                return
            # ② 用户主动操作后的宽限期 -> 不干预
            if time.monotonic() < getattr(self, "_focus_grace_until", 0.0):
                return
            fg = user32.GetForegroundWindow()
            if fg and fg == top:
                shell = user32.GetShellWindow()
                if shell:
                    user32.SetForegroundWindow(shell)
                n = getattr(self, "_focus_kicks", 0) + 1
                self._focus_kicks = n
                if n == 1:
                    try:
                        self._log_q.put_nowait(
                            "[gui] 窗口曾抢到前台 -> 已交还焦点"
                            "(窗口在前台时键盘钩子会失效)")
                    except queue.Full:
                        pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                if not self._closing:
                    self.root.after(700, self._focus_watch_tick)
            except Exception:  # noqa: BLE001
                pass

    def _on_user_activity(self, _event=None) -> None:
        """用户与窗口交互(点击/按键/获得焦点/窗口显示) -> 给宽限期。

        ★ 只给"可见且非最小化"的窗口宽限; 最小化时无需宽限
          (监控已经跳过最小化窗口)。
        """
        try:
            if not self.root.winfo_viewable():
                return
            if self.root.state() == "iconic":
                return
            self._grant_focus_grace()
        except Exception:  # noqa: BLE001
            pass

    def _grant_focus_grace(self, seconds: float = 8.0) -> None:
        """用户主动操作窗口后, 给它一段"可保持前台"的宽限期。

        ★ 为什么需要: 用户点任务栏恢复窗口 / 点开 GUI 想看状态时,
          监控若在 700ms 内把焦点交出去, 他根本用不了界面 ——
          实测表现就是"点最小化后无法恢复"。
          宽限期让用户的操作优先; 超时后自动恢复键盘保护
          (用户此时通常已经操作完, 回到跨屏使用)。
        """
        self._focus_grace_until = time.monotonic() + seconds

    def _on_stop(self) -> None:
        role = self._role
        if role is None or self._stop_thread is not None:
            return
        self._set_state("停止中...")
        self._mark_pending("stop")
        # ★★★ 用户主动停止 -> 清掉"运行中"标志。
        #   这是"记忆启动状态"的核心语义(用户要求):
        #     从**启动状态**退出(关窗口/托盘退出/关机) -> 下次自动重连
        #     用户主动点了**停止**再退出          -> 下次不自动连
        #   所以清标志必须在"停止"这个动作里做, 而不是在窗口关闭时做。
        try:
            from . import config as _cfg
            _cfg.mark_stopped()
        except Exception:  # noqa: BLE001
            pass

        def worker() -> None:
            try:
                role.stop()
            except BaseException:  # noqa: BLE001
                try:
                    traceback.print_exc()
                except BaseException:
                    pass
            self._role = None
            self._ui_queue.put(("stopped", None))

        self._stop_thread = threading.Thread(target=worker, daemon=True,
                                             name="RoleStopper")
        self._stop_thread.start()

    # -- 开机自启(静默, 无需管理员) --------------------------------------

    def _refresh_elev_state(self) -> None:
        """刷新"开机自启"区状态。

        ★★★ 已从「计划任务 + RunLevel Highest」改为 **HKCU Run 注册表项**。
          原因(用户明确要求): "不能加入需要用户确认的过程, 整个过程必须
          静默"。创建最高权限计划任务在 Windows 上**必然**弹 UAC, 这是
          系统安全边界绕不过; 而 HKCU\\...\\Run 属于当前用户, 读写都
          不需要管理员, 也不弹任何窗口。
        ★ 顺带解决了旧实现的两个真实问题:
          1. `schtasks` 子进程查询要 1~3 秒, 曾造成界面锁死(已改为异步);
          2. 父进程缓存读到旧值 -> 注册成功了界面仍显示"未启用"。
          注册表读取是**内存级**的(微秒级), 这两个问题都自然消失。
        """
        import threading as _th

        def worker() -> None:
            enabled = False
            try:
                from . import autostart
                enabled = autostart.is_enabled()
            except Exception:  # noqa: BLE001
                pass

            def apply() -> None:
                try:
                    if enabled:
                        self._elev_var.set("开机自启: 已启用(静默启动)")
                        self._elev_btn.configure(text="关闭自启")
                    else:
                        self._elev_var.set("未启用开机自启")
                        self._elev_btn.configure(text="开机自启")
                except tk.TclError:
                    pass

            self._post_ui(apply)

        _th.Thread(target=worker, daemon=True,
                   name="AutostartQuery").start()

    def _on_elev_toggle(self) -> None:
        """按钮入口: 启用/关闭开机自启。**全程静默, 不弹 UAC**。

        ★ 用户要求: "不能加入需要用户确认的过程，整个过程必须静默"。
          注册表读写是同步且极快的(实测 <50ms), 但仍放后台线程执行 ——
          保持主线程永不阻塞的原则。
        """
        if getattr(self, "_elev_toggling", False):
            return                      # 防重复点击
        self._elev_toggling = True
        try:
            self._elev_var.set("处理中…")
        except tk.TclError:
            pass

        def worker() -> None:
            from . import autostart
            was_on = False
            try:
                was_on = autostart.is_enabled()
            except Exception:  # noqa: BLE001
                was_on = False
            if was_on:
                ok, msg = autostart.disable()
            else:
                ok, msg = autostart.enable()

            def done() -> None:
                self._elev_toggling = False
                line = f"[自启] {msg}"
                self._log_q.put(line)
                # 同时落盘: 用户报问题时这是唯一的现场证据
                try:
                    from . import gui_log
                    gui_log.write_line(line)
                except Exception:  # noqa: BLE001
                    pass
                self._refresh_elev_state()

            self._post_ui(done)

        import threading as _th
        _th.Thread(target=worker, daemon=True,
                   name="AutostartToggle").start()

    # -- 状态轮询 ------------------------------------------------------

    def _poll(self) -> None:
        # ★★★ 日志队列 -> 主窗口缓冲 -> 日志窗口
        #   踩过的坑(2026-10-19): 早先这里写的是
        #       if self._log_win is not None: self._log_win.append(line)
        #   于是**日志窗口从未打开时, 所有日志被取出来直接丢掉** ——
        #   托盘启动失败、启动报错这类关键信息全部消失, 用户报问题时
        #   无从查证(实测: 想看"托盘是否就绪"却什么都看不到)。
        #   修法: 主窗口自己保留一份有界缓冲, 日志窗口创建时灌进去。
        try:
            while True:
                line = self._log_q.get_nowait()
                self._log_backlog.append(line)
                if self._log_win is not None:
                    self._log_win.append(line)
        except queue.Empty:
            pass
        # 缓冲上限: 防止长时间运行吃内存(日志窗口本身另有 5000 行上限)
        if len(self._log_backlog) > 4000:
            del self._log_backlog[:2000]

        # UI 状态消息(来自 worker 线程)
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                self._clear_pending()
                if kind == "started":
                    self._set_state(f"运行中 · {_ROLE_ZH.get(payload, payload)}")
                    self._stop_btn.configure(state="normal")
                    self._sync_tray(True)
                    # ★★★ 主电脑启动后必须**主动放弃窗口焦点** ——
                    #   本进程窗口是前台时, Windows 不会把键盘事件交给
                    #   我们的低级键盘钩子(实测 kbd_calls 8->0, 且重装无效)。
                    #   见 _release_focus_for_hook 的完整实测数据。
                    if payload == "server":
                        self._release_focus_for_hook()
                elif kind == "stopped":
                    self._set_state("已停止")
                    self._stop_btn.configure(state="disabled")
                    self._start_btn.configure(state="normal")
                    self._stop_thread = None
                    self._sync_tray(False)
                elif kind == "error":
                    self._set_state(f"启动失败: {payload}")
                    self._start_btn.configure(state="normal")
                    self._stop_btn.configure(state="disabled")
                    self._sync_tray(False)
                    self._role_thread = None
        except queue.Empty:
            pass

        self._check_watchdog()

        role = self._role
        if role is not None:
            try:
                self._update_stats(role)
            except Exception:  # noqa: BLE001
                pass
            self._sync_tray(True)

        self.root.after(500, self._poll)

    def _mark_pending(self, kind: str) -> None:
        self._pending_kind = kind
        self._pending_since = now_us() / 1e6

    def _clear_pending(self) -> None:
        self._pending_kind = None
        self._pending_since = None

    def _check_watchdog(self) -> None:
        """过渡态超时则恢复按钮并报错, 避免界面永久卡在"启动中/停止中"。

        阈值取得比正常启动耗时宽裕(Agent 的发现 3s + 对时约 1s), 避免
        把慢启动误判为失败。
        """
        if self._pending_since is None or self._pending_kind is None:
            return
        elapsed = now_us() / 1e6 - self._pending_since
        if elapsed < 20.0:
            return
        kind = self._pending_kind
        self._clear_pending()
        self._set_state(f"{'启动' if kind == 'start' else '停止'}超时(>20s), 已复位")
        self._log_q.put_nowait(
            f"!! {kind} 超时: 后台线程可能已崩溃, 详见上方日志。"
            f"若为启动失败, 请确认端口未被占用、声卡可用。"
        )
        if kind == "start":
            self._start_btn.configure(state="normal")
            self._stop_btn.configure(state="disabled")
            self._role_thread = None
        else:
            self._stop_thread = None
            self._start_btn.configure(state="normal")
            self._stop_btn.configure(state="disabled")

    def collect_status(self) -> dict:
        """收集实时状态, 供状态窗口拉取(主界面不再显示这些)。

        返回 {key: (文本, 级别)}; 级别: 0=未知 1=正常 2=警告 3=错误。
        ★ 保留原 _update_stats 的全部判据与文案 —— 这些文案是历次排障
          积累出来的(例如"键盘吞N/回调N"能一眼看出键盘失效),
          搬窗口只改展示位置, 不改内容。
        """
        out: dict[str, tuple[str, int]] = {}
        role = self._role
        if role is None:
            return out
        if isinstance(role, Server):
            peer_connected = bool(role.peer_addr)
            out["connection"] = (
                f"已连接副电脑: {'是' if peer_connected else '否'}"
                + (f" ({role.peer_addr})" if role.peer_addr else ""),
                1 if peer_connected else 2)
            buf_ms = role._peer.buffered_ms() if role._peer else 0.0
            ratio = role._peer.ratio if role._peer else 0.0
            lost = role._audio_rx.lost if role._audio_rx else 0
            resync = role._audio_rx.resyncs if role._audio_rx else 0
            out["audio"] = (
                f"收到 {role.stats['peer_frames']} 包 / "
                f"渲染 {role.stats['rendered_frames']} 帧", 1)
            level_ok = abs(buf_ms - role.jitter_ms) < role.jitter_ms * 0.8
            out["buffer"] = (
                f"水位 {buf_ms:.0f}ms (目标 {role.jitter_ms:.0f}) / "
                f"漂移比 {ratio:.6f} / 丢包 {lost}"
                + (f" / 流重同步 {resync} 次" if resync else ""),
                1 if level_ok else 2)
            if role._listener is not None:
                outside = bool(role._edge and role._edge.is_outside)
                state = "已交给副电脑" if outside else "在本机"
                tx = role._input_tx
                txinfo = ""
                if tx is not None:
                    txinfo = f" | 已发 {getattr(tx, 'sent', 0)}"
                    if getattr(tx, "failed", 0):
                        txinfo += f" 失败 {tx.failed}"
                    txinfo += f" -> {tx.addr[0]}"
                ld = role._listener.diag()
                out["input"] = (
                    f"控制权 {state} | 原始输入 {ld['mouse']} 鼠标/"
                    f"{ld['key']} 键盘{txinfo}", 1 if outside else 0)
            hook = getattr(role, "_hook", None)
            if hook is not None:
                hd = hook.diag()
                if not hd["installed"]:
                    out["hook"] = ("!! 钩子未装(点击会落回主机)", 3)
                else:
                    calls = hd.get("kbd_calls", 0)
                    txt = (f"鼠标吞{hd['mouse_eaten']}/过{hd['mouse_passed']}"
                           f" 键盘吞{hd['kbd_eaten']}/过{hd['kbd_passed']}"
                           f"/回调{calls}")
                    lvl = 1
                    if (hd["relay"] and calls == 0
                            and hd.get("mouse_eaten", 0) > 0):
                        txt += " (窗口在前台? 会自动交还焦点)"
                        lvl = 2
                    if hd.get("kbd_hook_alive") is False:
                        txt += " !! 键盘钩子句柄已掉"
                        lvl = 3
                    if hd.get("worker_alive") is False:
                        txt += " !! 转发线程已停"
                        lvl = 3
                    out["hook"] = (txt, lvl)
            rr = role.stats.get("render_ratio")
            if rr is not None:
                out["clock"] = (
                    f"渲染比例 {rr:.6f} / 到达率 "
                    f"{role.stats.get('arrival_rate', 0):.0f} Hz", 1)
        else:
            clock = (role._control.clock
                     if getattr(role, "_control", None) else None)
            synced = bool(clock and clock.synced)
            out["connection"] = (
                f"主电脑: {role.server_host}"
                + ("" if synced else " (对时中...)"),
                1 if synced else 2)
            out["audio"] = (f"已发送 {role.stats['frames_sent']} 包 "
                            f"({role.stats['bytes_sent'] / 1e6:.1f} MB)", 1)
            out["buffer"] = ("由主电脑端负责" if synced else "-", 0)
            rx = role._input_rx
            if rx is not None:
                d = rx.diag()
                out["input"] = (
                    f"注入 {role.stats['events_injected']} / 回传 "
                    f"{role.stats.get('events_returned', 0)} 事件 | "
                    f"收到 {d['packets']} 包"
                    + (f" (来自 {d['last_from']})" if d["last_from"] else "")
                    + (f", 心跳 {d['heartbeats']}" if d["heartbeats"] else ""),
                    1 if d["packets"] else 2)
            if clock:
                out["clock"] = (
                    f"offset {clock.offset_us / 1000:+.2f}ms / "
                    f"rtt {clock.rtt_us / 1000:.2f}ms / 样本 {clock.samples}",
                    1 if synced else 2)
        return out

    def _update_stats(self, role: Server | Agent) -> None:
        """保留旧入口(兼容既有测试), 实际工作交给 collect_status。"""
        self.collect_status()

    # -- 次级窗口 / 托盘 -----------------------------------------------

    def _open_status_window(self) -> None:
        if self._status_win is None:
            from .gui_windows import StatusWindow
            self._status_win = StatusWindow(self.root, self)
        self._status_win.show()

    def _open_log_window(self) -> None:
        first_time = self._log_win is None
        if first_time:
            from .gui_windows import LogWindow
            self._log_win = LogWindow(self.root, self)
        self._log_win.show()
        # ★ 首次打开时把主窗口留存的日志灌进去 ——
        #   这些是窗口未打开期间产生的(含托盘/启动诊断), 不能丢。
        if first_time and self._log_backlog:
            for line in self._log_backlog:
                self._log_win.append(line)

    def _sync_tray(self, running: bool) -> None:
        """托盘图标跟随服务状态(用户要求: 直观分辨是否已启动)。"""
        tray = getattr(self, "_tray", None)
        try:
            role_txt = _ROLE_ZH.get(self._role_var.get(), "服务")
            if tray is not None:
                if running and self._role is not None:
                    tray.set_status(True, f"Com Bridge · {role_txt}运行中")
                else:
                    tray.set_status(False, "Com Bridge · 已停止")
            self._set_dot(running)
        except Exception:  # noqa: BLE001
            pass

    def _set_dot(self, running: bool) -> None:
        """主界面状态圆点: 绿=运行中, 灰=停止, 红=失败。"""
        from . import ui_theme as T
        dot = getattr(self, "_dot", None)
        if dot is None:
            return
        try:
            state = self._state_var.get()
            if "失败" in state:
                dot.configure(foreground=T.DANGER)
            elif running:
                dot.configure(foreground=T.SUCCESS)
            else:
                dot.configure(foreground=T.TEXT_FAINT)
        except tk.TclError:
            pass

    def _post_ui(self, fn) -> bool:
        """从**后台线程**安全地投递一个 UI 回调到主线程。

        ★ 为什么需要这层包装(实测踩到):
          程序退出/窗口销毁后, 后台线程仍可能调用 `root.after` ——
          此时 tkinter 抛 `RuntimeError: main thread is not in main loop`
          (而不是 TclError)。未捕获会让线程带着 traceback 崩掉,
          日志里出现吓人的异常, 而实际只是收尾竞态。
        返回是否成功投递。
        """
        try:
            if self._closing:
                return False
            self.root.after(0, fn)
            return True
        except (tk.TclError, RuntimeError):
            return False

    def _init_tray(self) -> None:
        """创建托盘图标(服务启动时托盘必须存在)。

        ★ 回调都来自托盘线程 —— tkinter 不是线程安全的, 必须用
          root.after(0, ...) 切回主线程, 否则会随机崩溃。
        ★ 启动后把图标状态写进日志: 用户报"托盘没图标"时, 这一行
          是唯一的现场证据(能区分"图标生成失败"与"加入托盘失败")。
        """
        from .tray import TrayIcon
        try:
            self._tray = TrayIcon(
                tooltip="Com Bridge · 已停止",
                on_show=lambda: self._post_ui(self._show_from_tray),
                on_start=lambda: self._post_ui(self._on_start),
                on_stop=lambda: self._post_ui(self._on_stop),
                on_exit=lambda: self._post_ui(self._quit_app),
            )
            ok = self._tray.start()
            detail = getattr(self._tray, "icon_detail", "")
            good = ok and getattr(self._tray, "icon_ok", False)
            msg = (f"[gui] 托盘图标已就绪 ({detail})" if good else
                   f"!! 托盘图标未就绪: 启动={ok} {detail} "
                   f"(不影响服务运行, 但无法从托盘操作)")
            self._log_q.put(msg)
            # ★ 同时写进磁盘日志: `_log_q` 只喂界面, 进程一结束就没了。
            #   用户报"托盘没图标"时, 我们需要**事后**能看到这一行。
            try:
                from . import gui_log
                gui_log.write_line(msg)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            self._tray = None
            self._log_q.put(f"!! 托盘创建失败: {exc}")
            try:
                from . import gui_log
                gui_log.write_line(f"!! 托盘创建失败: {exc}")
            except Exception:  # noqa: BLE001
                pass

    def _show_from_tray(self) -> None:
        """托盘"显示窗口": 恢复主窗口到前台。"""
        try:
            self.root.deiconify()
            self.root.lift()
            self._grant_focus_grace()
            self.root.focus_force()
        except tk.TclError:
            pass

    def _set_state(self, text: str) -> None:
        self._state_var.set(text)

    def _on_close(self) -> None:
        """点 X: **只隐藏窗口**, 程序继续在托盘运行(用户要求)。

        ★ 语义(用户明确要求):
          "按 X 关闭程序时仅关闭程序窗口, 托盘图标必须右键关闭才能
           彻底关闭程序。"
          这是托盘类程序的通行做法: 服务要持续跑, 关窗口不该把服务停掉。
        ★ 首次点 X 时提示一次"程序仍在托盘运行", 否则用户会以为退出了、
          然后再去点开始菜单重新启动一个实例(导致端口冲突)。
        """
        if getattr(self, "_tray", None) is not None:
            try:
                self.root.withdraw()
            except tk.TclError:
                pass
            if not getattr(self, "_hide_hinted", False):
                self._hide_hinted = True
                self._log_q.put(
                    "[gui] 窗口已隐藏 —— 程序仍在托盘运行(服务不受影响)。"
                    "要彻底退出: 右键托盘图标 -> 退出 Com Bridge。")
            return
        # 没有托盘(创建失败)时退化为真关闭, 否则用户无法退出程序
        self._quit_app()

    def _quit_app(self) -> None:
        """彻底退出: 停服务、清配置标志、销毁托盘与窗口。

        ★ 与"点 X"的区别: 这里必须把 was_running 置回 False 吗?
          不 —— 用户从托盘选"退出", 若当时服务在跑, 说明他希望
          **下次开机继续跑**(那是他的使用习惯)。所以只在"点了停止"
          时才清 was_running(见 _on_stop)。
        """
        self._closing = True
        role = self._role
        if role is not None:
            try:
                role.stop()
            except Exception:  # noqa: BLE001
                pass
        tray = getattr(self, "_tray", None)
        if tray is not None:
            try:
                tray.stop()
            except Exception:  # noqa: BLE001
                pass
            self._tray = None
        for win in (getattr(self, "_status_win", None),
                    getattr(self, "_log_win", None)):
            if win is not None:
                try:
                    win.destroy()
                except Exception:  # noqa: BLE001
                    pass
        try:
            from . import config as _cfg
            _cfg.mark_exit_keeping_state()
        except Exception:  # noqa: BLE001
            pass
        sys.stdout = self._orig_stdout
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> int:
        self.root.mainloop()
        return 0


def main() -> int:
    # ★ 提权子进程模式: GUI 的"管理员自启"按钮用 ShellExecuteW(runas)
    #   重新调起**本 exe** 并带 `--elevated-child install|uninstall`。
    #   此刻进程已被 UAC 提权 —— 只做计划任务注册/卸载, 写结果文件,
    #   然后立即退出(绝不进入 GUI, 提权进程不该有常驻界面)。
    argv = sys.argv[1:]
    if "--elevated-child" in argv:
        i = argv.index("--elevated-child")
        mode = argv[i + 1] if i + 1 < len(argv) else "install"
        tag = ""
        if "--elevated-tag" in argv:
            j = argv.index("--elevated-tag")
            tag = argv[j + 1] if j + 1 < len(argv) else ""
        try:
            from . import elevated
            return elevated._run_elevated_child(mode, tag)
        except BaseException as exc:  # noqa: BLE001
            try:
                sys.stderr.write(f"[elevated-child] {exc}\n")
            except BaseException:
                pass
            return 1

    # ★★ 隐藏诊断入口: 在**打包后的 GUI 进程里**自测钩子。
    #   为什么需要它(实测对比): 同一台机器上
    #       com-bridge.exe kbdcheck(console 版)  -> probe=True 可达
    #       com-bridge-gui.exe 里的 server        -> kbd_calls 恒 0
    #   差异只可能在"打包成 --noconsole GUI"这一点上。本入口让 GUI exe
    #   自己在真实条件下报告, 结果写日志文件(无控制台也能取到)。
    if "--hookdiag" in argv:
        # ★ 加 --with-mainloop 时在**真实 tkinter mainloop 里**测 ——
        #   这是唯一还没排除的差异(GUI 运行时 mainloop 一直在跑)。
        return _hookdiag_in_gui_process(with_mainloop="--with-mainloop" in argv)

    # ★ 直接跑 `python -m com_bridge.gui` 时也要声明 DPI 感知 —— 正常路径
    #   走 cli.main() 已经声明过(那里是主入口), 但这里兜底, 避免有人绕过
    #   CLI 直接启动 GUI 时坐标被系统虚拟化(高 DPI 副机上方位会偏)。
    try:
        from .input import _enable_dpi_awareness
        _enable_dpi_awareness()
    except Exception:  # noqa: BLE001
        pass

    # ★★★ 单实例检查(用户实测: "我能同时启动两个甚至多个")
    #   多实例的真实危害不只是多个窗口:
    #     * 都去 bind 24802/TCP + 4010/UDP -> 后启动的绑定失败;
    #     * 都装低级键鼠钩子 -> 事件被吞两次/转发两次, 跨屏输入重影;
    #     * 托盘里出现多个同名图标;
    #     * 都写同一份 config.json -> 状态互相覆盖。
    #   所以第二个实例必须**主动退出**, 并唤起已有窗口(符合用户双击预期)。
    from . import single_instance
    if not single_instance.acquire():
        single_instance.notify_existing()
        try:
            from . import gui_log
            gui_log.write_line(
                "[gui] 已有实例在运行 -> 已唤起其窗口, 本次启动退出")
        except Exception:  # noqa: BLE001
            pass
        return 0

    app = ComBridgeGui()
    return app.run()


def _hookdiag_in_gui_process(with_mainloop: bool = False) -> int:
    """在 GUI exe 进程里跑钩子自检, 结果写日志文件 + stdout。

    分三段, 逐段定位:
      1. 纯钩子(与 kbdcheck 相同)
      2. 完整 Server.start()
      3. Server 钩子在穿越态(relay=True + hide)

    with_mainloop=True 时整段在**真实 tkinter mainloop**里执行 —— 这是
    唯一还没排除的差异: 生产 GUI 运行时 mainloop 一直占着主线程。
    """
    from . import gui_log
    gui_log.install()        # 无控制台时把输出落到日志文件

    def report(tag: str, ok, detail: str = "") -> None:
        print(f"[hookdiag] {tag}: {ok} {detail}", flush=True)

    try:
        import time as _t

        from .hook import LowLevelHook
        from .input import InputInjector

        def measure(h, tag):
            p = h.probe_keyboard_hook()
            b = h.diag()
            InputInjector.key(0xE8, 0, True)
            InputInjector.key(0xE8, 0, False)
            _t.sleep(0.45)
            a = h.diag()
            d = a["kbd_calls"] - b["kbd_calls"]
            report(tag, f"probe={p}",
                   f"kbd_calls+{d} pump={a.get('hook_pump_age_s', -1):.2f}s")
            return p, d

        print("[hookdiag] ===== 开始 =====", flush=True)
        report("frozen", getattr(sys, "frozen", False))
        report("stdout类型", type(sys.stdout).__name__)
        report("with_mainloop", with_mainloop)

        def run_all():
            """所有测量(可能在 mainloop 回调里跑)。"""
            print("[hookdiag] --- 1. 纯钩子 ---", flush=True)
            h1 = LowLevelHook(name="Diag1", on_key_event=lambda *a: None)
            h1.start()
            _t.sleep(0.3)
            measure(h1, "纯钩子")
            h1.stop()
            _t.sleep(0.3)

            print("[hookdiag] --- 2. 完整 Server ---", flush=True)
            from .server import Server
            srv = Server(name="diag", side="right",
                         accept_synthetic_input=True)
            srv.start()
            _t.sleep(1.0)
            measure(srv._hook, "Server钩子")

            print("[hookdiag] --- 3. 穿越态(relay+hide) ---", flush=True)
            srv._edge._outside = True
            srv._edge._streak = 2
            srv._acquire_control()
            _t.sleep(0.6)
            measure(srv._hook, "穿越态")
            srv._edge._outside = False
            srv._release_control()
            _t.sleep(0.3)
            srv.stop()
            print("[hookdiag] ===== 结束 =====", flush=True)

        if with_mainloop:
            # ★ 在真实 mainloop 里跑: 主线程被 tkinter 占用, 这正是
            #   生产 GUI 的状态。钩子线程需要在这期间拿到 GIL。
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            state = {"done": False}

            def kick():
                try:
                    run_all()
                except BaseException as exc:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    print(f"[hookdiag] 异常: {exc}", flush=True)
                finally:
                    state["done"] = True
                    root.quit()

            root.after(150, kick)
            root.mainloop()
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
        else:
            run_all()
        return 0
    except BaseException as exc:  # noqa: BLE001
        import traceback
        try:
            traceback.print_exc()
        except BaseException:
            pass
        print(f"[hookdiag] 异常: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
