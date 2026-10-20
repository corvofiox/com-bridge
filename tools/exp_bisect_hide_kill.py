# -*- coding: utf-8 -*-
"""用**真实 CursorHider.hide()** + 逐项屏蔽, 定位"hide 后键盘钩子失效"的具体动作。

## 已有事实(前两个实验)
  exp_hider_hide_kills_hook.py:
      hide() 后立刻 probe=True; 约 2.5~3s 后 probe=False;
      show() 后立刻 True。失效时 kbd_calls 冻结。
  exp_isolate_3s_kill.py:
      **完全不调 hide()**, 只把窗口铺全屏 + 抢前台 -> 6s 内 probe 恒 True。
      => "本进程窗口在前台"这件事**本身不杀钩子**。
      (这一条否定了 gui._release_focus_for_hook 里"前台即失效"的普适说法,
       它只对 tkinter 主窗口成立, 对 hider 这类窗口不成立。)

## 所以嫌疑只剩 hide() 里这几步
  A ShowCursor(FALSE)         —— _set_cursor_visibility(False)
  B SetCapture(hwnd)          —— hider 自己线程
  C SetTimer(100ms) 保活      —— 第 100ms 起反复重抢前台/重设捕获/重压计数器

## 方法
每个模式跑一个**独立进程**(ctypes 全局状态不串味), 屏蔽对应动作后调真实
hide(), 然后每 0.5s 探一次 probe, 共 6s, 记录**首次失败时刻**。

用法: python tools/exp_bisect_hide_kill.py <mode>
  mode = full | no_showcursor | no_capture | no_timer | no_all
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")

MODE = sys.argv[1] if len(sys.argv) > 1 else "full"

import com_bridge.cursor as cur            # noqa: E402
from com_bridge.hook import LowLevelHook   # noqa: E402

user32 = ctypes.windll.user32

OFF = set()
if MODE == "no_showcursor":
    OFF = {"showcursor"}
elif MODE == "no_capture":
    OFF = {"capture"}
elif MODE == "no_timer":
    OFF = {"timer"}
elif MODE == "no_all":
    OFF = {"showcursor", "capture", "timer"}

if "showcursor" in OFF:
    cur._set_cursor_visibility = lambda *a, **k: 0
if "capture" in OFF:
    user32.SetCapture = lambda *a: None
if "timer" in OFF:
    user32.SetTimer = lambda *a: 0


def main() -> int:
    hook = LowLevelHook()
    if not hook.start():
        print(f"{MODE}: hook start FAILED")
        return 1
    time.sleep(0.25)

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.2)

    base = hook.probe_keyboard_hook()
    if base is not True:
        print(f"{MODE}: baseline probe={base} (环境不允许, 跳过)")
        hook.stop()
        return 2

    hider = cur.CursorHider(hide_on_motion=False, name=f"Bisect-{MODE}")
    hider.start()
    time.sleep(0.3)
    hider.hide()

    t0 = time.monotonic()
    first_false = None
    seq = []
    for _ in range(12):                      # 12 x 0.5s = 6s
        time.sleep(0.5)
        p = hook.probe_keyboard_hook()
        seq.append("T" if p is True else ("F" if p is False else "?"))
        if p is False and first_false is None:
            first_false = time.monotonic() - t0
        if p is False:
            break
    d = hook.diag()
    kbd_alive = d.get("kbd_hook_alive")
    reinstalls = d.get("reinstalls")
    kbd_calls = d.get("kbd_calls")

    hider.show()
    time.sleep(0.5)
    after = hook.probe_keyboard_hook()
    hider.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)

    print(f"{MODE}: seq={''.join(seq)} first_false="
          f"{'-' if first_false is None else f'{first_false:.2f}s'} "
          f"kbd_hook_alive={kbd_alive} reinstalls={reinstalls} "
          f"kbd_calls={kbd_calls} after_show={after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
