"""系统光标的隐藏与恢复 —— 照搬 Deskflow 的 MSWindowsDesks "hider window"。

机制(与 Deskflow 一致):
  创建一个**全屏透明窗口**(WS_EX_TRANSPARENT 点击穿透 + WS_EX_TOOLWINDOW
  不进任务栏), 其窗口类的 hCursor 是**全透明光标**(createBlankCursor):
  AND 掩码全 0xff + XOR 掩码全 0x00。

★ 但只有窗口是不够的 —— 实测(diag_repro.py)证明还差两步, 缺一即失效:
  (1) 光标可见性计数器和窗口类光标都是**按线程**生效的。窗口"盖住屏幕"
      并不意味着 Windows 会用它的类光标或我们线程的计数器去画光标;
      真正说了算的是**正在接收鼠标输入的那个线程**。hider 线程若不接管
      鼠标输入, 我们在自己线程里 ShowCursor(FALSE) 对屏幕毫无影响 ——
      这正是用户实测"主机光标没有隐藏"的原因。
      Deskflow 为此: 主机侧 deskLeave 调 SetActiveWindow(hider),
      副机侧 deskLeave 调 SetCapture(hider)。我们统一用 SetCapture:
      它不抢前台(不破坏主机的键盘焦点), 又能让捕获线程接管鼠标输入。
  (2) ShowCursor 必须**循环调用**直到计数器达标(Deskflow 的
      setCursorVisibility 同样循环最多 10 次: "Windows maintains an
      internal counter for cursor visibility")。

实测数据(生产配置完整复现, tools/diag_repro.py):
  hide() 后  本线程 ShowCursor 计数器 = -1
             GetCursorInfo: bShowing=False, hCursor=NULL  -> 光标不可见
             此后持续 50 次绝对定位注入(模拟 125Hz 鼠标钉住)仍保持隐藏
  show() 后  计数器 = 0, bShowing=True, hCursor=箭头      -> 光标恢复

为什么不用 SetSystemCursor(全局替换系统光标):
  程序若崩溃, 系统光标会**永久**变成透明, 用户只能手工恢复。
  hider window 随进程生灭, 崩溃即自动复原 —— Deskflow 也是这么选的。

用法:
  主电脑: 越界交给副机时 hide(); 回到本机时 show()
  副电脑: 主机光标进入时 show(); 光标回到主机时 hide()
          (hide_on_motion=True 让本机物理键鼠一动就恢复光标, 与 Deskflow
           的 secondaryDeskProc 行为一致)
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

from .input import kernel32, user32

# 常量(照搬 MSWindowsDesks.cpp 顶部的定义)
SM_CXCURSOR = 13
SM_CYCURSOR = 14
WS_POPUP = 0x80000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOP = 0
HWND_BOTTOM = 1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
WM_MOUSEMOVE = 0x0200
WM_CAPTURECHANGED = 0x031D   # 捕获被别的窗口抢走(用户拖拽应用等)
WM_TIMER = 0x0113
WM_APP = 0x8000
MSG_HIDE = WM_APP + 1        # 显示 hider 窗口(= 隐藏光标)
MSG_SHOW = WM_APP + 2        # 收起 hider 窗口(= 恢复光标)

# ★★★ 保活定时器: hide 存续期间周期验证捕获仍在手上。
#   用户实测 BUG: "出点在浏览器滚动条/图片查看器下一页等可交互
#   元素上时, 主机指针进入副机后不消失"。这类窗口响应 hover 时会
#   自己 SetCapture(滚动条拖拽/悬停跟踪), 我们的 SetCapture 触发
#   对方 WM_CAPTURECHANGED 后它立刻重抢 —— 单次"被抢再夺回"
#   追不上持续争抢, 捕获丢失期间鼠标消息流回对方线程, 我们线程
#   的 ShowCursor 计数器失去效力, 光标现形。
#   解法: 定时器每 100ms 主动查 GetCapture; 不是自己就立刻重夺,
#   并再补一次抖动注入(强制提交计数器)。
KEEPALIVE_TIMER_ID = 1
KEEPALIVE_MS = 100

TRUE = 1
FALSE = 0

# ★ 模块级说明见文件头。这里再强调一句最容易漏的:
#   ShowCursor 与"窗口类光标"都是**按线程**生效的 —— 只有正在接收鼠标
#   输入的那个线程的可见性计数器才决定屏幕上画不画光标。hider 线程必须
#   接管鼠标输入(我们用 SetCapture), 否则 ShowCursor(FALSE) 毫无效果。
#   Deskflow 主机侧用 SetActiveWindow、副机侧用 SetCapture 达到同一目的。

# ★ 64 位进程必须显式声明参数/返回类型, 否则 ctypes 默认按 32 位 int
#   传 HINSTANCE/HCURSOR, 模块句柄超出 32 位会 OverflowError(实测)。
user32.CreateCursor.argtypes = [
    ctypes.c_void_p,                # hInst
    ctypes.c_int, ctypes.c_int,     # xHotSpot, yHotSpot
    ctypes.c_int, ctypes.c_int,     # nWidth, nHeight
    ctypes.c_void_p,                # AND 位掩码
    ctypes.c_void_p,                # XOR 位掩码
]
user32.CreateCursor.restype = ctypes.c_void_p
user32.DestroyCursor.argtypes = [ctypes.c_void_p]
user32.DestroyCursor.restype = wintypes.BOOL
user32.SetCapture.argtypes = [wintypes.HWND]
user32.SetCapture.restype = wintypes.HWND
user32.GetCapture.argtypes = []
user32.GetCapture.restype = wintypes.HWND
user32.ReleaseCapture.argtypes = []
user32.ReleaseCapture.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.GetCurrentProcessId.restype = wintypes.DWORD


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


# 桌面/外壳窗口类名 —— 它们覆盖全屏但不是"全屏程序", 不能据此放弃抢前台。
# 见 CursorHider._is_foreign_fullscreen。
_SHELL_CLASSES = frozenset({
    "Progman",          # 桌面
    "WorkerW",          # 壁纸/桌面子窗口(视 Windows 版本而定)
    "Shell_TrayWnd",    # 任务栏
    "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow",
})


def _window_class(hwnd) -> str:
    """取窗口类名(失败返回空串)。"""
    try:
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR,
                                         ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


def is_foreign_fullscreen(hwnd) -> bool:
    """该窗口是否**别的进程**的、覆盖整个虚拟桌面的窗口(即"全屏程序")。

    ★ 必须排除外壳/桌面窗口: 用户点击空桌面时 `GetForegroundWindow()`
      返回的是 explorer 的 Progman/WorkerW, 它**同样覆盖全屏** ——
      若一并当成"全屏程序", 就会退化成本项目早先反复踩过的
      "光标在空桌面上隐藏不掉"(见 CursorHider._cursor_guard_visible)。

    ★ 判定用"覆盖整个虚拟桌面"而不是"等于某个显示器": 单个最大化窗口只
      覆盖一块屏, 不是全屏; 无边框全屏(现代游戏常见)一定覆盖整个虚拟桌面。

    两个用途:
      * CursorHider: 前台是全屏程序时不抢前台(抢了会把它打断成黑屏);
      * Server: 前台是全屏程序(游戏/播放器/远程桌面)时**不穿越** ——
        否则游戏里把鼠标移到屏幕边就会把键鼠交给另一台电脑。
    """
    try:
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == kernel32.GetCurrentProcessId():
            return False
        if _window_class(hwnd) in _SHELL_CLASSES:
            return False
        r = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return False
        from .input import virtual_desktop
        d = virtual_desktop()
        return rect_covers((r.left, r.top, r.right, r.bottom),
                           (d.left, d.top, d.right, d.bottom))
    except Exception:  # noqa: BLE001
        return False


def foreign_fullscreen_foreground() -> bool:
    """当前前台窗口是否别的进程的全屏窗口(游戏/播放器/远程桌面)。"""
    try:
        return is_foreign_fullscreen(user32.GetForegroundWindow())
    except Exception:  # noqa: BLE001
        return False


def rect_covers(win, desk) -> bool:
    """窗口矩形是否**完全覆盖**桌面矩形(纯函数, 可单测)。

    两个矩形都用 (left, top, right, bottom)。判定"全屏"用"覆盖整个虚拟
    桌面"而不是"等于某个显示器", 这样多显示器拼屏 + 最大化窗口也能正确
    区分: 单个最大化窗口只覆盖一个显示器, 不会盖住整个虚拟桌面。
    """
    try:
        wl, wt, wr, wb = win
        dl, dt, dr, db = desk
        return wl <= dl and wt <= dt and wr >= dr and wb >= db
    except Exception:  # noqa: BLE001
        return False



def _set_cursor_visibility(visible: bool, attempts: int = 10) -> int:
    """循环调用 ShowCursor 直到计数器到达目标状态, 返回**最后的计数值**。

    照搬 Deskflow 的 setCursorVisibility: Windows 维护内部计数器,
    单次调用可能不生效(比如别的代码也调过 ShowCursor), 所以循环。
    返回值(<0 = 本线程光标已隐藏)可直接作为验收判据 —— 比"收到几条
    WM_MOUSEMOVE"可靠得多(系统会合并快速连续的移动消息, 条数不可控)。
    """
    user32.ShowCursor.argtypes = [wintypes.BOOL]
    user32.ShowCursor.restype = ctypes.c_int
    counter = 0
    for _ in range(attempts):
        counter = user32.ShowCursor(TRUE if visible else FALSE)
        if visible and counter >= 0:
            return counter
        if not visible and counter < 0:
            return counter
    return counter


class CursorHider:
    """跨屏期间隐藏本机系统光标。

    hide_on_motion=True(副机用): 本机物理鼠标一动, hider 窗口自动收起,
    光标恢复可见 —— 与 Deskflow 的 secondaryDeskProc 行为一致
    ("we just detect mouse motion ... so the user can use the physically
    attached mouse if desired")。
    """

    def __init__(self, hide_on_motion: bool = False, name: str = "CursorHider"):
        self._hide_on_motion = hide_on_motion
        self._name = name
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._cursor = None
        self._proc_ref = None          # 常驻引用, 防 GC 回收回调
        self._shown = False            # hider 窗口当前是否盖在屏幕上
        self._stolen_foreground = None  # hide() 抢走的前台窗口(deskEnter 恢复用)
        # 诊断: 隐藏生效的**前提**是本线程在接收鼠标输入(实测用例 B vs H)。
        # 验收脚本据此断言, 不再"机制层完好但屏幕上没隐藏"。
        self._moves_seen = 0           # hider 线程收到的 WM_MOUSEMOVE 数
        self._injected_seen = 0        # 其中判定为注入(带 TAG)的条数
        self._moves_nonzero = 0        # 其中客户区坐标非零的条数(仅诊断)
        self._captured = False         # 最近一次 SetCapture 是否成功
        # ★ BUG2 的关键诊断: hide() 时 hider 窗口是否真的成为**前台窗口**。
        #   为 False 时本线程收不到鼠标消息, 光标不会消失(见 _force_foreground)。
        self._activated = False
        # 连续非注入移动消息计数(滤掉孤立的合成消息, 见 PHYSICAL_STREAK_N)
        self._physical_streak = 0
        # hide() 完成的单调时刻(静默期起点, 见 HIDE_SETTLE_S)
        self._hide_mono = 0.0
        # hide() 时本线程的 ShowCursor 返回值(<0 = 输入线程光标已关)。
        # 这是隐藏生效的**直接判据**(条数会被系统合并, 不可靠)。
        self._counter_after_hide = 0
        # show() 时本线程的 ShowCursor 返回值(>=0 = 光标已恢复)
        self._counter_after_show = 0
        # 诊断: hide 期间 WM_CAPTURECHANGED 的次数。>0 说明有别的窗口
        # (或系统)在跟我们争鼠标捕获 —— 争抢会中断"本线程接收鼠标输入"
        # 这一隐藏前提, 光标现形(用户实测: 出点在滚动条/按钮等可交互
        # 元素上时主机指针不消失)。
        self._capture_changed = 0
        # ★ 诊断: hider 是否真的抢到了前台 —— 这是 SetCapture 生效的
        #   **前提**(MSDN: 只有前台窗口才能捕获鼠标)。
        self._is_foreground = False
        # 前台被抢走的次数(保活定时器重夺计数)。>0 说明有程序在抢焦点。
        self._fg_regrabs = 0
        # ★★★ 抢前台的**刹车**(2026-10-21 事故复盘新增)。
        #   背景: 保活定时器每 100ms 检查一次前台, 丢了就 AttachThreadInput
        #   + SetForegroundWindow 抢回来。这等于**我们永不放弃焦点** ——
        #   对普通桌面程序无所谓, 但前台若是**全屏程序**(游戏 / 远程桌面
        #   客户端 / 播放器), 抢走焦点会让它黑屏或停止出画, 而它每次想
        #   拿回焦点又会被我们 100ms 后抢走 -> 永久黑屏 + 键鼠全无反应。
        #   两个刹车:
        #     1) 前台是**别的进程的全屏窗口**时, 干脆不抢(光标可能因此
        #        保持可见 —— 用"看得见的光标"换"不全屏黑掉", 值得);
        #     2) 连续抢 N 次仍抢不到(有程序比我们更凶), 就放弃本轮,
        #        避免无限抢焦点的抖动。
        self._give_up_steal = False
        self._fg_fail_streak = 0
        self._fg_steal_skipped = 0
        self._fullscreen_skip_logged = False
        self._class_name = f"ComBridgeHider_{id(self):x}"   # 每实例唯一

    # -- 生命周期 ------------------------------------------------------

    def start(self) -> bool:
        """启动 hider 线程(窗口必须由带消息循环的线程创建)。"""
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=self._name)
        self._thread.start()
        return self._ready.wait(3.0)

    def stop(self) -> None:
        """停止并确保光标恢复可见(进程退出前必须调用)。"""
        self.show()                    # 无论当前状态, 先恢复光标
        self._stop.set()
        if self._hwnd:
            user32.PostMessageW(self._hwnd, 0x0012, 0, 0)   # WM_QUIT

    # -- 对外操作(线程安全, 经消息投递到 hider 线程) --------------------

    def hide(self) -> None:
        """隐藏本机光标(显示全屏 hider 窗口)。"""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, MSG_HIDE, 0, 0)

    def show(self) -> None:
        """恢复本机光标(收起 hider 窗口)。"""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, MSG_SHOW, 0, 0)

    @property
    def hidden(self) -> bool:
        return self._shown

    # -- 内部(hider 线程) ----------------------------------------------

    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        self._cursor = self._create_blank_cursor(hinst)
        from .input import WNDCLASSW, WNDPROC     # input.py 里已定义
        wc = WNDCLASSW()
        wc.lpfnWndProc = WNDPROC(self._proc)
        wc.hInstance = hinst
        wc.hCursor = self._cursor                 # ★ 全透明光标: 隐藏的关键
        wc.lpszClassName = self._class_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            self._ready.set()
            return

        # 1x1 创建, hide() 时再定位/放大(照搬 Deskflow 的做法)
        self._hwnd = user32.CreateWindowExW(
            WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW,
            self._class_name, "ComBridgeHider", WS_POPUP,
            0, 0, 1, 1, None, None, hinst, None,
        )
        self._ready.set()
        if not self._hwnd:
            return

        msg = wintypes.MSG()
        while not self._stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # 清理: 确保光标恢复 + 销毁窗口与光标
        _set_cursor_visibility(True)
        if self._hwnd:
            user32.SetWindowPos(self._hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_HIDEWINDOW)
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        if self._cursor:
            user32.DestroyCursor(self._cursor)
            self._cursor = None
        user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        user32.UnregisterClassW(self._class_name, hinst)

    def _create_blank_cursor(self, hinst):
        """全透明光标(照搬 Deskflow 的 createBlankCursor):
        AND 掩码全 0xff + XOR 掩码全 0x00 → 无论底下是什么都显示为透明。"""
        cw = user32.GetSystemMetrics(SM_CXCURSOR)
        ch = user32.GetSystemMetrics(SM_CYCURSOR)
        row = (cw + 31) >> 2                      # 每行字节数(32bit 对齐)
        size = ch * row
        AND = (ctypes.c_ubyte * size)(*( [0xFF] * size ))
        XOR = (ctypes.c_ubyte * size)(*( [0x00] * size ))
        return user32.CreateCursor(hinst, 0, 0, cw, ch, AND, XOR)

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == MSG_HIDE:
            self._do_hide()
            return 0
        if msg == MSG_SHOW:
            self._do_show()
            return 0
        if msg == WM_TIMER and wparam == KEEPALIVE_TIMER_ID and self._shown:
            # ★★★ 保活: **前台 + 捕获**都必须在手上, 缺一光标就现形。
            #   前台可能被用户点别的窗口抢走; 捕获可能被可交互控件
            #   (滚动条/悬停跟踪)抢走。两者都每 100ms 重验一次。
            #
            #   为什么前台是必需的: MSDN 规定 SetCapture 只在"按钮按下
            #   或窗口是前台"时生效。丢了前台, 捕获随时会失效, 光标就
            #   重新出现 —— 这正是用户反复报的"光标没消失"。
            need_recover = False
            # ★ 抢前台刹车(全屏他程序 / 连续抢不到) —— 一旦触发就**不再**
            #   动前台与捕获, 直接返回: 继续抢只会制造抖动, 而光标可见
            #   只是体验降级, 全屏程序被反复打断却是灾难性的。
            if self._steal_disabled(hwnd):
                return 0
            if user32.GetForegroundWindow() != hwnd:
                fg = user32.GetForegroundWindow()
                fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
                my_tid = kernel32.GetCurrentThreadId()
                attached = False
                if fg_tid and fg_tid != my_tid:
                    try:
                        attached = bool(
                            user32.AttachThreadInput(fg_tid, my_tid, TRUE))
                    except Exception:  # noqa: BLE001
                        attached = False
                try:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                finally:
                    if attached:
                        try:
                            user32.AttachThreadInput(fg_tid, my_tid, FALSE)
                        except Exception:  # noqa: BLE001
                            pass
                self._is_foreground = (user32.GetForegroundWindow() == hwnd)
                need_recover = True
                self._fg_regrabs = getattr(self, "_fg_regrabs", 0) + 1
                # 踩刹车②: 抢不到就别无休止地抢(见 __init__ 的说明)
                if self._is_foreground:
                    self._fg_fail_streak = 0
                else:
                    self._fg_fail_streak += 1
                    if self._fg_fail_streak >= self.MAX_FG_STEAL_FAILS:
                        self._give_up_steal = True
                        print(f"[cursor] !! 连续 {self._fg_fail_streak} 次抢前台"
                              f"失败(有更强势的程序在占焦点) -> 本轮穿越放弃"
                              f"隐藏光标(本机光标会保持可见, 键鼠仍归副机)",
                              flush=True)
            if user32.GetCapture() != hwnd:
                self._capture_changed += 1
                user32.SetCapture(hwnd)
                self._captured = (user32.GetCapture() == hwnd)
                need_recover = True
            if need_recover:
                # 重新提交光标可见性(前台/捕获变化会让系统重新评估光标)
                _set_cursor_visibility(False)
                if self._hide_mono:
                    self._hide_mono = time.monotonic()
            else:
                # ★★★ 光标可见性守卫(用户实测: 光标在**空桌面**时隐藏会
                #   在 1.2s 后失效, 而记事本/其它程序位置却稳)。
                #   诊断发现: Z 序最顶是 **Quicker 的悬浮面板窗口**
                #   (HwndWrapper[Quicker.exe...]) —— 这类第三方工具在
                #   自己显示/隐藏悬浮层时会调用 ShowCursor(TRUE), 把
                #   我们的隐藏状态**从外部重置**。
                #   我们的前台与捕获都还在(regrabs=0/capchg=0), 但系统
                #   的光标可见性计数器被别人加回去了 -> 光标现形。
                #   守卫: 每 100ms 主动查一次 GetCursorInfo, 若光标又
                #   可见了就重新压一次计数器。这才是"任何位置都稳定"
                #   的关键 —— 不能假设只有我们能改可见性。
                if self._cursor_guard_visible():
                    self._guard_represses = \
                        getattr(self, "_guard_represses", 0) + 1
                    _set_cursor_visibility(False)
            return 0
        if msg == WM_CAPTURECHANGED and self._shown:
            # 捕获被抢走(用户点击拖拽了别的应用): 不重新捕获的话,
            # 按线程的 ShowCursor 计数器和类光标都会**失去效力**, 光标
            # 重新现形。Deskflow 靠前台窗口维持, 我们靠重新捕获。
            self._capture_changed += 1
            user32.SetCapture(hwnd)
            self._captured = (user32.GetCapture() == hwnd)
            return 0
        if msg == WM_MOUSEMOVE:
            # 收到鼠标消息 = **本线程确实是鼠标输入的接收者**。
            # 这是隐藏生效的前提(实测: 不捕获时本线程一条都收不到,
            # ShowCursor 计数器与类光标都管不着屏幕上画的那个光标)。
            self._moves_seen += 1
            # ★ 客户区坐标只作**诊断**用, 不再作为"物理 vs 注入"的判据。
            #   原因: 两个角色现在都用**全屏窗**(见 _do_hide 的 BUG2 说明),
            #   客户区坐标恒非零, 分不开两者。真正的判据是**注入时间戳**
            #   (见下方 _is_injected_motion), 它对窗口尺寸不敏感。
            #   Deskflow 能用客户区坐标, 是因为它用 1x1 窗 + 把光标钉在窗口
            #   中心; 我们的光标会移动, 不能用那套。
            cx = ctypes.c_short(lparam & 0xFFFF).value
            cy = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            if cx or cy:
                self._moves_nonzero += 1
            # 注入判据**无条件求值**: 它同时维护 _injected_seen 诊断计数,
            # 若只在某条件下才调用, 计数会漏, 验收脚本会误读成"判据失效"。
            injected = self._is_injected_motion()
            # ★★ 物理移动必须**连续多条**才算数(实测工具 diag_hider_state.py):
            #   窗口激活/SetWindowPos 会合成一条 WM_MOUSEMOVE(extra=0), 它
            #   可能被延迟投递到注入窗口期之外, 于是单条判据会把它误判成
            #   "用户动了本机鼠标"而收起 hider —— 症状正是用户报的
            #   "退回主机后副机光标依然存在不消失"。
            #   真物理移动一定产生**成串**消息, 而合成消息是孤立的, 所以
            #   要求连续 N 条非注入移动才恢复(Deskflow 用客户区坐标天然
            #   规避了这一点, 我们改用时间戳判据后必须自己补上)。
            #
            # ★ 再加一道"落定静默期": hide() 自己会注入 ±1px 抖动来提交
            #   光标可见性, 那两条消息可能**延迟**到注入窗口(0.25s)之后才
            #   送达, 于是被当成物理移动。实测 3 次探针里有 1 次因此收起
            #   hider(见 diag_hider_state.py)。静默期内一律不计物理移动。
            settling = (time.monotonic() - self._hide_mono) < self.HIDE_SETTLE_S
            if injected or not self._shown or settling:
                self._physical_streak = 0
            else:
                self._physical_streak += 1
            if (self._hide_on_motion and self._shown
                    and self._physical_streak >= self.PHYSICAL_STREAK_N):
                # 本机**物理**鼠标动了 → 收起 hider, 恢复光标(Deskflow 的
                # secondaryDeskProc 行为, 让副机自己的键鼠可用)。
                #
                # ★ 区分"注入 vs 物理"不能用 GetMessageExtraInfo(实测三个坑):
                #   ①消息合并后 extra 可能取链中某条(60 对 TAG 注入合并成 2 条
                #     消息, 1 条 extra 恰好非 TAG → hider 被误顶掉);
                #   ②窗口激活/SetWindowPos/捕获变化会**合成** WM_MOUSEMOVE,
                #     其 extra=0, 恰好长得像"物理移动";
                #   ③文档语义本身有歧义(可能读到下一条消息的 extra)。
                #   改用**注入时间戳**判据: 所有注入移动都从 InputInjector.
                #   move_relative/move_absolute 出发并打点(_last_move_ts),
                #   hider 收到移动消息时查"最近 N ms 内是否有注入" —— 注入流
                #   (主机转发)与抖动提交都在窗口期内, 一律忽略; 真物理移动
                #   (副机自己的鼠标)远离任何注入时刻, 恢复光标。
                self._physical_streak = 0
                self._do_show()
                return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # 判定"本机物理鼠标动了"所需的**连续**非注入移动消息条数。
    # 取 2 是为了滤掉孤立的合成消息(窗口激活/定位), 同时不影响真实使用 ——
    # 用户手一动就会产生远超 2 条的消息流。
    PHYSICAL_STREAK_N = 2

    # 连续抢前台失败多少次就放弃本轮(见 __init__ 的刹车②)。
    MAX_FG_STEAL_FAILS = 3

    def _steal_disabled(self, hwnd) -> bool:
        """本轮是否**放弃抢前台**(见 __init__ 的两个刹车)。

        返回 True 时调用方必须直接返回: 不抢前台、不重夺捕获、不压光标
        可见性 —— 因为这三件事都以"拿到前台"为前提, 抢不到时继续做只会
        变成每 100ms 一次的无效抖动。
        """
        if getattr(self, "_give_up_steal", False):
            return True
        fg = user32.GetForegroundWindow()
        if not fg or fg == hwnd:
            return False
        if self._is_foreign_fullscreen(fg):
            self._fg_steal_skipped = getattr(self, "_fg_steal_skipped", 0) + 1
            if not getattr(self, "_fullscreen_skip_logged", False):
                self._fullscreen_skip_logged = True
                print("[cursor] 前台是别的进程的**全屏窗口** -> 不抢焦点"
                      "(抢了会让全屏程序/远程桌面黑屏)。\n"
                      "         本轮穿越的光标可能保持可见, 键鼠仍归副机;"
                      "按 Ctrl+Alt+Esc 可随时收回控制权。", flush=True)
            return True
        return False

    def _is_foreign_fullscreen(self, hwnd) -> bool:
        """该窗口是否别的进程的全屏窗口(见模块级 is_foreign_fullscreen)。"""
        return is_foreign_fullscreen(hwnd)

    @staticmethod
    def _window_class(hwnd) -> str:
        try:
            user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR,
                                             ctypes.c_int]
            user32.GetClassNameW.restype = ctypes.c_int
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            return buf.value
        except Exception:  # noqa: BLE001
            return ""

    # hide() 之后的"落定静默期"(秒): 此期间收到的移动消息一律不计入物理
    # 移动。用于吸收 hide() 自身抖动注入的延迟回声(见 _proc 的说明)。
    HIDE_SETTLE_S = 0.4

    # 注入窗口期长度: 注入事件本身产生/合并的 WM_MOUSEMOVE 都应落在
    # 窗口内(实测 SendInput 到 wnd proc 收到 < 5ms), 但又要小到"用户
    # 一动手就恢复"。0.25s 与旧 MOTION_GRACE_S 同级, 但窗口由**最近
    # 一次注入**锚定而非 hide() 时刻: 主机持续转发注入时窗口一直在
    # (不误恢复 = 修掉"副机光标隐藏不掉"), 主机停注入 0.25s 后副机
    # 物理鼠标一动立即生效(旧宽限期是死等 0.3s, 与注入无关)。
    INJECTED_MOVE_WINDOW_S = 0.25

    def _is_injected_motion(self) -> bool:
        """当前 WM_MOUSEMOVE 是否处于注入窗口期(最近 INJECTED_MOVE_WINDOW_S
        秒内发生过注入移动)。物理移动不会恰好落在持续注入流的窗口里,
        而激活/定位合成的移动(extra=0)必然紧邻 hide() 的抖动注入。"""
        from .input import InputInjector   # 局部导入避免 input↔cursor 环
        injected = InputInjector.last_move_age() <= self.INJECTED_MOVE_WINDOW_S
        if injected:
            self._injected_seen += 1
        return injected

    def _cursor_guard_visible(self) -> bool:
        """光标当前是否**又变回可见**了(被外部软件重置)。

        ★ 为什么要这个守卫(实测): 光标停在空桌面时隐藏会在 1.2s 后失效,
          而记事本位置却稳定。诊断发现 Z 序最顶是 **Quicker 的悬浮面板**
          (HwndWrapper[Quicker.exe...]) —— 这类第三方工具显示/隐藏悬浮
          层时会调 ShowCursor(TRUE), 把系统的可见性计数器加回去。
          我们无法阻止别人调 ShowCursor, 只能**周期性检查并重新压下去**。

        用 GetCursorInfo 的 CURSOR_SHOWING 标志判定(真实屏幕状态),
        而不是 ShowCursor 的返回值(那只是本线程计数器的快照)。
        """
        try:
            class CURSORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD),
                            ("flags", wintypes.DWORD),
                            ("hCursor", wintypes.HANDLE),
                            ("ptScreenPos", wintypes.POINT)]
            ci = CURSORINFO()
            ci.cbSize = ctypes.sizeof(CURSORINFO)
            user32.GetCursorInfo.argtypes = [ctypes.c_void_p]
            user32.GetCursorInfo.restype = wintypes.BOOL
            if not user32.GetCursorInfo(ctypes.byref(ci)):
                return False
            CURSOR_SHOWING = 0x00000001
            return bool(ci.flags & CURSOR_SHOWING)
        except Exception:  # noqa: BLE001
            return False

    def _do_hide(self) -> None:
        """隐藏光标(幂等)。

        ★★★ 幂等是**必须**的(2026-10-22 修, 真机日志实证):
          此前每次 hide() 都无条件重走整套流程 —— 全屏 SetWindowPos +
          SetTimer + SetCapture + 抢前台 + 两次 ±1px 注入 + 两个 sleep,
          单次 ≈ 50ms。而副机的回传分支此前会对着同一批积压事件连续
          hide() 上百次(实测一次跨界 145 次), hider 线程要排 **7 秒**
          才轮得到后面的 show() —— 用户看到的就是"光标很久不出现,
          出现时又贴在边缘"。保活定时器(每 100ms)本来就负责维持隐藏
          状态, 重复 hide() 没有任何额外收益。
        """
        if self._shown:
            return
        from .input import get_cursor_pos
        # 新一轮穿越: 刹车复位(上一轮放弃过不代表这一轮也抢不到)
        self._give_up_steal = False
        self._fg_fail_streak = 0
        self._fullscreen_skip_logged = False
        # ★★★ 生产组合(低级钩子吞掉物理输入)下的关键差异:
        #   ShowCursor 的按线程计数器只有在该线程**处理鼠标输入**时才被
        #   提交到屏幕。钩子把物理事件全部吞掉后, 没有任何鼠标消息流动,
        #   hider 线程的计数器(-1)永远不提交 -> 光标停在原地可见
        #   (用户实测"光标停靠在边缘"而非隐藏)。diag_repro 之所以曾经
        #   全绿, 是因为它用**注入**事件验证 —— 注入事件会流动, 掩盖了
        #   生产环境"零消息"的实况。
        #   Deskflow 的两个分支合起来正是解法:
        #   * primary(deskLeave :600-623): 把 desk 窗口**激活为前台**
        #     (EnableWindow + SetActiveWindow + AttachThreadInput +
        #     SetForegroundWindow)。窗口激活自带一次光标状态提交,
        #     且不依赖任何鼠标消息(内核直接做), 钩子吞不掉。
        #   * secondary(deskLeave :625-646): 1x1 窗口摆到光标正下方 +
        #     SetCapture + sleep(0.03) + **注入一次归中移动**
        #     (deskMouseMove)强制产生一条 WM_MOUSEMOVE 提交计数器。
        # ★★★ BUG2 的关键修正(实测结论, 见 tools/diag_bug2_approach.py):
        #   **两个角色都用全屏窗**。此前副机(hide_on_motion)照搬 Deskflow 用
        #   1x1 窗, 那是错的 —— Deskflow 的 1x1 窗之所以有效, 是因为它同时
        #   把光标**钉在窗口中心**(deskLeave 末尾的 deskMouseMove(m_xCenter,
        #   m_yCenter)); 而我们的副机光标是**会移动**的(主机转发注入), 窗口
        #   若不跟随, 光标一离开那 1x1 像素就立刻现形 —— 用户报的
        #   "副机退回主机后, 副机的光标依然存在不消失"。
        #
        #   实测(GetCursorInfo 判据, tools/diag_bug2_approach.py):
        #     1x1 跟随窗: hide() 后与 60 次注入期间 showing 恒为 True(失败)
        #     全屏窗    : hide() 后与 60 次注入期间 showing 恒为 False(成功)
        #   原因是 WM_MOUSEMOVE 会被系统**合并**(60 次注入只送达个位数条),
        #   跟随逻辑永远追不上光标; 全屏窗不需要跟随。
        #
        #   代价: 全屏窗的客户区坐标恒非零, 于是"注入 vs 物理"不能再用
        #   客户区坐标区分, 只能用**注入时间戳**判据 —— 那本来就是主判据
        #   (见 _proc 的长注释), 所以这个代价是零。
        from .input import virtual_desktop
        d = virtual_desktop()
        user32.SetWindowPos(self._hwnd, HWND_TOP, d.left, d.top,
                            d.width, d.height,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)

        # --- 激活 + 抢前台(这是光标隐藏能生效的**必要条件**) ---
        #
        # ★★★ 2026-10-19 第二次纠正(用户: "光标没消失...并非一定在这个
        #   条件下才会触发, 你需要全面审查整个跨屏幕光标隐藏功能"):
        #
        #   **旧实现刻意不抢前台, 那是错的。**
        #   MSDN 对 SetCapture 的明确限制:
        #     "This function only captures the mouse when the mouse button
        #      is down or the window is in the foreground."
        #     "Only the foreground window can capture the mouse."
        #   旧代码只做 SetActiveWindow 而不抢前台 -> SetCapture 在
        #   "鼠标按钮未按下 + 非前台"时**根本不生效** -> 本线程收不到
        #   鼠标消息 -> ShowCursor 的按线程计数器不提交 ->
        #   **光标一直可见**。这与用户观察完全吻合: 光标停在别的程序
        #   窗口上层时最容易察觉, 但其实是**任何位置都可能失败**
        #   (取决于当时是否有按钮按下/窗口是否恰好是前台)。
        #
        #   ★ 为什么以前会得出"抢前台会杀死键盘钩子"的结论?
        #     那次实验用的是 **tkinter 主窗口**(普通 GUI 窗口), 实测
        #     kbd_calls 8->0。但 hider 是完全不同的窗口类型:
        #       WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | 全屏 | 类光标全透明
        #     重新实测(tools/exp_hider_foreground_hook.py):
        #       A. 基线(焦点在资源管理器)         kbd_calls +2
        #       B. **hider 抢前台后**              kbd_calls +2  同样可达!
        #       C. 抢前台 + 同一线程 SetCapture   光标 vis=False ✅
        #     即: hider 抢前台**不会**破坏键盘钩子。过去那条结论
        #     被错误地从"tkinter 主窗口"外推到了"所有窗口"。
        #
        #   ★ 还必须注意 SetCapture 是**按线程**的: 必须在 hider 自己的
        #     线程里调用(本函数正是运行在 hider 线程的消息循环里)。
        #     从别的线程调 SetCapture(hider) 实测 GetCapture() 返回 None。
        #
        #   这样也不用再依赖"抖动注入来提交计数器"那条脆弱链路 ——
        #   前台窗口的光标状态会被内核直接提交。
        self._stolen_foreground = user32.GetForegroundWindow()
        # ★★ 抢前台的刹车(见 __init__): 前台若是**别的进程的全屏窗口**,
        #   这里就不抢。代价是这一轮光标隐藏不了(SetCapture 需要前台),
        #   但换来"不会把全屏程序/远程桌面打断成黑屏" —— 后者是灾难性的,
        #   前者只是体验降级, 而且 Ctrl+Alt+Esc 随时能收回控制权。
        if self._steal_disabled(self._hwnd):
            # 拿不到前台 -> SetCapture 必然失败(MSDN: 只有前台窗口能捕获),
            # 所以这里连试都不试, 也不去压光标可见性: 本轮以"光标可见"运行,
            # 只保证 hider 窗口存在且已置顶, 其余交给上层。
            self._captured = False
            self._counter_after_hide = _set_cursor_visibility(True)
            self._is_foreground = False
            self._activated = False
            self._hide_mono = time.monotonic()
            self._physical_streak = 0
            self._shown = True
            return
        # 诊断: 完整隐藏流程执行次数(幂等提前返回的那条路不计入)
        self._hide_exec = getattr(self, "_hide_exec", 0) + 1
        user32.EnableWindow(self._hwnd, TRUE)
        user32.SetActiveWindow(self._hwnd)
        # 用 AttachThreadInput 强制抢前台(照搬 MSWindowsDesks.cpp :617-622)
        fg = user32.GetForegroundWindow()
        if fg and fg != self._hwnd:
            fg_tid = user32.GetWindowThreadProcessId(fg, None)
            my_tid = kernel32.GetCurrentThreadId()
            attached = False
            if fg_tid and fg_tid != my_tid:
                attached = bool(user32.AttachThreadInput(fg_tid, my_tid, TRUE))
            try:
                user32.BringWindowToTop(self._hwnd)
                user32.SetForegroundWindow(self._hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(fg_tid, my_tid, FALSE)
        # 诊断: 前台是否真的抢到了(这是光标能隐藏的前提)
        self._is_foreground = (user32.GetForegroundWindow() == self._hwnd)
        self._activated = (user32.GetActiveWindow() == self._hwnd)

        # --- 捕获 + 计数器(照搬 :633 + setCursorVisibility) ---
        # 现在已是前台, SetCapture 才会真正生效。
        user32.SetCapture(self._hwnd)
        self._captured = (user32.GetCapture() == self._hwnd)
        self._counter_after_hide = _set_cursor_visibility(False)

        # ★ 启动保活定时器: 周期重验"前台 + 捕获"(见 WM_TIMER 分支)。
        #   前台可能被别的程序抢走(用户点了别的窗口), 捕获也可能被
        #   可交互控件抢走 —— 两者任一丢失, 光标就会现形。
        user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t,
                                    wintypes.UINT, ctypes.c_void_p]
        user32.SetTimer.restype = ctypes.c_size_t
        user32.SetTimer(self._hwnd, KEEPALIVE_TIMER_ID, KEEPALIVE_MS, None)

        # --- 强制提交(照搬 :644-645) ---
        # "windows can take a while to hide the cursor, so wait a few
        #  milliseconds to ensure the cursor is hidden before centering"
        # 前台已能提交光标状态, 这里保留抖动作为**双保险**
        # (生产中物理事件被钩子吞掉, 只能靠注入产生一次消息)。
        time.sleep(0.03)
        from .input import InputInjector
        InputInjector.move_relative(1, 0)
        time.sleep(0.02)
        InputInjector.move_relative(-1, 0)

        # 静默期起点: 从这一刻起 HIDE_SETTLE_S 内的移动消息不算物理移动
        # (吸收上面抖动注入的延迟回声, 见 HIDE_SETTLE_S)。
        self._hide_mono = time.monotonic()
        self._physical_streak = 0
        self._shown = True

    def _do_show(self) -> None:
        # 诊断: 收起次数(与 _hide_exec 一起看, 突增 = hider 在反复抖动)
        self._show_exec = getattr(self, "_show_exec", 0) + 1
        # ★ 先置 _shown=False 再 ReleaseCapture: 释放捕获可能触发
        #   WM_CAPTURECHANGED, 若那时 _shown 仍为 True, 重捕获逻辑会把
        #   捕获抢回来 -> 光标永远隐藏不掉(自己造的死锁)。
        self._shown = False
        # 停保活定时器(不再需要重验捕获)
        user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
        user32.KillTimer.restype = wintypes.BOOL
        user32.KillTimer(self._hwnd, KEEPALIVE_TIMER_ID)
        user32.ReleaseCapture()
        self._captured = False
        user32.SetWindowPos(self._hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                            | SWP_HIDEWINDOW)
        user32.EnableWindow(self._hwnd, FALSE)   # 照搬 deskEnter :558
        self._counter_after_show = _set_cursor_visibility(True)
        self._is_foreground = False
        # ★★★ 必须把前台**还给用户原来的窗口**(2026-10-19 修正)。
        #   _do_hide 现在会抢前台(这是 SetCapture 生效的前提), 所以在
        #   退出穿越时必须恢复, 否则用户的键盘焦点会停在那个全屏透明
        #   窗口上 —— 表现为"回到主机后打字没反应, 要点一下窗口才行"。
        #   恢复方式同抢: AttachThreadInput + SetForegroundWindow。
        prev = self._stolen_foreground
        if prev:
            try:
                fg = user32.GetForegroundWindow()
                if fg != prev:
                    fg_tid = user32.GetWindowThreadProcessId(fg, None) \
                        if fg else 0
                    my_tid = kernel32.GetCurrentThreadId()
                    attached = False
                    if fg_tid and fg_tid != my_tid:
                        attached = bool(
                            user32.AttachThreadInput(fg_tid, my_tid, TRUE))
                    try:
                        user32.SetForegroundWindow(prev)
                    finally:
                        if attached:
                            user32.AttachThreadInput(fg_tid, my_tid, FALSE)
            except Exception:  # noqa: BLE001
                pass
        self._stolen_foreground = None
