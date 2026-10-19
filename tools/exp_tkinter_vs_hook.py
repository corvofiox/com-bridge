# -*- coding: utf-8 -*-
"""★ 隔离变量: 是"前台窗口"导致钩子回调失效吗?

## 已观察到的矛盾
* diag_kbd_owner_live(无窗口)      -> probe=True,  kbd_calls+6  ✅
* verify_key_leak_window(有 tkinter 前台窗口) -> kbd_calls+0  ❌

## 假设
tkinter 主线程运行 mainloop 时, 它持有 GIL 并阻塞在 Windows 消息循环。
而我们的**钩子线程**需要 GIL 才能执行 ctypes 回调。
若 tkinter 的 mainloop 长期持有 GIL(或与钩子线程争抢), 回调就会
超过 LowLevelHooksTimeout(约 300ms) -> 被系统**静默摘除**。

## 本实验三组对照
A. 无 tkinter(纯控制台)            -> 预期可达
B. tkinter 窗口 + mainloop(真)     -> 看是否失效
C. tkinter 窗口但不跑 mainloop     -> 区分"窗口"与"mainloop"

每组都测 probe + kbd_calls。
"""
from __future__ import annotations

import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, ".")

from com_bridge.hook import LowLevelHook
from inject_util import PROBE_VK, burst, require_injection


def measure(tag: str) -> tuple[bool | None, int]:
    h = LowLevelHook(name=tag, on_key_event=lambda *a: None)
    if not h.start():
        h.stop()
        return None, 0
    p = h.probe_keyboard_hook()
    b = h.diag()
    sent = burst(3, PROBE_VK)
    try:
        require_injection(sent, 6)
    except Exception:  # noqa: BLE001
        h.stop()
        return p, -1
    time.sleep(0.4)
    a = h.diag()
    d = a["kbd_calls"] - b["kbd_calls"]
    h.stop()
    time.sleep(0.3)
    return p, d


print("== A. 无 tkinter(纯控制台) ==")
pa, ca = measure("NoTk")
print(f"  probe={pa} kbd_calls+{ca}")

print()
print("== B. 有 tkinter 窗口 + 真 mainloop ==")
result_b = {}


def run_b():
    root_b = tk.Toplevel(root)
    root_b.geometry("300x150+50+50")
    root_b.deiconify()
    root_b.lift()
    root_b.focus_force()
    root_b.update()
    time.sleep(0.3)
    p, c = measure("WithTkMainloop")
    result_b["p"] = p
    result_b["c"] = c
    root_b.destroy()
    root.quit()


root = tk.Tk()
root.withdraw()
root.after(150, run_b)
root.mainloop()
print(f"  probe={result_b.get('p')} kbd_calls+{result_b.get('c')}")

print()
print("== C. 有 tkinter 窗口但不跑 mainloop(手动 update) ==")
root2 = tk.Tk()
w = tk.Toplevel(root2)
w.geometry("300x150+60+60")
w.deiconify()
w.lift()
w.focus_force()
for _ in range(10):
    root2.update()
    time.sleep(0.03)
pc, cc = measure("WithTkNoLoop")
print(f"  probe={pc} kbd_calls+{cc}")
root2.destroy()

print()
print("== 对照表 ==")
print(f"  A 无 tkinter          : probe={pa}  calls+{ca}")
print(f"  B tkinter + mainloop  : probe={result_b.get('p')}  "
      f"calls+{result_b.get('c')}")
print(f"  C tkinter 无 mainloop : probe={pc}  calls+{cc}")
print()
if result_b.get("c", 0) == 0 and ca > 0:
    print("  ★★★ 结论: **tkinter mainloop 会导致钩子回调失效**")
    print("     原因: mainloop 长期持有 GIL, 钩子线程拿不到 CPU ->")
    print("     回调超过 LowLevelHooksTimeout(300ms) -> 系统静默摘除。")
    print("     ★ 这解释了用户现场: GUI 版(tkinter mainloop)失败,")
    print("       而 console 版/诊断脚本(无 mainloop)成功!")
else:
    print("  tkinter mainloop 不是原因(或影响不稳定)")
