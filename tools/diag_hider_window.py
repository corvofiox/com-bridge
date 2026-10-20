# -*- coding: utf-8 -*-
"""只读诊断: 枚举所有顶层窗口, 找出 CursorHider 窗口, 打印样式/位置/可见性.

不创建任何窗口, 不改变任何状态 —— 纯读取, 可在生产运行时安全执行。
用来确认 cursor.py 里那个全屏 hider 窗口的真实属性:
  * 有没有 WS_EX_LAYERED(全透明靠它, 没有就可能是不透明黑)
  * 有没有 WS_EX_TRANSPARENT / WS_EX_TOOLWINDOW
  * 当前 rect / 是否 visible / 是否 foreground
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                 wintypes.LPARAM)

user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_NOREDIRECTIONBITMAP = 0x00200000
WS_POPUP = 0x80000000


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


hits = []


def _cb(hwnd, lparam):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    cls = buf.value
    if "ComBridgeHider" not in cls:
        return True
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title, 256)
    alpha = (wintypes.DWORD * 1)()
    flags = (wintypes.DWORD * 1)()
    ok = user32.GetLayeredWindowAttributes(hwnd, None,
                                           ctypes.byref(alpha),
                                           ctypes.byref(flags)) if (ex & WS_EX_LAYERED) else 0
    hits.append(dict(
        hwnd=hwnd, cls=cls, pid=pid.value, title=title.value,
        style=style, ex=ex,
        layered=bool(ex & WS_EX_LAYERED),
        transparent=bool(ex & WS_EX_TRANSPARENT),
        toolwindow=bool(ex & WS_EX_TOOLWINDOW),
        noactivate=bool(ex & WS_EX_NOACTIVATE),
        noredir=bool(ex & WS_EX_NOREDIRECTIONBITMAP),
        popup=bool(style & WS_POPUP),
        visible=bool(user32.IsWindowVisible(hwnd)),
        fg=(user32.GetForegroundWindow() == hwnd),
        rect=(r.left, r.top, r.right - r.left, r.bottom - r.top),
        has_lwa=bool(ok), alpha=alpha[0], lwa_flags=flags[0],
    ))
    return True


def main() -> int:
    cb = WNDENUMPROC(_cb)
    user32.EnumWindows(cb, 0)
    if not hits:
        print("没找到 ComBridgeHider 窗口(光标隐藏未激活, 或程序没在跑)")
        return 0
    for h in hits:
        print("=" * 70)
        for k, v in h.items():
            print(f"  {k:12s} = {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
