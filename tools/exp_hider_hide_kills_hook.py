# -*- coding: utf-8 -*-
"""★★★ 决定性实验: 走**真实生产路径** `CursorHider.hide()` 会不会杀死键盘钩子?

## 为什么要重做(旧实验 exp_hider_foreground_hook.py 不足以下结论)
旧实验只手动调了一次 `SetForegroundWindow(hider)`, **没有调用 `hide()`**。
而生产路径 `_do_hide()` 做的事情多得多:
  1. SetWindowPos 把窗口铺成**全屏** (旧实验里它一直是 1x1 且从未 ShowWindow)
  2. EnableWindow + SetActiveWindow + AttachThreadInput + SetForegroundWindow
  3. **在 hider 自己的线程里** SetCapture  (旧实验是从主线程调的, 按线程捕获 -> 无效)
  4. ShowCursor(-1) 隐藏光标
  5. SetTimer(100ms) 保活: 每 100ms 重抢前台 + 重设捕获 + 重提交光标可见性

## 真机日志给出的因果范围
  ...
  [server] 越界交接 -> ...            <- 交接瞬间
  [server] 键盘钩子恢复可达 -> ...     <- _reverify, probe=True (此时 hider 尚未 hide)
  [server] !! 上一轮穿越期间键盘钩子失效(回调从未被调用)  <- _verify_kbd_on_release,
                                          probe=False (此时 hider 仍 hidden, show 还没调)
即: probe 的 True/False 与 hider 的 hidden 状态**完全同步**。
本实验直接检验这一点, 而不是再猜。

## 判据
  A 基线(无 hider)            -> probe True
  B hider.hide() 之后          -> probe True/False  <-- 关键
  C hide 状态下持续 3s 轮询     -> 是否恒 False(排除"只是一瞬间")
  D hider.show() 之后          -> probe 是否恢复 True
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")

user32 = ctypes.windll.user32

from com_bridge.cursor import CursorHider          # noqa: E402
from com_bridge.hook import LowLevelHook           # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def cursor_showing() -> bool:
    class CI(ctypes.Structure):
        from ctypes import wintypes as _w
        _fields_ = [("cbSize", _w.DWORD), ("flags", _w.DWORD),
                    ("hCursor", _w.HANDLE), ("pt", _w.POINT)]
    ci = CI()
    ci.cbSize = ctypes.sizeof(CI)
    user32.GetCursorInfo.argtypes = [ctypes.c_void_p]
    user32.GetCursorInfo(ctypes.byref(ci))
    return bool(ci.flags & 1)


def title(h) -> str:
    if not h:
        return "(无)"
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, buf, n + 2)
    return buf.value or f"<{h}>"


def main() -> int:
    print("== 启动钩子(生产配置, relay=False) ==")
    hook = LowLevelHook()
    ok = hook.start()
    print(f"  钩子启动: {ok}, installed={hook.installed}")
    if not ok:
        print("  钩子装不上, 实验无意义")
        return 1
    time.sleep(0.3)

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.3)

    print()
    print("== A. 基线(hider 尚未启动) ==")
    a = hook.probe_keyboard_hook()
    print(f"  probe = {a}, fg = {title(user32.GetForegroundWindow())}")
    check("A 基线钩子可达", a is True, f"probe={a}")

    print()
    print("== 启动 CursorHider 并 hide()(完全走生产路径) ==")
    hider = CursorHider(hide_on_motion=False, name="ExpHider")
    hider.start()
    time.sleep(0.4)
    print(f"  hider._hwnd = {hider._hwnd}")

    hider.hide()
    time.sleep(0.5)
    fg = user32.GetForegroundWindow()
    print(f"  hide() 后: fg = {title(fg)} (hider={hider._hwnd}, "
          f"是否 hider={fg == hider._hwnd})")
    print(f"  GetCapture = {user32.GetCapture()} "
          f"(== hider? {user32.GetCapture() == hider._hwnd})")
    print(f"  光标可见 = {cursor_showing()} (期望 False)")

    print()
    print("== B. hide 状态下 probe(关键) ==")
    b = hook.probe_keyboard_hook()
    print(f"  probe = {b}")
    check("★ hide() 后钩子仍可达", b is True,
          f"probe={b} (若 False 则 hide 抢前台/捕获会杀死键盘钩子)")

    print()
    print("== C. hide 状态持续 3s 轮询(保活定时器每 100ms 抢前台) ==")
    results = []
    for i in range(6):
        time.sleep(0.5)
        p = hook.probe_keyboard_hook()
        results.append(p)
        print(f"  [{i}] probe={p} fg={title(user32.GetForegroundWindow())} "
              f"capture={user32.GetCapture() == hider._hwnd} "
              f"vis={cursor_showing()} kbd_calls={hook.diag()['kbd_calls']}")
    check("C hide 期间持续可达", all(r is True for r in results),
          f"序列={results}")

    print()
    print("== D. show() 之后是否恢复 ==")
    hider.show()
    time.sleep(0.6)
    d = hook.probe_keyboard_hook()
    print(f"  show() 后 probe = {d}, "
          f"fg = {title(user32.GetForegroundWindow())}, "
          f"光标可见 = {cursor_showing()}")
    check("D show() 后钩子可达", d is True, f"probe={d}")

    hider.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)

    print()
    print(f"结果: {len(FAILED)} 项失败")
    for f in FAILED:
        print(f"  - {f}")
    print()
    if b is False or not all(r is True for r in results):
        print("判读: hider.hide() **就是**键盘钩子失效的原因")
        print("      -> 修法: hide 期间不要用'本进程窗口当前台'的方式, "
              "改为不抢前台的光标隐藏, 或 hide 期间不依赖前台窗口。")
    else:
        print("判读: hide() 本身不杀钩子 -> 真机故障另有原因, 需继续排查")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
