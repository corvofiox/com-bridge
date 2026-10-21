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
from . import brand           # noqa: E402


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
# ★ 2026-10-21: 主界面顶栏的「日志」入口已按用户要求移除, 日志窗口改由
#   托盘菜单打开 —— 功能不能因为少一个按钮就消失。
CMD_LOG = 1004
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


# --- 图标几何(16px 基准) -------------------------------------------------
#
# 设计(用户 2026-10-21 二次定稿):
#   主体 = 「H」: 两根竖直柱 + 中间一条短横杠
#     (两柱 = 两台电脑, 横杠 = 连接链路)
#   右下角圆点 = 状态: 绿=已连接 / 黄=已启动未连接 / 无点=未启动
#   主体色随任务栏明暗反转(Win11 托盘图标就是这么做的)
#
# ★★★ 2026-10-21 用户第三次改造型: 三条横线 -> 「H」。
#   用户原话: "当前托盘图标是三条横线, 直接改成H, 黄绿点状态保持不变"。
#   几何集中在 com_bridge/brand.py(tray_rects), 与窗口图标/exe 图标
#   共用同一份定义 —— 早先四处各画一套, 改一次要改四处且会逐渐漂移。
#
# ★ 为什么在代码里用 GDI 画, 而不是读 .ico/.png:
#   单文件 exe 解包后资源路径会变(见 _make_dot_icon 的历史注释),
#   纯代码画零路径依赖; 而且状态色需要运行时切换。
#
# 圆点: 右下角, 叠在右柱下端外侧
# ★ 2026-10-21 修正: 原来半径 2.25px、且带一圈"透明挖空环"。用户实测反馈
#   两个缺陷: ①绿点不圆(16px 直绘无抗锯齿, 4px 的圆只能是方块);
#   ②绿点上方有白块(挖空环把第三条横线切了个缺口)。
#   挖空环当初是为"黑点压在黑线上"准备的; 现在只有绿/黄两色, 与黑线天然
#   高对比, **完全不需要挖空** —— 去掉它, 白块随之消失。
#   圆度问题靠 8x 超采样解决(见 _render_rgba)。
# 圆点中心落在 12.8(而非 13.0): 半径 2.35 时右/下边缘到 15.15,
# 仍在画布内且留 0.35px 余量, 不会被托盘裁切。
# 中心取 .8 而不是 .0, 是为了让圆点跨越像素格的中心, 8x 采样下
# 绿/黄两色的覆盖率更一致(实测中心在整数格上时黄点会小一圈)。
DOT_CX, DOT_CY, DOT_R = 12.8, 12.8, 2.35
# 采样倍数: 8x -> 16px 图标内部按 128x128 计算, 圆点边缘有真实的灰度过渡
SS = 8

# 状态 -> 圆点颜色(None = 不画点)
STATE_DOT = {
    "connected": (0x22, 0xC5, 0x5E),     # 绿
    "waiting": (0xFB, 0xBF, 0x24),       # 黄
    "off": None,                          # 未启动: 无点
}
# 主体色: 浅色任务栏纯黑, 深色任务栏纯白
BODY_LIGHT = (0x00, 0x00, 0x00)
BODY_DARK = (0xFF, 0xFF, 0xFF)


def _taskbar_is_dark() -> bool:
    """当前任务栏是否为深色主题(决定图标主体用黑还是白)。

    ★ 读注册表 `SystemUsesLightTheme`:
        1 = 浅色任务栏 -> 图标用黑色
        0 = 深色任务栏 -> 图标用白色
      读取失败时按浅色处理(Windows 默认)。
    """
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as k:
            val, _ = winreg.QueryValueEx(k, "SystemUsesLightTheme")
            return int(val) == 0
    except (OSError, ValueError, FileNotFoundError):
        return False


def _make_channel_icon(dot_color, size: int = 16,
                       dark_taskbar: bool = False) -> int:
    """画托盘图标: 三条横线 + 右下角圆点。返回 HICON(失败 0)。

    逐像素写 32bpp DIB + AND 掩码(与 _make_dot_icon 同一套已验证的路径),
    区别只是"画什么": 圆 -> 三条横线 + 圆点。

    ★ 两个必须注意的点(踩过的坑, 见 _make_dot_icon 注释):
      1. restype 必须在调用之前声明, 否则 64 位句柄被截断 -> 图标空白
      2. 透明必须由 AND 掩码决定(1=透明), 只靠 alpha 不可靠
    """
    hdc_screen = 0
    hdc = 0
    hbm = 0
    hbm_mask = 0
    hicon = 0
    try:
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

        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = size
        bi.bmiHeader.biHeight = size
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0

        bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bi), 0,
                                     ctypes.byref(bits), None, 0)
        if not hbm or not bits.value:
            return 0
        gdi32.SelectObject(hdc, hbm)

        body = BODY_DARK if dark_taskbar else BODY_LIGHT
        br, bg, bb = body

        total = size * size
        color_px = (ctypes.c_ubyte * (total * 4)).from_address(bits.value)
        mask_stride = ((size + 31) // 32) * 4
        mask_buf = (ctypes.c_ubyte * (mask_stride * size))()
        for i in range(mask_stride * size):
            mask_buf[i] = 0xFF              # 默认全透明

        k = size / 16.0                     # 16px 基准 -> 实际尺寸

        # ★★★ 超采样抗锯齿(2026-10-21 用户反馈修正)
        #   原实现直接在 size×size 上逐像素判定"是否在圆内", 结果 16px 下
        #   直径 ~4.5px 的圆点只能落成**方块**(用户: "绿点也不圆")。
        #   现在按 SS 倍超采样累积覆盖率, 再缩回目标尺寸:
        #     cov[y][x] = 覆盖该像素的子样本数 / SS²
        #   颜色按覆盖率与背景混合 -> 边缘有真实的灰度过渡, 圆就圆了。
        #
        # ★★★ 2026-10-21 造型改为「H」(用户: "直接改成H"):
        #   形状来自 brand.tray_rects(), 与窗口图标 / exe 图标同一份定义。
        #   三个矩形(左柱/右柱/横杠)是同一颜色的实体, 覆盖率取并集。
        SS_LOCAL = SS if size <= 32 else 4   # 大尺寸不需要 8x(省时间)
        cov_body = [[0] * size for _ in range(size)]
        cov_dot = [[0] * size for _ in range(size)]
        n_sub = SS_LOCAL * SS_LOCAL
        rects = brand.tray_rects(16.0)

        for sy in range(size * SS_LOCAL):
            fy = (sy + 0.5) / SS_LOCAL      # 子样本在图标坐标里的位置
            ty = fy / k                     # 换算到 16px 基准
            yb = int(fy)
            if yb >= size:
                continue
            row_b = cov_body[yb]
            row_d = cov_dot[yb]
            for sx in range(size * SS_LOCAL):
                fx = (sx + 0.5) / SS_LOCAL
                tx = fx / k
                # --- H 的三个矩形 ---
                inside = False
                for (x0, y0, x1, y1) in rects:
                    if x0 <= tx < x1 and y0 <= ty < y1:
                        inside = True
                        break
                if inside:
                    row_b[int(fx)] += 1
                # --- 圆点(画在 H 之上, 覆盖式) ---
                if dot_color is not None:
                    dx = tx - DOT_CX
                    dy = ty - DOT_CY
                    if dx * dx + dy * dy <= DOT_R * DOT_R:
                        row_d[int(fx)] += 1

        def put_rgba(px: int, py: int, rgb, alpha: int) -> None:
            """写一个带 alpha 的像素(alpha 只用于"是否完全透明"的掩码)。"""
            if not (0 <= px < size and 0 <= py < size) or alpha <= 0:
                return
            row = size - 1 - py
            i = (row * size + px) * 4
            r_, g_, b_ = rgb
            color_px[i] = b_
            color_px[i + 1] = g_
            color_px[i + 2] = r_
            color_px[i + 3] = alpha
            byte_i = row * mask_stride + (px // 8)
            if alpha >= 128:
                mask_buf[byte_i] &= ~(0x80 >> (px % 8)) & 0xFF
            else:
                mask_buf[byte_i] |= (0x80 >> (px % 8)) & 0xFF

        for py in range(size):
            for px in range(size):
                cd = cov_dot[py][px] / n_sub      # 圆点覆盖率 0..1
                cb = cov_body[py][px] / n_sub     # 横线覆盖率 0..1
                if cd <= 0 and cb <= 0:
                    continue
                # ★★★ 正确的 over 合成(2026-10-21 用户反馈修正)
                #   圆点画在横线**之上**。旧实现只按圆点覆盖率写颜色:
                #     cd=0.3 -> 30% 绿 + 70% **透明** -> 露出任务栏白底
                #   这正是用户看到的"绿点上方有一块白"。
                #   正确公式(dot over body):
                #     out_a = cd + cb*(1-cd)
                #     out_c = (dot*cd + body*cb*(1-cd)) / out_a
                #   于是"圆点边缘压在横线上"得到的是**不透明的深绿**,
                #   而不是半透明绿 + 白色。
                out_a = cd + cb * (1.0 - cd)
                if out_a <= 0:
                    continue
                dr, dg, db = dot_color if dot_color is not None else body
                mix = []
                for i in range(3):
                    v = (dr, dg, db)[i] * cd + (br, bg, bb)[i] * cb * (1.0 - cd)
                    mix.append(int(round(v / out_a)))
                put_rgba(px, py, tuple(mix), int(round(255 * out_a)))
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

    on_show() / on_log() / on_start() / on_stop() / on_exit() 由调用方提供,
    它们会在**托盘线程**里被调用 —— 调用方必须自己切回 UI 线程
    (tkinter 里用 root.after(0, ...))。
    """

    def __init__(self, tooltip: str = "Com Bridge",
                 on_show=None, on_start=None, on_stop=None, on_exit=None,
                 on_log=None, ico_path: str | None = None):
        self.tooltip = tooltip
        self._on_show = on_show
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_exit = on_exit
        self._on_log = on_log
        self._ico_path = ico_path
        self._hwnd = 0
        self._hicon = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.running = False
        # 三态: connected / waiting / off(见 set_status)
        self.state = "off"
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

    def set_status(self, running: bool, text: str | None = None,
                   state: str | None = None) -> None:
        """更新状态(图标圆点 + tooltip)。

        ★ 三态(用户 2026-10-21 定稿):
            connected  绿点  已连接
            waiting    黄点  已启动但未连接(等待主电脑)
            off        无点  未启动

        参数:
          running  兼容旧接口(True/False), 仅在 state 未给出时用于推断
          text     tooltip 文本
          state    上面三种之一; 省略时 running=True -> connected, False -> off
        """
        self.running = running
        if state is None:
            state = "connected" if running else "off"
        if state not in STATE_DOT:
            state = "connected" if running else "off"
        self.state = state
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
        # ★ 三态图标: 三条横线 + 状态圆点(绿/黄/无点)
        #   主体色随任务栏明暗反转(读 SystemUsesLightTheme)
        try:
            dark_bar = _taskbar_is_dark()
            state = getattr(self, "state", "off")
            h = _make_channel_icon(STATE_DOT.get(state), size=_tray_icon_size(),
                                   dark_taskbar=dark_bar)
            if h:
                return h
        except Exception:  # noqa: BLE001
            pass
        # 兜底: 旧圆点图标(仅在 channel 图标生成失败时)
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
            user32.AppendMenuW(hmenu, MF_STRING, CMD_LOG, "查看日志")
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
                elif cmd == CMD_LOG and self._on_log:
                    self._on_log()
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
