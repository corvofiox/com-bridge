# -*- coding: utf-8 -*-
"""验证不用抢前台隐藏光标的新方案: **可命中测试的全屏窗 + 空白类光标**。

## 为什么现有方案必然二选一(已实测)
  抢前台   -> 光标隐藏 ✅ / 物理键盘也到不了钩子 ❌ (阶段2: 钩子+0, RawInput+36)
  不抢前台 -> 键盘正常 ✅ / 光标隐藏不了 ❌

## 不抢前台为什么隐藏不了? 一个新发现的疑点
hider 窗口带 `WS_EX_TRANSPARENT`(点击穿透)。而 **WM_SETCURSOR 是按命中测试
分发的** —— 穿透窗口不参与命中测试, 于是系统把 WM_SETCURSOR 发给**下面那个
窗口**, 用它的光标; hider 类里那个"全透明光标"**从来没被用上**。

## 本实验的假设(可证伪)
若把 hider 改成**参与命中测试**(去掉 WS_EX_TRANSPARENT), 那么:
  * 光标停在它上面时, 系统用它的类光标(全透明) -> 光标看不见
  * 全程**不需要抢前台** -> 键盘钩子不受影响
  * 需要一条 WM_SETCURSOR 来触发提交: 注入一次带 TAG 的 ±1px 抖动即可
    (TAG 事件会被钩子放行, 正常到达窗口)

## 三种变体
  V1 现状: WS_EX_TRANSPARENT + 不抢前台           (预期: 光标仍可见)
  V2 可命中: 无 TRANSPARENT + 空白类光标 + 抖动   (预期: 光标不可见, 键盘正常)
  V3 V2 + 显式处理 WM_SETCURSOR(SetCursor(blank)) (预期: 同上, 更稳)

用法: python tools/exp_cursor_hittest_hider.py <V1|V2|V3>
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

# ★ 复用项目里唯一的 WNDCLASSW / WNDPROC 声明, 绝不重复定义
#   (ctypes argtypes 是进程全局的, 重复定义会报 "expected LP_... instance")
from com_bridge.input import WNDCLASSW, WNDPROC   # noqa: E402

V = (sys.argv[1] if len(sys.argv) > 1 else "V2").upper()

WM_SETCURSOR = 0x0020
WM_MOUSEMOVE = 0x0200
HTCLIENT = 1

WS_POPUP = 0x80000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
HWND_TOPMOST = -1
HWND_BOTTOM = -2
SM_CXCURSOR = 13
SM_CYCURSOR = 14

user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_void_p
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.SetCursor.argtypes = [wintypes.HANDLE]
user32.SetCursor.restype = wintypes.HANDLE
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.CreateCursor.restype = wintypes.HANDLE
user32.GetCursorInfo.argtypes = [ctypes.c_void_p]


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


def cursor_state() -> tuple[bool, int]:
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    user32.GetCursorInfo(ctypes.byref(ci))
    return bool(ci.flags & 1), ci.hCursor


def make_blank_cursor():
    cw = user32.GetSystemMetrics(SM_CXCURSOR)
    ch = user32.GetSystemMetrics(SM_CYCURSOR)
    row = (cw + 31) >> 2
    size = ch * row
    AND = (ctypes.c_ubyte * size)(*([0xFF] * size))
    XOR = (ctypes.c_ubyte * size)(*([0x00] * size))
    CreateCursor = user32.CreateCursor
    CreateCursor.argtypes = [wintypes.HINSTANCE, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int,
                             ctypes.POINTER(ctypes.c_ubyte),
                             ctypes.POINTER(ctypes.c_ubyte)]
    return CreateCursor(kernel32.GetModuleHandleW(None), 0, 0, cw, ch, AND, XOR)


class HiderWin:
    def __init__(self, hittest: bool) -> None:
        self.blank = make_blank_cursor()
        self.hittest = hittest
        self._ref = WNDPROC(self._proc)
        self._cls = f"HittestHider{id(self):x}"
        self._ready = threading.Event()
        self.hwnd = None
        self.moves = 0
        self.setcursors = 0
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(3.0)

    def _proc(self, hwnd, msg, wp, lp):
        if msg == WM_SETCURSOR and (lp & 0xFFFF) == HTCLIENT:
            self.setcursors += 1
            user32.SetCursor(self.blank)      # ★ 让系统用空白光标
            return 1                          # TRUE = 已处理
        if msg == WM_MOUSEMOVE:
            self.moves += 1
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._ref
        wc.hInstance = hinst
        wc.hCursor = self.blank               # 类光标 = 全透明
        wc.lpszClassName = self._cls
        user32.RegisterClassW(ctypes.byref(wc))
        ex = WS_EX_TOOLWINDOW | WS_EX_TOPMOST
        if not self.hittest:
            ex |= WS_EX_TRANSPARENT
        self.hwnd = user32.CreateWindowExW(
            ex, self._cls, "HittestHider", WS_POPUP,
            0, 0, 1, 1, None, None, hinst, None)
        self._ready.set()
        if not self.hwnd:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def show_fullscreen(self) -> None:
        from com_bridge.input import virtual_desktop
        d = virtual_desktop()
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, d.left, d.top,
                            d.width, d.height, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def hide(self) -> None:
        user32.SetWindowPos(self.hwnd, HWND_BOTTOM, 0, 0, 1, 1,
                            SWP_NOMOVE | SWP_NOSIZE | 0x0080)  # SWP_HIDEWINDOW


def main() -> int:
    from com_bridge.hook import LowLevelHook
    from com_bridge.input import EV_KEY, RawInputListener, InputInjector

    rk = []

    def on_ev(ev):
        if ev.type == EV_KEY:
            rk.append(ev.code)

    hook = LowLevelHook()
    assert hook.start()
    time.sleep(0.3)
    li = RawInputListener(on_event=on_ev, accept_synthetic=True)
    li.start()
    assert li.wait_ready()
    time.sleep(0.3)

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.4)

    vis0, _ = cursor_state()
    p0 = hook.probe_keyboard_hook()
    print(f"基线: 光标可见={vis0} 钩子={p0} fg={user32.GetForegroundWindow()}")

    hw = HiderWin(hittest=(V in ("V2", "V3")))
    time.sleep(0.3)
    hw.show_fullscreen()
    time.sleep(0.25)

    # ★ 触发 WM_SETCURSOR: 注入带 TAG 的 ±1px 抖动。
    #   TAG 事件会被低级钩子**放行**, 能正常送达窗口。
    InputInjector.move_relative(1, 0)
    time.sleep(0.08)
    InputInjector.move_relative(-1, 0)
    time.sleep(0.35)

    vis1, hcur = cursor_state()
    p1 = hook.probe_keyboard_hook()
    fg = user32.GetForegroundWindow()
    print(f"{V}: 光标可见={vis1} (hCursor={hcur}) 钩子={p1} "
          f"fg_is_hider={fg == hw.hwnd} moves={hw.moves} "
          f"setcursors={hw.setcursors}")

    # 持续 3s 看是否稳定 / 键盘是否可用
    vis_seq, prb_seq = [], []
    for _ in range(6):
        time.sleep(0.5)
        v, _h = cursor_state()
        p = hook.probe_keyboard_hook()
        vis_seq.append("V" if v else "-")
        prb_seq.append("T" if p is True else ("F" if p is False else "?"))
    print(f"    3s 内: 光标={''.join(vis_seq)} 钩子={''.join(prb_seq)}")

    hw.hide()
    time.sleep(0.3)
    vis2, _h = cursor_state()
    print(f"    收窗后: 光标可见={vis2}")

    li.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)

    hid_ok = (not vis1) and ("-" in "".join(vis_seq))
    kbd_ok = (p1 is True) and ("F" not in "".join(prb_seq))
    print()
    print(f"结论 {V}: 光标隐藏={hid_ok} 键盘可用={kbd_ok} "
          f"{'★ 可用方案' if hid_ok and kbd_ok else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
