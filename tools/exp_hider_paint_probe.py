# -*- coding: utf-8 -*-
"""实验: 一个与 cursor.py 的 hider **完全同型**的窗口, 屏幕上到底看起来是什么?

cursor.py 的 hider 窗口属性:
    WS_POPUP  +  WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
    类背景画刷 hbrBackground = NULL(从未设置)
    消息处理里 **没有 WM_PAINT / WM_ERASEBKGND 分支**
    => DefWindowProc 只 BeginPaint/EndPaint, 什么也不画

问题: 这样一个"从不绘制"的不分层窗口(没有 WS_EX_LAYERED), DWM 合成的
结果是"透出桌面"还是"一块黑"? 这决定了 _do_hide() 把窗口铺满整个虚拟
桌面时, 用户会不会看到**整屏黑**.

本脚本在屏幕**左上角**放一个 600x400 的同型窗口, 停留 1.2s, 期间用 GDI
直接读屏幕像素, 与该区域**静止基线**对比:
  * 若窗口区域像素 ≈ 基线(桌面内容) -> 窗口是"透"的, 黑屏另有原因
  * 若窗口区域像素 ≈ 全黑(且基线不是黑) -> **窗口不透明黑, 黑屏机制确认**

同时做 A/B: 同一个窗口加 WS_EX_LAYERED + SetLayeredWindowAttributes
(alpha=0) 再看一次 —— 这是 Deskflow 的做法, 用来验证修法有效。

运行结束后销毁窗口, 不改变任何系统状态。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateWindow.argtypes = [wintypes.HWND]
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.DWORD
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, wintypes.HDC,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

WS_POPUP = 0x80000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
SRCCOPY = 0x00CC0020
LWA_ALPHA = 0x00000002

X, Y, W, H = 0, 0, 600, 400


def grab_screen_region(x, y, w, h):
    """用 BitBlt 从屏幕 DC 抓一块, 返回 (均值亮度, 最小, 最大) 与采样点。"""
    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, SRCCOPY)
    vals = []
    for sy in range(0, h, max(1, h // 12)):
        for sx in range(0, w, max(1, w // 12)):
            v = gdi32.GetPixel(hdc_mem, sx, sy)
            if v == 0xFFFFFFFF:      # CLR_INVALID
                continue
            r, g, b = v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF
            vals.append((r + g + b) / 3.0)
    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)
    if not vals:
        return None
    return (sum(vals) / len(vals), min(vals), max(vals), len(vals))


def make_window(name, ex_style):
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = WNDPROC(lambda hw, m, w, l: user32.DefWindowProcW(hw, m, w, l))
    wc.hInstance = hinst
    wc.hCursor = None
    wc.lpszClassName = name
    wc.hbrBackground = None          # ★ 与 cursor.py 完全一致: 无背景画刷
    atom = user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(ex_style, name, name, WS_POPUP,
                                  X, Y, 1, 1, None, None, hinst, None)
    return hwnd, wc.lpfnWndProc


def trial(label, ex_style, layered_alpha=None):
    name = f"PaintProbe_{label}_{id(label) & 0xFFFF:x}"
    hwnd, cb = make_window(name, ex_style)
    if not hwnd:
        print(f"[{label}] CreateWindowExW 失败: {kernel32.GetLastError()}")
        return None
    if layered_alpha is not None:
        user32.SetLayeredWindowAttributes.argtypes = [
            wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
        user32.SetLayeredWindowAttributes(hwnd, 0, layered_alpha, LWA_ALPHA)
    # 与 _do_hide 相同的定位/显示调用
    user32.SetWindowPos(hwnd, -1, X, Y, W, H, SWP_NOACTIVATE | SWP_SHOWWINDOW)
    user32.UpdateWindow(hwnd)
    time.sleep(0.45)
    res = grab_screen_region(X, Y, W, H)
    time.sleep(0.15)
    res2 = grab_screen_region(X, Y, W, H)
    # 收起
    user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0,
                        SWP_NOACTIVATE | 0x0001 | 0x0002 | SWP_HIDEWINDOW)
    user32.DestroyWindow(hwnd)
    print(f"[{label}] 区域像素 均值/最小/最大 = {res}")
    print(f"[{label}] 1.2s后复查             = {res2}")
    return res, res2


def main() -> int:
    print("A. 基线(不放窗口)")
    base = grab_screen_region(X, Y, W, H)
    print("   基线 =", base)

    print("\nB. 同型窗(curor.py 原样: 不分层, 不绘制)")
    b = trial("plain", WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)

    print("\nC. Deskflow 式(分层 + alpha=0)")
    c = trial("layered0", WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED,
              layered_alpha=0)

    print("\nD. 对照(分层 + alpha=255, 应当整块黑/不透明)")
    d = trial("layered255", WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED,
              layered_alpha=255)

    print("\n==== 判定 ====")
    if base and b and b[0]:
        lb, lmin = b[0][0], b[0][1]
        print(f"基线均值={base[0]:.1f}  同型窗均值={lb:.1f} 最小={lmin:.1f}")
        if lb < 8 and base[0] > 25:
            print("=> ★ 同型窗**不透明黑**: 铺满全屏 = 整屏黑屏, 机制确认")
        elif abs(lb - base[0]) < 12:
            print("=> 同型窗**透出桌面**(与基线一致): 黑屏不是它造成的")
        else:
            print("=> 同型窗区域与基线明显不同, 需人工看截图")
    if c and c[0]:
        print(f"分层 alpha=0 均值={c[0][0]:.1f} (期望 ≈ 基线 {base[0]:.1f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
