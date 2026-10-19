# -*- coding: utf-8 -*-
"""★★★ 验证修复: 窗口获得焦点 -> 放弃焦点 -> 钩子恢复。

## 已确认根因(tools/exp_window_vs_hook_precise.py, 100% 复现)
    无窗口            -> kbd_calls+8  ✅
    窗口显示(无焦点)  -> kbd_calls+8  ✅
    窗口获得焦点      -> kbd_calls+0  ❌ 回调永久失效
    焦点转移          -> kbd_calls+6  ✅ 立刻恢复
    有焦点时重装钩子  -> kbd_calls+0  ❌ 重装无效

## 本工具验证生产修复
1. 起 Server + 真窗口, 让窗口获得焦点(模拟用户点"启动")
2. 调用 GUI 的 _release_focus_for_hook 逻辑(交焦点给 shell)
3. 测 kbd_calls -> 必须 > 0
"""
from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk

sys.path.insert(0, ".")

from com_bridge.server import Server
from inject_util import PROBE_VK, burst, require_injection
from com_bridge.input import user32

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def measure(srv, tag):
    h = srv._hook
    b = h.diag()
    sent = burst(3, PROBE_VK)
    require_injection(sent, 6)
    time.sleep(0.55)
    a = h.diag()
    d = a["kbd_calls"] - b["kbd_calls"]
    fg = user32.GetForegroundWindow()
    print(f"  [{tag}] kbd_calls+{d} fg={fg}")
    return d


print("== 1. 起 Server + 真窗口, 窗口获得焦点(模拟点'启动') ==")
srv = Server(name="fixverify", side="right", accept_synthetic_input=True)
srv.start()
time.sleep(0.8)
srv._edge._outside = True
srv._edge._streak = 2
srv._acquire_control()
time.sleep(0.4)

root = tk.Tk()
root.geometry("400x200+80+80")
root.update()
root.focus_force()
root.update()
time.sleep(0.5)
hwnd_top = user32.GetAncestor(int(root.winfo_id()), 2)
print(f"  窗口 hwnd={hwnd_top} 前台={user32.GetForegroundWindow()}")

d_before = measure(srv, "焦点在窗口")
check("复现: 窗口有焦点时钩子失效", d_before == 0, f"+{d_before}")

print()
print("== 2. 应用修复: 交焦点给 shell(与 GUI 的 _release_focus_for_hook 相同) ==")
shell = user32.GetShellWindow()
print(f"  shell hwnd={shell}")
user32.SetForegroundWindow(shell)
root.focus_set()          # tkinter 侧放弃
root.update()
time.sleep(0.6)
print(f"  当前前台={user32.GetForegroundWindow()}")

d_after = measure(srv, "焦点已放弃")
check("修复后钩子恢复(可吞键)", d_after > 0, f"+{d_after}")

print()
print("== 3. 再次抢回焦点, 验证会重新失效(证明因果) ==")
root.focus_force()
root.update()
time.sleep(0.5)
d_again = measure(srv, "再次抢焦点")
check("抢回焦点后重新失效(因果确认)", d_again == 0, f"+{d_again}")

print()
print("== 4. 再放弃焦点, 确认可再次恢复 ==")
user32.SetForegroundWindow(user32.GetShellWindow())
root.focus_set()
root.update()
time.sleep(0.6)
d_final = measure(srv, "再次放弃")
check("再次放弃焦点后恢复", d_final > 0, f"+{d_final}")

try:
    root.destroy()
except Exception:  # noqa: BLE001
    pass
srv._edge._outside = False
srv._release_control()
srv.stop()

print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
