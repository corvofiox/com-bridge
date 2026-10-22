"""输入子系统: 原始输入捕获 + SendInput 注入 + 越界几何判定。

实现要点(Windows):
  - 捕获: 注册原始输入(WM_INPUT, RIDEV_INPUTSINK)接收鼠标/键盘事件。
    比 SetWindowsHookEx(WH_MOUSE_LL) 更稳, 不受 UIPI 限制, 且能拿到
    绝对/相对位移与 usbUsagePage 信息。
  - 越界判定: 用 GetCursorPos 拿光标全局坐标, 与目标屏幕矩形比较;
    进入副屏幕一侧(带粘滞 hysteresis)时, 后续事件转发给副电脑,
    本机光标吸附在边界。
  - 注入: 副电脑侧用 SendInput 注入 INPUT 结构。
"""

from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import (
    POINTER as P,
    byref,
    c_int,
    c_uint,
    c_ushort,
    c_void_p,
    windll,
    wintypes,
)
from dataclasses import dataclass

from .protocol import (
    EV_KEY,
    EV_MOUSE_BUTTON,
    EV_MOUSE_HWHEEL,
    EV_MOUSE_MOVE_REL,
    EV_MOUSE_WHEEL,
    MB_DOWN,
    MB_UP,
    KEY_DOWN,
    KEY_EXTENDED,
    KEY_UP,
)


user32 = windll.user32
kernel32 = windll.kernel32

# ctypes.wintypes 没有 WNDPROC/WNDCLASSW(那些在 ctypes.windll.user32 的
# 类型命名空间里也没有), 只能自己声明。WNDPROC 必须用 WINFUNCTYPE 且
# 实例要常驻, 否则回调时野指针 -> access violation。
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", c_int),
        ("cbWndExtra", c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]

# --- 常量 ---------------------------------------------------------------
WM_INPUT = 0x00FF
RIDEV_INPUTSINK = 0x00000100
# ★★★ 注销某条 RawInput 注册: usUsage + RIDEV_REMOVE。见 RawInputListener
#   的 keyboard 说明 —— 这是我们用来"解掉键盘 RawInput 与低级钩子冲突"的手段。
RIDEV_REMOVE = 0x00000001
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

# SystemParametersInfo 的鼠标相关动作(用于临时禁用指针加速)
SPI_GETMOUSE = 0x0003
SPI_SETMOUSE = 0x0004
SPI_GETMOUSESPEED = 0x0070
SPI_SETMOUSESPEED = 0x0071

RI_MOUSE_LEFT_BUTTON_DOWN = 0x0001
RI_MOUSE_LEFT_BUTTON_UP = 0x0002
RI_MOUSE_RIGHT_BUTTON_DOWN = 0x0004
RI_MOUSE_RIGHT_BUTTON_UP = 0x0008
RI_MOUSE_MIDDLE_BUTTON_DOWN = 0x0010
RI_MOUSE_MIDDLE_BUTTON_UP = 0x0020
RI_MOUSE_BUTTON_4_DOWN = 0x0040
RI_MOUSE_BUTTON_4_UP = 0x0080
RI_MOUSE_BUTTON_5_DOWN = 0x0100
RI_MOUSE_BUTTON_5_UP = 0x0200
RI_MOUSE_WHEEL = 0x0400
RI_MOUSE_HORIZONTAL_WHEEL = 0x0800   # 倾斜滚轮(Deskflow 也处理 WM_MOUSEHWHEEL)

# --- API 原型声明 ---------------------------------------------------------
# 不声明 argtypes 时 ctypes 用默认 int(32位) 传 HWND/msg, 64位下会截断或
# 报 "int too long to convert", 必须在 import 后一次性配好。
_MONITOR_ENUM_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(wintypes.RECT), ctypes.c_void_p,
)
for _fn, _proto in (
    (user32.DefWindowProcW, (wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)),
    (user32.GetMessageW, (c_int, P(wintypes.MSG), wintypes.HWND,
                          wintypes.UINT, wintypes.UINT)),
    (user32.TranslateMessage, (wintypes.BOOL, P(wintypes.MSG))),
    (user32.DispatchMessageW, (wintypes.LPARAM, P(wintypes.MSG))),
    (user32.RegisterClassW, (wintypes.ATOM, P(WNDCLASSW))),
    (user32.CreateWindowExW, (wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR,
                              wintypes.LPCWSTR, wintypes.DWORD, c_int, c_int,
                              c_int, c_int, wintypes.HWND, wintypes.HMENU,
                              wintypes.HINSTANCE, wintypes.LPVOID)),
    (user32.RegisterRawInputDevices, (wintypes.BOOL, c_void_p, wintypes.UINT,
                                      wintypes.UINT)),
    (user32.GetRawInputData, (wintypes.UINT, wintypes.HANDLE, wintypes.UINT,
                              c_void_p, P(wintypes.UINT), wintypes.UINT)),
    (user32.GetCursorPos, (wintypes.BOOL, P(wintypes.POINT))),
    (user32.SetCursorPos, (wintypes.BOOL, c_int, c_int)),
    # SendInput 的原型在 INPUT 结构定义之后单独声明(见文件下方),
    # 因为它的参数类型 POINTER(INPUT) 依赖那个结构。
    (user32.EnumDisplayMonitors, (wintypes.BOOL, wintypes.HDC, wintypes.LPRECT,
                                  _MONITOR_ENUM_PROC, wintypes.LPARAM)),
    (user32.GetSystemMetrics, (c_int, c_int)),
    (user32.PostMessageW, (wintypes.BOOL, wintypes.HWND, wintypes.UINT,
                           wintypes.WPARAM, wintypes.LPARAM)),
    (kernel32.GetModuleHandleW, (wintypes.HMODULE, wintypes.LPCWSTR)),
):
    _fn.restype = _proto[0]
    _fn.argtypes = list(_proto[1:])

RI_KEY_MAKE = 0
RI_KEY_BREAK = 1
RI_KEY_E0 = 0x02
RI_KEY_E1 = 0x04


# --- 结构 ---------------------------------------------------------------

class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", c_ushort),
        ("usUsage", c_ushort),
        ("dwFlags", c_uint),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", c_uint),
        ("dwSize", c_uint),
        ("hDevice", c_void_p),
        ("wParam", c_uint),
    ]


class RAWMOUSE(ctypes.Structure):
    """RAWMOUSE —— 布局必须严格照搬 Windows 的 winuser.h。

    ★★ 这里有一个**极隐蔽、后果极严重**的布局陷阱(实测踩到, 用户症状是
       "进入副机后点击完全没作用, 但鼠标能移动"):

    Windows 的定义是
        typedef struct tagRAWMOUSE {
            USHORT usFlags;
            union {
                ULONG ulButtons;            // ★ union 按 4 字节对齐
                struct {
                    USHORT usButtonFlags;
                    USHORT usButtonData;
                };
            };
            ULONG  ulRawButtons;
            LONG   lLastX;
            LONG   lLastY;
            ULONG  ulExtraInformation;
        } RAWMOUSE;

    union 里有 `ULONG`, 所以 union 的**对齐是 4 字节** —— 编译器会在
    `usFlags` 之后插入 **2 字节填充**, `usButtonFlags` 实际落在
    **offset 4**, `usButtonData` 在 offset 6。

    而 ctypes 按结构体字段自然对齐: 2 字节的 usFlags 后面直接接 2 字节的
    usButtonFlags -> **offset 2**。于是我们读到的"按钮标志"其实是
    usButtonData 的高半部分, 真正的标志位被读成了 0。

    后果(用真实原始字节证实):
        左键按下: 真实字节 00 00 00 00 | 01 00 00 00 ...
                  -> 正确解析 usButtonFlags = 0x0001
                  -> 错误布局解析 usButtonFlags = 0x0000 (标志永远为 0)
        所以 `_emit_mouse` 里每一个 `if bf & RI_MOUSE_*` 都**永不成立**,
        **点击/滚轮事件一次都没有被转发出去**; 而位移字段 lLastX/lLastY
        恰好在 offset 12(两边一致), 所以移动一直正常 —— 症状正是
        "鼠标能跨屏移动, 但点击完全没作用到副机"。

    修法: 显式插入一个 2 字节的占位字段, 把按钮字段顶到 offset 4/6。
    断言见 tests/test_rawmouse_layout.py(用真实捕获字节做回归)。
    """

    _fields_ = [
        ("usFlags", c_ushort),
        # ★ 2 字节对齐填充: 让后面 union 从 offset 4 开始(见类文档)
        ("_pad_buttons_align", c_ushort),
        ("usButtonFlags", c_ushort),
        ("usButtonData", c_ushort),
        ("ulRawButtons", c_uint),
        ("lLastX", c_int),
        ("lLastY", c_int),
        ("ulExtraInformation", c_uint),
    ]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", c_ushort),
        ("Flags", c_ushort),
        ("Reserved", c_ushort),
        ("VKey", c_ushort),
        ("Message", c_uint),
        ("ExtraInformation", c_uint),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", c_int),
        ("dy", c_int),
        ("mouseData", c_uint),
        ("dwFlags", c_uint),
        ("time", c_uint),
        ("dwExtraInfo", c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", c_ushort),
        ("wScan", c_ushort),
        ("dwFlags", c_uint),
        ("time", c_uint),
        ("dwExtraInfo", c_void_p),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", c_uint),
        ("union", _INPUTUNION),
    ]


# SendInput(UINT cInputs, LPINPUT pInputs, int cbSize)
# 单独声明: 参数类型依赖上面刚定义的 INPUT 结构。
# 注意上面那张表的约定是 (restype, *argtypes) —— 第一个元素是返回值类型,
# 其余才是参数。曾把它误写成 (UINT, c_void_p, c_int), 于是 UINT 被当成
# restype, 只剩 2 个 argtypes, 调用时传 3 个参数直接报
# "this function takes 2 arguments (3 given)" —— 注入功能整体失效。
user32.SendInput.restype = c_uint
user32.SendInput.argtypes = [c_uint, P(INPUT), c_int]

# SystemParametersInfoW: 第四个参数是 PVOID(指向结构/整数), 不声明会按
# 32 位截断指针 -> 静默失败或崩溃。
user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         c_void_p, wintypes.UINT]


# --- 指针加速 ------------------------------------------------------------

# Windows 对**注入的**相对位移同样应用指针加速曲线(实测: 请求 (40,25)
# 实际走 (92,57), 放大 2.29 倍)。这会让副机鼠标手感与主机完全不一致
# (用户会觉得"飘、不听使唤、划不动")。
#
# Deskflow 的 deskMouseRelativeMove 专门处理它, 注释原文:
#   "relative moves are subject to cursor acceleration which we don't want.
#    so we disable acceleration, do the relative move, then restore
#    acceleration."
# 做法: SPI_GETMOUSE 存旧值 -> SPI_SETMOUSE {0,0,0}(关加速) ->
#       SendInput -> SPI_SETMOUSE 还原。
#
# ★ 为什么不干脆永久关掉用户的加速: 那会改掉用户自己的鼠标手感(桌面
#   日常使用立刻能感觉到), 且程序崩溃后会**永久残留**。临时改+立即还原
#   只影响我们注入的这一条事件, 与 Deskflow 的选择一致。
_ACCEL_LOCK = threading.Lock()

_SPI_ARR3 = c_int * 3


def _save_mouse_accel():
    """返回 (阈值1, 阈值2, 加速级别)；失败返回 None。"""
    a = _SPI_ARR3(0, 0, 0)
    if not user32.SystemParametersInfoW(SPI_GETMOUSE, 0, ctypes.byref(a), 0):
        return None
    return (a[0], a[1], a[2])


def _set_mouse_accel(triple) -> bool:
    a = _SPI_ARR3(*triple)
    return bool(user32.SystemParametersInfoW(SPI_SETMOUSE, 0,
                                             ctypes.byref(a), 0))


def _get_mouse_speed():
    """读指针速度档位(1..20, 10=默认)。失败返回 None。

    ★ 这是**第二个**位移放大机制, 和加速曲线相互独立:
      加速曲线(SPI_SETMOUSE)管"快动时额外放大",
      速度档位(SPI_SETMOUSESPEED)管"所有位移的整数倍放缩"。
      只关前者不关后者, 注入的位移仍会被放大 —— 用户就会感觉
      "副屏鼠标显著更快"。Deskflow 两者都关(见 move_relative 的说明)。
    """
    v = ctypes.c_int(0)
    if not user32.SystemParametersInfoW(SPI_GETMOUSESPEED, 0,
                                        ctypes.byref(v), 0):
        return None
    n = int(v.value)
    # 夹到合法范围: 0 或越界值都视为"读不到"
    return n if 1 <= n <= 20 else None


def _set_mouse_speed(speed: int) -> bool:
    """设指针速度档位。1 = 最慢(1:1), 10 = 默认, 20 = 最快。"""
    try:
        s = max(1, min(20, int(speed)))
        return bool(user32.SystemParametersInfoW(
            SPI_SETMOUSESPEED, 0, ctypes.c_void_p(s), 0x0001 | 0x0002))
    except Exception:  # noqa: BLE001
        return False


# --- 越界几何 -----------------------------------------------------------

@dataclass
class ScreenRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


class EdgeDetector:
    """带 hysteresis 的越界判定。

    光标在 self 屏幕内时不转发; 抵达 self 屏幕朝向 peer 的那条**边**并保持
    `confirm_frames` 帧后切换到"已越界"状态, 之后所有输入事件转发给 peer。

    ★ 边界必须是**光标可达的像素**。Windows 把光标钳在
      x ∈ [rect.left, rect.right-1]、y ∈ [rect.top, rect.bottom-1]。
      早先写成 `x >= rect.right`, 而 1920 宽屏的最右可达 x 是 1919, 于是
      条件永远为假 —— 光标根本越不出去(鼠标无法跨屏)。
      正确条件是"到达最右/最下那一列/行": `x >= rect.right - 1`。
      判断"是否已离开屏幕"的写法在这种钳制下不可能成立, 必须改成"是否
      抵边"。迟滞带也相应地从边界内侧量起, 否则会在贴边时反复进出。
    """

    # 抵边判定的容差(像素): 光标落在距边界这么多像素内即视为抵边。
    # 取 2 是"手感 vs 误触"的折中: 取 1 时用户必须精确推到最外一列,
    # 手抖 1px 就不触发, 实际体验是"怎么推都过不去"; 取 2~3 明显更易
    # 触发, 而 2px 在 1920 宽屏上占比极小, 不会造成屏内误触发。
    EDGE_TOLERANCE = 2

    def __init__(self, self_rect: ScreenRect, peer_rect: ScreenRect,
                 side: str, confirm_frames: int = 2, hysteresis: int = 8,
                 edge_tolerance: int | None = None):
        self.self_rect = self_rect
        self.peer_rect = peer_rect
        self.side = side  # "left" | "right" | "top" | "bottom"
        self.confirm_frames = confirm_frames
        self.hysteresis = hysteresis
        if edge_tolerance is not None:
            self.EDGE_TOLERANCE = edge_tolerance
        self._outside = False
        self._streak = 0

    def _at_edge(self, x: int, y: int) -> bool:
        """光标是否抵达朝向 peer 的那条边(用可达像素判定)。"""
        r = self.self_rect
        tol = self.EDGE_TOLERANCE
        if self.side == "left":
            return x <= r.left + tol
        if self.side == "right":
            return x >= r.right - 1 - tol
        if self.side == "top":
            return y <= r.top + tol
        if self.side == "bottom":
            return y >= r.bottom - 1 - tol
        return False

    def _back_inside(self, x: int, y: int) -> bool:
        """是否已明显退回屏内(超出迟滞带), 用于退出越界态。"""
        r = self.self_rect
        h = self.hysteresis
        if self.side == "left":
            return x >= r.left + h
        if self.side == "right":
            return x <= r.right - 1 - h
        if self.side == "top":
            return y >= r.top + h
        if self.side == "bottom":
            return y <= r.bottom - 1 - h
        return False

    def maybe_exit(self, x: int, y: int) -> bool:
        """在穿越态下检查是否该退出; 返回 True 表示已退出。

        ★ 这是穿越态**最可靠的退出依据**: 早先之所以放弃按坐标判退出,
          是因为那时会把自己注入的钳制误当成"光标还在边界"。现在穿越期间
          完全不再注入(见 Server._on_input_event), 读到的坐标就是真实的
          物理光标位置, 所以"光标明显离开边界"可以放心用作退出条件。

        没有它会出现什么(用户实测报告): 用户往回拉但不足
        RETURN_INTENT_S(0.7s) 就停下, 意图计时被重置, 穿越态**永不退出**
        —— 此后每一次鼠标位移都被无条件转发, 表现为"副电脑光标跟着主机
        一起动、主机还没到边缘副机光标就动了"。
        """
        if not self._outside:
            return False
        if self._back_inside(x, y):
            self.reset()
            return True
        return False

    def update(self, x: int, y: int) -> bool:
        """喂入光标全局坐标, 返回当前是否应把输入转发给 peer。"""
        if self._outside:
            if self._back_inside(x, y):
                self._outside = False
                self._streak = 0
        else:
            if self._at_edge(x, y):
                self._streak += 1
                if self._streak >= self.confirm_frames:
                    self._outside = True
            else:
                self._streak = 0
        return self._outside

    @property
    def is_outside(self) -> bool:
        return self._outside

    def reset(self) -> None:
        self._outside = False
        self._streak = 0


def clamp_to_edge(x: int, y: int) -> tuple[int, int]:
    """把光标吸附到本机屏幕边界**内侧**, 防止越界后仍继续触发。

    ★ 上界必须用 `right - 1` 而不是 `right`: Windows 的光标可达范围是
      [left, right-1], 传 right 会被钳回 right-1 但逻辑上仍"贴着边界",
      导致越界态无法稳定退出、光标在边界抖动。
    """
    r = _virtual_screen()
    return (max(r.left, min(r.right - 1, x)),
            max(r.top, min(r.bottom - 1, y)))


def _enable_dpi_awareness() -> str:
    """声明本进程为 **per-monitor DPI 感知**, 必须在任何坐标 API 之前调用。

    ★★ 为什么必须(用户实测 BUG3① 的真正根因):
      进程默认是 `DPI_UNAWARE`。此时 Windows 会对**所有**坐标类 API 做
      **DPI 虚拟化**: 系统按当前显示缩放把真实像素换算成"逻辑像素"再返回。
      实测本机(96 DPI, 100%)看不出问题, 但副机 2560x1600 通常是高 DPI 屏
      (150%/200%), 于是:
        * `GetSystemMetrics(SM_CXVIRTUALSCREEN)` 返回**缩放后**的尺寸
          (2560 在 200% 下读成 1280);
        * `GetCursorPos` 返回逻辑坐标, `SetCursorPos`/`SendInput` 也按逻辑
          坐标解释 —— 于是"按比例算出的像素"与"屏幕真实像素"不是同一套
          坐标系, 落点自然偏移, 表现为**方位没有等比例对齐**。
      声明 per-monitor 感知后, 所有 API 统一返回**物理像素**, 与比例换算
      的假设一致(实测: 未声明时读到 UNAWARE; 声明后坐标与真实像素一致)。

    选 per-monitor v2(而非 system-aware): 多显示器各自缩放不同时,
    system-aware 仍会被虚拟化, per-monitor 才是"永远拿到物理像素"的那档。

    调用顺序很重要: 必须在**第一次调用** GetCursorPos/GetSystemMetrics/
    EnumDisplayMonitors 之前设置, 否则那些 API 的结果已被虚拟化缓存。
    返回实际生效的模式名(供 selfcheck/GUI 显示, 便于排查)。

    ★ 幂等: 重复调用不会报错。Windows 对"已设置过"的再次设置会返回失败
      (ERROR_ACCESS_DENIED), 若不特判就会误报 "设置失败"。因此这里先查
      当前状态, 已达标就直接返回。
    """
    import ctypes
    from ctypes import wintypes

    # ⓪ 已经设置过(或本进程已是 per-monitor)就直接返回 —— 保证幂等
    cur = dpi_awareness()
    if cur == "per-monitor":
        return "per-monitor(已生效)"

    # ① 首选 SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2, Win10 1703+)
    #    值 -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:  # noqa: BLE001
        pass
    # ② 退: shcore.SetProcessDpiAwareness(2 = PER_MONITOR_DPI_AWARE, Win8.1+)
    try:
        shcore = ctypes.windll.shcore
        if shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except Exception:  # noqa: BLE001
        pass
    # ③ 退: 老 API(只能到 system-aware, 但远好于 unaware)
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except Exception:  # noqa: BLE001
        pass
    # ④ 兜底: 若上面都"失败"但实际已经是 system/per-monitor, 也算成功
    final = dpi_awareness()
    if final in ("per-monitor", "system"):
        return f"{final}(已生效)"
    return "unaware(设置失败!)"


def dpi_awareness() -> str:
    """查询当前进程的 DPI 感知模式(诊断用, 不修改状态)。"""
    import ctypes
    from ctypes import wintypes
    try:
        shcore = ctypes.windll.shcore
        shcore.GetProcessDpiAwareness.restype = ctypes.c_long
        shcore.GetProcessDpiAwareness.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
        val = ctypes.c_int()
        if shcore.GetProcessDpiAwareness(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(val)) == 0:
            return {0: "unaware", 1: "system", 2: "per-monitor"}.get(
                val.value, f"unknown({val.value})")
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


# ★★★ 模块导入即声明 DPI 感知(必须在本模块的**第一个坐标 API 调用之前**)。
#   本模块拥有全部坐标 API(GetCursorPos/SetCursorPos/SendInput/
#   GetSystemMetrics/EnumDisplayMonitors), 在这里声明可以保证**任何**调用方
#   都不可能绕过 —— 包括直接 `Server()` 而不经 `cli.main()` 的路径。
#   函数幂等, 因此 cli.main()/gui.main() 里的再次调用无副作用。
#   放在函数定义之后是因为 Python 需要先绑定函数名。
_DPI_MODE = _enable_dpi_awareness()


def _virtual_screen() -> ScreenRect:
    left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
    width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    height = user32.GetSystemMetrics(79) # SM_CYVIRTUALSCREEN
    return ScreenRect(left, top, left + width, top + height)


_MONITOR_ENUM_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(wintypes.RECT), ctypes.c_void_p,
)


def _make_enum_cb(out: list):
    """EnumDisplayMonitors 回调: 每台显示器追加一个 ScreenRect。"""

    @_MONITOR_ENUM_PROC
    def cb(hmon, hdc, rect, data):
        r = rect.contents
        out.append(ScreenRect(r.left, r.top, r.right, r.bottom))
        return 1

    return cb


def enumerate_monitors() -> list[ScreenRect]:
    out: list[ScreenRect] = []
    cb = _make_enum_cb(out)
    # 第 5 个参数是 LPARAM(整数), 不能传 None —— ctypes 会报
    # "argument 4: TypeError: 'NoneType' object cannot be interpreted as an integer"
    user32.EnumDisplayMonitors(None, None, cb, 0)
    return out


def virtual_desktop() -> ScreenRect:
    """整个虚拟桌面(所有显示器合并后的包围盒)。

    ★ 多显示器时必须用它而不是单块显示器:
      副机若有两块屏(例如左屏 0..1919、右屏 1920..3839), 那么"桌面最左边缘"
      是 0, 而 `_main_monitor()` 只会返回面积最大的那一块 —— 用它算回传边
      会把边界定在错误的位置(实测: 光标跑到右屏右边缘蹭、往左一拉就回主机)。
    """
    return _virtual_screen()


def entry_point(side: str) -> tuple[int, int]:
    """光标从主电脑进入本机时, 应该落在的**屏幕坐标**。

    side 的语义: 副电脑位于主电脑的哪一侧 —— 也就是"主电脑在副电脑的另一侧"。
      主机 side="right"(副机在主机的右边)
        -> 用户从主机右边缘向右推进来
        -> 副机应把光标放在**它自己的最左边缘**(副机的 left 边)
      主机 side="left" -> 副机最右边缘
      主机 side="top"  -> 副机最下边缘
      主机 side="bottom" -> 副机最上边缘

    返回点取该边的**中间**(垂直/水平居中), 这样从主机任意高度进来都不会
    让副机光标跳到角落。
    """
    d = virtual_desktop()
    if side == "right":
        return d.left, d.top + d.height // 2
    if side == "left":
        return d.right - 1, d.top + d.height // 2
    if side == "top":
        return d.left + d.width // 2, d.bottom - 1
    return d.left + d.width // 2, d.top


def return_edge_side(pc_side: str) -> str:
    """由"副电脑位于主电脑哪一侧"推出**副机的回传边**。

    pc_side="right" => 副机在主机右侧 => 主机在副机**左侧**
                    => 副机的回传边是 "left"
    """
    return {"right": "left", "left": "right",
            "top": "bottom", "bottom": "top"}[pc_side]


def position_fraction(x: int, y: int, side: str) -> float:
    """把光标位置换算成 [0,1] 的**比例**(沿跨越边的切向)。

    照搬 Deskflow 的 mapToFraction: 跨屏时传递的是**比例**而不是像素,
    这样两台机器分辨率不同也能"从同一相对高度进入"。
    例如主机 1920x1080 在 y=540(50%) 处出去, 副机 1366x768 就在 y=384
    (同样 50%) 处进来 —— 若直接传像素, 1080 屏的 540 落到 768 屏上会
    偏低/偏高的位置。
    """
    d = virtual_desktop()
    if side in ("left", "right"):
        return (y - d.top + 0.5) / max(1, d.height)
    return (x - d.left + 0.5) / max(1, d.width)


# 进入点向屏内推入的像素数(照搬 Deskflow 的 jumpZone 语义)。
# 若进入点恰好在回传边上, 进入瞬间一次朝主电脑的抖动就会立刻越过回传边
# 再次回传 —— Deskflow 在 Server.cpp 的 avoidJumpZone 里刻意把进入点向内
# 推过跳入区, 落点与边缘之间留出死区, 用户必须真正"拉回"才触发回传。
#
# ★ 2026-10-22 由 12 提到 24: 用户复报"副机靠主机那一侧的边缘有吸力/
#   粘滞"。12px 的死区在真机上太薄 —— 指针一落到副机就贴在边缘, 手一点
#   横向漂移(斜着推、或管道滞后导致手已回拉而指针还在往左走)就让回传
#   条件成立, 回到主机后又同样落在离边 12px 处, 于是一碰就来回弹。
#   配合 Agent.RETURN_PUSH_OUT_PX(必须再推过边界 24px 才交回), 边缘现在
#   是"硬墙 + 需要主动顶出去", 而不是"碰到就弹"。
ENTRY_JUMP_ZONE_PX = 24


def _frac_to_axis(f: float, extent: int, origin: int) -> int:
    """比例 -> 轴坐标, 是 position_fraction 的精确逆变换。

    推导: position_fraction 给出 f = (v - origin + 0.5) / extent, 反解
        v = f * extent - 0.5 + origin
    取整必须用**四舍五入到最近整数(half-up)**:
        v = floor(f * extent - 0.5 + 0.5) + origin = floor(f * extent) + origin
    即直接 `int(f * extent) + origin` —— 那个 +0.5 的偏置与 floor 恰好抵消。

    ⚠ 千万不要写成 `int(round(f * extent - 0.5))`: Python 的 round() 是
      **banker's rounding**(半值取偶), 在 f*extent 恰为整数时会算错
      (实测 e=3, f=1/3 → 得 0 而正确值是 1)。同理 `round()` 也别用在
      任何"比例 → 像素"的还原路径上。
    """
    return int(f * extent) + origin


def fraction_to_pixel(f: float, side: str) -> tuple[int, int]:
    """把比例换算成**本机**进入边**内侧**的像素坐标(照搬 mapToPixel)。

    ★ 与 Deskflow avoidJumpZone 对齐: 落点不在边上, 而是向屏内推入
      ENTRY_JUMP_ZONE_PX。之前落点恰好在回传边那一列, 进入后光标贴着
      边缘, 副机的回传判定(抵边+朝内推)在进入瞬间就可能成立 —— 一次
      朝主机的抖动就把光标弹回去, 手感表现为"进不去, 一进去就弹回"。
      推入后进入点与边缘之间有一段死区, 必须真正朝主电脑移动越过它
      才会回传, 与 Deskflow 的跳入区行为一致。
    """
    d = virtual_desktop()
    f = min(max(f, 0.0), 1.0)
    if side == "left":
        # 主电脑在左侧: 从左边缘进入, 落点向右(屏内)推
        y = _frac_to_axis(f, d.height, d.top)
        return d.left + ENTRY_JUMP_ZONE_PX, \
            max(d.top, min(d.bottom - 1, y))
    if side == "right":
        y = _frac_to_axis(f, d.height, d.top)
        return d.right - 1 - ENTRY_JUMP_ZONE_PX, \
            max(d.top, min(d.bottom - 1, y))
    if side == "top":
        x = _frac_to_axis(f, d.width, d.left)
        return max(d.left, min(d.right - 1, x)), d.top + ENTRY_JUMP_ZONE_PX
    x = _frac_to_axis(f, d.width, d.left)
    return max(d.left, min(d.right - 1, x)), d.bottom - 1 - ENTRY_JUMP_ZONE_PX


def get_cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(byref(pt))
    return pt.x, pt.y


# --- 原始输入捕获 -------------------------------------------------------

class RawInputListener(threading.Thread):
    """后台线程, 消息循环接收 WM_INPUT, 把鼠标/键盘事件转成 InputEvent。"""

    def __init__(self, on_event, accept_synthetic: bool = False,
                 register_keyboard: bool = True):
        super().__init__(daemon=True, name="RawInputListener")
        self._on_event = on_event
        self._hwnd = None
        self._ready = threading.Event()
        self._rid = RAWINPUTDEVICE()
        self._running = False
        self._proc_ref = WNDPROC(self._wnd_proc)   # 常驻, 防 GC
        # ★★★ register_keyboard=False: **不注册键盘 RawInput**。
        #
        #   为什么必须能关掉(2026-10-19 物理按键实测, 用户亲手按):
        #   当本进程的窗口(光标隐藏器 CursorHider)抢占前台时, 若同时又注册了
        #   键盘 RawInput(RIDEV_INPUTSINK, usUsage=0x06), 那么本进程的低级
        #   键盘钩子(WH_KEYBOARD_LL)**收不到任何键盘事件** —— 物理键也不行。
        #   三组对照(每步都由用户按真实按键, 用 kbd_calls 计数):
        #      不注册任何 RawInput       正常 +20 / 隐藏光标后 **+26 正常**
        #      只注册鼠标 0x02           正常 +22 / 隐藏光标后 **+24 正常**
        #      只注册键盘 0x06           正常 +116 / 隐藏光标后 **+0 全灭**
        #   => 冲突只来自**键盘** RawInput; 鼠标 RawInput 完全无害。
        #
        #   这正是 Deskflow 从未踩到这个坑的原因(见 tools/deskflow_full):
        #   它也抢前台(MSWindowsDesks.cpp deskLeave 的 primary 分支),
        #   钩子装法与我们完全相同(SetWindowsHookEx(WH_KEYBOARD_LL, ...)),
        #   但它**全程不注册 RawInput**, 键盘靠低级钩子 + GetKeyboardState。
        #
        #   所以: 鼠标 RawInput 保留(越界判定/回声过滤/TAG 判据都靠它),
        #   键盘事件改由低级钩子提供(钩子本来就在做这件事, 且隐藏光标时正常)。
        self.register_keyboard = bool(register_keyboard)
        # ★★ 测试后门(默认 False): SendInput 注入的事件 hDevice==NULL,
        #   生产必须过滤(防别的程序注入/防回声); 但**活体验收工具**要用
        #   注入模拟"物理输入"驱动 EdgeDetector —— 此前 4 轮全绿的验收
        #   全部栽在这里: 注入被过滤 -> 越界从未被注入触发过, 测的是
        #   "自我过滤"的死循环(实测 synthetic+5, mouse+0)。
        #   accept_synthetic=True 时只放行**无 TAG** 的合成事件
        #   (TAG 是我们自己的注入标记, 永远过滤, 回声防护不动)。
        self.accept_synthetic = bool(accept_synthetic)
        # 诊断计数: 用于区分"根本没收到物理事件"与"收到了但没判越界"。
        # 鼠标不能跨屏时, 这两个数字能立刻指出问题在哪一层。
        self.raw_total = 0        # 收到的全部 WM_INPUT
        self.raw_synthetic = 0    # 其中被判定为合成(hDevice==NULL)而丢弃的
        self.raw_mouse = 0        # 真正上报的鼠标事件
        self.raw_key = 0          # 真正上报的键盘事件
        self.size_errors = 0      # GetRawInputData 取数失败次数

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            self._handle_raw(wparam, lparam)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_raw(self, wparam, lparam) -> None:
        size = c_uint(0)
        user32.GetRawInputData(lparam, RID_INPUT, None, byref(size),
                               ctypes.sizeof(RAWINPUTHEADER))
        if size.value == 0:
            self.size_errors += 1
            return
        buf = ctypes.create_string_buffer(size.value)
        got = user32.GetRawInputData(lparam, RID_INPUT, buf, byref(size),
                                     ctypes.sizeof(RAWINPUTHEADER))
        if got != size.value:
            self.size_errors += 1
            return
        self.raw_total += 1
        hdr = RAWINPUTHEADER.from_buffer_copy(buf.raw[: ctypes.sizeof(RAWINPUTHEADER)])
        # ★ 过滤本进程用 SendInput 注入的事件。
        #
        # ★★ 判据顺序至关重要: **先按 TAG 判, 再看 hDevice**。
        #
        #   两道判据的含义:
        #     (1) dwExtraInfo == 我们的标记: 我们自己注入时打的标记。这是
        #         **唯一可靠**的判据 —— 与 hDevice 无关, 一定能识别出来。
        #     (2) hDevice == NULL: 合成事件的"常见"特征, 但它**不可靠**,
        #         有两个方向都会出错:
        #           * 反向漏判: 绝对移动(MOUSEEVENTF_ABSOLUTE)等注入事件
        #             常带**非 NULL** hDevice, 只靠 (2) 会漏掉它们 -> 回声;
        #           * 正向误杀: 被低级钩子**吞掉**的物理事件, 其 hDevice
        #             在实测中也可能为 NULL(见 tools/diag_hook_types.py)。
        #             若先判 hDevice, 这些真实输入会被当成合成事件**丢弃**
        #             -> 穿越期间主机"收不到"物理输入, 表现与钩子完全失效
        #             一样。
        #   因此: 先用 TAG 精确识别自己的注入, 再把 hDevice==NULL 当
        #   兜底(它仍然能挡掉**别的程序**注入的合成事件)。
        base = ctypes.sizeof(RAWINPUTHEADER)
        if hdr.dwType == RIM_TYPEMOUSE:
            rm = RAWMOUSE.from_buffer_copy(
                buf.raw[base : base + ctypes.sizeof(RAWMOUSE)])
            if rm.ulExtraInformation == InputInjector.TAG:
                self.raw_synthetic += 1
                return
            if not hdr.hDevice and not self.accept_synthetic:
                self.raw_synthetic += 1
                return
            self.raw_mouse += 1
            self._emit_mouse(rm)
        elif hdr.dwType == RIM_TYPEKEYBOARD:
            rk = RAWKEYBOARD.from_buffer_copy(
                buf.raw[base : base + ctypes.sizeof(RAWKEYBOARD)]
            )
            if rk.ExtraInformation == InputInjector.TAG:
                self.raw_synthetic += 1
                return
            if not hdr.hDevice and not self.accept_synthetic:
                self.raw_synthetic += 1
                return
            self.raw_key += 1
            self._emit_key(rk)

    def diag(self) -> dict:
        return {
            "total": self.raw_total,
            "synthetic": self.raw_synthetic,
            "mouse": self.raw_mouse,
            "key": self.raw_key,
            "size_errors": self.size_errors,
        }

    def _emit_mouse(self, rm: RAWMOUSE) -> None:
        ts = time.monotonic_ns() // 1000
        if rm.usFlags & 0x01:  # MOUSE_MOVE_ABSOLUTE
            return
        if rm.lLastX or rm.lLastY:
            from .protocol import InputEvent
            self._on_event(InputEvent(EV_MOUSE_MOVE_REL, 0, 0, rm.lLastX, rm.lLastY, ts))
        bf = rm.usButtonFlags
        from .protocol import InputEvent
        if bf & RI_MOUSE_LEFT_BUTTON_DOWN:
            self._on_event(InputEvent(EV_MOUSE_BUTTON, MB_DOWN, 1, 0, 0, ts))
        if bf & RI_MOUSE_LEFT_BUTTON_UP:
            self._on_event(InputEvent(EV_MOUSE_BUTTON, MB_UP, 1, 0, 0, ts))
        if bf & RI_MOUSE_RIGHT_BUTTON_DOWN:
            self._on_event(InputEvent(EV_MOUSE_BUTTON, MB_DOWN, 2, 0, 0, ts))
        if bf & RI_MOUSE_RIGHT_BUTTON_UP:
            self._on_event(InputEvent(EV_MOUSE_BUTTON, MB_UP, 2, 0, 0, ts))
        if bf & RI_MOUSE_MIDDLE_BUTTON_DOWN:
            self._on_event(InputEvent(EV_MOUSE_BUTTON, MB_DOWN, 3, 0, 0, ts))
        if bf & RI_MOUSE_MIDDLE_BUTTON_UP:
            self._on_event(InputEvent(EV_MOUSE_BUTTON, MB_UP, 3, 0, 0, ts))
        if bf & RI_MOUSE_BUTTON_4_DOWN:
            self._on_event(InputEvent(EV_MOUSE_BUTTON, MB_DOWN, 4, 0, 0, ts))
        if bf & RI_MOUSE_BUTTON_4_UP:
            self._on_event(InputEvent(EV_MOUSE_BUTTON, MB_UP, 4, 0, 0, ts))
        if bf & RI_MOUSE_BUTTON_5_DOWN:
            self._on_event(InputEvent(EV_MOUSE_BUTTON, MB_DOWN, 5, 0, 0, ts))
        if bf & RI_MOUSE_BUTTON_5_UP:
            self._on_event(InputEvent(EV_MOUSE_BUTTON, MB_UP, 5, 0, 0, ts))
        if bf & RI_MOUSE_WHEEL:
            delta = ctypes.c_short(rm.usButtonData).value
            self._on_event(InputEvent(EV_MOUSE_WHEEL, 0, 0, 0, delta, ts))
        if bf & RI_MOUSE_HORIZONTAL_WHEEL:
            # 倾斜滚轮: delta 语义与垂直相同(正=向右), 由注入侧映射到 HWHEEL
            delta = ctypes.c_short(rm.usButtonData).value
            self._on_event(InputEvent(EV_MOUSE_HWHEEL, 0, 0, 0, delta, ts))

    def _emit_key(self, rk: RAWKEYBOARD) -> None:
        from .protocol import InputEvent

        if rk.VKey == 0xFF:
            return
        ts = time.monotonic_ns() // 1000
        flags = 0
        if rk.Flags & RI_KEY_BREAK:
            flags |= KEY_UP
        else:
            flags |= KEY_DOWN
        if rk.Flags & RI_KEY_E0:
            flags |= KEY_EXTENDED
        self._on_event(InputEvent(EV_KEY, flags, rk.VKey, rk.MakeCode, 0, ts))

    # 进程内窗口类名必须唯一: 单进程同时跑 server 与 agent(自环测试)时,
    # 第二个 RegisterClassW 会返回 ERROR_CLASS_ALREADY_EXISTS(1410)。
    _class_seq = 0
    _class_lock = threading.Lock()

    @classmethod
    def _new_class_name(cls) -> str:
        with cls._class_lock:
            cls._class_seq += 1
            return f"ComBridgeRawInputSink{cls._class_seq}"

    def run(self) -> None:
        self._class_name = self._new_class_name()
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc_ref
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = self._class_name
        if not user32.RegisterClassW(byref(wc)):
            err = kernel32.GetLastError()
            # 1410 = ERROR_CLASS_ALREADY_EXISTS: 名字唯一化后不该出现, 但
            # 残留进程/热重载可能撞上, 此时窗口类可用, 继续即可。
            if err != 1410:
                raise OSError("RegisterClassW 失败: %d" % err)
        self._hwnd = user32.CreateWindowExW(
            0, self._class_name, self._class_name, 0,
            0, 0, 0, 0, None, None, wc.hInstance, None,
        )
        if not self._hwnd:
            raise OSError("CreateWindowExW 失败: %d" % kernel32.GetLastError())
        self._rid.usUsagePage = 0x01   # generic desktop
        self._rid.usUsage = 0x02       # mouse
        self._rid.dwFlags = RIDEV_INPUTSINK
        self._rid.hwndTarget = self._hwnd
        if not user32.RegisterRawInputDevices(byref(self._rid), 1,
                                              ctypes.sizeof(RAWINPUTDEVICE)):
            raise OSError("RegisterRawInputDevices(mouse) 失败")
        if self.register_keyboard:
            self._rid.usUsage = 0x06       # keyboard
            if not user32.RegisterRawInputDevices(byref(self._rid), 1,
                                                  ctypes.sizeof(RAWINPUTDEVICE)):
                raise OSError("RegisterRawInputDevices(keyboard) 失败")
        else:
            # ★ 显式**注销**键盘 RawInput(不只是"不注册")。
            #   为什么要 REMOVE: RIDEV_INPUTSINK 是**按 hwndTarget 持久**的,
            #   若同进程更早的监听器注册过键盘(热重载/自环测试/重复启动),
            #   单纯"这次不注册"并不会撤销那条旧注册 —— 冲突会继续存在。
            #   显式 REMOVE 保证无论之前状态如何, 键盘都不再走 RawInput。
            stale = RAWINPUTDEVICE()
            stale.usUsagePage = 0x01
            stale.usUsage = 0x06
            stale.dwFlags = RIDEV_REMOVE
            stale.hwndTarget = None
            user32.RegisterRawInputDevices(byref(stale), 1,
                                           ctypes.sizeof(RAWINPUTDEVICE))
        self._running = True
        self._ready.set()

        msg = wintypes.MSG()
        while self._running:
            r = user32.GetMessageW(byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))

    def stop(self) -> None:
        self._running = False
        if self._hwnd:
            user32.PostMessageW(self._hwnd, 0x0012, 0, 0)  # WM_QUIT

    def wait_ready(self, timeout: float = 2.0) -> bool:
        return self._ready.wait(timeout)


# --- 注入 ---------------------------------------------------------------

class InputInjector:
    """用 SendInput 注入输入。

    ★ 所有注入事件都带一个**特征标记**(dwExtraInfo), 用于在原始输入
      监听里把"我们自己注入的"与"用户真实操作"区分开。

      为什么必须打标记(这是"鼠标到边缘被疯狂弹回"的真正原因):
        server 判定越界后会把光标**钳制**回边界(clamp_to_edge + 
        move_absolute)。而 RIDEV_INPUTSINK 监听器**同样会收到 SendInput
        产生的合成事件**。仅靠 `hDevice == NULL` 过滤并不可靠 —— 绝对
        移动事件常常带着非 NULL 的 hDevice, 于是这个钳制事件被当成
        "用户往回拉鼠标"重新进入状态机, 立刻退出越界态; 用户再推向边缘,
        又触发一次 —— 表现为光标在边缘被高频弹回, 永远进不去对面。
        打了标记后, 监听器可以无条件丢弃自己发出的注入事件, 与 hDevice
        无关, 状态机不再被自激事件扰动。
    """

    # 标记值: 任意非零常量即可, 只要不与其它软件的约定冲突。
    # 采用 "CBRG" 的 ASCII 便于在调试器里辨认。
    TAG = 0x43425247          # 'CBRG'

    # -- 注入时间戳(hider 的"注入 vs 物理"判据用) ----------------------
    # GetMessageExtraInfo 在合并消息/窗口激活合成的移动上不可靠(实测:
    # 60 对 TAG 注入合并成 2 条 WM_MOUSEMOVE, 其中 1 条 extra 恰好非 TAG,
    # 还有窗口激活/SetWindowPos 产生的合成移动 extra=0), 而所有注入移动
    # 都从 move_relative/move_absolute 出发 —— 在这里打点最可靠:
    # hider 收到 WM_MOUSEMOVE 时查"最近 N ms 内有没有注入移动"即可。
    _last_move_ts: float = 0.0

    @staticmethod
    def last_move_age() -> float:
        """距最近一次注入移动的秒数(从未注入过 = 无穷大)。"""
        ts = InputInjector._last_move_ts
        return float("inf") if ts <= 0.0 else max(0.0, time.monotonic() - ts)

    @staticmethod
    def _send(*inputs: INPUT) -> int:
        n = len(inputs)
        arr = (INPUT * n)(*inputs)
        return user32.SendInput(n, arr, ctypes.sizeof(INPUT))

    @staticmethod
    def move_relative(dx: int, dy: int) -> int:
        """副机侧: 把主机的相对位移**精确**地重现为光标移动。

        ★★★ 为什么不能用 MOUSEEVENTF_MOVE + 改系统设置(实测数据):
          Windows 对相对位移会施加**两层独立的放大**:
            ① 加速曲线 SPI_SETMOUSE —— "指针精度", 快动时额外放大;
            ② 速度档位 SPI_SETMOUSESPEED —— 对**所有**位移的整数倍放缩。
          实测本机(档位 10)的完整映射表(请求 dx=40):
              档位  1 -> 0.215x    档位  5 -> 1.080x
              档位  4 -> 0.860x    档位 10 -> 2.160x
              档位 20 -> 4.315x
          * 档位 10(Windows 默认)= **2.16 倍放大** —— 这正是用户报的
            "进入副屏后鼠标速度显著快于主屏"。
          * 即使把加速曲线关掉, 速度档位仍在放大; Deskflow 的做法是
            两者都临时改(见 MSWindowsDesks.cpp :466-495, 它把速度设为 1),
            但 **档位 1 实测是 0.215x**, 也不是 1:1 —— 所以 Deskflow 的
            方案同样不精确(它的相对模式是可选实验特性)。

        ★ 解法: **改用绝对定位**。MOUSEEVENTF_ABSOLUTE 的坐标直接映射到
          屏幕像素, **完全绕过加速曲线与速度档位**(两者都只作用于相对
          位移)。做法: 读当前光标 -> 加上 dx/dy -> 绝对注入。
          * 精确: 请求多少像素就走多少像素, 与两台机器的鼠标设置无关;
          * 无需改系统设置: 不动用户的"指针精度"/速度, 崩溃也无残留;
          * 代价: 每次要读一次光标(GetCursorPos, 微秒级)。
        ★ 边界: 绝对坐标必须夹在虚拟桌面内, 否则 Windows 会钳到边缘
          (那会造成"推到头就不动了"的错觉, 但这是正确的物理行为)。
        """
        from .input import virtual_desktop
        v = virtual_desktop()
        cur = get_cursor_pos()
        nx = int(cur[0]) + int(dx)
        ny = int(cur[1]) + int(dy)
        # 夹到虚拟桌面范围内(留 1px 余量, 避免落到边界外触发钳制)
        nx = max(v.left, min(v.right - 1, nx))
        ny = max(v.top, min(v.bottom - 1, ny))
        return InputInjector.move_absolute(nx, ny)

    @staticmethod
    def move_absolute(x: int, y: int) -> int:
        """返回 SendInput 实际注入的事件数(0 = 被系统拒绝), 便于诊断。"""
        # ★ 归一化公式必须**照搬 Deskflow 的 deskMouseMove**:
        #     dx = 65535 * x / (w - 1) + 0.5   (除以 w-1, 并且四舍五入)
        #   之前写成 `int((x - left) * 65535 / w)` —— 除以 w 且截断, 每次
        #   注入都**少 1px**(实测: 反复注入同一目标 (1919,540), 60 次后光标
        #   落在 (1918,539))。单次无害, 但"钉住边缘"是**每个鼠标事件都重注入
        #   一次**, 而且传的是读回来的当前坐标 —— 误差于是逐事件累积,
        #   表现为用户实测的"主机光标沿右边缘一路向上爬, 最后停在右上角"
        #   (y 每事件 -1px 一路到顶; x 因为每轮传常量右边缘所以稳在边上)。
        #   除以 (w-1) 后映射是**幂等**的: 同一目标反复注入落在同一像素。
        v = _virtual_screen()
        ax = int(65535.0 * (x - v.left) / max(1, v.width - 1) + 0.5)
        ay = int(65535.0 * (y - v.top) / max(1, v.height - 1) + 0.5)
        i = INPUT(type=INPUT_MOUSE)
        i.union.mi.dx = ax
        i.union.mi.dy = ay
        i.union.mi.dwFlags = (
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        )
        i.union.mi.dwExtraInfo = InputInjector.TAG
        n = InputInjector._send(i)
        if n > 0:
            InputInjector._last_move_ts = time.monotonic()
        return n

    @staticmethod
    def button(code: int, down: bool) -> None:
        """code: 1=L 2=R 3=M 4=X1 5=X2

        ★ 非法 code 必须**安全忽略**而不是抛 KeyError(实测踩到):
          旧实现用字典直接查表, `code=0`(或任何协议不匹配的值)会抛
          `KeyError: 0`。而 KeyError **不是 OSError**, agent 的
          `_on_input_event` 只捕 OSError, 于是它一路上抛到
          `InputReceiver` 的 `except Exception: pass` —— **静默吞掉,
          日志里一个字都没有**, 表现为"输入完全失效且无从排查"。
          现在改为 `.get(code)`, 未知按钮直接返回 0(不注入、不抛)。
        """
        spec = {
            1: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
            2: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
            3: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
            4: (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, 0x0001),
            5: (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, 0x0002),
        }.get(int(code))
        if spec is None:
            return 0          # 未知按钮: 安全忽略
        down_flag, up_flag, data = spec
        i = INPUT(type=INPUT_MOUSE)
        i.union.mi.mouseData = data
        i.union.mi.dwFlags = down_flag if down else up_flag
        # ★ 必须打标记: 所有注入都要能被本机监听器与钩子识别为"我们的"。
        #   漏掉它的后果(实测踩到, 是"点击无法回传"的成因之一):
        #     * 本机钩子只放行带 TAG 的事件 -> 回程注入的点击被自己**吞掉**;
        #     * RawInputListener 按 TAG 识别合成事件 -> 认不出, 形成回声。
        #   历史: 只有 move_relative/move_absolute 打了标记, button/wheel/key
        #   三条漏了 —— 表现就是"键盘和点击的注入时好时坏"。
        i.union.mi.dwExtraInfo = InputInjector.TAG
        return InputInjector._send(i)

    @staticmethod
    def wheel(delta: int, horizontal: bool = False) -> int:
        """delta: 120 的整数倍为标准格; horizontal=True 为倾斜滚轮(HWHEEL)。"""
        i = INPUT(type=INPUT_MOUSE)
        i.union.mi.mouseData = ctypes.c_uint(delta).value & 0xFFFFFFFF
        i.union.mi.dwFlags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
        i.union.mi.dwExtraInfo = InputInjector.TAG   # 同上: 必须打标记
        return InputInjector._send(i)

    @staticmethod
    def key(vk: int, scan: int, down: bool, extended: bool = False) -> int:
        i = INPUT(type=INPUT_KEYBOARD)
        i.union.ki.wVk = vk
        i.union.ki.wScan = scan
        flags = 0
        if not down:
            flags |= KEYEVENTF_KEYUP
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        i.union.ki.dwFlags = flags
        # ★ 键盘同样必须打标记(理由同上); RAWKEYBOARD.ExtraInformation
        #   会被监听器与钩子读取。
        i.union.ki.dwExtraInfo = InputInjector.TAG
        return InputInjector._send(i)
