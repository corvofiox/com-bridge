# -*- coding: utf-8 -*-
"""精确定位: 是我们的**哪一条** RawInput 注册导致键盘钩子失效?

## 已确证(物理按键, 用户亲手按)
  | 条件                     | 正常 | hider 隐藏光标 |
  | 无 RawInput              | +20  | **+26 正常**   |
  | 有 RawInput (鼠标+键盘)   | +16  | **+0  全灭**   |
=> 罪魁是 RIDEV_INPUTSINK 注册。

## 待定: 是"键盘注册"还是"鼠标注册"还是"两者叠加"?
本项目的 RawInputListener 注册了两条:
    usUsagePage=0x01, usUsage=0x02  (mouse)    + RIDEV_INPUTSINK
    usUsagePage=0x01, usUsage=0x06  (keyboard) + RIDEV_INPUTSINK

这对修法影响很大:
  * 若只有**键盘**注册有害 -> 只注销键盘 RawInput, 鼠标 RawInput 保留
    (越界判定/回声过滤都靠鼠标 RawInput, 几乎不用改架构!)
  * 若**鼠标**注册也有害 -> 必须重新设计事件来源

用法: python tools/exp_which_rawinput.py <KB|MOUSE|BOTH>
提示走屏幕上 tkinter 窗口, 不会看不到。
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

WHICH = (sys.argv[1] if len(sys.argv) > 1 else "KB").upper()

from com_bridge.input import (          # noqa: E402
    WNDCLASSW, WNDPROC, RAWINPUTDEVICE, RIDEV_INPUTSINK, WM_INPUT,
)
RIM_TYPEMOUSE, RIM_TYPEKEYBOARD = 0, 1
RID_INPUT = 0x10000003
WM_DESTROY = 0x0002

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


class Sink:
    """只注册指定 usage 的 RawInput(不解析事件, 只看钩子是否还活着)。"""

    def __init__(self, usages: list[int]) -> None:
        self._usages = usages
        self._ref = WNDPROC(self._proc)
        self._cls = f"SinkProbe{id(self):x}"
        self._ready = threading.Event()
        self.hwnd = None
        self.raw_msgs = 0
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(3.0)

    def _proc(self, hwnd, msg, wp, lp):
        if msg == WM_INPUT:
            self.raw_msgs += 1
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
            0, self._cls, self._cls, 0, 0, 0, 0, 0, None, None, hinst, None)
        self._ready.set()
        if not self.hwnd:
            return
        for usage in self._usages:
            rid = RAWINPUTDEVICE()
            rid.usUsagePage = 0x01
            rid.usUsage = usage
            rid.dwFlags = RIDEV_INPUTSINK
            rid.hwndTarget = self.hwnd
            ok = user32.RegisterRawInputDevices(
                ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE))
            print(f"    register usage=0x{usage:02x} -> {bool(ok)}")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def main() -> int:
    import tkinter as tk
    from com_bridge.hook import LowLevelHook
    from com_bridge.cursor import CursorHider

    usages = {"KB": [0x06], "MOUSE": [0x02], "BOTH": [0x02, 0x06],
              "NONE": []}[WHICH]

    hook = LowLevelHook()
    assert hook.start()
    time.sleep(0.3)

    sink = None
    if usages:
        print(f"  注册 RawInput: {WHICH} -> usages={usages}")
        sink = Sink(usages)
        time.sleep(0.3)

    hider = CursorHider(hide_on_motion=False, name="WhichHider")
    hider.start()
    time.sleep(0.4)

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.3)

    root = tk.Tk()
    root.title(f"Com Bridge 诊断 — {WHICH}")
    root.geometry("560x250+120+120")
    root.attributes("-topmost", True)
    tl = tk.Label(root, text="", font=("Microsoft YaHei UI", 15, "bold"))
    tl.pack(pady=(16, 6))
    bd = tk.Label(root, text="", font=("Microsoft YaHei UI", 11),
                  justify="left", wraplength=520)
    bd.pack(pady=4)
    ct = tk.Label(root, text="", font=("Consolas", 12), fg="#0a7")
    ct.pack(pady=6)
    btn = tk.Button(root, text="", font=("Microsoft YaHei UI", 11),
                    width=16, height=1)
    btn.pack(pady=6)

    st = {"c0": 0, "cur": 0, "done": False, "r1": 0}

    def refresh() -> None:
        st["cur"] = hook.diag()["kbd_calls"] - st["c0"]
        extra = f"   RawInput消息={sink.raw_msgs}" if sink else ""
        ct.config(text=f"钩子已收到按键: {st['cur']} 次{extra}")

    def tick() -> None:
        if st["done"]:
            return
        refresh()
        root.after(120, tick)

    def phase1() -> None:
        tl.config(text=f"第 1 步 / 2：正常状态（{WHICH}）")
        bd.config(text="请按键盘任意键 5~10 下。\n"
                       "★ 看到上面数字变大后再点按钮。")
        st["c0"] = hook.diag()["kbd_calls"]
        btn.config(text="按好了，下一步", command=go_hide)
        refresh()

    def go_hide() -> None:
        st["r1"] = st["cur"]
        if st["r1"] == 0:
            bd.config(text="⚠ 还没收到按键，请先按几下键盘。")
            btn.config(text="再试一次", command=phase1)
            return
        hider.hide()
        root.lift()
        root.attributes("-topmost", True)
        root.after(400, phase2)

    def phase2() -> None:
        tl.config(text=f"第 2 步 / 2：隐藏光标状态（{WHICH}）")
        bd.config(text="屏幕会短暂变空（光标隐藏），正常。\n"
                       "请再按 5~10 下键盘，然后点按钮。")
        st["c0"] = hook.diag()["kbd_calls"]
        btn.config(text="按好了，结束", command=finish)
        refresh()

    def finish() -> None:
        r2 = st["cur"]
        st["done"] = True
        hider.show()
        tl.config(text=f"结果：{WHICH}")
        bd.config(text=f"正常状态收到: {st['r1']} 次\n"
                       f"隐藏光标后收到: {r2} 次\n\n"
                       f"{'✅ 键盘正常' if r2 > 0 else '❌ 键盘失效'}")
        btn.config(text="关闭", command=lambda: root.destroy())
        print()
        print(f"WHICH={WHICH}  正常={st['r1']}  隐藏后={r2}  "
              f"=> {'OK' if r2 > 0 else 'BROKEN'}")

    phase1()
    root.after(120, tick)
    root.protocol("WM_DELETE_WINDOW", lambda: (hider.show(), root.destroy()))
    root.mainloop()

    hider.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)
    return 0


if __name__ == "__main__":
    sys.exit(main())
