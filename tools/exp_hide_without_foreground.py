# -*- coding: utf-8 -*-
"""关键可行性验证: **不抢前台**能隐藏光标吗? 且键盘钩子是否保持可用?

## 为什么必须问
已实测确定的因果链:
    hider 抢前台 -> 前台是本进程窗口 -> 本进程低级键盘钩子**收不到任何键**
    (TAG / 无TAG / keybd_event 三种注入全部 delta=0)
    -> 键盘不吞 \ 不转发 -> 两机同时响应 -> 自愈重装无效 -> 暂停键盘转发

而 `no_fore`(跳过抢前台) 组实测键盘钩子 **ok**。但跳过抢前台后, MSDN 说
SetCapture 只在"鼠标按下或窗口是前台"时生效 —— 那光标还能隐藏吗?

本项目历史注记说"不抢前台 -> 光标不消失"。但那时的实现只做了
SetActiveWindow 而没有做别的; 现在可以用更可靠的现代手段:

  方案 P1: SetCapture + 注入一次鼠标移动提交计数器 (Deskflow secondary 分支)
  方案 P2: SetCursor(NULL)  — 全局置空光标, 不需要前台/捕获
  方案 P3: 全屏窗 + WS_EX_TRANSPARENT + 类光标全透明, 靠窗口盖住
  方案 P4: ShowCursor 计数器 + 周期注入小移动 (不抢前台)

本脚本测 P1 / P2 / P3, 每个方案都同时测:
   * 光标是否真的不可见 (GetCursorInfo 的 CURSOR_SHOWING)
   * 键盘钩子是否仍可达 (probe)
   * 鼠标钩子是否仍可达

用法: python tools/exp_hide_without_foreground.py <P1|P2|P3>
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")
user32 = ctypes.windll.user32
SCHEME = (sys.argv[1] if len(sys.argv) > 1 else "P1").upper()


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


def main() -> int:
    from com_bridge.hook import LowLevelHook
    from com_bridge.input import RawInputListener, InputInjector
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

    hider = CursorHider(hide_on_motion=False, name=f"NoFore-{SCHEME}")
    hider.start()
    time.sleep(0.4)
    hwnd = hider._hwnd

    # 铺全屏 + 显示窗口, 但**不抢前台**
    from com_bridge.input import virtual_desktop
    d = virtual_desktop()
    SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
    user32.SetWindowPos(hwnd, -1, d.left, d.top, d.width, d.height,
                        SWP_NOACTIVATE | SWP_SHOWWINDOW)
    time.sleep(0.3)

    print(f"  方案 {SCHEME}: fg={title(user32.GetForegroundWindow())} "
          f"(应为非本进程窗口)")

    if SCHEME == "P1":
        # SetCapture + 注入一次移动提交计数器 (不抢前台)
        user32.SetCapture(hwnd)
        got = user32.GetCapture() == hwnd
        print(f"  SetCapture 生效 = {got}")
        InputInjector.move_relative(1, 0)
        time.sleep(0.06)
        InputInjector.move_relative(-1, 0)
        time.sleep(0.2)
        from com_bridge.cursor import _set_cursor_visibility
        c = _set_cursor_visibility(False)
        print(f"  ShowCursor(False) 计数 = {c}")
    elif SCHEME == "P2":
        user32.SetCursor(0)          # 全局置空光标
        time.sleep(0.15)
        print("  SetCursor(NULL) 已调用")
    elif SCHEME == "P3":
        # 只靠全屏透明窗盖住 + 类光标全透明 (窗口已设 hCursor=blank)
        from com_bridge.cursor import _set_cursor_visibility
        _set_cursor_visibility(False)
        print("  WS_EX_TRANSPARENT 全屏窗 + ShowCursor(False)")

    time.sleep(0.5)
    vis1 = cursor_showing()
    probe1 = hook.probe_keyboard_hook()
    kbd1 = hook.diag()["kbd_calls"]

    # 持续 3s 观察是否回弹
    seq = []
    vis_seq = []
    for _ in range(6):
        time.sleep(0.5)
        p = hook.probe_keyboard_hook()
        seq.append("T" if p is True else ("F" if p is False else "?"))
        vis_seq.append("V" if cursor_showing() else "-")
        if p is False:
            break

    print(f"  光标可见 = {vis1} (期望 False), 之后 3s: {''.join(vis_seq)}")
    print(f"  键盘 probe = {probe1} kbd_calls={kbd1} 之后 3s: {''.join(seq)}")

    # 清理
    user32.SetCursor(user32.LoadCursorW(0, 32512))     # IDC_ARROW
    user32.ReleaseCapture()
    from com_bridge.cursor import _set_cursor_visibility
    _set_cursor_visibility(True)
    user32.SetWindowPos(hwnd, -2, 0, 0, 1, 1,
                        0x0080)                        # HWND_BOTTOM, SWP_HIDEWINDOW
    hider.stop()
    listener.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)

    ok_hide = not vis1
    ok_kbd = probe1 is True and "F" not in "".join(seq)
    print(f"  => 隐藏成功={ok_hide} 键盘可用={ok_kbd} "
          f"{'★可用方案' if ok_hide and ok_kbd else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
