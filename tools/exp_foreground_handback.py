# -*- coding: utf-8 -*-
"""出路验证: 抢前台隐藏光标后**立刻把前台交还**, 光标是否保持隐藏? 键盘是否恢复?

## 冲突(全部实测, 结论互斥)
  抢前台   -> 光标隐藏 ✅ / 键盘钩子收不到任何键 ❌
  不抢前台 -> 键盘钩子正常 ✅ / 光标隐藏不了 ❌ (SetCapture 拿不到, ShowCursor 按线程无效)
  且 SetCapture 在任何情况下都没成功过 (GetCapture 恒 False)

## 本脚本验证的假设
光标隐藏可能是**一次性提交**的: 内核在"窗口成为前台"那一刻提交一次光标状态,
之后即使前台被交还, 已隐藏状态也可能保持(直到别人 ShowCursor(TRUE) 或移动鼠标)。
若成立 -> hide() 抢前台 + 立刻交还前台 = 光标隐藏 且 键盘可用。

## 测法
1. 基线: probe + 光标可见
2. hider 铺全屏, 抢前台
3. 立刻把前台还给 shell(模拟 gui 的 _focus_watch_tick)
4. 测: fg 是否已不是本进程 / 光标是否仍隐藏 / probe 是否恢复
5. 持续 4s 观察光标是否回弹、键盘是否稳定

用法: python tools/exp_foreground_handback.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")
user32 = ctypes.windll.user32


def cursor_showing() -> bool:
    class CI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hCursor", wintypes.HANDLE), ("pt", wintypes.POINT)]
    ci = CI()
    ci.cbSize = ctypes.sizeof(CI)
    user32.GetCursorInfo.argtypes = [ctypes.c_void_p]
    user32.GetCursorInfo(ctypes.byref(ci))
    return bool(ci.flags & 1)


def title(h) -> str:
    if not h:
        return "(none)"
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, buf, n + 2)
    return buf.value or f"<{h}>"


def fg_is_ours(hwnd) -> bool:
    fg = user32.GetForegroundWindow()
    return fg == hwnd


def main() -> int:
    from com_bridge.hook import LowLevelHook
    from com_bridge.input import RawInputListener
    from com_bridge.cursor import CursorHider, _set_cursor_visibility

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

    hider = CursorHider(hide_on_motion=False, name="Handback")
    hider.start()
    time.sleep(0.4)
    hwnd = hider._hwnd

    print(f"baseline: probe={hook.probe_keyboard_hook()} "
          f"vis={cursor_showing()} fg={title(user32.GetForegroundWindow())}")

    print()
    print("== 1) 抢前台隐藏 (生产 hide 完整路径) ==")
    hider.hide()
    time.sleep(0.45)
    print(f"  抢前台后: fg_is_hider={fg_is_ours(hwnd)} "
          f"vis={cursor_showing()} probe={hook.probe_keyboard_hook()}")

    print()
    print("== 2) 立刻把前台交还 shell ==")
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.45)
    p2 = hook.probe_keyboard_hook()
    v2 = cursor_showing()
    print(f"  交还后: fg_is_hider={fg_is_ours(hwnd)} vis={v2} probe={p2} "
          f"fg={title(user32.GetForegroundWindow())}")

    print()
    print("== 3) 持续 4s 观察(光标是否回弹 / 键盘是否稳定) ==")
    vis_seq, prb_seq = [], []
    for _ in range(8):
        time.sleep(0.5)
        vis_seq.append("V" if cursor_showing() else "-")
        p = hook.probe_keyboard_hook()
        prb_seq.append("T" if p is True else ("F" if p is False else "?"))
        print(f"    vis={vis_seq[-1]} probe={prb_seq[-1]} "
              f"fg_is_hider={fg_is_ours(hwnd)}")
        if p is False:
            break

    print()
    print("== 清理 ==")
    hider.show()
    time.sleep(0.3)
    _set_cursor_visibility(True)
    hider.stop()
    listener.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)

    hid_ok = not v2
    kbd_ok = p2 is True and "F" not in "".join(prb_seq)
    print(f"交还后: 光标隐藏={hid_ok} 键盘可用={kbd_ok}")
    print(f"4s 后:  vis={''.join(vis_seq)} kbd={''.join(prb_seq)}")
    if hid_ok and kbd_ok and "-" in "".join(vis_seq) and "F" not in "".join(prb_seq):
        print("=> ★ 方案可行: 抢前台隐藏 + 立刻交还, 光标保持隐藏且键盘可用")
    else:
        print("=> 不可行或部分可行, 见上面序列")
    return 0


if __name__ == "__main__":
    sys.exit(main())
