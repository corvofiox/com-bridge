# -*- coding: utf-8 -*-
"""最小复现: 键盘钩子回调到底有没有被调用?

## 为什么需要它

用户实测: 进入副机后 GUI 显示"钩子吞输入(吞 N 鼠标/0 键盘)" ——
**kbd_eaten 恒为 0** 而 mouse_eaten 正常增长。`kbd_eaten` 是在**回调内部**
自增的, 所以 0 只可能意味着: **键盘钩子回调从未被系统调用**。

回看本项目此前的诊断输出, 同一现象早就出现过(verify_hook_loop 里
`kbd_eaten: 0, kbd_passed: 0` 而 `mouse_passed: 61`), 当时被误判成
"本机没注入键盘所以为 0" —— 其实那次实验**注入过**键盘。

## 本实验(对照设计)

同一进程、同一钩子对象, 分别注入**无标记**的鼠标移动与按键:
  * 无标记鼠标移动 -> 若 mouse_eaten 增长, 说明钩子链正常、relay 生效;
  * 无标记按键     -> 若 kbd_eaten 不增长, 则**键盘回调确实没被调用**。
两者用同一套 SendInput、同一种"无标记"注入, 因此差异只可能来自钩子本身。

★ 不依赖窗口/前台/GetAsyncKeyState(那些判据在本机已证明不可用:
  SendInput 注入不进入调用线程的输入队列)。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

from com_bridge.hook import LowLevelHook
from com_bridge.input import INPUT_KEYBOARD, INPUT_MOUSE, KEYEVENTF_KEYUP, user32

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
      + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


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


def send_mouse(dx, dy):
    ev = INP(type=INPUT_MOUSE)
    ev.mi.dx, ev.mi.dy = dx, dy
    ev.mi.dwFlags = 0x0001          # MOUSEEVENTF_MOVE
    ev.mi.dwExtraInfo = None        # 无标记 = 与物理同判
    return user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INP))


def send_key(vk, up=False):
    ev = INP(type=INPUT_KEYBOARD)
    ev.ki.wVk = vk
    ev.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    ev.ki.dwExtraInfo = None        # 无标记 = 与物理同判
    return user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INP))


print("== 安装钩子 ==")
h = LowLevelHook(on_key_event=None)
produced = []
h._on_key_event = lambda *a: produced.append(a)
ok = h.start()
check("钩子安装成功", ok, f"last_error={h.last_error}")
if not ok:
    sys.exit(1)
h.set_relay(True)
time.sleep(0.3)

print("== 对照 A: 注入无标记**鼠标**移动 ==")
a0 = dict(h.stats)
for _ in range(10):
    send_mouse(7, 3)
    time.sleep(0.01)
time.sleep(0.4)
a1 = dict(h.stats)
d_mouse = a1["mouse_eaten"] - a0["mouse_eaten"]
print(f"  (信息) mouse_eaten +{d_mouse}, mouse_passed "
      f"+{a1['mouse_passed'] - a0['mouse_passed']}, "
      f"last_call 更新={a1['last_call'] != a0['last_call']}")
check("鼠标钩子回调被调用(对照组必须成立)", d_mouse > 0,
      f"mouse_eaten +{d_mouse}")

print("== 对照 B: 注入无标记**键盘**按键 ==")
b0 = dict(h.stats)
for _ in range(5):
    send_key(0x41)          # 'A' down
    send_key(0x41, up=True)
    time.sleep(0.02)
time.sleep(0.5)
b1 = dict(h.stats)
d_kbd = b1["kbd_eaten"] - b0["kbd_eaten"]
d_kbd_pass = b1["kbd_passed"] - b0["kbd_passed"]
d_inj = b1["injected_passed"] - b0["injected_passed"]
print(f"  (信息) kbd_eaten +{d_kbd}, kbd_passed +{d_kbd_pass}, "
      f"injected_passed +{d_inj}, 自产={len(produced)}")
print(f"  (信息) 钩子句柄: mouse={h._hook_mouse} kbd={h._hook_kbd} "
      f"installed={h.installed}")

if d_kbd > 0:
    print("\n  => 键盘钩子回调**确实被调用**了。用户的 kbd_eaten=0 另有原因")
    print("     (例如另一程序先吃掉事件, 或用户端根本没注入键盘)。")
else:
    print("\n  => ★★ 复现: 键盘钩子回调**从未被调用**, 而鼠标正常。")
    print("     这解释了用户看到的 'kbd_eaten 恒为 0'。")

check("键盘钩子回调被调用(预期会 FAIL = 复现 bug)", d_kbd > 0,
      f"kbd_eaten +{d_kbd}")

h.set_relay(False)
h.stop()
print()
print(f"结果: {len(FAILED)} 项失败" + ("(其中键盘一项即复现)" if FAILED else ""))
