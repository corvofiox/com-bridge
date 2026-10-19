# -*- coding: utf-8 -*-
"""★★★ 验证: _focus_watch_tick 是否会在最小化/恢复时捣乱?

## 嫌疑逻辑(生产代码)
每 700ms:
    if GetForegroundWindow() == our_hwnd:
        SetForegroundWindow(GetShellWindow())     # 把前台交给 shell

## 最小化场景
窗口最小化后, Windows 的"前台窗口"通常仍指向它(只是 iconic)。
若监控在此时把前台交给 shell, 当用户点任务栏想恢复时,
窗口刚从最小化恢复就又被"交出去" —— 表现为**点了没用/闪一下消失**。
更糟: 某些情况下 SetForegroundWindow(shell) 会让最小化窗口
彻底退出前台序列, 任务栏按钮失去恢复能力。

## 本工具
模拟生产监控逻辑 + 最小化, 观察能否恢复。
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
SW_RESTORE = 9


def top_hwnd(root: tk.Tk) -> int:
    return user32.GetAncestor(int(root.winfo_id()), 2) or int(root.winfo_id())


def state(h: int) -> str:
    return (f"visible={bool(user32.IsWindowVisible(h))} "
            f"iconic={bool(user32.IsIconic(h))} "
            f"fg={user32.GetForegroundWindow() == h}")


print("== 带 NOACTIVATE + 焦点监控, 最小化 -> 恢复 ==")
root = tk.Tk()
root.geometry("320x160+200+200")
root.update()
h = top_hwnd(root)
cur = user32.GetWindowLongW(h, GWL_EXSTYLE)
user32.SetWindowLongW(h, GWL_EXSTYLE, cur | WS_EX_NOACTIVATE)

kicks = {"n": 0}
stop = {"v": False}


def watch():
    """复刻生产逻辑。"""
    if stop["v"]:
        return
    try:
        fg = user32.GetForegroundWindow()
        if fg and fg == h:
            user32.SetForegroundWindow(user32.GetShellWindow())
            kicks["n"] += 1
    except Exception:  # noqa: BLE001
        pass
    root.after(100, watch)          # 加速: 生产是 700ms, 这里 100ms


root.after(100, watch)
root.update()
time.sleep(0.3)
print(f"  监控启动后: {state(h)}  kicks={kicks['n']}")

print()
print("  -- 最小化 --")
root.iconify()
for _ in range(15):                 # 让监控跑 1.5 秒
    root.update()
    time.sleep(0.1)
print(f"  最小化后: {state(h)}  kicks={kicks['n']}")

print()
print("  -- 模拟用户点任务栏恢复(ShowWindow RESTORE) --")
user32.ShowWindow(h, SW_RESTORE)
for _ in range(15):
    root.update()
    time.sleep(0.1)
print(f"  恢复尝试后: {state(h)}  kicks={kicks['n']}")

print()
print("  -- 再用 tk deiconify --")
try:
    root.deiconify()
except Exception as exc:  # noqa: BLE001
    print(f"    deiconify 异常: {exc}")
for _ in range(10):
    root.update()
    time.sleep(0.1)
print(f"  deiconify 后: {state(h)}  kicks={kicks['n']}")

stop["v"] = True
root.destroy()

print()
print("== 判读 ==")
print("  若恢复后 iconic=True 或 visible=False -> 监控确实干扰了恢复")
print("  若 kicks 在最小化期间持续增长 -> 监控在最小化时仍在抢前台")
