# -*- coding: utf-8 -*-
"""用**真实生产 Server** 复现"穿越期间键盘钩子失效"。

## 为什么要做到这一步
前面两个实验已经**否定**了"hider 抢前台 / hide() / 捕获 / 保活定时器"是原因:
在干净环境下, 真实 hide() 五种模式(含全开)6 秒内 probe 恒 True。
(那次"3s 后失效"是在**用户实例占用前台**的污染环境里测的, 已作废。)

所以差异只可能在**生产 Server 的完整环境**里。本脚本直接构造真实 Server,
手工走 `_acquire_control()`(relay=True + hider.hide()) /
`_release_control()`, 在穿越期间持续探针, 记录:
  * probe 序列
  * kbd_calls 是否推进(回调是否真的被调用)
  * hook_pump_age(钩子线程是否还在泵消息)
  * 我方 tick 线程的间隔抖动(是否被 GIL 饿死)

## 判读
  若穿越期间 probe 变 False 且 pump_age 正常 -> 钩子被系统摘除(超时)
  若 pump_age 巨大 -> 钩子线程被卡住(泵消息停了)
  若 probe 恒 True -> 生产 server 也不复现, 说明只在特定操作序列下出现
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time

sys.path.insert(0, ".")
user32 = ctypes.windll.user32


def title(h) -> str:
    if not h:
        return "(none)"
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, buf, n + 2)
    return buf.value or f"<{h}>"


def main() -> int:
    from com_bridge.server import Server

    print("== construct real Server (port 24899 to avoid clashing) ==")
    srv = Server(name="repro", port=24899, side="right",
                 capture_local=False, accept_synthetic_input=True)
    srv.start()
    print("  server started")
    time.sleep(2.0)

    hook = srv._hook
    if hook is None:
        print("  !! no hook installed -> cannot repro")
        srv.stop()
        return 1
    print(f"  hook installed={hook.installed} "
          f"kbd_alive={hook.diag()['kbd_hook_alive']}")

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.3)

    print()
    print("== baseline (not crossing) ==")
    for i in range(3):
        p = hook.probe_keyboard_hook()
        d = hook.diag()
        print(f"  [{i}] probe={p} kbd_calls={d['kbd_calls']} "
              f"pump_age={d['hook_pump_age_s']:.2f} relay={d['relay']}")
        time.sleep(0.5)

    print()
    print("== acquire control (relay=True + hider.hide()) ==")
    t_acq = time.monotonic()
    srv._acquire_control()
    print(f"  acquire took {(time.monotonic() - t_acq) * 1000:.0f}ms  "
          f"fg={title(user32.GetForegroundWindow())}  "
          f"relay={hook.relay}")

    print()
    print("== poll during crossing (8s) ==")
    seq = []
    prev_calls = hook.diag()["kbd_calls"]
    t0 = time.monotonic()
    for i in range(16):
        time.sleep(0.5)
        p = hook.probe_keyboard_hook()
        d = hook.diag()
        calls = d["kbd_calls"]
        delta = calls - prev_calls
        prev_calls = calls
        seq.append("T" if p is True else ("F" if p is False else "?"))
        print(f"  [{i}] t={time.monotonic() - t0:4.1f}s probe={p} "
              f"kbd_calls={calls}(+{delta}) pump_age={d['hook_pump_age_s']:.2f} "
              f"fg={title(user32.GetForegroundWindow())}")
        if p is False:
            print(f"  !! first False at t={time.monotonic() - t0:.2f}s")
            break
    print(f"  seq={''.join(seq)}")

    print()
    print("== release control ==")
    srv._release_control()
    time.sleep(0.5)
    print(f"  after release probe={hook.probe_keyboard_hook()} "
          f"fg={title(user32.GetForegroundWindow())}")

    print()
    print("== stop server ==")
    srv.stop()
    time.sleep(0.5)
    if shell:
        user32.SetForegroundWindow(shell)
    print("  done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
