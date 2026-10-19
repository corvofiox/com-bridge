# -*- coding: utf-8 -*-
"""★ 精确归因: hide() 里哪一步杀死了键盘钩子?

exp_hider_vs_hook 已证明 hide() 是凶手。hide() 内部有三步可疑操作:
  1. SetWindowPos 全屏 + EnableWindow + **SetActiveWindow + SetForegroundWindow**
     (= 抢前台)
  2. SetCapture(hider)  (= 抢鼠标捕获)
  3. ShowCursor(FALSE)  (= 光标可见性计数器)

本实验逐步施加, 每步后测 kbd_calls, 精确定位。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

from com_bridge.server import Server
from com_bridge.input import user32, kernel32
from inject_util import PROBE_VK, burst, require_injection

user32.ShowCursor.argtypes = [wintypes.BOOL]
user32.ShowCursor.restype = ctypes.c_int


def probe(hook, tag):
    b = hook.diag()
    sent = burst(2, PROBE_VK)
    require_injection(sent, 4)
    time.sleep(0.35)
    a = hook.diag()
    d = a["kbd_calls"] - b["kbd_calls"]
    print(f"    [{tag}] kbd_calls+{d}  fg={user32.GetForegroundWindow()}")
    return d


print("== 起 Server(只装钩子, 不 hide) ==")
srv = Server(name="attrib", side="right", accept_synthetic_input=True)
srv.start()
time.sleep(1.0)
hook = srv._hook
hider = srv._hider
print(f"  hider={hider}  hwnd={getattr(hider, '_hwnd', None)}")

p0 = probe(hook, "基线")

print()
print("== 步骤1: 只 SetWindowPos 全屏 + EnableWindow(不抢前台) ==")
hwnd = hider._hwnd
from com_bridge.cursor import (SWP_NOACTIVATE, SWP_SHOWWINDOW, HWND_TOP,
                               TRUE, FALSE)
user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 1920, 1080,
                    SWP_NOACTIVATE | SWP_SHOWWINDOW)
user32.EnableWindow(hwnd, TRUE)
time.sleep(0.3)
p1 = probe(hook, "全屏窗显示后")

print()
print("== 步骤2: + SetActiveWindow(不抢前台, 只是激活) ==")
user32.SetActiveWindow(hwnd)
time.sleep(0.3)
p2 = probe(hook, "SetActiveWindow 后")

print()
print("== 步骤3: + SetForegroundWindow(抢前台!) ==")
user32.SetForegroundWindow(hwnd)
time.sleep(0.3)
p3 = probe(hook, "SetForegroundWindow 后")

print()
print("== 步骤4: + SetCapture ==")
user32.SetCapture(hwnd)
time.sleep(0.3)
p4 = probe(hook, "SetCapture 后")

print()
print("== 步骤5: + ShowCursor(FALSE) ==")
for _ in range(10):
    if user32.ShowCursor(FALSE) < 0:
        break
time.sleep(0.3)
p5 = probe(hook, "ShowCursor(FALSE) 后")

# 还原
for _ in range(10):
    if user32.ShowCursor(TRUE) >= 0:
        break
user32.ReleaseCapture()
user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010 | 0x0080)
user32.EnableWindow(hwnd, FALSE)
srv.stop()

print()
print("== 归因表 ==")
print(f"  基线                    : {p0}")
print(f"  + 全屏窗                : {p1}  {'<< 死了' if p1 == 0 else ''}")
print(f"  + SetActiveWindow       : {p2}  {'<< 死了' if p2 == 0 else ''}")
print(f"  + SetForegroundWindow   : {p3}  {'<< 死了' if p3 == 0 else ''}")
print(f"  + SetCapture            : {p4}  {'<< 死了' if p4 == 0 else ''}")
print(f"  + ShowCursor(FALSE)     : {p5}  {'<< 死了' if p5 == 0 else ''}")
