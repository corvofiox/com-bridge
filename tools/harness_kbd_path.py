# -*- coding: utf-8 -*-
"""复现台架: 完全照生产的接线重放一次穿越, 找出 kbd_eaten 恒为 0 的原因。

## 为什么需要台架而不是继续推理

我此前连续给出多个"根因"都被推翻。这次直接把生产的三件套按**同样的接线**
搭起来, 注入真实按键, 观察每个环节的计数:

    LowLevelHook(on_key_event=...)   与 Server._ensure_hook 同款
    RawInputListener(on_event=...)   与 Server._start_input 同款
    _on_raw_input_event 的**自愈触发**也照搬(这是最可疑的一环:
      若它误判"钩子掉了", 就会反复 request_reinstall -> 钩子被反复摘掉)

## 观察点

* `kbd_calls`  —— 回调被系统调用的总次数(0 = 根本没被调用)
* `kbd_eaten`  —— 其中被吞的(0 = relay 没生效, 或回调没被调用)
* `reinstalls` —— 重装次数(被自愈逻辑反复重装 = 症状来源)
* 转发条数      —— 上层实际拿到多少键盘事件

★ 注入一律走 inject_util(强制复用生产 INPUT 结构并断言尺寸)。
"""
from __future__ import annotations

import sys
import threading
import time

sys.path.insert(0, ".")

from com_bridge.hook import PASSTHROUGH_VKS, LowLevelHook
from com_bridge.input import RawInputListener
from inject_util import PROBE_VK, burst, require_injection

# --- 模拟 Server 的状态 -------------------------------------------------
forwarded: list = []
heal_calls = 0
heal_lock = threading.Lock()
_kbd_leaks = 0
KBD_LEAK_REINSTALL_LIMIT = 3
stop = threading.Event()
hook: LowLevelHook | None = None


def on_key_event(ev_type, flags, code, a, b, ts_us):
    """等价 Server._on_hook_key_event -> _on_input_event 的最小面。"""
    forwarded.append((ev_type, flags, code))


def start_kbd_heal():
    """等价 Server._start_kbd_heal(只计数, 不自起线程以免干扰观察)。"""
    global heal_calls
    with heal_lock:
        heal_calls += 1


def on_raw(ev):
    """等价 Server._on_raw_input_event(**含最可疑的自愈触发**)。"""
    global _kbd_leaks
    if ev.is_key and hook is not None and hook.relay \
            and ev.code not in PASSTHROUGH_VKS:
        _kbd_leaks += 1
        start_kbd_heal()


def report(tag):
    d = hook.diag()
    print(f"  [{tag}] kbd_calls={d['kbd_calls']} kbd_eaten={d['kbd_eaten']} "
          f"kbd_passed={d['kbd_passed']} injected={d['injected_passed']} "
          f"reinstalls={d['reinstalls']} installed={d['installed']} "
          f"relay={d['relay']} kbd_alive={d['kbd_hook_alive']} "
          f"worker={d['worker_alive']} | forwarded={len(forwarded)} "
          f"heals={heal_calls} leaks={_kbd_leaks}")
    return d


def main() -> int:
    global hook, _kbd_leaks, heal_calls

    print("=== 1. 照生产接线: 钩子 + RawInput 监听器 + 转发线程 ===")
    hook = LowLevelHook(name="Harness", on_key_event=on_key_event)
    if not hook.start():
        print("  钩子安装失败"); return 2
    print(f"  probe_keyboard_hook() = {hook.probe_keyboard_hook()}")
    lis = RawInputListener(on_event=on_raw)
    lis.start()
    if not lis.wait_ready(3.0):
        print("  监听器启动失败"); return 2
    time.sleep(0.3)
    report("装好, relay=False")

    print("=== 2. 穿越: set_relay(True) + reclaim_head() (照 _acquire_control) ===")
    hook.set_relay(True)
    hook.reclaim_head()
    time.sleep(0.5)
    d = report("穿越后")

    print("=== 3. 注入 10 个无标记按键(与物理键同判) ===")
    before = hook.diag()
    before_fwd = len(forwarded)
    before_leaks = _kbd_leaks
    sent = burst(5, PROBE_VK)
    require_injection(sent, 10)
    after = hook.diag()
    d_calls = after["kbd_calls"] - before["kbd_calls"]
    d_eaten = after["kbd_eaten"] - before["kbd_eaten"]
    d_fwd = len(forwarded) - before_fwd
    d_leaks = _kbd_leaks - before_leaks
    d_re = after["reinstalls"] - before["reinstalls"]
    print(f"  SendInput={sent}/10")
    print(f"  kbd_calls+{d_calls}  kbd_eaten+{d_eaten}  forwarded+{d_fwd}  "
          f"leaks+{d_leaks}  reinstalls+{d_re}")
    report("注入后")

    print()
    print("=== 结论 ===")
    if d_calls == 0:
        print("  ★ 键盘回调**从未被调用** -> 钩子不在链首, 或根本没装上。")
    elif d_eaten == 0:
        print("  ★ 回调被调用了但没吞 -> relay 判定有问题。")
    else:
        print(f"  钩子正常吞键(eaten+{d_eaten})。")
    if d_fwd == 0:
        print("  ★ 上层**一条都没收到** -> 自产/转发链路断了。")
    if d_leaks:
        print(f"  ★★ RawInput 在穿越态仍报来 {d_leaks} 个键盘事件 -> "
              f"自愈被误触发 {heal_calls} 次; 真实环境会因此反复摘钩。")
    if d_re:
        print(f"  ★★ 本阶段发生了 {d_re} 次重装 —— 重装期间钩子不存在, "
              f"按键会穿透到主机。")

    hook.set_relay(False)
    hook.stop()
    lis.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
