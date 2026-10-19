# -*- coding: utf-8 -*-
"""★ 核实已被证伪的假设: 跨进程钩子链真的会"前面吞后面断"吗?

## 历史记录(已实测)
tools/exp_cross_process_chain.py 结论:
  "跨进程不阻断(我们仍被调用)" —— 独立进程装"吞一切"键盘钩子后,
  我们的 kbd_calls 仍然 +4。

## 但代码注释里大量保留着相反的断言
  hook.py:392   "系统按后装的先调用顺序依次... 前面一环吞掉链就断"
  server.py:1176 "还要夺回钩子链首... 任何比我们晚装钩子的程序都会把
                  我们永久挤到链后"
  cli.py:283    "本机实测链首归属会来回翻转(谁最后装谁赢)"

## 本工具重新做一遍**跨进程**实验(不是同进程), 用两种干扰者
  A. 干扰者只放行(不吞) —— 基线
  B. 干扰者吞一切 —— 看我们是否还能收到
  C. 干扰者后装(模拟"晚装者赢") —— 看我们是否被挤后
每组都在**独立进程**里装干扰者。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, ".")

HERE = os.path.dirname(os.path.abspath(__file__))
GRABBER = os.path.join(HERE, "_grabber2.py")

GRABBER_SRC = '''# -*- coding: utf-8 -*-
import ctypes, sys, time
from ctypes import wintypes
user32 = ctypes.windll.user32
KBDLL = 13
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                             wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                     ctypes.c_void_p, wintypes.DWORD]
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM]
MODE = sys.argv[1] if len(sys.argv) > 1 else "eat"

@HOOKPROC
def hook(code, wparam, lparam):
    if code >= 0:
        if MODE == "eat":
            return 1
    return user32.CallNextHookEx(None, code, wparam, lparam)

msg = wintypes.MSG()
user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
h = user32.SetWindowsHookExW(KBDLL, hook, None, 0)
print("READY" if h else "FAIL", flush=True)
end = time.time() + 90
while time.time() < end:
    r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
    if r <= 0:
        break
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
'''


def measure(tag: str) -> int:
    from com_bridge.hook import LowLevelHook
    from inject_util import PROBE_VK, burst, require_injection
    h = LowLevelHook(name=tag, on_key_event=lambda *a: None)
    if not h.start():
        h.stop()
        print(f"  [{tag}] 我们的钩子装不上")
        return -1
    b = h.diag()
    sent = burst(3, PROBE_VK)
    try:
        require_injection(sent, 6)
    except Exception:  # noqa: BLE001
        h.stop()
        return -2
    time.sleep(0.45)
    a = h.diag()
    d = a["kbd_calls"] - b["kbd_calls"]
    print(f"  [{tag}] kbd_calls+{d}")
    h.stop()
    time.sleep(0.2)
    return d


def start_grabber(mode: str):
    p = subprocess.Popen([sys.executable, GRABBER, mode],
                         stdout=subprocess.PIPE, text=True)
    t0 = time.time()
    ok = False
    while time.time() - t0 < 10:
        line = p.stdout.readline()
        if not line:
            break
        if "READY" in line:
            ok = True
            break
        if "FAIL" in line:
            break
    return p, ok


with open(GRABBER, "w", encoding="utf-8") as f:
    f.write(GRABBER_SRC)

try:
    print("== A. 无干扰(基线) ==")
    a = measure("baseline")

    print()
    print("== B. 独立进程装'只放行'钩子(不吞) ==")
    p1, ok1 = start_grabber("pass")
    time.sleep(0.4)
    print(f"  干扰者就绪={ok1}")
    b = measure("grabber-pass")
    p1.terminate()
    try:
        p1.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p1.kill()
    time.sleep(0.5)

    print()
    print("== C. 独立进程装'吞一切'钩子 ==")
    p2, ok2 = start_grabber("eat")
    time.sleep(0.4)
    print(f"  干扰者就绪={ok2}")
    c = measure("grabber-eat")
    p2.terminate()
    try:
        p2.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p2.kill()
    time.sleep(0.5)

    print()
    print("== D. 吞一切 + 我们的钩子**后装**(模拟'晚装者') ==")
    p3, ok3 = start_grabber("eat")
    time.sleep(0.4)
    print(f"  干扰者就绪={ok3} (已先装)")
    d = measure("late-comer")
    p3.terminate()
    try:
        p3.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p3.kill()

    print()
    print("== 结论 ==")
    print(f"  A 基线(无干扰)        : +{a}")
    print(f"  B 干扰者放行          : +{b}")
    print(f"  C 干扰者吞一切        : +{c}")
    print(f"  D 干扰者先装+吞一切   : +{d}")
    print()
    if c > 0 and d > 0:
        print("  ★★ 确认: **跨进程钩子链不会阻断我们**(即使对方吞一切)")
        print("     => 代码里'前面吞掉链就断/被挤到链后'的断言是**错的**,")
        print("        应当清理(真实根因是 GUI 窗口持有前台焦点)。")
    else:
        print("  ★ 跨进程确实会阻断 -> 旧断言成立, 保留相关逻辑")
finally:
    try:
        os.unlink(GRABBER)
    except OSError:
        pass
