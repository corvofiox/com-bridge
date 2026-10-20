# -*- coding: utf-8 -*-
"""定位: hider.hide() 之后**约 3 秒**键盘钩子失效的机制。

上一实验(exp_hider_hide_kills_hook.py)事实:
  A 基线 probe=True
  B hide() 后立刻 probe=True (光标已隐藏, fg=ComBridgeHider)
  C 持续轮询 6 次(每 0.5s): T T T T T **F**   <- 约 2.5~3s 处失效
     失效时 kbd_calls 冻结在 30, fg 仍是 ComBridgeHider
  D show() 后立刻 probe=True

且 GetCapture 恒为 False —— 捕获从未真正拿到, 但光标确实隐藏了。

=> 3 秒处发生了什么? 候选机制:
   M1 前台窗口的"静默超时" — Windows 在无人操作后改变了前台/激活状态
   M2 SetTimer(100ms) 保活累积到某个阈值
   M3 ShowCursor 计数器被系统/其它程序重置(需 3s 才表现出来)
   M4 hider 窗口被系统当作"无响应"而降级

本脚本把 hide() 拆成**逐步累加**的 4 个阶段, 每阶段后测 probe,
用"哪一步之后失效"来定位机制。全程不依赖推测。
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")
user32 = ctypes.windll.user32

from com_bridge.hook import LowLevelHook      # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def title(h) -> str:
    if not h:
        return "(none)"
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, buf, n + 2)
    return buf.value or f"<{h}>"


def fg_info() -> str:
    fg = user32.GetForegroundWindow()
    tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    pid = ctypes.c_ulong()
    if fg:
        user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    return (f"fg={title(fg)} hwnd={fg} tid={tid} pid={pid.value} "
            f"active={user32.GetActiveWindow() == fg}")


def poll_probe(hook, seconds: float, tag: str) -> list:
    """持续轮询 probe, 返回每半秒一个结果。"""
    out = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        p = hook.probe_keyboard_hook()
        out.append(p)
        d = hook.diag()
        print(f"    [{tag}] t={time.monotonic() - t0:5.2f}s probe={p} "
              f"kbd_calls={d['kbd_calls']} pump_age={d['hook_pump_age_s']:.2f}")
        if p is False:
            break
    return out


def main() -> int:
    print("== start hook ==")
    hook = LowLevelHook()
    if not hook.start():
        print("hook start failed")
        return 1
    time.sleep(0.3)

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.3)

    base = hook.probe_keyboard_hook()
    print(f"baseline probe={base}")
    check("baseline reachable", base is True, f"probe={base}")

    # --- 阶段 1: 只创建 hider 窗口(1x1, 隐藏), 不 hide ---
    print()
    print("== S1: create hider window only (1x1, not shown) ==")
    from com_bridge.cursor import CursorHider
    hider = CursorHider(hide_on_motion=False, name="ExpHider2")
    hider.start()
    time.sleep(0.4)
    hwnd = hider._hwnd
    print(f"  hwnd={hwnd} {fg_info()}")
    r1 = hook.probe_keyboard_hook()
    print(f"  probe={r1}")
    check("S1 hook ok after window creation", r1 is True, f"probe={r1}")

    # --- 阶段 2: 全屏 ShowWindow(不抢前台, 不捕获) ---
    print()
    print("== S2: fullscreen ShowWindow only (no foreground, no capture) ==")
    from com_bridge.input import virtual_desktop
    d = virtual_desktop()
    SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
    user32.SetWindowPos(hwnd, -1, d.left, d.top, d.width, d.height,
                        SWP_NOACTIVATE | SWP_SHOWWINDOW)
    time.sleep(0.4)
    print(f"  {fg_info()}")
    r2 = hook.probe_keyboard_hook()
    print(f"  probe={r2}")
    check("S2 hook ok after fullscreen show (no fg steal)", r2 is True,
          f"probe={r2}")

    # --- 阶段 3: 抢前台(不捕获) ---
    print()
    print("== S3: steal foreground (no capture yet) ==")
    user32.EnableWindow(hwnd, 1)
    user32.SetActiveWindow(hwnd)
    fg = user32.GetForegroundWindow()
    if fg and fg != hwnd:
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        att = False
        if fg_tid and fg_tid != my_tid:
            att = bool(user32.AttachThreadInput(fg_tid, my_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if att:
                user32.AttachThreadInput(fg_tid, my_tid, False)
    print(f"  {fg_info()}")
    r3 = hook.probe_keyboard_hook()
    print(f"  probe={r3}")

    # --- 阶段 4: 持续轮询 6s, 看出现在什么时候 ---
    print()
    print("== S4: poll 6s with foreground held (no capture, no timer) ==")
    res = poll_probe(hook, 6.0, "S4")
    dur = len(res) * 0.0
    print(f"  results={res}")

    print()
    print("== cleanup ==")
    hider.show()
    time.sleep(0.3)
    hider.stop()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.4)
    after = hook.probe_keyboard_hook()
    print(f"after cleanup probe={after}")
    hook.stop()

    print()
    print(f"failures: {FAILED}")
    print()
    print("READ:")
    print("  if S4 goes False -> foreground window itself is the killer;")
    print("     and we can measure HOW LONG it takes.")
    print("  if S4 stays True -> capture / timer / ShowCursor is the cause.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
