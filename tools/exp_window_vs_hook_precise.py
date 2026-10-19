# -*- coding: utf-8 -*-
"""★★★ 已稳定复现: 有前台窗口时 kbd_calls=0, 无窗口时 +10。
   现在精确定位是窗口的哪个属性导致钩子回调失效。

## 对照设计
  A. 无窗口(基线)
  B. 有窗口但不显示(withdraw)
  C. 有窗口显示但不 focus_force
  D. 有窗口 + focus_force  <- 疑似根因
  E. 有窗口 + focus_force + 主线程跑 mainloop

每组都测 Server 钩子的 kbd_calls。
"""
from __future__ import annotations

import sys
import time
import tkinter as tk

sys.path.insert(0, ".")

from com_bridge.server import Server
from inject_util import PROBE_VK, burst, require_injection


def measure_server(tag: str) -> int:
    """起 Server -> 穿越 -> 注入 -> 返回 kbd_calls 增量。"""
    srv = Server(name=f"s-{tag}", side="right", accept_synthetic_input=True)
    srv.start()
    time.sleep(0.8)
    h = srv._hook
    srv._edge._outside = True
    srv._edge._streak = 2
    srv._acquire_control()
    time.sleep(0.5)
    b = h.diag()
    sent = burst(4, PROBE_VK)
    try:
        require_injection(sent, 8)
    except Exception:  # noqa: BLE001
        srv._edge._outside = False
        srv._release_control()
        srv.stop()
        return -1
    time.sleep(0.6)
    a = h.diag()
    d = a["kbd_calls"] - b["kbd_calls"]
    print(f"  [{tag}] kbd_calls+{d} 泵={a.get('hook_pump_age_s', -1):.2f}s")
    srv._edge._outside = False
    srv._release_control()
    time.sleep(0.3)
    srv.stop()
    time.sleep(0.5)
    return d


print("== A. 无窗口(基线) ==")
a = measure_server("nowin")

print()
print("== B. 有窗口但 withdraw(不显示) ==")
root = tk.Tk()
root.withdraw()
b = measure_server("withdrawn")

print()
print("== C. 窗口显示但**不** focus_force ==")
root.deiconify()
root.geometry("400x200+80+80")
root.update()
c = measure_server("shown-nofocus")

print()
print("== D. 窗口显示 + focus_force(疑似根因) ==")
root.focus_force()
root.update()
time.sleep(0.3)
d = measure_server("focused")

print()
print("== E. 解除焦点后再测 ==")
try:
    root.withdraw()
    root.update()
    time.sleep(0.3)
except Exception:  # noqa: BLE001
    pass
e = measure_server("after-withdraw")

try:
    root.destroy()
except Exception:  # noqa: BLE001
    pass

print()
print("== 对照表 ==")
print(f"  A 无窗口              : +{a}")
print(f"  B 窗口 withdraw       : +{b}")
print(f"  C 窗口显示(无焦点)     : +{c}")
print(f"  D 窗口 + focus_force  : +{d}   <- 疑似 0")
print(f"  E 再次 withdraw       : +{e}")
print()
if d == 0 and c > 0:
    print("  ★★★ 确认: **tkinter 窗口获得焦点(focus_force)后, 低级键盘钩子失效**")
elif d == 0 and c == 0:
    print("  ★★ 窗口显示本身就导致失效(与焦点无关)")
else:
    print("  结果不稳定, 需要重复验证")
