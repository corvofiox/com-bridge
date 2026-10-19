# -*- coding: utf-8 -*-
"""★★★ 验证最小化修复(生产逻辑完整复刻)。

## 用户报告
"当我点击最小化时, 程序 GUI 完全从桌面消失, 任务栏中也消失了,
 使得窗口无法再恢复"

## 修复(两处)
1. **移除 WS_EX_NOACTIVATE** —— 实测它让窗口永远无法激活
   (最小化后 ShowWindow(RESTORE) 能清 iconic, 但拿不到前台)。
2. **焦点监控加两个跳过条件**:
   a) 窗口最小化/不可见 -> 不干预(此时本就不是前台, 钩子不受影响)
   b) 用户刚操作过窗口 -> 8 秒宽限期(否则点开界面立刻被夺走焦点)

## 本工具复刻**修复后**的生产逻辑并验证完整循环
"""
from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk

sys.path.insert(0, ".")

user32 = ctypes.windll.user32
SW_RESTORE = 9
GRACE_S = 8.0
FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def top_hwnd(root: tk.Tk) -> int:
    return user32.GetAncestor(int(root.winfo_id()), 2) or int(root.winfo_id())


root = tk.Tk()
root.geometry("360x200+220+220")
root.update()
h = top_hwnd(root)

state = {"kicks": 0, "grace_until": 0.0, "stop": False}


def watch() -> None:
    """生产逻辑: 最小化/宽限期内不干预。"""
    if state["stop"]:
        return
    try:
        if not user32.IsWindowVisible(h) or user32.IsIconic(h):
            return
        if time.monotonic() < state["grace_until"]:
            return
        if user32.GetForegroundWindow() == h:
            shell = user32.GetShellWindow()
            if shell:
                user32.SetForegroundWindow(shell)
            state["kicks"] += 1
    except Exception:  # noqa: BLE001
        pass
    finally:
        root.after(150, watch)


def user_activity() -> None:
    """生产逻辑: 用户操作 -> 给宽限期。"""
    try:
        if root.winfo_viewable() and root.state() != "iconic":
            state["grace_until"] = time.monotonic() + GRACE_S
    except Exception:  # noqa: BLE001
        pass


for seq in ("<Button-1>", "<Key>", "<FocusIn>", "<Map>"):
    try:
        root.bind_all(seq, lambda _e=None: user_activity(), add="+")
    except Exception:  # noqa: BLE001
        pass

root.after(150, watch)
for _ in range(8):
    root.update()
    time.sleep(0.05)

check("初始窗口可见", bool(user32.IsWindowVisible(h)))

print()
print("  -- 最小化 --")
root.iconify()
kicks_at_min = state["kicks"]
for _ in range(20):
    root.update()
    time.sleep(0.12)
check("最小化后是 iconic", bool(user32.IsIconic(h)))
check("最小化期间监控不干预", state["kicks"] == kicks_at_min,
      f"kicks {kicks_at_min}->{state['kicks']}")

print()
print("  -- 用户点任务栏恢复(模拟: 触发 Map + RESTORE) --")
user32.ShowWindow(h, SW_RESTORE)
root.update()
# 用户操作 -> 触发宽限期
state["grace_until"] = time.monotonic() + GRACE_S
user32.SetForegroundWindow(h)
for _ in range(20):          # 3 秒(宽限期内)
    root.update()
    time.sleep(0.15)

check("恢复后不再最小化", not bool(user32.IsIconic(h)))
check("恢复后窗口可见", bool(user32.IsWindowVisible(h)))
check("★ 恢复后窗口可被激活(关键修复点)",
      user32.GetForegroundWindow() == h,
      f"fg={user32.GetForegroundWindow()} h={h}")
check("宽限期内监控未夺焦点", state["kicks"] == kicks_at_min,
      f"kicks={state['kicks']}")

print()
print("  -- 等宽限期结束, 监控应恢复保护 --")
state["grace_until"] = 0.0        # 直接结束宽限, 不等 8 秒
user32.SetForegroundWindow(h)
for _ in range(20):
    root.update()
    time.sleep(0.12)
check("★ 宽限期后监控恢复交出焦点(键盘保护未失效)",
      state["kicks"] > kicks_at_min,
      f"kicks {kicks_at_min}->{state['kicks']}")

state["stop"] = True
root.destroy()

print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
