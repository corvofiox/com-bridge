# -*- coding: utf-8 -*-
"""实验 v2: 把"同型窗口"盖在屏幕上, **直接截图**存盘, 肉眼判定它是什么.

对照四种情况各截一张 PNG:
  before  不放窗口(基线)
  plain   同型窗(cursor.py 原样: 不分层/无背景刷/不绘制)
  layered 同型窗 + WS_EX_LAYERED + alpha=0 (Deskflow 做法)
  full    同型窗**铺满全屏**(复现 _do_hide 的真实条件, 极短 0.8s)

截图用 Pillow 的 ImageGrab(BitBlt 屏幕 DC), 不依赖任何第三方窗口。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

from PIL import ImageGrab

user32 = ctypes.WinDLL("user32", use_last_error=True)
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
user32.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
user32.DestroyWindow.argtypes = [wintypes.HWND]

WS_POPUP = 0x80000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
LWA_ALPHA = 0x00000002

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_paint_probe")
os.makedirs(OUT, exist_ok=True)

_procs = []


def make_window(name, ex_style):
    hinst = kernel32.GetModuleHandleW(None)
    cb = WNDPROC(lambda hw, m, w, l: user32.DefWindowProcW(hw, m, w, l))
    _procs.append(cb)                       # 防 GC
    wc = WNDCLASSW()
    wc.lpfnWndProc = cb
    wc.hInstance = hinst
    wc.hCursor = None
    wc.lpszClassName = name
    wc.hbrBackground = None                 # ★ 与 cursor.py 一致: 无背景画刷
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(ex_style, name, name, WS_POPUP,
                                  0, 0, 1, 1, None, None, hinst, None)
    return hwnd


def shot(tag, box=None):
    time.sleep(0.35)
    img = ImageGrab.grab(bbox=box, all_screens=True)
    path = os.path.join(OUT, f"{tag}.png")
    img.save(path)
    # 亮度统计
    small = img.convert("L")
    hist = small.histogram()
    total = sum(hist) or 1
    mean = sum(i * c for i, c in enumerate(hist)) / total
    dark = sum(hist[:16]) / total
    print(f"  [{tag}] {img.size} 平均亮度={mean:.1f} 全黑像素占比={dark * 100:.1f}% -> {path}")
    return path


def trial(tag, box, ex_style, alpha=None):
    x, y, w, h = box
    name = f"PaintProbe_{tag}"
    hwnd = make_window(name, ex_style)
    if not hwnd:
        print(f"  [{tag}] 创建失败 {kernel32.GetLastError()}")
        return
    if alpha is not None:
        user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
    user32.SetWindowPos(hwnd, 0, x, y, w, h,       # HWND_TOP, 同 _do_hide
                        SWP_NOACTIVATE | SWP_SHOWWINDOW)
    shot(tag, box)
    user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_HIDEWINDOW)
    user32.DestroyWindow(hwnd)


def main() -> int:
    print("A. 基线")
    shot("before", (0, 0, 600, 400))

    print("B. 同型窗 600x400, 不分层不绘制")
    trial("plain", (0, 0, 600, 400),
          WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)

    print("C. 同型窗 600x400 + LAYERED alpha=0")
    trial("layered", (0, 0, 600, 400),
          WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED, alpha=0)

    print("D. 同型窗 **铺满全屏** 1920x1080 (0.8s, 立即收起)")
    x, y = 0, 0
    w, h = 1920, 1080
    name = "PaintProbe_full"
    hwnd = make_window(name, WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)
    if hwnd:
        user32.SetWindowPos(hwnd, 0, x, y, w, h,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)
        time.sleep(0.8)
        full = ImageGrab.grab(all_screens=True)
        p = os.path.join(OUT, "full.png")
        full.save(p)
        small = full.convert("L")
        hist = small.histogram()
        total = sum(hist) or 1
        mean = sum(i * c for i, c in enumerate(hist)) / total
        dark = sum(hist[:16]) / total
        print(f"  [full] {full.size} 平均亮度={mean:.1f} 全黑占比={dark * 100:.1f}% -> {p}")
        user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_HIDEWINDOW)
        user32.DestroyWindow(hwnd)
    time.sleep(0.3)
    shot("after", (0, 0, 600, 400))
    print("\n判定: 看 *_paint_probe/*.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
