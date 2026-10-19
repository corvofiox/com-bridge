# -*- coding: utf-8 -*-
"""★ 验证持续监控方案: 窗口一旦成为前台, 立刻交出去。

## 为什么 NOACTIVATE 不够(实测)
设置 WS_EX_NOACTIVATE 后, Tk 的 `focus_force()` 仍能抢到前台
(exstyle 0x8000100 已设, 但 fg 仍是我们的窗口)。
=> 不能只靠窗口样式, 必须有**主动监控 + 强制交出**。

## 本工具验证监控逻辑(与 GUI 的 _focus_watch_tick 相同)
每 200ms 检查一次: 自己是前台就 SetForegroundWindow(shell)。
观察钩子是否能在监控下恢复。
"""
from __future__ import annotations

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


def pump(seconds: float) -> None:
    """在等待期间**持续跑 tkinter 事件循环**(否则 root.after 的监控不执行)。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def measure(srv, tag):
    h = srv._hook
    b = h.diag()
    sent = burst(3, PROBE_VK)
    require_injection(sent, 6)
    pump(0.55)          # ★ 用 pump 而不是 sleep, 让监控继续工作
    a = h.diag()
    d = a["kbd_calls"] - b["kbd_calls"]
    print(f"  [{tag}] kbd_calls+{d} fg={user32.GetForegroundWindow()}")
    return d


print("== 起 Server + 窗口 ==")
srv = Server(name="watch", side="right", accept_synthetic_input=True)
srv.start()
time.sleep(0.8)
srv._edge._outside = True
srv._edge._streak = 2
srv._acquire_control()
time.sleep(0.4)

root = tk.Tk()
root.geometry("400x200+80+80")
root.update()
hwnd = int(root.winfo_id())
top = user32.GetAncestor(hwnd, 2) or hwnd

print()
print("== 1. 无监控时: 窗口抢焦点 -> 钩子失效(基线) ==")
root.focus_force()
root.update()
time.sleep(0.5)
d0 = measure(srv, "无监控")
check("基线: 有焦点时失效", d0 == 0, f"+{d0}")

print()
print("== 2. 启动监控(与 _focus_watch_tick 相同逻辑, 200ms 周期) ==")
state = {"kicks": 0, "stop": False}


def watch():
    if state["stop"]:
        return
    try:
        fg = user32.GetForegroundWindow()
        if fg and fg == top:
            shell = user32.GetShellWindow()
            if shell:
                user32.SetForegroundWindow(shell)
            state["kicks"] += 1
    except Exception:  # noqa: BLE001
        pass
    try:
        root.after(200, watch)
    except Exception:  # noqa: BLE001
        pass


root.after(200, watch)
pump(1.2)
print(f"  监控已交出焦点 {state['kicks']} 次, "
      f"当前 fg={user32.GetForegroundWindow()} top={top}")
check("监控把焦点交出去了", user32.GetForegroundWindow() != top)

d1 = measure(srv, "监控中")
check("★ 监控下钩子可用(键盘跨屏正常)", d1 > 0, f"+{d1}")

print()
print("== 3. 用户反复点窗口(抢焦点), 监控应持续纠正 ==")
ok = True
for i in range(3):
    root.focus_force()
    pump(0.7)            # 给监控时间纠正
    d = measure(srv, f"第{i+1}次抢焦点后")
    if d <= 0:
        ok = False
check("反复点击窗口也不会破坏键盘跨屏", ok, f"kicks={state['kicks']}")

state["stop"] = True
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
