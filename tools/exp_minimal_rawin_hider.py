# -*- coding: utf-8 -*-
"""最小复现: RawInputListener(RIDEV_INPUTSINK) + CursorHider.hide() => 键盘钩子失效?

## 已收窄的事实
  独立进程: hide() 单独 -> 6s probe 恒 True (不含 RawInput)
  真实 Server: hide() 单独 -> **立刻 False** (含 RawInputListener)
  真实 Server: 不启动 RawInput -> 恒 True
  真实 Server: 不调 hide()   -> 恒 True

## 本脚本只保留两个组件, 四组组合, 每组独立进程
  A: hook only                 (基线)
  B: hook + rawinput           (RawInput 单独)
  C: hook + hider.hide()       (hider 单独)  <- 已知 True
  D: hook + rawinput + hide()  (组合)        <- 预期 False

若 D=False 而 B/C 都 True, 则因果确定为「RIDEV_INPUTSINK 的 RawInput 与
hider 抢前台/捕获并存」导致键盘钩子收不到事件。

用法: python tools/exp_minimal_rawin_hider.py [A|B|C|D]
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")
user32 = ctypes.windll.user32

WHICH = (sys.argv[1] if len(sys.argv) > 1 else "D").upper()


def main() -> int:
    from com_bridge.hook import LowLevelHook

    hook = LowLevelHook()
    if not hook.start():
        print(f"{WHICH}: hook start failed")
        return 1
    time.sleep(0.3)

    listener = None
    if WHICH in ("B", "D"):
        from com_bridge.input import RawInputListener
        listener = RawInputListener(on_event=lambda ev: None,
                                    accept_synthetic=True)
        listener.start()
        if not listener.wait_ready():
            print(f"{WHICH}: rawinput not ready")
            hook.stop()
            return 1
        time.sleep(0.3)

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.3)

    hider = None
    if WHICH in ("C", "D"):
        from com_bridge.cursor import CursorHider
        hider = CursorHider(hide_on_motion=False, name=f"Mini-{WHICH}")
        hider.start()
        time.sleep(0.4)
        hider.hide()
        time.sleep(0.5)

    base = hook.probe_keyboard_hook()
    seq = []
    calls0 = hook.diag()["kbd_calls"]
    for _ in range(8):
        p = hook.probe_keyboard_hook()
        seq.append("T" if p is True else ("F" if p is False else "?"))
        if p is False:
            break
        time.sleep(0.4)
    calls1 = hook.diag()["kbd_calls"]

    if hider is not None:
        hider.show()
        time.sleep(0.3)
        hider.stop()
    if listener is not None:
        listener.stop()
    after = hook.probe_keyboard_hook()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)

    print(f"{WHICH}: first={base} seq={''.join(seq)} "
          f"kbd_calls={calls0}->{calls1} after={after} "
          f"=> {'BROKEN' if not base else 'ok'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
