# -*- coding: utf-8 -*-
"""★★★ 真实物理按键测试: 不用 SendInput 注入, 让**人**按键。

## 为什么必须这样
之前所有测试都用 SendInput 注入。但**注入事件带 LLKHF_INJECTED 标志**,
而物理按键没有。若系统/钩子对两者处理不同, 注入测试就会得出错误结论。

## 本工具
1. 起 Server(真实生产配置)
2. 进入穿越态(relay=True)
3. 提示用户**在物理键盘上按几个键**(5 秒窗口)
4. 报告: kbd_calls 增量、kbd_eaten 增量、RawInput key 增量

判读:
  * kbd_calls 增加 -> 回调正常, 键被吞(正常)
  * kbd_calls 不增 -> 物理键到不了我们(环境问题)
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.server import Server

print("== 起 Server(生产配置) ==")
srv = Server(name="real", side="right")
srv.start()
time.sleep(1.0)
hook = srv._hook
print(f"  hook.installed={hook.installed}")

print()
print("== 进入穿越态(relay=True) ==")
srv._edge._outside = True
srv._edge._streak = 2
srv._acquire_control()
time.sleep(0.5)
d0 = hook.diag()
r0 = srv._listener.diag()
print(f"  relay={d0['relay']}")

print()
print("=" * 60)
print("  请在**物理键盘**上按 5 个键(任意键, 5 秒内)")
print("=" * 60)
for i in range(5, 0, -1):
    print(f"  倒计时 {i}...", flush=True)
    time.sleep(1.0)

d1 = hook.diag()
r1 = srv._listener.diag()

print()
print("== 结果 ==")
print(f"  kbd_calls : +{d1['kbd_calls'] - d0['kbd_calls']}")
print(f"  kbd_eaten : +{d1['kbd_eaten'] - d0['kbd_eaten']}")
print(f"  kbd_passed: +{d1['kbd_passed'] - d0['kbd_passed']}")
print(f"  RawInput key: +{r1['key'] - r0['key']}")
print(f"  泵心跳: {d1.get('hook_pump_age_s', -1):.2f}s")

print()
print("== 判读 ==")
calls = d1['kbd_calls'] - d0['kbd_calls']
eaten = d1['kbd_eaten'] - d0['kbd_eaten']
raw = r1['key'] - r0['key']
if calls > 0 and eaten > 0 and raw == 0:
    print("  ✓ 正常: 物理键被回调看到并吞掉, 没有泄漏到 RawInput")
elif calls == 0:
    print("  ★★ 复现: 物理键**到不了我们的回调**(kbd_calls=0)")
    print("     这是用户症状。可能原因: 别的进程先吞 / 权限 / 输入法")
    if raw > 0:
        print(f"     但 RawInput 看到了 {raw} 个键盘事件 -> 键流进了系统")
elif eaten == 0:
    print("  ★ 回调被调用但没吞键(relay 判定问题)")
elif raw > 0:
    print("  ★ 钩子吞了键但 RawInput 也收到了 -> 双重路径")

srv._edge._outside = False
srv._release_control()
time.sleep(0.3)
srv.stop()
