# -*- coding: utf-8 -*-
"""原始 dump: 每个 WM_INPUT 的 dwType/dwSize/hDevice/前 32 字节。

用于回答一个**无法靠推理确定**的问题: 注入键盘时到底收到了什么?
本机实测 `raw_total` 增长但 `raw_key`/`raw_mouse`/`raw_synthetic` 全都不动
—— 说明有 WM_INPUT 落进了"两个分支都不匹配"的空隙, 必须看原始字节。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

from com_bridge import input as I
from com_bridge.input import (INPUT_KEYBOARD, INPUT_MOUSE, KEYEVENTF_KEYUP,
                              RAWINPUTDEVICE, RAWINPUTHEADER, RIDEV_INPUTSINK,
                              RID_INPUT, WNDCLASSW, WNDPROC, kernel32, user32)

dumps: list[tuple] = []
_ready = False


class KBD(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class MI(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class U(ctypes.Union):
    _fields_ = [("ki", KBD), ("mi", MI)]


class INP(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", U)]


user32.SendInput.restype = ctypes.c_uint
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]

print(f"sizeof(RAWINPUTHEADER) = {ctypes.sizeof(RAWINPUTHEADER)}  (Windows 64 位应为 24)")
print(f"sizeof(RAWINPUTDEVICE) = {ctypes.sizeof(RAWINPUTDEVICE)}  (应为 16)")
print(f"sizeof(RAWMOUSE)       = {ctypes.sizeof(I.RAWMOUSE)}")
print(f"sizeof(RAWKEYBOARD)    = {ctypes.sizeof(I.RAWKEYBOARD)}")
print()

hwnd_box = []
ready_ev = __import__("threading").Event()


def proc(hwnd, msg, wparam, lparam):
    if msg == 0x00FF:      # WM_INPUT
        size = ctypes.c_uint(0)
        user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size),
                               ctypes.sizeof(RAWINPUTHEADER))
        if size.value:
            buf = ctypes.create_string_buffer(size.value)
            got = user32.GetRawInputData(lparam, RID_INPUT, buf,
                                         ctypes.byref(size),
                                         ctypes.sizeof(RAWINPUTHEADER))
            hdr = RAWINPUTHEADER.from_buffer_copy(
                buf.raw[:ctypes.sizeof(RAWINPUTHEADER)])
            dumps.append((hdr.dwType, hdr.dwSize, hdr.hDevice,
                          buf.raw[:32].hex(" ")))
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def win_thread():
    hinst = kernel32.GetModuleHandleW(None)
    cls = f"RawDump_{id(dumps):x}"
    wc = WNDCLASSW()
    wc.lpfnWndProc = WNDPROC(proc)
    wc.hInstance = hinst
    wc.lpszClassName = cls
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, cls, "RawDump", 0, 0, 0, 0, 0,
                                  None, None, hinst, None)
    hwnd_box.append(hwnd)
    rid = RAWINPUTDEVICE()
    rid.usUsagePage = 0x01
    rid.usUsage = 0x02
    rid.dwFlags = RIDEV_INPUTSINK
    rid.hwndTarget = hwnd
    user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                   ctypes.sizeof(RAWINPUTDEVICE))
    rid.usUsage = 0x06
    ok = user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                        ctypes.sizeof(RAWINPUTDEVICE))
    print(f"键盘 RawInput 注册 = {bool(ok)}")
    ready_ev.set()
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


import threading
threading.Thread(target=win_thread, daemon=True).start()
ready_ev.wait(3.0)
time.sleep(0.3)

print("=== 注入鼠标移动 ===")
dumps.clear()
for _ in range(3):
    ev = INP(type=INPUT_MOUSE)
    ev.mi.dx, ev.mi.dy = 5, 5
    ev.mi.dwFlags = 0x0001
    ev.mi.dwExtraInfo = None
    user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INP))
    time.sleep(0.03)
time.sleep(0.4)
for t, sz, hd, raw in dumps:
    print(f"  dwType={t} dwSize={sz} hDevice={hd} raw={raw}")
print(f"  共 {len(dumps)} 条")

print()
print("=== 注入键盘按键 ===")
dumps.clear()
for _ in range(3):
    ev = INP(type=INPUT_KEYBOARD)
    ev.ki.wVk = 0x41
    ev.ki.dwFlags = 0
    ev.ki.dwExtraInfo = None
    user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INP))
    ev.ki.dwFlags = KEYEVENTF_KEYUP
    user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INP))
    time.sleep(0.03)
time.sleep(0.4)
for t, sz, hd, raw in dumps:
    print(f"  dwType={t} dwSize={sz} hDevice={hd} raw={raw}")
print(f"  共 {len(dumps)} 条")
print()
print("说明: dwType 0=鼠标 1=键盘 2=HID。若键盘注入后一条都没有,")
print("      说明键盘 WM_INPUT 根本没送达本窗口(被别的程序抢走了)。")
