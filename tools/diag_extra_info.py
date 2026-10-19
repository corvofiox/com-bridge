# -*- coding: utf-8 -*-
"""微诊断: WM_MOUSEMOVE 的 wnd proc 里 GetMessageExtraInfo 到底返回什么?

背景: verify_hider.py 第二跑发现 60 对带 TAG 注入只产生 2 条合并后的
WM_MOUSEMOVE, 其中 1 条 GetMessageExtraInfo() != TAG(漏判) → hider 被
物理判据误顶掉。本脚本逐条打印 wnd proc 收到 WM_MOUSEMOVE 时的
GetMessageExtraInfo 值, 弄清它的真实语义(当前消息的? 下一条的? 合并后
取哪条的?), 以决定注入判别的最终机制。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes

sys.path.insert(0, ".")

from com_bridge.input import (
    INPUT,
    INPUT_KEYBOARD,
    KEYEVENTF_KEYUP,
    WNDCLASSW,
    WNDPROC,
    InputInjector,
    kernel32,
    user32,
)

TAG = InputInjector.TAG

# 用一个独立窗口(不依赖 CursorHider)避免副作用
seen = []          # (msg, extra, lparam)
ready = threading.Event()
stop = threading.Event()
hwnd_holder = {}

user32.GetMessageExtraInfo.restype = ctypes.c_ssize_t
user32.GetMessageExtraInfo.argtypes = []


def proc(hwnd, msg, wparam, lparam):
    if msg == 0x0200:  # WM_MOUSEMOVE
        seen.append((msg, user32.GetMessageExtraInfo(), lparam))
        return 0
    if msg == 0x0012:  # WM_QUIT
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def run():
    hmod = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = WNDPROC(proc)
    wc.hInstance = hmod
    wc.lpszClassName = "ComBridgeDiagExtraInfo"
    if not user32.RegisterClassW(ctypes.byref(wc)):
        err = kernel32.GetLastError()
        if err != 1410:
            print("RegisterClassW 失败:", err)
            return
    hwnd = user32.CreateWindowExW(
        0, wc.lpszClassName, "diag",
        0x80000000,          # WS_POPUP(顶层可见窗口, SetCapture 才能收到)
        10, 10, 60, 40, None, None, hmod, None)
    print("  hwnd=%s (CreateWindowExW %s)" % (
        hwnd, "OK" if hwnd else "FAILED err=%d" % kernel32.GetLastError()))
    user32.ShowWindow(hwnd, 5)   # SW_SHOW
    hwnd_holder["hwnd"] = hwnd
    cap = user32.SetCapture(hwnd)
    print("  SetCapture=%s GetLastError=%d"
          % ("OK" if user32.GetCapture() == hwnd else "FAILED",
             kernel32.GetLastError()))
    ready.set()
    msg = wintypes.MSG()
    while not stop.is_set():
        r = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001)
        if r:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.002)


def send_move(dx, dy, tag):
    class MI(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong),
                    ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.c_void_p)]

    class INP(ctypes.Structure):
        class U(ctypes.Union):
            _fields_ = [("mi", MI)]
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_ulong), ("u", U)]

    ev = INP(type=0)
    ev.mi.dx = dx
    ev.mi.dy = dy
    ev.mi.dwFlags = 0x0001  # MOVE
    ev.mi.dwExtraInfo = TAG if tag else None
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
    user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INP))


def main():
    print("=" * 72)
    print("微诊断: wnd proc 内 GetMessageExtraInfo 的真实返回值")
    print("=" * 72)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    if not ready.wait(2.0):
        print("窗口未就绪")
        return 1
    time.sleep(0.3)

    # A: 5 次带 TAG 注入(留间隔, 避免合并)
    print("\nA. 5 次带 TAG 注入(间隔 120ms 避免合并)")
    seen.clear()
    for _ in range(5):
        send_move(2, 1, tag=True)
        send_move(-2, -1, tag=False)
        time.sleep(0.12)
    time.sleep(0.3)
    for i, (m, extra, lp) in enumerate(seen):
        print("  #%d extra=0x%016X  %s  lparam=0x%08X"
              % (i, extra & 0xFFFFFFFFFFFFFFFF,
                 "TAG!" if extra == TAG else "    ", lp & 0xFFFFFFFF))

    # B: 快速连续带 TAG 注入(会合并)
    print("\nB. 60 对快速带 TAG 注入(会合并)")
    seen.clear()
    for _ in range(60):
        send_move(3, 2, tag=True)
        send_move(-3, -2, tag=False)
        time.sleep(0.005)
    time.sleep(0.3)
    tags = sum(1 for _, e, _ in seen if e == TAG)
    print("  收到 %d 条 WM_MOUSEMOVE, 其中 extra==TAG: %d 条"
          % (len(seen), tags))
    for i, (m, extra, lp) in enumerate(seen[:10]):
        print("  #%d extra=0x%016X  %s"
              % (i, extra & 0xFFFFFFFFFFFFFFFF,
                 "TAG!" if extra == TAG else "    "))

    stop.set()
    time.sleep(0.2)
    print("\n(对照: TAG=0x%X)" % TAG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
