# -*- coding: utf-8 -*-
"""决定性区分: BROKEN 状态下, 钩子是真的死了, 还是**只有注入的键**进不去?

## 为什么必须问这个
`probe_keyboard_hook()` 用 `InputInjector.key(0xE8, ...)` —— 即 **SendInput 注入**。
而诊断显示 BROKEN 时:
    SendInput 返回值 = 1/1 (成功)
    kbd_calls        = 不推进
    kbd_alive/installed = True, reinstalls = 0, pump_age 正常
    fg = 本进程窗口 (ComBridgeHider)

如果只是「本进程窗口在前台时, **注入的**键盘事件不经过本进程的低级钩子」,
那么 probe 会**假报**"钩子失效", 而用户真实按键一切正常 ——
真实后果就是日志里那句"已暂停键盘转发", 键盘跨屏被无故关掉。

## 做法
在 D 条件(RawInput + hider.hide())下, 分别用三种方式产生键盘事件, 各自看
kbd_calls 是否推进:
  1. 带 TAG 的 SendInput (probe 用的方式)
  2. **不带 TAG** 的 SendInput (模拟"非本程序"的合成键)
  3. keybd_event (另一条注入 API)

若 1 不推进而 2/3 推进 -> probe 假阴性, 修 probe 而不必改前台策略。
若 1/2/3 都不推进  -> 此时本进程收不到任何键盘事件, 前台策略必须改。

用法: python tools/exp_probe_false_negative.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")
user32 = ctypes.windll.user32

# 让下方模块级 import 能解析到 com_bridge(必须在导入之前设置好路径)
assert "." in sys.path


# ★★★ 复用 com_bridge.input 里**已经声明过**的 INPUT / INPUT_KEYBOARD /
#   KEYEVENTF_KEYUP / user32.SendInput.argtypes —— 绝不在这里重复声明:
#   ctypes 的 argtypes 是**进程全局**的, 且按 Python 类的**身份**做类型检查。
#   重声明会让同一份 API 出现两个不同的 INPUT 类, 于是
#   "expected LP_INPUT instance instead of pointer to INPUT" 这种荒谬错误
#   (本项目已经被这个坑咬过三次)。
from com_bridge.input import (          # noqa: E402
    INPUT, INPUT_KEYBOARD, KEYEVENTF_KEYUP,
)
KEYDOWN_FLAGS = 0


def send_key(vk: int, down: bool, extra: int | None) -> int:
    i = INPUT(type=INPUT_KEYBOARD)
    i.union.ki.wVk = vk
    i.union.ki.wScan = 0
    i.union.ki.dwFlags = KEYDOWN_FLAGS if down else KEYEVENTF_KEYUP
    i.union.ki.time = 0
    i.union.ki.dwExtraInfo = extra if extra is not None else 0
    return user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


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

    hider = CursorHider(hide_on_motion=False, name="FN-Hider")
    hider.start()
    time.sleep(0.4)

    def measure(tag: str) -> None:
        calls0 = hook.diag()["kbd_calls"]
        tag0 = hook.diag()["injected_passed"]
        eaten0 = hook.diag()["kbd_eaten"]

        # 方式 1: 带 TAG 的 SendInput (probe 的方式)
        InputInjector.key(0xE8, 0, True)
        InputInjector.key(0xE8, 0, False)
        time.sleep(0.2)
        d = hook.diag()
        c1, t1 = d["kbd_calls"], d["injected_passed"]

        # 方式 2: 不带 TAG 的 SendInput
        send_key(0xE9, True, None)
        send_key(0xE9, False, None)
        time.sleep(0.2)
        d = hook.diag()
        c2 = d["kbd_calls"]

        # 方式 3: keybd_event
        user32.keybd_event(0xEA, 0, 0, 0)
        user32.keybd_event(0xEA, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.2)
        d = hook.diag()
        c3 = d["kbd_calls"]

        fg = user32.GetForegroundWindow()
        fg_pid = ctypes.c_ulong()
        if fg:
            user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
        my_pid = ctypes.windll.kernel32.GetCurrentProcessId()

        print(f"  [{tag}] fg={title(fg)} same_proc="
              f"{fg_pid.value == my_pid} kbd_alive={d['kbd_hook_alive']}")
        print(f"        TAG注入   : kbd_calls {calls0}->{c1} "
              f"injected_passed {tag0}->{t1}")
        print(f"        无TAG注入 : kbd_calls ->{c2} (delta={c2 - c1})")
        print(f"        keybd_event: kbd_calls ->{c3} (delta={c3 - c2})")

    print("== A. hider 未隐藏(基线) ==")
    measure("baseline")

    print()
    print("== B. hider.hide() 抢前台后 ==")
    hider.hide()
    time.sleep(0.5)
    measure("hidden")

    print()
    print("== C. hider.show() 恢复后 ==")
    hider.show()
    time.sleep(0.5)
    measure("shown")

    hider.stop()
    listener.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)

    print()
    print("READ:")
    print("  B 里 TAG 不推进、但 无TAG/keybd_event 推进 -> probe 假阴性,")
    print("    真实键盘没问题, 不该因此暂停键盘转发。")
    print("  B 里三种都不推进 -> 本进程确实收不到键盘事件, 前台策略必须改。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
