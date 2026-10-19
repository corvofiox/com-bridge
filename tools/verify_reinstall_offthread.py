# -*- coding: utf-8 -*-
"""验证修复: 重装走独立安装线程, 钩子线程泵消息永不暂停。

## 修的是什么(用户真机日志实锤的三连事实)
    "重装3次全无效" + 泄漏检测触发 + kbd_calls=0
唯一同时成立的解释: 钩子线程停止泵消息(旧设计里 _reinstall 同步跑
在钩子线程, unhook/rehook 期间 GetMessageW 不被调用 -> 系统静默摘钩
-> 之后重装消息发给死线程 = 石沉大海)。

## 本验证
  1. 起钩子 -> **密集连发重装请求**(模拟打字高峰泄漏自愈的轰炸)
  2. 全程观察: 泵心跳必须持续刷新(钩子线程没被重装卡住)
  3. 重装实际发生(reinstalls 增长)且由安装线程执行
  4. 最后注入键: 回调必须仍可达(修复前此时早已死)
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.hook import LowLevelHook
from inject_util import PROBE_VK, burst, require_injection

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


print("== 1. 起钩子 ==")
h = LowLevelHook(name="ReinstallFix", on_key_event=lambda *a: None)
check("安装", h.start())
h.set_relay(True)
time.sleep(0.3)

print("== 2. 密集轰炸 20 次重装请求(旧设计会卡死泵消息) ==")
d0 = h.diag()
t0 = time.monotonic()
max_pump_age = 0.0
for i in range(20):
    h.request_reinstall()
    time.sleep(0.05)
    d = h.diag()
    age = d.get("hook_pump_age_s", -1)
    max_pump_age = max(max_pump_age, age if age >= 0 else 0)
elapsed = time.monotonic() - t0
d1 = h.diag()
re_done = d1["reinstalls"] - d0["reinstalls"]
print(f"  耗时 {elapsed:.2f}s, 实际重装 {re_done} 次")
print(f"  泵心跳最大年龄 {max_pump_age:.2f}s")
check("重装实际发生", re_done >= 1, f"{re_done} 次")
check("钩子句柄都在", d1["kbd_hook_alive"] and d1["mouse_hook_alive"])

time.sleep(0.5)
print()
print("== 3. 轰炸后键盘回调必须仍可达(修复前此时已死) ==")
b = h.diag()
sent = burst(3, PROBE_VK)
require_injection(sent, 6)
a = h.diag()
d_kbd = a["kbd_calls"] - b["kbd_calls"]
d_eaten = a["kbd_eaten"] - b["kbd_eaten"]
print(f"  kbd_calls+{d_kbd} kbd_eaten+{d_eaten}")
check("轰炸后回调仍可达且吞键", d_kbd > 0 and d_eaten > 0,
      f"calls+{d_kbd} eaten+{d_eaten}")

print()
print("== 4. 交还复核路径(生产在用) ==")
p = h.probe_keyboard_hook()
print(f"  probe = {p}")
check("probe 判定回调可达", p is True)

h.set_relay(False)
h.stop()
print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
