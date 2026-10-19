# -*- coding: utf-8 -*-
"""★ 验证修复: 去掉 SetForegroundWindow 后, 钩子存活 + 光标仍能隐藏。

两个都必须成立:
  1. kbd_calls 持续增长(钩子活着, 键能被吞)  <- 本轮修复目标
  2. GetCursorInfo().bShowing == False(光标真的隐藏了) <- 不能回归 BUG2
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

from com_bridge.server import Server
from com_bridge.input import user32
from inject_util import PROBE_VK, burst, require_injection

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", ctypes.c_void_p),
                ("ptScreenPos", wintypes.POINT)]


def cursor_showing() -> bool:
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        return True
    return bool(ci.flags & 0x00000001)   # CURSOR_SHOWING


def probe_kbd(hook, tag):
    b = hook.diag()
    sent = burst(2, PROBE_VK)
    require_injection(sent, 4)
    time.sleep(0.35)
    a = hook.diag()
    d_calls = a["kbd_calls"] - b["kbd_calls"]
    d_eaten = a["kbd_eaten"] - b["kbd_eaten"]
    print(f"    [{tag}] kbd_calls+{d_calls} kbd_eaten+{d_eaten} "
          f"光标显示={cursor_showing()}")
    return d_calls, d_eaten


print("== 起 Server ==")
srv = Server(name="fix", side="right", accept_synthetic_input=True)
srv.start()
time.sleep(1.0)
hook = srv._hook

print()
print("== 生产路径: 进入穿越态(_acquire_control) ==")
srv._edge._outside = True
srv._edge._streak = 2
srv._acquire_control()
time.sleep(0.5)

d1c, d1e = probe_kbd(hook, "穿越后 500ms")
time.sleep(0.6)
d2c, d2e = probe_kbd(hook, "穿越后 1.1s")
time.sleep(1.0)
d3c, d3e = probe_kbd(hook, "穿越后 2.1s")

check("钩子回调持续被调用(修复核心)", d1c > 0 and d2c > 0 and d3c > 0,
      f"{d1c}/{d2c}/{d3c}")
check("钩子真的吞键", d1e > 0 and d2e > 0 and d3e > 0,
      f"{d1e}/{d2e}/{d3e}")
check("光标已隐藏(不回归 BUG2)", cursor_showing() is False,
      f"showing={cursor_showing()}")

print()
print("== 退出穿越态 ==")
srv._edge._outside = False
srv._release_control()
time.sleep(0.6)
d4c, _ = probe_kbd(hook, "退回后")
check("退回后钩子仍在(放行本机输入)", d4c > 0, f"+{d4c}")
check("光标已恢复", cursor_showing() is True, f"showing={cursor_showing()}")

srv.stop()
print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
