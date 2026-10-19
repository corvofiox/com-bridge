# -*- coding: utf-8 -*-
"""验证: SetForegroundWindow 在测试环境被拒绝, 但用 AttachThreadInput 可成功。

## 背景
verify_minimize_fix 里"恢复后窗口可被激活"失败: fg 仍是 shell(65928),
我们的窗口(2228954)拿不到前台。
这可能是**测试进程不是前台进程**导致系统拒绝(Windows 的
SetForegroundWindow 有严格的前台锁规则), 而不是产品缺陷。

## 但这对用户也可能成立!
用户点任务栏恢复窗口时, 如果系统拒绝激活, 表现就是"点了没反应"。
所以产品代码里**恢复窗口时也必须用 AttachThreadInput 强制**,
不能只依赖 tk 的 deiconify 或裸 SetForegroundWindow。

## 本工具
对比三种恢复方式在同一环境下的成功率。
"""
from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk
from ctypes import wintypes

sys.path.insert(0, ".")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
SW_RESTORE = 9


def top_hwnd(root: tk.Tk) -> int:
    return user32.GetAncestor(int(root.winfo_id()), 2) or int(root.winfo_id())


def force_foreground(hwnd: int) -> bool:
    """用 AttachThreadInput 强制把窗口设为前台(生产可用的做法)。"""
    try:
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        that = wintypes.DWORD()
        user32.GetWindowThreadProcessId(fg, ctypes.byref(that))
        this = kernel32.GetCurrentThreadId()
        if that.value and that.value != this:
            user32.AttachThreadInput(that.value, this, True)
            try:
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                user32.AttachThreadInput(that.value, this, False)
        else:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        return user32.GetForegroundWindow() == hwnd
    except Exception:  # noqa: BLE001
        return False


print("== 三种恢复方式对比 ==")

for label in ("裸 SetForegroundWindow", "AttachThreadInput 强制"):
    root = tk.Tk()
    root.geometry("300x150+240+240")
    root.update()
    h = top_hwnd(root)
    root.iconify()
    root.update()
    time.sleep(0.4)

    if label.startswith("裸"):
        user32.ShowWindow(h, SW_RESTORE)
        user32.SetForegroundWindow(h)
        time.sleep(0.3)
        ok = user32.GetForegroundWindow() == h
    else:
        ok = force_foreground(h)

    print(f"  {label:26} -> 激活成功={ok} "
          f"(fg={user32.GetForegroundWindow()} h={h})")
    root.destroy()
    time.sleep(0.3)

print()
print("== 判读 ==")
print("  若 AttachThreadInput 能成功而裸调用失败 -> 生产代码恢复窗口时")
print("  必须用前者, 否则用户点任务栏也会'没反应'。")
