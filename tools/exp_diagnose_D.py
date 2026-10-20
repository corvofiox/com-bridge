# -*- coding: utf-8 -*-
"""在 BROKEN 状态(RawInput + hider 抢前台)下, 收集**诊断事实**而非猜测。

## 已定位
  no_fore (不抢前台)           -> ok
  no_capture / no_showcur      -> 仍 BROKEN
  => 触发点是「hider **抢前台**」, 且必须存在 RawInput。

## 需要回答的问题
D 组里 kbd_calls 从 0 开始且**不涨** —— 说明注入的 0xE8 键连钩子回调都没进。
这可能意味着:
  q1 钩子句柄其实已经失效(被系统摘除), 但 self.installed 仍是 True
  q2 注入失败(InputInjector.key 返回 0), probe 因此返回 None 而非 False
  q3 前台窗口(hider) 属于本进程 -> 系统按"前台进程自己的键"处理, 不分发给
     本进程的低级钩子(即已知的焦点机制, 但只对**持有前台的进程**生效)

本脚本直接测量这三条, 不做推断。
"""
from __future__ import annotations

import ctypes
import sys
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
    from com_bridge.hook import LowLevelHook
    from com_bridge.input import InputInjector, RawInputListener
    from com_bridge.cursor import CursorHider

    hook = LowLevelHook()
    assert hook.start()
    time.sleep(0.3)
    listener = RawInputListener(on_event=lambda ev: None, accept_synthetic=True)
    listener.start()
    assert listener.wait_ready()
    time.sleep(0.3)

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.3)

    hider = CursorHider(hide_on_motion=False, name="DiagHider")
    hider.start()
    time.sleep(0.4)

    def snapshot(tag):
        d = hook.diag()
        fg = user32.GetForegroundWindow()
        fg_pid = ctypes.c_ulong()
        if fg:
            user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
        my_pid = ctypes.windll.kernel32.GetCurrentProcessId()
        # 直接注入, 看 SendInput 的返回值
        n_down = InputInjector.key(0xE8, 0, True)
        n_up = InputInjector.key(0xE8, 0, False)
        time.sleep(0.25)
        d2 = hook.diag()
        print(f"  [{tag}] kbd_calls={d['kbd_calls']}->{d2['kbd_calls']} "
              f"injected_passed={d['injected_passed']}->{d2['injected_passed']}")
        print(f"        kbd_alive={d['kbd_hook_alive']} "
              f"installed={d['installed']} reinstalls={d['reinstalls']} "
              f"sendinput={n_down}/{n_up} "
              f"pump_age={d['hook_pump_age_s']:.2f}")
        print(f"        fg={title(fg)} fg_pid={fg_pid.value} "
              f"my_pid={my_pid} same_process={fg_pid.value == my_pid}")

    print("== before hide (baseline) ==")
    snapshot("before")
    print()
    print("== after hider.hide() (foreground stolen) ==")
    hider.hide()
    time.sleep(0.5)
    snapshot("after-hide")
    print()
    print("== hider.show() again ==")
    hider.show()
    time.sleep(0.5)
    snapshot("after-show")

    hider.stop()
    listener.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)
    return 0


if __name__ == "__main__":
    sys.exit(main())
