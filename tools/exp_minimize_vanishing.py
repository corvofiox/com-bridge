# -*- coding: utf-8 -*-
"""★★★ 复现: 最小化后窗口从桌面与任务栏消失、无法恢复。

## 用户报告
"当我点击最小化时, 程序 GUI 完全从桌面消失, 任务栏中也消失了,
 使得窗口无法再恢复"

## 两个嫌疑
1. `WS_EX_NOACTIVATE`(0x08000000) 改变了窗口的激活语义。
   正常窗口最小化后, 点击任务栏按钮会 `SetForegroundWindow+restore`;
   带 NOACTIVATE 的窗口可能**不被任务栏当作可激活窗口**,
   于是点任务栏也恢复不出来。
2. `_focus_watch_tick` 每 700ms 检查"自己是否前台", 若是就把前台
   交给 shell。窗口刚从最小化恢复时正是"前台", 会被立刻交出去 ——
   表现为"点了也弹不出来/闪一下又没了"。

## 本工具: 逐项验证
A. 普通窗口 -> iconify -> 能否 deiconify 恢复
B. 带 NOACTIVATE 的窗口 -> iconify -> 能否 deiconify 恢复
C. 带 NOACTIVATE + 焦点监控 -> 最小化后监控是否捣乱

★ 全部用 tk 的 iconify/deiconify + Win32 状态查询, 不需要人工点击。
"""
from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk

sys.path.insert(0, ".")

user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOW = 5


def win_state(hwnd: int) -> str:
    """返回窗口可视状态描述。"""
    visible = bool(user32.IsWindowVisible(hwnd))
    iconic = bool(user32.IsIconic(hwnd))
    # exstyle 里是否还带 WS_EX_TOOLWINDOW(会隐藏任务栏按钮)
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    tool = bool(ex & 0x00000080)
    return (f"visible={visible} iconic={iconic} toolwindow={tool} "
            f"ex={hex(ex)}")


def top_hwnd(root: tk.Tk) -> int:
    return user32.GetAncestor(int(root.winfo_id()), 2) or int(root.winfo_id())


print("== A. 普通窗口(不加 NOACTIVATE) ==")
r1 = tk.Tk()
r1.geometry("320x160+200+200")
r1.update()
h1 = top_hwnd(r1)
print(f"  初始: {win_state(h1)}")
r1.iconify()
r1.update()
time.sleep(0.4)
print(f"  最小化后: {win_state(h1)}")
r1.deiconify()
r1.update()
time.sleep(0.4)
print(f"  恢复后: {win_state(h1)}")
r1.destroy()

print()
print("== B. 带 WS_EX_NOACTIVATE 的窗口 ==")
r2 = tk.Tk()
r2.geometry("320x160+200+200")
r2.update()
h2 = top_hwnd(r2)
cur = user32.GetWindowLongW(h2, GWL_EXSTYLE)
user32.SetWindowLongW(h2, GWL_EXSTYLE, cur | WS_EX_NOACTIVATE)
r2.update()
print(f"  初始: {win_state(h2)}")
r2.iconify()
r2.update()
time.sleep(0.4)
print(f"  最小化后: {win_state(h2)}")

# 关键: 用 Win32 尝试恢复(模拟用户点任务栏)
user32.ShowWindow(h2, SW_RESTORE)
r2.update()
time.sleep(0.4)
print(f"  ShowWindow(RESTORE) 后: {win_state(h2)}")
st = win_state(h2)
if "visible=False" in st or "iconic=True" in st:
    print("  ★★ 复现: 带 NOACTIVATE 的窗口无法通过 Win32 恢复!")

# 再试 tk 的 deiconify
r2.deiconify()
r2.update()
time.sleep(0.4)
print(f"  deiconify 后: {win_state(h2)}")
r2.destroy()

print()
print("== 结论 ==")
print("  若 B 组最小化后无法恢复, 而 A 组可以 -> NOACTIVATE 是元凶")
