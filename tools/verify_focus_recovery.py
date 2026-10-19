# -*- coding: utf-8 -*-
"""★★★ 验证回归修复: 启动自检失败后, 焦点交出时必须自动恢复键盘。

## 上一版的回归(用户实测)
"鼠标进入副机后, 键盘不会在副机输入, 键盘跨屏完全失效"

原因: GUI 启动时窗口**有焦点** -> 自检失败 -> `_kbd_hook_verified=False`
-> 键盘转发被**永久**禁用。之后 GUI 放弃焦点、钩子恢复了, 但没有任何
代码把结论更新回来 -> 键盘一直不可用。

## 本工具验证三件事
1. 窗口有焦点时: 自检失败, 键盘转发被禁用(正确, 避免两台同时生效)
2. 放弃焦点后: 复检自动恢复, 键盘转发重新启用(修复点)
3. 穿越开始时强制复检(不依赖 TTL 过期)
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


print("== 1. 起 Server + 真窗口, 窗口获得焦点(模拟点'启动') ==")
srv = Server(name="regression", side="right", accept_synthetic_input=True)
srv.start()
time.sleep(0.8)

root = tk.Tk()
root.geometry("400x200+80+80")
root.update()
root.focus_force()
root.update()
time.sleep(0.4)

srv._edge._outside = True
srv._edge._streak = 2
srv._acquire_control()          # 内部会强制复检
time.sleep(0.5)

print(f"  窗口有焦点: _kbd_hook_verified={srv._kbd_hook_verified}")
ready_focused = srv._keyboard_relay_ready()
print(f"  _keyboard_relay_ready()={ready_focused}")
check("窗口有焦点时禁用键盘转发(避免两台同时生效)",
      ready_focused is False, f"{ready_focused}")

# 确认确实收不到键
b = srv._hook.diag()
burst(2, PROBE_VK)
time.sleep(0.4)
a = srv._hook.diag()
d_focused = a["kbd_calls"] - b["kbd_calls"]
print(f"  实测 kbd_calls+{d_focused}")
check("有焦点时回调确实不被调用", d_focused == 0, f"+{d_focused}")

print()
print("== 2. 放弃焦点(GUI 的 _release_focus_for_hook 等价操作) ==")
user32.SetForegroundWindow(user32.GetShellWindow())
root.focus_set()
root.update()
time.sleep(0.8)

# 等 TTL 过期, 或直接强制复检
print("  等待 TTL(1.0s) 后复检...")
time.sleep(1.2)
ready_after = srv._keyboard_relay_ready()
print(f"  _kbd_hook_verified={srv._kbd_hook_verified} "
      f"ready={ready_after}")
check("★ 放弃焦点后自动恢复键盘转发(修复回归)", ready_after is True,
      f"{ready_after}")

# 确认真的能吞键了
b2 = srv._hook.diag()
burst(3, PROBE_VK)
time.sleep(0.5)
a2 = srv._hook.diag()
d_after = a2["kbd_calls"] - b2["kbd_calls"]
print(f"  实测 kbd_calls+{d_after}")
check("恢复后回调确实被调用", d_after > 0, f"+{d_after}")

print()
print("== 3. 穿越开始时的强制复检(不依赖 TTL) ==")
# 故意把结论设成失败, 再穿越, 看是否被复检纠正
srv._kbd_hook_verified = False
srv._kbd_verify_mono = time.monotonic()      # 让 TTL 还没过期
srv._edge._outside = False
srv._release_control()
time.sleep(0.3)
srv._edge._outside = True
srv._edge._streak = 2
srv._acquire_control()                        # 应强制复检
time.sleep(0.6)
print(f"  穿越后 _kbd_hook_verified={srv._kbd_hook_verified}")
check("穿越开始时强制复检(不等 TTL)", srv._kbd_hook_verified is True,
      f"{srv._kbd_hook_verified}")

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
