# -*- coding: utf-8 -*-
"""系统托盘图标 —— 纯 ctypes 调 Shell_NotifyIconW, 不依赖 pystray/pywin32。

## 为什么不引入第三方库
本项目打包成单文件 exe, 每多一个依赖就多一份体积与"打包后行为不同"的风险
(之前已因 --noconsole / stdout 等打包差异踩过坑)。
Shell_NotifyIconW 是 Windows 自带 API, 用 ctypes 直接调即可, 零依赖。

## 设计要点(都对应真实 Windows 行为)
1. 托盘图标必须由**一个拥有窗口过程的消息循环**来接收回调 ——
   这里创建一个隐藏的 message-only 窗口(hwnd), 用它当托盘宿主。
2. 右键菜单用 `CreatePopupMenu` + `TrackPopupMenu`, 并在弹菜单前
   调用 `SetForegroundWindow(hwnd)` —— **这是必需的**:
   否则菜单点外面不会消失(Windows 的经典坑)。
3. 图标用 `LoadIconW(None, IDI_APPLICATION)` 做兜底,
   若提供了 .ico 文件则优先用文件。
4. 状态区分(用户要求"直观分辨服务是否已启动"):
   运行中用绿点图标, 停止用灰点图标 —— 用动态生成的 HICON 实现。

## 线程模型
托盘必须跑在**独立的线程**里(它要 `GetMessage` 泵消息),
不能用 tkinter 的 mainloop: tk 的消息循环不保证转发自定义 WM_ 消息。
命令通过回调投递回 tk 线程(调用方负责用 `root.after` 切回来)。
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32
gdi32 = ctypes.windll.gdi32

# --- 常量 -------------------------------------------------------------
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 20          # 托盘回调消息
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
WS_OVERLAPPED = 0x00000000
CW_USEDEFAULT = 0x80000000
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
WM_TASKBARCREATED = 0  # 动态注册, 见 _taskbar_created_msg

# --- 结构 -------------------------------------------------------------
# ★★★ WNDPROC / WNDCLASSW 必须**复用 input.py 的定义**, 不能各写一份。
#   原因(实测踩到, 直接导致主电脑无法启动):
#     ctypes 的 argtypes/结构体是按 **Python 类身份** 做检查的。
#     input.py 里 `RegisterClassW.argtypes = [POINTER(它的 WNDCLASSW)]`,
#     tray.py 若再定义一份同名但不同的 WNDCLASSW 并覆盖 argtypes,
#     之后 input.py 调用 RegisterClassW 就会抛:
#         ctypes.ArgumentError: expected LP_WNDCLASSW instance
#                               instead of pointer to WNDCLASSW
#     -> RawInputListener 线程直接崩 -> "原始输入监听启动失败"。
#   这是**同一个坑的第二次**: 先前是 GetCursorPos 的 POINT 类型冲突。
#   结论: 任何 Windows 结构/函数签名, 全项目只允许有一份定义。
from .input import WNDCLASSW, WNDPROC  # noqa: E402


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# ★★★ ctypes 的 argtypes 是**全局共享**的: 如果这里声明
#   user32.GetCursorPos.argtypes = [POINTER(自己的 POINT)],
#   会覆盖 input.py 里用 wintypes.POINT 的声明 —— 之后 input.py 调用
#   GetCursorPos 就会报 "expected LP_POINT instance instead of pointer
#   to POINT"(实测踩到, 表现为坐标读取全部失败)。
#   因此这里**不声明** GetCursorPos 的 argtypes, 直接用 wintypes.POINT。


# --- 参数类型(64 位必须显式声明, 否则句柄被截断) ----------------------
# ★★★ 只声明 **input.py 没声明过**的函数。
#   ctypes 的 argtypes 是全局共享的: 若这里重复声明一个 input.py 也用
#   的函数(且参数类型来自本模块自己的结构体), 就会覆盖掉 input.py 的
#   声明, 导致那边运行时抛 ArgumentError。
#   已踩过两次(POINT / WNDCLASSW), 都直接导致主电脑无法启动。
#   因此下面只保留托盘自己的 API; CreateWindowExW / DefWindowProcW /
#   RegisterClassW / GetCursorPos 的签名由 input.py 统一负责。
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.restype = wintypes.BOOL
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                               ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.DestroyMenu.restype = wintypes.BOOL
user32.LoadIconW.restype = wintypes.HICON
user32.LoadImageW.restype = wintypes.HANDLE
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.PostMessageW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                      ctypes.POINTER(NOTIFYICONDATAW)]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH

# 菜单命令 ID
CMD_SHOW = 1001
CMD_START = 1002
CMD_STOP = 1003
CMD_EXIT = 1099


def _tray_icon_size() -> int:
    """系统小图标尺寸(托盘显示尺寸), 已按 DPI 缩放。

    SM_CXSMICON = 49。取不到时退回 16(100% DPI 的值)。
    夹在 16~64 之间: 防止异常环境返回荒谬值导致画图开销失控。
    """
    try:
        n = int(user32.GetSystemMetrics(49))       # SM_CXSMICON
        if 16 <= n <= 64:
            return n
    except Exception:  # noqa: BLE001
        pass
    return 16


def _make_dot_icon(color: tuple[int, int, int],
                   size: int = 16) -> int:
    """生成一个实心圆点图标(纯代码画, 无外部资源文件)。

    ★ 为什么不用 .ico 文件: 打包成单文件 exe 后, 资源文件需要
      datas 声明且路径在解包目录里变化, 容易在用户机器上找不到。
      纯代码画图标零依赖、颜色随时可改(正好用来区分运行/停止状态)。

    ★★★ 这里踩过两个坑(图标显示为空白), 修法都在下面标注:
      坑1. **restype 必须在调用之前声明**。原实现把
           `gdi32.CreateBitmap.restype = HBITMAP` 写在调用**之后**,
           句柄按默认 c_int 返回被**截断成 32 位**, 后续 CreateIconIndirect
           拿到的是无效句柄 -> 整个图标创建失败(返回 0)。
      坑2. **32bpp 图标的透明必须由 AND 掩码决定**。只设 DIB 的 alpha
           通道不可靠(CreateIconIndirect 对 alpha 处理依实现而异)。
           正确做法: AND 掩码里 圆内=0(不透明)、圆外=1(透明);
           彩色位图圆外填 0 即可。

    返回 HICON(调用方负责 DestroyIcon), 失败返回 0。
    """
    hdc_screen = 0
    hdc = 0
    hbm = 0
    hbm_mask = 0
    hicon = 0
    try:
        # ---- 先声明所有签名(坑1: 必须在调用之前) ----
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        gdi32.CreateBitmap.restype = wintypes.HBITMAP
        gdi32.CreateBitmap.argtypes = [
            ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT,
            ctypes.c_void_p]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        user32.GetDC.restype = wintypes.HDC
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.CreateIconIndirect.restype = wintypes.HICON
        user32.CreateIconIndirect.argtypes = [ctypes.c_void_p]
        user32.DestroyIcon.restype = wintypes.BOOL
        user32.DestroyIcon.argtypes = [wintypes.HICON]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                        ("bmiColors", wintypes.DWORD * 3)]

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL),
                        ("xHotspot", wintypes.DWORD),
                        ("yHotspot", wintypes.DWORD),
                        ("hbmMask", wintypes.HBITMAP),
                        ("hbmColor", wintypes.HBITMAP)]

        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            return 0
        hdc = gdi32.CreateCompatibleDC(hdc_screen)
        if not hdc:
            return 0

        # ---- 彩色位图(32bpp, 自下而上) ----
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = size
        bi.bmiHeader.biHeight = size        # 正数 = 自下而上(图标要求)
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0      # BI_RGB

        bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bi), 0,
                                     ctypes.byref(bits), None, 0)
        if not hbm or not bits.value:
            return 0
        gdi32.SelectObject(hdc, hbm)

        br, bg, bb = color
        total = size * size
        color_px = (ctypes.c_ubyte * (total * 4)).from_address(bits.value)
        # 掩码: 每个字节 8 像素, 行按 4 字节对齐
        mask_stride = ((size + 31) // 32) * 4
        mask_buf = (ctypes.c_ubyte * (mask_stride * size))()
        # 掩码语义: 1 = 该像素透明(露出桌面), 0 = 不透明(显示彩色)
        for row in range(mask_stride * size):
            mask_buf[row] = 0xFF            # 默认全透明
        r = size / 2.0 - 0.5
        cx = cy = (size - 1) / 2.0
        for y in range(size):
            # DIB 是自下而上: 内存第 0 行 = 图像最底行
            img_y = size - 1 - y
            for x in range(size):
                dx = x - cx
                dy = img_y - cy
                inside = (dx * dx + dy * dy) ** 0.5 <= r
                i = (y * size + x) * 4
                if inside:
                    color_px[i] = bb
                    color_px[i + 1] = bg
                    color_px[i + 2] = br
                    color_px[i + 3] = 255
                    # 不透明: 掩码位清 0
                    byte_i = y * mask_stride + (x // 8)
                    mask_buf[byte_i] &= ~(0x80 >> (x % 8)) & 0xFF
                else:
                    color_px[i] = color_px[i + 1] = 0
                    color_px[i + 2] = color_px[i + 3] = 0

        hbm_mask = gdi32.CreateBitmap(size, size, 1, 1, mask_buf)
        if not hbm_mask:
            return 0

        ii = ICONINFO()
        ii.fIcon = 1
        ii.xHotspot = 0
        ii.yHotspot = 0
        ii.hbmMask = hbm_mask
        ii.hbmColor = hbm
        hicon = user32.CreateIconIndirect(ctypes.byref(ii))
        return int(hicon or 0)
    except Exception:  # noqa: BLE001
        return 0
    finally:
        # 清理 GDI 对象(图标句柄 hicon 已由系统复制, 可以释放位图)
        try:
            if hbm_mask:
                gdi32.DeleteObject(hbm_mask)
        except Exception:  # noqa: BLE001
            pass
        try:
            if hbm:
                gdi32.DeleteObject(hbm)
        except Exception:  # noqa: BLE001
            pass
        try:
            if hdc:
                gdi32.DeleteDC(hdc)
        except Exception:  # noqa: BLE001
            pass
        try:
            if hdc_screen:
                user32.ReleaseDC(None, hdc_screen)
        except Exception:  # noqa: BLE001
            pass


class TrayIcon:
    """托盘图标宿主: 独立线程 + 隐藏窗口 + 右键菜单。

    on_show()  / on_start() / on_stop() / on_exit() 由调用方提供,
    它们会在**托盘线程**里被调用 —— 调用方必须自己切回 UI 线程
    (tkinter 里用 root.after(0, ...))。
    """

    def __init__(self, tooltip: str = "Com Bridge",
                 on_show=None, on_start=None, on_stop=None, on_exit=None,
                 ico_path: str | None = None):
        self.tooltip = tooltip
        self._on_show = on_show
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_exit = on_exit
        self._ico_path = ico_path
        self._hwnd = 0
        self._hicon = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.running = False
        # 图标是否真的进了托盘(见 _add_icon 的诊断记录)
        self.icon_ok = False
        self.icon_detail = "未初始化"
        self._taskbar_created_msg = 0
        # ★ 保存 WNDPROC 的引用, 防止被 GC 回收后野指针崩溃(踩过这个坑)
        self._wndproc_ref = None

    # -- 对外 ----------------------------------------------------------

    def start(self) -> bool:
        """启动托盘线程并等待图标真正加进托盘。返回是否成功。"""
        if self._thread is not None:
            return self.running
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="TrayIcon")
        self._thread.start()
        self._ready.wait(3.0)
        return self.running

    def stop(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.running = False

    def set_status(self, running: bool, text: str | None = None) -> None:
        """更新状态(图标颜色 + tooltip)。用户要求"直观分辨是否已启动"。"""
        self.running = running
        if text:
            self.tooltip = text
        if self._hwnd:
            # 通过消息让托盘线程自己更新(避免跨线程操作 GDI 对象)
            user32.PostMessageW(self._hwnd, WM_TRAYICON + 1, 
                                1 if running else 0, 0)

    # -- 内部 ----------------------------------------------------------

    def _run(self) -> None:
        try:
            hinst = kernel32.GetModuleHandleW(None)
            cls_name = f"ComBridgeTray_{id(self)}"
            self._wndproc_ref = WNDPROC(self._wnd_proc)
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc_ref
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = 0
            wc.hCursor = 0
            wc.hbrBackground = 0
            wc.lpszMenuName = None
            wc.lpszClassName = cls_name
            atom = user32.RegisterClassW(ctypes.byref(wc))
            if not atom:
                self._ready.set()
                return
            # message-only 窗口(HWND_MESSAGE = -3): 不显示, 只收消息
            self._hwnd = user32.CreateWindowExW(
                0, cls_name, "ComBridgeTray", WS_OVERLAPPED,
                0, 0, 0, 0, wintypes.HWND(-3), None, hinst, None)
            if not self._hwnd:
                self._ready.set()
                return
            # 注册 TaskbarCreated 消息: 资源管理器重启后托盘会清空,
            # 必须重新添加图标, 否则用户以为程序退出了。
            self._taskbar_created_msg = user32.RegisterWindowMessageW(
                "TaskbarCreated")
            self._add_icon()
            self.running = True
            self._ready.set()
            # 消息循环
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:  # noqa: BLE001
            self._ready.set()
        finally:
            self.running = False

    def _icon_handle(self, running: bool) -> int:
        """运行中=绿色圆点, 停止=灰色圆点; 有 .ico 文件则优先用它。

        ★ 尺寸取**系统小图标尺寸**(SM_CXSMICON, 托盘就是按它显示):
          * 100% DPI 时是 16x16 —— 正好是系统要的尺寸, 最清晰;
          * 125%/150% DPI 时是 20/24 —— 若写死 16 会被放大而发糊。
          本项目在 input.py 里已开启 per-monitor DPI 感知, 所以这里
          读到的就是缩放后的真实尺寸。
        """
        if self._ico_path:
            try:
                h = user32.LoadImageW(None, self._ico_path, IMAGE_ICON,
                                      0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if h:
                    return int(h)
            except Exception:  # noqa: BLE001
                pass
        color = (46, 160, 67) if running else (140, 140, 140)
        h = _make_dot_icon(color, size=_tray_icon_size())
        if h:
            return h
        return int(user32.LoadIconW(None, IDI_APPLICATION) or 0)

    def _add_icon(self) -> None:
        """把图标加进托盘。记录是否成功, 便于从日志诊断"托盘没图标"。"""
        if not self._hwnd:
            self.icon_ok = False
            return
        if self._hicon:
            try:
                user32.DestroyIcon(self._hicon)
            except Exception:  # noqa: BLE001
                pass
        self._hicon = self._icon_handle(self.running)
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip[:127]
        ok = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        # ★ 记录状态: 图标句柄为 0 或 NIM_ADD 失败都会导致"托盘里没有图标",
        #   把两者都记下来, 用户报问题时一眼能定位。
        self.icon_ok = bool(ok and self._hicon)
        self.icon_detail = (
            f"Shell_NotifyIcon={ok} hIcon={self._hicon} "
            f"size={_tray_icon_size()} tooltip={self.tooltip!r}"
            if self.icon_ok else
            f"失败: Shell_NotifyIcon={ok} hIcon={self._hicon}"
            + (" (图标生成失败, 退回系统默认)"
               if not self._hicon else " (加入托盘失败)"))

    def _modify_icon(self) -> None:
        if not self._hwnd:
            return
        if self._hicon:
            try:
                user32.DestroyIcon(self._hicon)
            except Exception:  # noqa: BLE001
                pass
        self._hicon = self._icon_handle(self.running)
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_TIP
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _remove_icon(self) -> None:
        if not self._hwnd:
            return
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))

    def _show_menu(self) -> None:
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            user32.AppendMenuW(hmenu, MF_STRING, CMD_SHOW, "显示窗口")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            if self.running:
                user32.AppendMenuW(hmenu, MF_STRING, CMD_STOP, "停止服务")
            else:
                user32.AppendMenuW(hmenu, MF_STRING, CMD_START, "启动服务")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, CMD_EXIT, "退出 Com Bridge")
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # ★ 必须 SetForegroundWindow, 否则菜单点外面不消失
            user32.SetForegroundWindow(self._hwnd)
            user32.TrackPopupMenu(
                hmenu, TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
                pt.x, pt.y, 0, self._hwnd, None)
            user32.PostMessageW(self._hwnd, 0x0000, 0, 0)   # WM_NULL
        finally:
            user32.DestroyMenu(hmenu)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAYICON:
                low = lparam & 0xFFFF
                if low == WM_LBUTTONUP or low == WM_LBUTTONDBLCLK:
                    if self._on_show:
                        self._on_show()
                elif low == WM_RBUTTONUP:
                    self._show_menu()
                return 0
            if msg == WM_TRAYICON + 1:
                # set_status 投递过来的更新请求
                self._modify_icon()
                return 0
            if msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == CMD_SHOW and self._on_show:
                    self._on_show()
                elif cmd == CMD_START and self._on_start:
                    self._on_start()
                elif cmd == CMD_STOP and self._on_stop:
                    self._on_stop()
                elif cmd == CMD_EXIT and self._on_exit:
                    self._on_exit()
                return 0
            if msg == self._taskbar_created_msg \
                    and self._taskbar_created_msg:
                # 资源管理器重启: 重新添加图标
                self._add_icon()
                return 0
            if msg == WM_DESTROY:
                self._remove_icon()
                user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
