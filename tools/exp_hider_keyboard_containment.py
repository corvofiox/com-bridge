# -*- coding: utf-8 -*-
"""决定性验证: hider 持前台时, 键盘事件去哪了? RawInput 还能拿到吗?

## 为什么这是修复的关键
已证实: hider 抢前台 -> 本进程低级键盘钩子**收不到任何键**(三种注入全 delta=0)。

但 hider 是个**全屏、无控件、WS_EX_TRANSPARENT 的 popup 窗口**。
如果它当前台时:
   (a) 按键被送到 hider 窗口并被 DefWindowProc 丢弃  -> 等于已经"吞掉"了,
       不会污染用户原先前台的应用;
   (b) RawInputListener 仍然能收到这些键         -> 事件来源仍在, 可以转发副机;
那么**键盘跨屏在 hider 持前台时本应正常工作**, 只是我们的 `_keyboard_relay_ready()`
因为 probe=False 把转发路径掐断了 —— 那是自己造成的故障。

## 测什么
1. 造一个"受害者"窗口(记录它收到的 WM_KEYDOWN 次数), 让它当前台
2. 不带 TAG 的 SendInput 打一个键 -> 受害者应收到 (证明测法有效)
3. hider.hide() 抢前台
4. 同样打键 -> 看 (i) 受害者是否还收到 (ii) hider 窗口是否收到
   (iii) RawInputListener 是否仍收到键盘事件
5. hider.show() 后复测

## 判读
  若 (i) 不收到 + (iii) 仍收到  -> hider 天然吞键 + RawInput 有事件
                                    => 键盘转发应该启用, 修 _keyboard_relay_ready
  若 (iii) 也不收到             -> 键盘事件彻底不可用, 需要另一套方案

用法: python tools/exp_hider_keyboard_containment.py
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes

sys.path.insert(0, ".")
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_DESTROY = 0x0002

# ★★★ 绝不重复声明 WNDPROC / WNDCLASSW / RegisterClassW.argtypes ——
#   ctypes 的 argtypes 是进程全局的, 且按 Python 类的**身份**检查类型。
#   本项目已被这个坑咬过多次(先是 tray 重声明 WNDCLASSW, 现在是本脚本),
#   症状永远是 "expected LP_XXX instance instead of pointer to XXX"。
#   一律从 com_bridge.input 复用**唯一**的那份声明。
from com_bridge.input import WNDCLASSW, WNDPROC   # noqa: E402


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_void_p
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
# ★ 不覆盖 RegisterClassW.argtypes: input.py 已把它设为 c_void_p,
#   若这里改成别的(或不设), 会与上面复用的 WNDCLASSW 冲突。
user32.RegisterClassW.restype = wintypes.ATOM
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


class Victim:
    """一个会记录自己收到多少 WM_KEYDOWN 的窗口。"""

    def __init__(self) -> None:
        self.keydowns = 0
        self.hwnd = None
        self._ref = WNDPROC(self._proc)
        self._cls = f"Victim{id(self):x}"
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(3.0)

    def _proc(self, hwnd, msg, wp, lp):
        if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
            self.keydowns += 1
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._ref
        wc.hInstance = hinst
        wc.lpszClassName = self._cls
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(
            0, self._cls, "VictimWindow", 0x00CF0000,
            40, 40, 380, 260, None, None, hinst, None)
        self._ready.set()
        if not self.hwnd:
            return
        user32.ShowWindow(self.hwnd, 1)     # SW_SHOWNORMAL
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def title(h) -> str:
    if not h:
        return "(none)"
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, buf, n + 2)
    return buf.value or f"<{h}>"


def main() -> int:
    from com_bridge.hook import LowLevelHook
    from com_bridge.input import (INPUT, INPUT_KEYBOARD, RawInputListener)

    # --- 键盘事件收集器(RawInput 侧) ---
    rk_events = []

    def on_ev(ev):
        from com_bridge.input import EV_KEY
        if ev.type == EV_KEY:
            rk_events.append((ev.code, time.monotonic()))

    hook = LowLevelHook()
    assert hook.start()
    time.sleep(0.3)
    listener = RawInputListener(on_event=on_ev, accept_synthetic=True)
    listener.start()
    assert listener.wait_ready()
    time.sleep(0.3)

    victim = Victim()
    time.sleep(0.4)
    print(f"victim hwnd={victim.hwnd}")

    def send_key(vk: int) -> None:
        for down in (True, False):
            i = INPUT(type=INPUT_KEYBOARD)
            i.union.ki.wVk = vk
            i.union.ki.wScan = 0
            i.union.ki.dwFlags = 0 if down else 0x0002
            i.union.ki.dwExtraInfo = 0      # ★ 不带 TAG: 模拟"外部"按键
            user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
            time.sleep(0.03)

    def measure(tag: str) -> None:
        v0 = victim.keydowns
        r0 = len(rk_events)
        k0 = hook.diag()["kbd_calls"]
        send_key(0x41)                     # 'A'
        time.sleep(0.35)
        fg = user32.GetForegroundWindow()
        print(f"  [{tag}] fg={title(fg)}")
        print(f"        victim WM_KEYDOWN +{victim.keydowns - v0} | "
              f"RawInput 键盘 +{len(rk_events) - r0} | "
              f"低级钩子 kbd_calls +{hook.diag()['kbd_calls'] - k0}")

    shell = user32.GetShellWindow()

    print()
    print("== A. 受害者窗口在前台(基线) ==")
    # ★ SetForegroundWindow 从后台进程调用会被 Windows 拒绝(实测: fg 仍是
    #   别的程序)。可靠做法是让**目标窗口自己**在前台线程里请求激活。
    user32.PostMessageW(victim.hwnd, 0x0007, 0, 0)   # WM_SETFOCUS
    user32.BringWindowToTop(victim.hwnd)
    user32.SetForegroundWindow(victim.hwnd)
    # 退而求其次: 用 AttachThreadInput 提升本线程前台权限
    fg = user32.GetForegroundWindow()
    if fg != victim.hwnd:
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        my_tid = kernel32.GetCurrentThreadId()
        att = False
        if fg_tid and fg_tid != my_tid:
            att = bool(user32.AttachThreadInput(fg_tid, my_tid, True))
        try:
            user32.BringWindowToTop(victim.hwnd)
            user32.SetForegroundWindow(victim.hwnd)
        finally:
            if att:
                user32.AttachThreadInput(fg_tid, my_tid, False)
    time.sleep(0.5)
    print(f"  victim-is-fg={user32.GetForegroundWindow() == victim.hwnd}")
    measure("victim-fg")

    print()
    print("== B. hider 抢前台 ==")
    from com_bridge.cursor import CursorHider
    hider = CursorHider(hide_on_motion=False, name="ContainHider")
    hider.start()
    time.sleep(0.4)
    hider.hide()
    time.sleep(0.5)
    measure("hider-fg")

    print()
    print("== C. hider 交还前台(show) ==")
    hider.show()
    time.sleep(0.5)
    measure("after-show")

    print()
    print("== 判读 ==")
    print("  B 里 victim 不涨 = hider 天然挡住了键(等于吞键)")
    print("  B 里 RawInput 涨 = 事件来源仍在, 可以转发副机")
    print("  两者同时成立 -> 键盘跨屏在 hider 前台时本应可用")

    hider.stop()
    listener.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)
    return 0


if __name__ == "__main__":
    sys.exit(main())
