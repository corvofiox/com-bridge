# -*- coding: utf-8 -*-
"""活体验证: `CursorHider._is_foreign_fullscreen` 真的能认出"别的进程的全屏窗"。

为什么必须活体验证: `rect_covers` 只是纯函数(已单测), 但 Win32 胶水
(枚举 pid / GetWindowRect / 虚拟桌面)只能真跑。判断错了会有两种后果:
  * 该认的没认出来 -> 从全屏程序(游戏/远程桌面)抢前台 -> 黑屏
  * 不该认的认了   -> 空桌面(Progman)也当成全屏 -> 光标隐藏不掉

做法(零视觉干扰): 由**另一个进程**创建一个铺满虚拟桌面的
`WS_EX_LAYERED + alpha=0` 窗口(不可见但几何上是全屏), 再让本进程判断。
对照组: 同一进程的 500x500 窗口、以及外壳窗口类名(Progman) 必须判 False。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import textwrap
import time
from ctypes import wintypes

from com_bridge.cursor import CursorHider, _SHELL_CLASSES

HELPER = textwrap.dedent(r"""
    import ctypes, sys, time
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
    class WC(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR)]
    u.CreateWindowExW.restype = wintypes.HWND
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
        wintypes.HINSTANCE, ctypes.c_void_p]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
        ctypes.c_ubyte, wintypes.DWORD]
    u.DefWindowProcW.restype = ctypes.c_ssize_t
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                 wintypes.LPARAM]
    u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, wintypes.UINT]
    cb = WNDPROC(lambda h, m, w, l: u.DefWindowProcW(h, m, w, l))
    wc = WC(); wc.lpfnWndProc = cb; wc.hInstance = k.GetModuleHandleW(None)
    wc.lpszClassName = "FsProbe_%d" % (ctypes.c_int(1).value * 7)
    u.RegisterClassW(ctypes.byref(wc))
    w = u.GetSystemMetrics(78); h = u.GetSystemMetrics(79)     # SM_CXVIRTUALSCREEN/Y
    hw = u.CreateWindowExW(0x00080000 | 0x00000020 | 0x00000080,  # LAYERED|TRANSPARENT|TOOLWINDOW
                           wc.lpszClassName, "FsProbe", 0x80000000,
                           0, 0, w, h, None, None, wc.hInstance, None)
    u.SetLayeredWindowAttributes(hw, 0, 0, 0x00000002)          # alpha=0 -> 不可见
    u.SetWindowPos(hw, 0, 0, 0, w, h, 0x0010 | 0x0040)          # NOACTIVATE|SHOWWINDOW
    print("%d %d %d" % (hw, w, h), flush=True)
    time.sleep(8)
""")


def main() -> int:
    proc = subprocess.Popen([sys.executable, "-c", HELPER],
                            stdout=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline().strip()
        if not line:
            print("!! 辅助进程没有输出")
            return 1
        hwnd, w, h = (int(x) for x in line.split())
        time.sleep(0.3)
        hdr = CursorHider()
        got = hdr._is_foreign_fullscreen(hwnd)
        cls = hdr._window_class(hwnd)
        print(f"辅助全屏窗 hwnd={hwnd} {w}x{h} class={cls}")
        print(f"  _is_foreign_fullscreen -> {got}  (期望 True)")

        # 对照组1: 本进程窗口(pid 相同)必须不算"别人的"
        me = ctypes.WinDLL("user32").GetForegroundWindow()
        print(f"  本进程/前台窗口 {me} -> {hdr._is_foreign_fullscreen(me)}"
              f"  (期望 False)")

        # 对照组2: 外壳窗口类名不得被当成全屏程序
        print(f"  _SHELL_CLASSES 排除 Progman/WorkerW: "
              f"{'Progman' in _SHELL_CLASSES and 'WorkerW' in _SHELL_CLASSES}")

        ok = (got is True)
        print("=> " + ("通过" if ok else "失败"))
        return 0 if ok else 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
