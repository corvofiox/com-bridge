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

from . import __version__ as _app_version
from .agent import Agent, HostWaitCancelled
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


def _fmt_num(v) -> str:
    """把配置里的数字显示成"人读的样子"。

    ★ 配置里 jitter_ms 存的是 float(60.0), 直接 str() 会让输入框显示
      "60.0" —— 这在界面上非常扎眼(用户截图反馈过)。整数值一律去掉
      小数部分, 只有真的是小数时才保留。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else str(v)


class ComBridgeGui:
    def __init__(self, start_hidden: bool = False) -> None:
        # 必须先补好 stdout/stderr: 窗口化 exe 里它们是 None, 后面 tee 与
        # 角色代码里的 print 都依赖它
        _ensure_std_streams()
        self.root = tk.Tk()
        self.root.title("Com Bridge — 双机键鼠/音频同步")
        self.root.geometry("640x480")
        self.root.minsize(560, 420)
        # ★★ 开机自启的静默启动(用户要求: "开机自动启动后不弹出 GUI 前台
        #   页面, 而是后台静默启动")。此时窗口根本不映射, 只有托盘图标 +
        #   自动拉起上次的角色; 托盘左键 -> _show_from_tray 随时叫出来。
        #   顺带一个好处: 窗口不显示就不会抢前台, 而"本进程窗口是前台"
        #   恰好是键盘低级钩子失效的唯一原因(见 _release_focus_for_hook)。
        self._start_hidden = bool(start_hidden)

        self._role: Server | Agent | None = None
        # ★ 已构造但还没 start() 完的角色(用于取消"等待主电脑"的启动)。
        #   旧实现里 _on_stop 只认 self._role, 而它要等 start() 返回才被赋值
        #   —— 于是"等待中"的启动完全无法取消。
        self._pending_role: Server | Agent | None = None
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

        # ★ 先定配色再建界面: 所有控件都在构建时从 ui_theme 的模块级
        #   常量取色, 所以模式必须在 _build() 之前确定。
        try:
            from . import ui_theme as _T
            _T.set_mode(self._resolve_mode())
        except Exception:  # noqa: BLE001
            pass

        self._build()
        # ★ 应用图标(标题栏 + 任务栏)。内嵌 base64, 零路径依赖 ——
        #   单文件 exe 解包后读文件会失效(见 tray.py 的历史注释)。
        try:
            from . import appicon
            appicon.apply(self.root)
        except Exception:  # noqa: BLE001
            pass
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
        if self._start_hidden:
            # 必须在 mainloop 之前 withdraw: 否则窗口会闪一下再消失
            try:
                self.root.withdraw()
            except tk.TclError:
                pass
            self._log_q.put("[gui] 静默启动(开机自启): 主窗口不显示, "
                            "服务在托盘运行; 左键托盘图标可随时打开窗口")
            try:
                from . import gui_log
                gui_log.write_line("[gui] 静默启动(--autostart): 窗口不显示")
            except Exception:  # noqa: BLE001
                pass
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
                # ★ 必须走 _fmt_num: 直接 str(60.0) 会让输入框显示 "60.0"
                self._jitter_var.set(_fmt_num(cfg["jitter_ms"]))
            if cfg.get("side") and getattr(self, "_side_var", None):
                self._side_var.set(str(cfg["side"]))
                grp = getattr(self, "_side_group", None)
                if grp is not None and str(cfg["side"]) in SIDES:
                    grp.set_value(str(cfg["side"]))
        except Exception:  # noqa: BLE001
            pass
        self._log_q.put("[gui] 上次为运行状态 -> 自动重连…")
        self._on_start()

    # -- 布局 ----------------------------------------------------------

    _ROLE_DESC = {
        "server": "主电脑接键盘鼠标与音箱",
        "agent": "静音本机，把声音送到主电脑",
    }

    def _build(self) -> None:
        """主界面布局(v6, 2026-10-21 用户选定 A/B 双主题)。

        ## 结构

            [标记] Com Bridge          [深色] [状态] [日志]
            ┌ 状态卡 ─────────────────────────────┐
            │ ●运行中                        61 ms │
            │ 192.168.10.12 → 192.168.10.201      │
            │ [键盘鼠标] [音频同步]                │
            └─────────────────────────────────────┘
            ┌ 设置卡 ─────────────────────────────┐
            │ [icon] 这台电脑的角色  [主电脑|副电脑] │
            │ ───────────────────────────────────  │
            │ [icon] 副电脑方位      [左|右|上|下]   │
            │ ...                                  │
            └─────────────────────────────────────┘
                          (弹性空白)
            [          启动服务           ]
                      停止服务

        ## 与 v5 的差别
        v5 是"白纸 + 1px 细线, 一个盒子都不画", 用户判定"还不如上一版"。
        v6 回到**卡片 + 层次 + 色彩**, 但用真实高斯阴影和整张位图渲染,
        避免 v4 那种灰脏边和毛刺。

        ## 两套配色
        布局与所有尺寸完全相同, 只有颜色来自 ui_theme 的模块级常量;
        切换主题时整体重建窗口(_on_toggle_theme -> _rebuild_ui)。
        """
        from . import ui_theme as T
        from . import ui_render as R

        root = self.root
        root.title("Com Bridge")
        # ★ 让 Tk 的**磅值**字体跟随界面缩放(1pt = scaling/72 像素)。
        #   我们自己的 PIL 位图字体走 ui_render.font 的 SCALE, 两条路各管一半。
        try:
            root.tk.call("tk", "scaling", 96.0 * R.SCALE / 72.0)
        except tk.TclError:
            pass
        T.apply_theme(root)
        root.configure(background=T.BG)

        # 重建前的状态: 主题切换会重跑本函数, 状态不能丢
        prev_state = (self._state_var.get()
                      if getattr(self, "_state_var", None) is not None
                      else "未启动")
        for w in root.winfo_children():
            w.destroy()
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)          # 弹性空白(仅用于撑满拉伸后的窗口)
        # 状态文本仍然保留在 StringVar 里(不直接显示, 由状态胶囊表达;
        # 但 _set_dot / _check_watchdog 等旧逻辑仍读它)
        self._state_var = tk.StringVar(value=prev_state)

        # ── 背板(浅色/深色目前都是纯色; 渐变保留能力) ────────────────
        if T.BG_GRAD:
            self._page_bg = tk.Label(root, bd=0, background=T.BG)
            self._page_bg.place(x=0, y=0, relwidth=1, relheight=1)

            def _paint_bg(ev):
                # ★ 窗口高度现在是**内容决定**的, 渐变必须按实际尺寸重铺,
                #   否则底部会露出一条没画出渐变的缝。
                w, h = max(1, ev.width), max(1, ev.height)
                self._page_photo = R.linear_gradient(w, h, T.BG_GRAD,
                                                     self._page_bg)
                self._page_bg.configure(image=self._page_photo)

            root.bind("<Configure>", _paint_bg, add="+")
        else:
            self._page_bg = None

        # 卡片容器左右留 CARD_M, 使**可见卡边**正好落在 PAGE_PAD
        wrap_pad = T.PAGE_PAD - T.CARD_M
        gap_between = T.sp(14) - T.CARD_M * 2        # pack 间距(可见卡间距 14)

        # ── 顶栏: 已合并进状态卡, 这里不再单独占一行 ────────────────

        # ── 顶部: 不再有独立一栏 ───────────────────────────────────
        # ★★★ 2026-10-21 用户反馈: "深色、状态、日志三个按钮单独一栏太空了,
        #   挪到播放暂停按钮的下边" —— 随后改主意: "把水位延迟和播放暂停
        #   往下挪, 把这三个按钮挪到水位延迟上方"。
        #   最终布局(自上而下): 三个按钮 → 延迟数字 → 播放/停止,
        #   全部放在状态卡右侧那一列, 不再独占一行。

        # ── 状态卡 ─────────────────────────────────────────────────
        hero_wrap = tk.Frame(root, background=T.BG)
        hero_wrap.grid(row=0, column=0, sticky="ew", padx=wrap_pad,
                       pady=(T.sp(16) - T.CARD_M, 0))
        hero = T.Card(hero_wrap, bg=T.BG)
        hero.pack(fill="x")
        hin = tk.Frame(hero.body, background=T.CARD)
        hin.pack(fill="x", padx=T.CARD_PAD_X, pady=T.sp(16))

        # ★★★ 2026-10-21 用户反馈: "左上角, 右下角全是大片空白, 运行中的
        #   标识完全悬空, 不能更紧凑一点么"。
        #   根因: 早先左右是两个独立容器 —— 左边一个整块(状态胶囊),
        #   右边又竖着摞了三段(按钮 / 延迟 / 播放停止)。右侧总高度远大于
        #   左侧, 于是左边顶部空、右下角也空。
        #   修法: 改成**三行网格**, 左右内容按同一行对齐:
        #        row0:  状态胶囊        |  深色/状态/日志
        #        row1:  已连接…·缓冲…   |  延迟数字
        #        row2:  键盘鼠标/音频同步 |  播放/停止
        #   这样每一行左右都等高, 卡片高度由"三行"决定而不是"左一右三"。
        grid = tk.Frame(hin, background=T.CARD)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)

        # ---- row0: 状态胶囊 | 三个入口按钮 ----
        r0 = tk.Frame(grid, background=T.CARD)
        r0.grid(row=0, column=0, sticky="ew")
        self._status = T.StatusBadge(r0, "idle", bg=T.CARD)
        self._status.pack(side="left")

        nav = tk.Frame(r0, background=T.CARD)
        nav.pack(side="right")
        theme_label = "浅色" if T.is_dark() else "深色"
        T.PillButton(nav, "日志", self._open_log_window,
                     bg=T.CARD).pack(side="right", padx=(T.sp(6), 0))
        T.PillButton(nav, "状态", self._open_status_window,
                     bg=T.CARD).pack(side="right")
        T.PillButton(nav, theme_label, self._on_toggle_theme,
                     bg=T.CARD).pack(side="right", padx=(0, T.sp(6)))

        # ---- row1: 摘要(含音频缓冲) ----
        # ★★★ 2026-10-21 用户决定: "保留左边的缓冲, 把缓冲的文本改为
        #   音频缓冲避免歧义, 右侧的大缓冲延迟移除, 它使得音频缓冲看起来
        #   像连接整体延迟"。
        #   所以右侧那个 27px 大数字去掉了 —— 它只是**音频抖动缓冲**,
        #   单独放大放在最显眼的位置, 会让人误以为是"整条链路的延迟"。
        #   现在它作为摘要的一部分, 明确标成"音频缓冲"。
        r1 = tk.Frame(grid, background=T.CARD)
        r1.grid(row=1, column=0, sticky="ew", pady=(T.sp(10), 0))
        self._hero_meta = tk.StringVar(value="选择角色后点 ▶")
        self._hero_meta_lbl = tk.Label(
            r1, textvariable=self._hero_meta, background=T.CARD,
            foreground=T.TEXT_MUTED, font=T.FONT_UI_SMALL, anchor="w",
            justify="left", padx=0)
        self._hero_meta_lbl.pack(side="left")

        # ---- row2: 标签 | 播放/停止 ----
        r2 = tk.Frame(grid, background=T.CARD)
        r2.grid(row=2, column=0, sticky="ew", pady=(T.sp(10), 0))
        tags = tk.Frame(r2, background=T.CARD)
        tags.pack(side="left", pady=(T.sp(6), 0))
        for name in ("键盘鼠标", "音频同步"):
            T.Tag(tags, name, bg=T.CARD).pack(
                side="left", padx=(0, T.sp(6)))

        ctl = tk.Frame(r2, background=T.CARD)
        ctl.pack(side="right")
        # ★ 用户指定顺序: "播放在左, 停止在右"。
        self._start_btn = T.MediaButton(ctl, "play", self._on_start,
                                        size=T.sp(28),
                                        bg=T.CARD)
        self._start_btn.pack(side="left")
        self._stop_btn = T.MediaButton(ctl, "stop", self._on_stop,
                                       size=T.sp(28),
                                       bg=T.CARD)
        self._stop_btn.pack(side="left", padx=(T.sp(10), 0))

        # ── 设置卡 ─────────────────────────────────────────────────
        set_wrap = tk.Frame(root, background=T.BG)
        set_wrap.grid(row=1, column=0, sticky="ew", padx=wrap_pad,
                      pady=(gap_between, 0))
        self._card_set = T.Card(set_wrap, bg=T.BG)
        self._card_set.pack(fill="x")
        body = self._card_set.body

        # 角色行常驻(切换角色时不能重建发起回调的控件)
        if getattr(self, "_role_var", None) is None:
            self._role_var = tk.StringVar(value="server")
        self._role_hint = tk.StringVar(
            value=self._ROLE_DESC.get(self._role_var.get(), ""))
        h1 = self._setting_row(body, "screen", 0, "这台电脑的角色",
                               self._role_hint, top_border=False)
        self._role_seg = T.Segmented(
            h1, [("server", "主电脑"), ("agent", "副电脑")],
            self._role_var.get(), command=self._select_role, bg=T.CARD)
        self._role_seg.pack(side="right")

        # 配置行(随角色重建; 容器常驻 -> pack 顺序不会乱)
        self._cfgf = tk.Frame(body, background=T.CARD)
        self._cfgf.pack(fill="x")

        # 开机自启行常驻
        self._elev_var = tk.StringVar(value="检测中…")
        h4 = self._setting_row(body, "power", 3, "开机自启",
                               self._elev_var, top_border=True)
        self._elev_sw = T.toggle(h4, on=False, bg=T.CARD,
                                 command=self._on_autostart_switch)
        self._elev_sw.pack(side="right")

        # ── 底部提示 ───────────────────────────────────────────────
        # ★ 用户要求"底边的大空白去掉": 最后一行的提示紧贴内容, 不再把
        #   窗口撑到固定高度。窗口高度由内容决定(见下方 geometry)。
        foot = tk.Frame(root, background=T.BG)
        foot.grid(row=3, column=0, sticky="ew", padx=T.PAGE_PAD,
                  pady=(T.sp(10), T.sp(12)))
        # ★ 这个标签有两个用途(审查项 #7): 平时显示提示语; 查到新版本后
        #   变成可点击的"发现新版本 vX，点此下载 ↗"。
        self._foot_hint = tk.Label(
            foot, text="运行时最小化到托盘，关闭窗口不停止服务",
            background=T.BG, foreground=T.TEXT_FAINT,
            font=T.FONT_UI_TINY, padx=0)
        self._foot_hint.pack(side="left")

        if not hasattr(self, "_status_win"):
            self._status_win = None
        if not hasattr(self, "_log_win"):
            self._log_win = None

        # 建配置区 -> 恢复状态 -> 定尺寸
        self._rebuild_config_for(self._role_var.get())
        self._restore_visuals(prev_state)
        # ★★★ 窗口尺寸 = 内容的实际需求高度。
        #   早先固定 660, 而 v9 的内容只有 ~600, 底部就空出近 60px 的
        #   白区(用户截图里指出的"底边的大空白")。现在按
        #   winfo_reqheight 设高度, 内容多少窗口就多高。
        try:
            self.root.update_idletasks()
            need = int(self.root.winfo_reqheight())
            h = max(480, need)
            self.root.geometry(f"{T.WINDOW_W}x{h}")
            self.root.minsize(T.WINDOW_W, h)
        except tk.TclError:
            pass
        self.root.after(200, self._refresh_elev_state)

        # 开机自启静默修复: 旧版注册的计划任务缺 --autostart, 登录后会弹窗口
        self._selfheal_elevated_autostart()

        # 自动更新检查(审查项 #7): 后台查一次, 有新版本就把底栏提示换成
        # 可点击的下载链接。只在**有新版本**时才动界面 —— 查不到
        # (离线/限流/GitHub 不通)完全静默, 绝不在界面上留红字。
        self._start_update_check()

    # -- 自动更新检查 --------------------------------------------------

    def _selfheal_elevated_autostart(self) -> None:
        """把旧的"会弹窗口"的管理员自启任务就地升级成静默启动。

        2026-10-26 用户报障: 开机自启弹 GUI。原因是 elevated._exe_path() 当初
        只注册了 `gui` 子命令, 没带 --autostart。**已注册的任务不会自己变**,
        所以这里做一次自愈:

        ★ 只有**本进程已是管理员**时才动手 —— 那说明我们正是被那个最高权限
          任务拉起来的, 改写任务不需要再弹 UAC; 否则(普通权限)只记一行日志,
          提示用户重新点一次「开机自启（管理员）」。
        ★ 全部在后台线程做: schtasks 查询要 ~0.2s, 不能拖慢启动。
        """
        import threading

        def run() -> None:
            try:
                from . import elevated
                silent = elevated.task_is_silent()
                if silent is None or silent is True:
                    return                      # 没有任务 / 已经是静默
                if not elevated.is_elevated():
                    self._log_q.put(
                        "[gui] 管理员自启任务是旧版登记的(会弹窗口)；"
                        "重新点一次「开机自启（管理员）」即可升级为静默")
                    return
                rc = elevated._do_register()
                if rc == 0:
                    self._log_q.put(
                        "[gui] 管理员自启任务已升级为静默启动(下次开机不再弹窗口)")
                    print("[gui] 管理员自启任务已升级: 动作参数补上 --autostart",
                          flush=True)
                else:
                    self._log_q.put(
                        f"[gui] !! 管理员自启任务升级失败(退出码 {rc})")
            except Exception:                   # noqa: BLE001
                pass

        threading.Thread(target=run, daemon=True,
                         name="SelfHealAutostart").start()

    def _start_update_check(self) -> None:
        """后台线程查一次更新(不阻塞界面, 失败静默)。"""
        import threading

        def run() -> None:
            try:
                from .update_check import check
                info = check()
            except Exception:                 # noqa: BLE001
                return
            if not info or not info.get("newer"):
                return
            try:
                self.root.after(0, lambda: self._on_update_found(info))
            except Exception:                 # noqa: BLE001
                pass

        threading.Thread(target=run, daemon=True, name="UpdateCheck").start()

    def _on_update_found(self, info: dict) -> None:
        """有新版本: 底栏换成可点击链接 + 写一行日志(不动主界面其它部分)。"""
        ver = info.get("version") or "?"
        url = info.get("url") or ""
        try:
            from . import ui_theme as T
            self._foot_hint.configure(
                text=f"发现新版本 v{ver}，点此下载 ↗",
                foreground=T.ACCENT if hasattr(T, "ACCENT") else T.TEXT,
                cursor="hand2")
            if url:
                self._foot_hint.bind(
                    "<Button-1>",
                    lambda _e: __import__("webbrowser").open(url))
        except Exception:                     # noqa: BLE001
            pass
        self._log_q.put(
            f"[gui] 发现新版本 v{ver}（当前 v{_app_version}）: {url}")

    # -- 布局小工具 ----------------------------------------------------

    def _setting_row(self, parent, icon: str, index: int, title: str,
                     sub, *, top_border: bool = True) -> tk.Frame:
        """一行设置: [图标徽章] 标题/说明 ……… 右侧控件。

        ★ 高度固定 66px: 四行等高才有节奏感; 行高交给内容撑会被
          "说明有没有换行"带歪。
        """
        from . import ui_theme as T
        row = tk.Frame(parent, background=T.CARD, height=T.sp(72))
        row.pack(fill="x")
        row.pack_propagate(False)
        if top_border:
            tk.Frame(row, background=T.HAIR, height=1, bd=0).pack(fill="x")
        holder = tk.Frame(row, background=T.CARD)
        holder.pack(fill="both", expand=True, padx=T.CARD_PAD_X)

        T.Badge(holder, icon, size=T.sp(30), index=index,
                bg=T.CARD).pack(side="left", padx=(0, T.sp(12)))
        tf = tk.Frame(holder, background=T.CARD)
        tf.pack(side="left")
        tk.Label(tf, text=title, background=T.CARD, foreground=T.TEXT,
                 font=T.FONT_UI, anchor="w", padx=0).pack(anchor="w")
        kw = ({"textvariable": sub} if isinstance(sub, tk.Variable)
              else {"text": sub})
        tk.Label(tf, **kw, background=T.CARD, foreground=T.TEXT_FAINT,
                 font=T.FONT_UI_SMALL, anchor="w", justify="left",
                 padx=0).pack(anchor="w", pady=(T.sp(2), 0))
        return holder

    # -- 主题切换 ------------------------------------------------------

    def _theme_pref(self) -> str:
        return str(self._load_cfg().get("ui_theme") or "auto")

    def _resolve_mode(self) -> str:
        """auto -> 跟随 Windows; 显式 light/dark 就用它。"""
        from . import ui_theme as T
        pref = self._theme_pref()
        if pref in ("light", "dark"):
            return pref
        return "dark" if T.system_prefers_dark() else "light"

    def _on_toggle_theme(self) -> None:
        """顶栏"深色/浅色"按钮: 直接切到相反配色并记住。"""
        from . import ui_theme as T
        new = "light" if T.current_mode() == "dark" else "dark"
        try:
            from . import config as _cfg
            _cfg.update(ui_theme=new)
        except Exception:  # noqa: BLE001
            pass
        self._rebuild_ui()

    def _rebuild_ui(self) -> None:
        """按当前主题偏好整体重建窗口(控件颜色取自模块级常量)。

        ★ 次级窗口也要重建: 它们是在"打开时的主题"下构建的, 而主题切换
          发生在另一个时刻。销毁后按原可见性重新打开, 比逐控件改色可靠。
        """
        from . import ui_theme as T
        T.set_mode(self._resolve_mode())
        was_visible: dict[str, bool] = {}
        for name in ("_status_win", "_log_win"):
            w = getattr(self, name, None)
            try:
                was_visible[name] = bool(w is not None and w.visible)
            except Exception:  # noqa: BLE001
                was_visible[name] = False
            if w is not None:
                try:
                    w.destroy()
                except Exception:  # noqa: BLE001
                    pass
        try:
            self._build()
        except tk.TclError:
            pass
        for name, vis in was_visible.items():
            w = getattr(self, name, None)
            if vis and w is not None:
                try:
                    w.show()
                except Exception:  # noqa: BLE001
                    pass

    def _restore_visuals(self, state_text: str) -> None:
        """重建后把"界面表现"恢复成运行时真实状态。

        ★ 重建会丢掉所有视觉状态(按钮可用性/状态胶囊/圆点), 所以主题
          切换后必须重算一遍, 否则界面上会出现"正在运行但按钮显示可启动"。
        """
        try:
            self._set_state(state_text)
        except tk.TclError:
            return
        running = self._role is not None
        waiting = self._waiting_for_host() is not None
        if running:
            kind = "run"
        elif waiting:
            kind = "wait"
        else:
            kind = "idle"
        self._set_hero_state(kind)
        try:
            self._start_btn.set_enabled(not (running or waiting))
            self._stop_btn.set_enabled(bool(running or waiting))
        except tk.TclError:
            pass
        self._sync_tray(bool(running))

    # -- 状态区(三态) --------------------------------------------------

    def _set_hero_state(self, kind: str) -> None:
        """切换状态胶囊: run(绿) / wait(琥珀) / idle(灰)。

        ★ idle 时若状态文本不是"未启动"(例如"启动失败"/"已取消"),
          胶囊就直接显示那段文本 —— 否则用户只看到灰色"未启动",
          根本不知道刚才失败了。
        """
        badge = getattr(self, "_status", None)
        label = None
        if kind == "idle":
            txt = ""
            try:
                txt = self._state_var.get()
            except (tk.TclError, AttributeError):
                txt = ""
            if txt and txt not in ("未启动", "运行中", "等待主电脑"):
                label = txt
        if badge is not None:
            try:
                badge.set_state(kind, label)
            except tk.TclError:
                pass
        # ★★★ 播放/停止按钮的颜色是**唯一的状态载体**(无底色、无文字),
        #   所以必须在这里统一传播: 未启动黑/红, 等待黄, 连接成功绿。
        for btn in (getattr(self, "_start_btn", None),
                    getattr(self, "_stop_btn", None)):
            if btn is not None and hasattr(btn, "set_state"):
                try:
                    btn.set_state(kind)
                except tk.TclError:
                    pass
        if kind == "idle":
            # 右侧大数字已移除(见 row1 的说明), 这里不再需要清占位符。
            pass

    # -- 角色选择 ------------------------------------------------------

    def _select_role(self, value: str, fire: bool = True) -> None:
        """选中角色: 同步分段控件 + 角色说明 + 重建配置行。"""
        self._role_var.set(value)
        seg = getattr(self, "_role_seg", None)
        if seg is not None:
            try:
                seg.set_value(value)
            except tk.TclError:
                pass
        try:
            self._role_hint.set(self._ROLE_DESC.get(value, ""))
        except (tk.TclError, AttributeError):
            pass
        if fire:
            self._on_role_change()
        else:
            self._rebuild_config_for(value)

    def _rebuild_config_for(self, value: str) -> None:
        """重建配置行(只重建 ``_cfgf`` 的子控件, 容器本身保留)。"""
        self._clear_config()
        if value == "agent":
            self._build_config_agent()
        else:
            self._build_config_server()

    # -- 配置区构建(按角色) ---------------------------------------------

    def _clear_config(self) -> None:
        """销毁配置行并清引用, 否则后续 configure 会抛 TclError。"""
        for w in self._cfgf.winfo_children():
            w.destroy()
        self._side_seg = None
        self._side_var = None
        self._host_field = None
        self._jitter_field = None

    def _load_cfg(self) -> dict:
        try:
            from . import config as _cfg
            return dict(_cfg.load())
        except Exception:  # noqa: BLE001
            return {}

    def _build_config_server(self) -> None:
        """主电脑配置: 副电脑方位 + 延迟目标。"""
        from . import ui_theme as T
        saved = self._load_cfg()
        side = str(saved.get("side") or "right")
        if side not in SIDES:
            side = "right"
        self._side_var = tk.StringVar(value=side)
        h = self._setting_row(self._cfgf, "layout", 1, "副电脑方位",
                              "副机自动采用这里下发的方位")
        self._side_seg = T.Segmented(
            h, [(s, SIDE_ZH[s]) for s in SIDES], side,
            command=self._on_side_pick, bg=T.CARD)
        self._side_seg.pack(side="right")
        self._build_jitter_row()

    def _on_side_pick(self, value: str) -> None:
        """方位分段回调: 写回英文内部值并立即持久化。"""
        if self._side_var is not None:
            self._side_var.set(value)
        try:
            from . import config as _cfg
            _cfg.update(side=value)
        except Exception:  # noqa: BLE001
            pass

    def _build_jitter_row(self) -> None:
        """延迟目标行(两种角色共用)。"""
        from . import ui_theme as T
        jit = self._load_cfg().get("jitter_ms") or 60
        self._jitter_var = tk.StringVar(value=_fmt_num(jit))
        h = self._setting_row(self._cfgf, "latency", 2, "延迟目标",
                              "音频抖动缓冲，越大越稳")
        self._jitter_field = T.Field(h, self._jitter_var, width=78,
                                     height=32, suffix="ms", bg=T.CARD)
        self._jitter_field.pack(side="right")

    def _build_config_agent(self) -> None:
        """副电脑配置: 主电脑 IP + 延迟目标。"""
        from . import ui_theme as T
        remembered = str(self._load_cfg().get("server_host") or "")
        self._host_var = tk.StringVar(value=remembered)
        h = self._setting_row(self._cfgf, "layout", 1, "主电脑 IP",
                              "在主电脑上运行 ipconfig 查看")
        self._host_field = T.Field(h, self._host_var, width=176, height=32,
                                   justify="left", bg=T.CARD)
        self._host_field.pack(side="right")
        self._build_jitter_row()
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
        self._start_btn.set_enabled(False)

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
                    # ★ 诊断开关: CB_TONE_HZ=440 时副机改为**合成正弦**并直接
                    #   发送(跳过 WASAPI 采集)。用于把"爆音"分层定位:
                    #     纯正弦仍爆音 -> 问题在传输/渲染/声卡
                    #     纯正弦干净   -> 问题在副机的 loopback 采集
                    #   纯正弦上人耳能立刻分辨爆音, 比音乐容易判断得多。
                    import os as _os
                    try:
                        _tone = float(_os.environ.get("CB_TONE_HZ", "0") or 0)
                    except ValueError:
                        _tone = 0.0
                    role = Agent(server_host=host, name="agent",
                                 mute_local=True, discover_timeout_s=3.0,
                                 side=side or "right", tone_hz=_tone)
                # ★ 先把角色暴露出去, 再 start(): 副机可能长时间"等待主电脑",
                #   这期间用户必须能按停止取消(见 _on_stop / _check_watchdog)。
                self._pending_role = role
                role.start()
            except BaseException as exc:  # noqa: BLE001
                # ★ 取消(等待主电脑期间被停止)不是错误: 用灰色"已取消"
                #   收尾, 不要吓人的"启动失败: HostWaitCancelled"。
                if isinstance(exc, HostWaitCancelled):
                    try:
                        self._log_q.put_nowait("[gui] 已取消: 等待主电脑期间"
                                              "被停止, 本次启动作废")
                    except queue.Full:
                        pass
                    if role is not None:
                        try:
                            role.stop()
                        except BaseException:
                            pass
                    self._pending_role = None
                    self._ui_queue.put(("cancelled", None))
                    return
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
                self._pending_role = None
                self._ui_queue.put(("error", detail))
                return
            self._pending_role = None
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
            # ★★★ 顶层 hwnd **每次动态解析**, 不缓存!
            #   实测坑: 首次调用时窗口可能尚未完全成型(或 Tk 应用主题后
            #   重建了窗口层级), 缓存的 _top_hwnd 会指向**错误窗口** ——
            #   之后 tick 里 `fg == top` 永远不成立, 焦点监控形同虚设
            #   (用户实测 BUG: "程序窗口前台时副机键盘同步进主机窗口"
            #   的隐藏根因之一)。GetAncestor 是纯内核查询, 微秒级,
            #   每 700ms 一次的成本可以忽略。
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

        ★★★ 三条处理规则(前两条是实测回归, 第三条修本次报告的键盘泄漏):
          1. 窗口最小化 / 不可见 -> 不干预。此时它本来就不是前台; 若仍
             去抢前台, 用户点任务栏恢复时会被立刻夺走焦点, 表现为
             "点了没反应/闪一下又没了"。
          2. 用户刚主动操作过窗口(点击/恢复)之后的**宽限期** -> 不干预,
             用户点开 GUI 是为了用它, 这时强行把焦点交出去等于抢他的操作。
          3. ★ 穿越态(control 在副机)时**宽限期让位**(BUG 实测:
             "程序窗口处于前台时, 副机的键盘输入依然会同步到主机程序
             的窗口中")。宽限本来只是"让用户点界面", 但 GUI 在前台
             = 键盘钩子完全失效 = 副机每一次击键都泄漏进主机的 GUI,
             8 秒宽限等于 8 秒键盘泄漏。穿越态下不等宽限, 立即交还。
             用户正在副机打字, 不会同时盯着主机 GUI 点按钮,
             交还不会打断任何操作。
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # ★★★ 必须检查**所有本程序的顶层窗口**, 不只是主窗口。
            #   用户实测: "主窗口和日志窗口聚焦时在副机键盘输入确实会过滤
            #   主机的键盘同步, 但唯独实时状态这个窗口, 依然会同步键盘输入"。
            #   原因: 次级窗口(状态/日志)是**独立的 Toplevel**, 各有自己的
            #   顶层 hwnd, 而旧实现只比较主窗口的 hwnd —— 状态窗成为前台时
            #   fg 不等于主窗口 hwnd, 判定为"不是我们", 于是什么都不做,
            #   键盘钩子在状态窗前台期间失效, 副机击键全部泄漏进来。
            #
            #   收集方式: 从 root 递归遍历所有 Toplevel(含未映射的),
            #   用 GetAncestor 取各自的根 hwnd。每 700ms 一次, 开销可忽略。
            hwnd = int(self.root.winfo_id())
            top = user32.GetAncestor(hwnd, 2) or hwnd
            self._top_hwnd = top
            our_top_hwnds = set()
            for cand in [top]:
                try:
                    # ★ 最小化/不可见的窗口不算"抢了前台" —— 它本来就不
                    #   是前台, 而且此时去踢焦点会破坏最小化恢复
                    #   (实测回归: "点最小化后窗口无法恢复")。
                    if user32.IsWindowVisible(cand) and \
                            not user32.IsIconic(cand):
                        our_top_hwnds.add(cand)
                except Exception:  # noqa: BLE001
                    continue
            for win in (getattr(self, "_status_win", None),
                        getattr(self, "_log_win", None)):
                try:
                    tw = getattr(win, "win", None) if win is not None else None
                    if tw is None or not tw.winfo_exists():
                        continue
                    wh = int(tw.winfo_id())
                    wt = user32.GetAncestor(wh, 2) or wh
                    if user32.IsWindowVisible(wt) and \
                            not user32.IsIconic(wt):
                        our_top_hwnds.add(wt)
                except (AttributeError, tk.TclError, ValueError):
                    continue
            # ① 前台不是我们任何一个(可见且非最小化的)顶层窗口 -> 无事可做
            if not our_top_hwnds:
                return
            fg = user32.GetForegroundWindow()
            if not fg or fg not in our_top_hwnds:
                return
            # ②③ 穿越态判定 -> 宽限期是否让位
            crossing = False
            role = getattr(self, "_role", None)
            edge = getattr(role, "_edge", None) if role is not None else None
            if edge is not None:
                try:
                    crossing = bool(edge.is_outside)
                except Exception:  # noqa: BLE001
                    crossing = False
            if (not crossing) and time.monotonic() < \
                    getattr(self, "_focus_grace_until", 0.0):
                return
            shell = user32.GetShellWindow()
            if shell:
                user32.SetForegroundWindow(shell)
            n = getattr(self, "_focus_kicks", 0) + 1
            self._focus_kicks = n
            if n == 1:
                try:
                    self._log_q.put_nowait(
                        "[gui] 程序窗口曾抢到前台 -> 已交还焦点"
                        "(窗口在前台时键盘钩子会失效)")
                except queue.Full:
                    pass
            elif crossing and n % 10 == 0:
                try:
                    self._log_q.put_nowait(
                        "[gui] 穿越态下窗口在前台会泄漏副机键盘, "
                        "已再次交还焦点")
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
        # ★★ 用 _pending_role 兜底: 副机可能长时间卡在"等待主电脑"里,
        #    此时 start() 还没返回、self._role 仍是 None —— 旧代码直接
        #    return, 用户只能退出整个程序。现在按停止 = 取消这次启动
        #    (Agent.stop() 置标志 -> 等待循环抛 HostWaitCancelled ->
        #     启动线程汇报"已取消", 见 _on_start 的 worker)。
        role = self._role or self._pending_role
        if role is None or self._stop_thread is not None:
            return
        pending_start = self._role is None
        self._set_state("取消中..." if pending_start else "停止中...")
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
            if pending_start:
                # 取消路径: 由**启动线程**汇报"已取消"(它捕获
                # HostWaitCancelled), 这里不再重复投递状态, 否则界面会
                # 先"已停止"再"已取消"地闪一下。
                return
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
                        # ★ 这是行内小字(行标签已经写了"开机自启"),
                        #   所以只说状态, 不重复主语。
                        self._elev_var.set("已启用 —— 登录后静默启动到托盘")
                    else:
                        self._elev_var.set("未启用")
                    # 同步开关位置(Canvas 胶囊)
                    sw = getattr(self, "_elev_sw", None)
                    if sw is not None and hasattr(sw, "set_on"):
                        sw.set_on(enabled)
                except tk.TclError:
                    pass

            self._post_ui(apply)

        _th.Thread(target=worker, daemon=True,
                   name="AutostartQuery").start()

    def _on_autostart_switch(self, want_on: bool) -> None:
        """Canvas 开关回调: 直接把自启设成目标状态(而不是盲翻转)。

        ★ 为什么不用盲翻转: 开关是"目标态"语义, 而翻转式入口在
          "开关位置与实际状态短暂不一致"(异步查询期间)时会把用户想要
          的状态做反。这里按 want_on 显式设置。
        ★ 后台回调一律走 `_post_ui`(tkinter 非线程安全, 实测踩过
          "main thread is not in main loop")。
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
            try:
                cur = autostart.is_enabled()
            except Exception:  # noqa: BLE001
                cur = False
            if cur == want_on:
                ok, msg = True, ("已是目标状态" if want_on else "已是关闭状态")
            elif want_on:
                ok, msg = autostart.enable()
            else:
                ok, msg = autostart.disable()

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
                   name="AutostartSwitch").start()

    def _on_elev_toggle(self) -> None:
        """翻转开机自启(兼容旧入口 / 托盘菜单)。

        **全程静默, 不弹 UAC** —— 用户要求"不能加入需要用户确认的过程,
        整个过程必须静默"。注册表读写极快(实测 <50ms), 但仍放后台线程
        执行, 保持主线程永不阻塞; 结果经 `_post_ui` 回到主线程。
        """
        sw = getattr(self, "_elev_sw", None)
        want = not (sw.is_on() if hasattr(sw, "is_on") else False)
        self._on_autostart_switch(want)

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
                    self._set_hero_state("run")
                    self._stop_btn.set_enabled(True)
                    self._start_btn.set_enabled(False)
                    self._sync_tray(True)
                    # ★★★ 主电脑启动后必须**主动放弃窗口焦点** ——
                    #   本进程窗口是前台时, Windows 不会把键盘事件交给
                    #   我们的低级键盘钩子(实测 kbd_calls 8->0, 且重装无效)。
                    #   见 _release_focus_for_hook 的完整实测数据。
                    if payload == "server":
                        self._release_focus_for_hook()
                elif kind == "stopped":
                    self._set_state("未启动")
                    self._set_hero_state("idle")
                    self._stop_btn.set_enabled(False)
                    self._start_btn.set_enabled(True)
                    self._stop_thread = None
                    self._sync_tray(False)
                elif kind == "error":
                    self._set_state(f"启动失败")
                    self._set_hero_state("idle")
                    self._start_btn.set_enabled(True)
                    self._stop_btn.set_enabled(False)
                    self._sync_tray(False)
                    self._role_thread = None
                elif kind == "cancelled":
                    # 等待主电脑期间被用户取消 —— 不是错误
                    self._set_state("已取消")
                    self._set_hero_state("idle")
                    self._pending_role = None
                    self._start_btn.set_enabled(True)
                    self._stop_btn.set_enabled(False)
                    self._sync_tray(False)
                    self._role_thread = None
                    self._stop_thread = None
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

    def _waiting_for_host(self) -> tuple[str, float, int] | None:
        """当前角色是否正在**等待主电脑就绪**(Agent.host_wait_state 的封装)。

        返回 (阶段, 已等秒数, 探测次数) 或 None。给看门狗用: 等待期间
        不能把界面的"启动中"判成超时失败。
        """
        role = self._pending_role or self._role
        fn = getattr(role, "host_wait_state", None)
        if fn is None:
            return None
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None

    def _check_watchdog(self) -> None:
        """过渡态超时则恢复按钮并报错, 避免界面永久卡在"启动中/停止中"。

        阈值取得比正常启动耗时宽裕(Agent 的发现 3s + 对时约 1s), 避免
        把慢启动误判为失败。
        """
        if self._pending_since is None or self._pending_kind is None:
            return
        # ★★★ "等待主电脑"是**合法且预期**的长期状态(用户 2026-10-21:
        #   "副机自启后如果主机启动慢, 它会直接报错, 这不是预期行为")。
        #   这里把计时基准往后挪 = 视为"仍在正常推进", 并把停止按钮变成
        #   可用的"取消", 让用户随时能退出等待。
        wait = self._waiting_for_host()
        if wait is not None:
            _phase, elapsed, attempts = wait
            self._pending_since = now_us() / 1e6
            # ★ 状态区切成"等待"配色(琥珀), 摘要显示等待进度。
            #   主按钮保持"启动服务"且禁用 —— 没有"取消等待"按钮
            #   (用户 2026-10-21 明确要求), 想中止就点"停止"。
            self._set_state("等待主电脑")
            self._set_hero_state("wait")
            try:
                self._hero_meta.set(
                    f"已等 {elapsed:.0f}s · 探测 {attempts} 次 · 就绪后自动握手")
                self._stop_btn.set_enabled(True)
            except tk.TclError:
                pass
            # 托盘提示同步一次就够(每 500ms 刷 Shell_NotifyIcon 没必要),
            # 否则用户悬停在托盘上看到的还是"已停止", 会误以为没在工作。
            if not getattr(self, "_tray_wait_set", False):
                self._tray_wait_set = True
                tray = getattr(self, "_tray", None)
                if tray is not None:
                    try:
                        tray.set_status(False, "Com Bridge · 等待主电脑…")
                    except Exception:  # noqa: BLE001
                        pass
            return
        self._tray_wait_set = False
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
            self._start_btn.set_enabled(True)
            self._stop_btn.set_enabled(False)
            self._role_thread = None
        else:
            self._stop_thread = None
            self._start_btn.set_enabled(True)
            self._stop_btn.set_enabled(False)

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
                # ★★★ 2026-10-21 用户指出: "键盘已经改了, 不是 raw input 了,
                #   这个数据是不是也该同步修改?"
                #   查实: 主电脑确实以 register_keyboard=False 启动
                #   (见 server.py —— 注册键盘 RawInput 会让低级键盘钩子
                #   收不到任何按键, 这是键盘跨屏失效的根因修复)。
                #   所以:
                #     * 鼠标确实走 RawInput
                #     * 键盘走低级钩子(按键计数在下面的"键盘钩子"行里)
                #   旧文案一律写"原始输入 N 鼠标/M 键盘", 那个 M 恒为 0,
                #   既误导(以为键盘也走 RawInput 且坏了)又重复。
                #   现在按**实际采集路径**生成文案。
                if getattr(role._listener, "register_keyboard", False):
                    src = f"原始输入 {ld['mouse']} 鼠标/{ld['key']} 键盘"
                else:
                    src = f"原始输入 {ld['mouse']} 鼠标 · 键盘走低级钩子"
                out["input"] = (
                    f"控制权 {state} | {src}{txinfo}", 1 if outside else 0)
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
            out["clipboard"] = self._clip_status(role)
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
                jumps = int(getattr(clock, "offset_jumps", 0))
                maxjump = float(
                    getattr(clock, "max_offset_jump_us", 0.0)) / 1000.0
                out["clock"] = (
                    f"rtt {clock.rtt_us / 1000:.2f}ms / 样本 {clock.samples}"
                    f" / 偏移跳变 {jumps} 次(最大 {maxjump:.2f}ms)"
                    f" ｜ 单调钟偏移 {clock.offset_us / 1000:+.0f}ms"
                    f"（两机开机时刻之差，换算用，不是时钟误差）",
                    1 if synced else 2)
            out["clipboard"] = self._clip_status(role)
        return out

    @staticmethod
    def _clip_status(role) -> tuple[str, int]:
        """状态窗口的"剪贴板"一行: 发/收计数 + 文件数 + 流量。

        ★ 为什么需要(审查项 #5): 剪贴板是本程序的主力功能之一, 但它的
          统计(发了几次、收了几个文件、多少 MB)以前**只存在于日志里**,
          用户想知道"刚才那个 200MB 的文件夹到底传过去没有"只能翻日志。
        """
        tx = getattr(role, "_clip_tx", None)
        rx = getattr(role, "_clip_rx", None)
        sync = getattr(role, "_clip_sync", None)
        if tx is None and rx is None:
            return ("未启用", 0)
        parts: list[str] = []
        level = 1
        if tx is not None:
            st = tx.stats() if hasattr(tx, "stats") else {}
            seg = f"发 {st.get('sent', 0)} 项"
            if st.get("bytes_sent"):
                seg += f" ({st['bytes_sent'] / 1048576:.1f} MB)"
            if st.get("busy"):
                seg += " · 传输中…"
            if st.get("queued"):
                seg += f" · 排队 {st['queued']}"
            if st.get("cancelled"):
                seg += f" · 取消 {st['cancelled']}"
            if st.get("failed"):
                seg += f" · !! 失败 {st['failed']}"
                level = 3
            parts.append(seg)
        if rx is not None:
            d = getattr(rx, "stats", {}) or {}
            seg = f"收 {d.get('payloads', 0)} 项"
            if d.get("files"):
                seg += f"（文件 {d['files']} 个"
                seg += f", {d.get('bytes', 0) / 1048576:.1f} MB）"
            if d.get("rejected"):
                seg += f" · !! 拒收 {d['rejected']}"
                level = 3
            parts.append(seg)
        if sync is not None:
            sd = getattr(sync, "stats", {}) or {}
            if sd.get("skipped_big"):
                parts.append(f"过大跳过 {sd['skipped_big']}")
            if sd.get("errors"):
                parts.append(f"!! 采集错误 {sd['errors']}")
                level = max(level, 2)
        return (" ｜ ".join(parts), level)

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
        """托盘图标跟随服务状态(用户要求: 直观分辨是否已启动)。

        ★ 三态(2026-10-21 定稿):
            connected  绿点  已连接
            waiting    黄点  已启动但未连接(等待主电脑)
            off        无点  未启动
        """
        tray = getattr(self, "_tray", None)
        try:
            role_txt = _ROLE_ZH.get(self._role_var.get(), "服务")
            if tray is not None:
                if running and self._role is not None:
                    tray.set_status(True, f"Com Bridge · {role_txt}运行中",
                                    state="connected")
                elif self._waiting_for_host() is not None:
                    # 正在等主电脑: 已启动但未连接 -> 黄点
                    tray.set_status(False, "Com Bridge · 等待主电脑…",
                                    state="waiting")
                else:
                    tray.set_status(False, "Com Bridge · 已停止",
                                    state="off")
            self._set_dot(running)
        except Exception:  # noqa: BLE001
            pass
        # ★ 状态区摘要: 运行中显示对端 + 音频缓冲(主界面上唯一的状态信息,
        #   详细数据仍在「实时状态」窗口 —— 用户要求不搬进主界面)。
        #   ★ 2026-10-21: 早先这个数字被单独拎出来放大显示, 用户指出
        #   "它使得音频缓冲看起来像连接整体延迟", 现已合回摘要一句。
        try:
            if running and self._role is not None:
                # ★★★ 2026-10-21 用户要求把"运行中"拆成两个视觉状态:
                #   "等待连接时三角变黄, 连接成功时三角变绿"。
                #   所以这里先判断**是否真的连上了对端**, 再决定用 run 还是
                #   wait 配色 —— 早先一律 "_set_hero_state('run')", 于是
                #   "已启动但还没连上"也显示成绿色, 与事实不符。
                role = self._role
                parts = []
                # ★★★ 2026-10-21 用户决定: 大数字移除后, 音频缓冲回到摘要里,
                #   但要**明确标成"音频缓冲"**, 避免被当成整条链路的连接延迟。
                #   再进一步: 用括号把它挂在连接状态**后面**, 读作
                #   "已连接 X（音频缓冲 93 ms）" —— 是这条连接的属性,
                #   而不是与"已连接"并列的另一项指标(用 · 分隔会读成并列)。
                if isinstance(role, Server):
                    connected = bool(role.peer_addr)
                    if connected:
                        head = f"已连接 {role.peer_addr}"
                    else:
                        head = "等待副电脑接入"
                    extra = ""
                    if role._peer is not None:
                        try:
                            lat = role._peer.buffered_ms()
                            extra = f"（音频缓冲 {lat:.0f} ms）"
                        except Exception:  # noqa: BLE001
                            pass
                    parts.append(head + extra)
                else:
                    host = getattr(role, "server_host", "") or ""
                    clock = getattr(role, "_control", None)
                    connected = bool(clock is not None
                                     and getattr(clock, "clock", None)
                                     and clock.clock.synced)
                    head = f"主电脑 {host}" if host else "副电脑"
                    if not connected:
                        head += " · 等待对时"
                    extra = ""
                    if clock is not None and getattr(clock, "clock", None):
                        try:
                            # ★ 以前这里显示 offset_us —— 那是两机**单调钟
                            #   (perf_counter)纪元**之差, 等于"开机时刻相差
                            #   多少"(实测 +47568320ms ≈ 13.2 小时), 它是换算
                            #   基准而不是时钟误差。摆在界面上只会让人以为
                            #   时钟差了 13 小时。用户真正关心的"对时好不好"
                            #   = 往返 rtt 与偏移跳变次数。
                            cl = clock.clock
                            extra = f"（对时往返 {cl.rtt_us / 1000.0:.1f} ms"
                            jumps = int(getattr(cl, "offset_jumps", 0))
                            if jumps:
                                extra += f" · 偏移跳变 {jumps} 次"
                            extra += "）"
                        except Exception:  # noqa: BLE001
                            pass
                    parts.append(head + extra)
                self._set_hero_state("run" if connected else "wait")
                self._hero_meta.set("".join(parts))
        except tk.TclError:
            pass

    def _set_dot(self, running: bool) -> None:
        """兼容旧接口: v6 的状态颜色由状态胶囊(StatusBadge)表达。

        保留这个空实现是因为 `_sync_tray` 仍在调用它; 真正换色发生在
        `_set_hero_state("run"/"wait"/"idle")`。
        """
        _ = running

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
                on_log=lambda: self._post_ui(self._open_log_window),
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


def _is_autostart_argv(argv: list[str]) -> bool:
    """命令行是否要求**静默启动**(开机自启传入)。

    纯函数, 便于单测: 不创建 Tk、不碰注册表。
    """
    return "--autostart" in argv or "--silent" in argv


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

    # ★★ 静默启动标志(开机自启时由 Run 注册表项传入, 见 autostart)。
    #   语义: 不显示主窗口, 只建托盘 + 自动拉起上次的角色。
    start_hidden = _is_autostart_argv(argv)

    # ★★★ 单实例检查(用户实测: "我能同时启动两个甚至多个")
    #   多实例的真实危害不只是多个窗口:
    #     * 都去 bind 24802/TCP + 4010/UDP -> 后启动的绑定失败;
    #     * 都装低级键鼠钩子 -> 事件被吞两次/转发两次, 跨屏输入重影;
    #     * 托盘里出现多个同名图标;
    #     * 都写同一份 config.json -> 状态互相覆盖。
    #   所以第二个实例必须**主动退出**, 并唤起已有窗口(符合用户双击预期)。
    from . import single_instance
    if not single_instance.acquire():
        # ★ 静默启动(开机自启)时**不要**去唤起已有窗口 —— 那会在登录后
        #   毫无理由地把窗口弹到用户面前, 与"静默"的要求直接冲突。
        if not start_hidden:
            single_instance.notify_existing()
        try:
            from . import gui_log
            gui_log.write_line(
                "[gui] 已有实例在运行 -> " +
                ("静默启动, 本次退出" if start_hidden
                 else "已唤起其窗口, 本次启动退出"))
        except Exception:  # noqa: BLE001
            pass
        return 0

    # ★ 自启命令迁移: 旧版本登记的命令不带 --autostart, 不迁移的话老用户
    #   升级后照样会开机弹窗(看起来"修了但没生效")。注册表读写是微秒级,
    #   放在主线程没有性能问题。
    try:
        from . import autostart as _auto
        state = _auto.migrate_command()
        if state == "migrated":
            try:
                from . import gui_log
                gui_log.write_line(f"[gui] 自启命令已升级为静默启动: "
                                   f"{_auto.stored_command()}")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    app = ComBridgeGui(start_hidden=start_hidden)
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
