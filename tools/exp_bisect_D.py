# -*- coding: utf-8 -*-
"""在**已复现**的组合(RawInput + hider.hide())里逐项屏蔽, 找出真正触发点。

## 已确立的最小复现(每组独立进程)
  A hook only                  -> ok
  B hook + rawinput            -> ok
  C hook + hider.hide()        -> ok
  D hook + rawinput + hide()   -> **BROKEN** (probe 立刻 False, kbd_calls 0->0)

D 的 kbd_calls 从 0 开始且不涨 —— 说明**注入的键连钩子都没进**,
不只是"回调没被调用"。这排除了"回调太慢被摘钩", 指向"键被别处吃掉/钩子没装上"。

## 本脚本在 D 条件下逐项屏蔽 hide() 的动作
  full        : 完整 hide()                      (预期 BROKEN)
  no_showcur  : 跳过 ShowCursor(FALSE)
  no_capture  : 跳过 SetCapture
  no_fore     : 跳过抢前台(EnableWindow/SetActiveWindow/SetForegroundWindow)
  no_all      : 以上全跳过(只 ShowWindow 铺全屏)

用法: python tools/exp_bisect_D.py <mode>
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")
user32 = ctypes.windll.user32

MODE = (sys.argv[1] if len(sys.argv) > 1 else "full").lower()

import com_bridge.cursor as cur            # noqa: E402
from com_bridge.hook import LowLevelHook   # noqa: E402
from com_bridge.input import RawInputListener  # noqa: E402

OFF = set()
if MODE == "no_showcur":
    OFF = {"showcur"}
elif MODE == "no_capture":
    OFF = {"capture"}
elif MODE == "no_fore":
    OFF = {"fore"}
elif MODE == "no_all":
    OFF = {"showcur", "capture", "fore"}

if "showcur" in OFF:
    cur._set_cursor_visibility = lambda *a, **k: 0
if "capture" in OFF:
    user32.SetCapture = lambda *a: None
if "fore" in OFF:
    user32.SetForegroundWindow = lambda *a: 1
    user32.SetActiveWindow = lambda *a: None
    user32.EnableWindow = lambda *a: 1
    user32.AttachThreadInput = lambda *a: 1


def main() -> int:
    hook = LowLevelHook()
    if not hook.start():
        print(f"{MODE}: hook failed")
        return 1
    time.sleep(0.3)

    listener = RawInputListener(on_event=lambda ev: None,
                                accept_synthetic=True)
    listener.start()
    if not listener.wait_ready():
        print(f"{MODE}: rawinput failed")
        hook.stop()
        return 1
    time.sleep(0.3)

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.3)

    hider = cur.CursorHider(hide_on_motion=False, name=f"Bis-{MODE}")
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

    hider.show()
    time.sleep(0.3)
    hider.stop()
    listener.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)

    print(f"{MODE}: first={base} seq={''.join(seq)} "
          f"kbd_calls={calls0}->{calls1} => "
          f"{'BROKEN' if not base else 'ok'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
