# -*- coding: utf-8 -*-
"""决定性实验: relay=True 时低级键盘钩子是否真的吞掉按键。

## 设计(修正版)

上一版用"自己的窗口收 WM_KEYDOWN"判定, 失败原因是 SendInput 注入的键
不会路由到**非前台线程**的窗口(前台窗口的输入队列属于另一个线程)。
本版改用**系统级状态查询**, 与窗口/前台无关:

  * 装钩子前: 注入无标记按键 -> `GetAsyncKeyState(vk)` 应显示按下。
  * relay=True: 钩子返回 1 吞掉 -> `GetAsyncKeyState` 应**看不到**按下
    (事件被系统丢弃, 从未进入输入队列)。
  * 同时看钩子统计, 区分"回调没被调用"与"调用了但放行"。

⚠ 本脚本会在极短时间内注入并立即抬起几个键(0x41/0x42/0x43/0x44),
  它们都是普通字母键, 不会触发系统级动作; 但会**短暂**进入输入状态。
  请在**没有正在输入的文档**时运行。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

from com_bridge.hook import LowLevelHook
from com_bridge.input import INPUT_KEYBOARD, KEYEVENTF_KEYUP, user32

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


from inject_util import key as _inj_key

# GetAsyncKeyState 的重声明(注入本身已交给 inject_util)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

FAILED = []


def inject_key(vk: int, up: bool = False, extra: int | None = None) -> int:
    """★ 走 inject_util —— 本文件曾自己拼只含 "ki" 的 union(32 字节,
    应为 40), 使 SendInput 恒返回 0 且不抛异常, 注入从未发生。
    "判据基线不成立"的旧结论有一部分正是由此而来。"""
    return _inj_key(vk, up, extra, interval=0)


def key_down_seen(vk: int, tries: int = 6) -> bool:
    """注入按下后, 轮询 GetAsyncKeyState 看系统是否认为它被按下。

    注入与查询之间有竞态, 所以**按下后不立刻抬起**, 而是轮询几毫秒;
    查到就立刻抬起并返回 True。
    """
    inject_key(vk)
    seen = False
    for _ in range(tries):
        time.sleep(0.02)
        if user32.GetAsyncKeyState(vk) & 0x8000:
            seen = True
            break
    inject_key(vk, up=True)
    time.sleep(0.05)
    return seen


print("== 0. 环境 ==")
print(f"  (信息) 桌面 {user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}")

print("== 1. 基线: 无钩子时注入无标记按键, 系统应看到按下 ==")
base_seen = key_down_seen(0x41)     # 'A'
check("无钩子时按键进入输入状态(基线成立)", base_seen,
      "若为 False: 注入本身就不生效, 后续判据无意义")

if not base_seen:
    print("\n!! 基线不成立 —— 注入按键无法被系统看到(可能被其它 KVM 拦截),")
    print("   无法在本机判定钩子是否吞键。请退出 Deskflow/Synergy 后重跑。")
    sys.exit(2)

print("== 2. 关键: relay=True 时无标记按键**不应**进入输入状态 ==")
hook = LowLevelHook(on_key_event=None)
produced = []
hook._on_key_event = lambda *a: produced.append(a)
ok = hook.start()
check("钩子安装成功", ok, f"last_error={hook.last_error}")
hook.set_relay(True)
time.sleep(0.2)

before = dict(hook.stats)
eaten_seen = key_down_seen(0x42)    # 'B'
after = dict(hook.stats)
eaten = after["kbd_eaten"] - before["kbd_eaten"]
passed = after["kbd_passed"] - before["kbd_passed"]
print(f"  (信息) 钩子统计: kbd_eaten +{eaten}, kbd_passed +{passed}, "
      f"系统看到按下={eaten_seen}, 自产={len(produced)}")

check("钩子回调被调用", eaten + passed > 0,
      f"eaten={eaten} passed={passed}(全 0 = 钩子未生效/被摘除)")
check("按键被吞(系统未看到按下)", not eaten_seen,
      "系统看到了按下 = 按键泄漏到主机应用(输入法被唤起的原因)")
check("被吞的键已自产转发", len(produced) > 0, f"自产 {len(produced)} 条")

print("== 3. relay=False 时按键应正常进入输入状态 ==")
hook.set_relay(False)
time.sleep(0.2)
check("relay=False 时按键进入输入状态(本地可用)",
      key_down_seen(0x43))          # 'C'

print("== 4. 带 TAG 的注入即使在 relay=True 也应放行 ==")
hook.set_relay(True)
time.sleep(0.2)
inject_key(0x44, extra=0x43425247)
seen_tagged = False
for _ in range(6):
    time.sleep(0.02)
    if user32.GetAsyncKeyState(0x44) & 0x8000:
        seen_tagged = True
        break
inject_key(0x44, up=True, extra=0x43425247)
time.sleep(0.05)
check("带标记注入被放行", seen_tagged,
      "我们自己注入的回程键必须能到达本机")

hook.stop()
print()
print(f"结果: {len(FAILED)} 失败")
if FAILED:
    print(f"!! 失败项: {FAILED}")
    sys.exit(1)
print("全部通过 —— 钩子吞键机制在本机成立")
