"""精细诊断: 被钩子吞掉的**按钮/滚轮**在 RawInput 里到底长什么样?

上一实验现象: 钩子吞 9 次, raw 读取器 total=9(全都抵达!), 但按
`usButtonFlags` 解析出来 按钮=0 / 滚轮=0。

两种可能, 必须看**原始字节**才能分辨:
  (a) 我的探针解析写错(偏移/字段), 生产代码其实没问题;
  (b) 被吞掉的按钮在 RawInput 里**不是**用 usButtonFlags 承载 —— 那么
      生产代码 `RawInputListener._emit_mouse` 同样会漏掉它, 这就是
      "点击没作用到副机"的根因。

做法: 对每条收到的鼠标事件打印**全部字段 + 整个 RAWMOUSE 的原始十六进制**,
同时对照生产代码 RawInputListener 的计数(raw_mouse / raw_synthetic)。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import POINTER as P
from ctypes import c_int, c_ssize_t, wintypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from com_bridge.input import (
    INPUT,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_WHEEL,
    RAWINPUTDEVICE,
    RAWINPUTHEADER,
    RAWMOUSE,
    WNDCLASSW,
    WNDPROC,
    InputInjector,
    RawInputListener,
    kernel32,
    user32,
)

WH_MOUSE_LL = 14
WM_INPUT = 0x00FF
WM_QUIT = 0x0012
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDEV_INPUTSINK = 0x00000100
PM_REMOVE = 0x0001


class POINT(ctypes.Structure):
    _fields_ = [("x", c_int), ("y", c_int)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


HOOKPROC = ctypes.WINFUNCTYPE(c_ssize_t, c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [c_int, HOOKPROC, ctypes.c_void_p,
                                     wintypes.DWORD]
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.CallNextHookEx.restype = c_ssize_t
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, c_int, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.GetRawInputData.restype = wintypes.UINT
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                   ctypes.c_void_p, P(wintypes.UINT),
                                   wintypes.UINT]
user32.RegisterRawInputDevices.restype = wintypes.BOOL
user32.RegisterRawInputDevices.argtypes = [P(RAWINPUTDEVICE), wintypes.UINT,
                                           wintypes.UINT]
user32.CreateWindowExW.restype = wintypes.HWND
user32.PeekMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL

records: list[str] = []
eaten = {"n": 0}
hook_ref = None
hook_ready = threading.Event()
hook_stop = threading.Event()
raw_ready = threading.Event()
raw_stop = threading.Event()
raw_hwnd = None


def hook_proc(code, wparam, lparam):
    if code < 0:
        return user32.CallNextHookEx(None, code, wparam, lparam)
    info = ctypes.cast(lparam, P(MSLLHOOKSTRUCT)).contents
    if (info.dwExtraInfo or 0) == InputInjector.TAG:
        return user32.CallNextHookEx(None, code, wparam, lparam)
    eaten["n"] += 1
    return 1


def hook_thread():
    global hook_ref
    hook_ref = HOOKPROC(hook_proc)
    h = user32.SetWindowsHookExW(WH_MOUSE_LL, hook_ref, None, 0)
    hook_ready.set()
    if not h:
        print("  !! 钩子安装失败:", kernel32.GetLastError())
        return
    msg = wintypes.MSG()
    while not hook_stop.is_set():
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.002)
    user32.UnhookWindowsHookEx(h)


def raw_proc(hwnd, msg, wparam, lparam):
    if msg != WM_INPUT:
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    size = wintypes.UINT(0)
    user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size),
                           ctypes.sizeof(RAWINPUTHEADER))
    if not size.value:
        return 0
    buf = ctypes.create_string_buffer(size.value)
    got = user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size),
                                 ctypes.sizeof(RAWINPUTHEADER))
    if got != size.value:
        return 0
    hdr = RAWINPUTHEADER.from_buffer_copy(buf.raw[: ctypes.sizeof(RAWINPUTHEADER)])
    if hdr.dwType != RIM_TYPEMOUSE:
        return 0
    base = ctypes.sizeof(RAWINPUTHEADER)
    raw = buf.raw[base:base + ctypes.sizeof(RAWMOUSE)]
    rm = RAWMOUSE.from_buffer_copy(raw)
    records.append(
        f"hDevice=0x{(hdr.hDevice or 0):x} usFlags=0x{rm.usFlags:04x} "
        f"usButtonFlags=0x{rm.usButtonFlags:04x} usButtonData=0x{rm.usButtonData:04x} "
        f"ulRawButtons=0x{rm.ulRawButtons:08x} "
        f"lLastX={rm.lLastX:6d} lLastY={rm.lLastY:6d} "
        f"extra=0x{rm.ulExtraInformation:08x}\n"
        f"      raw bytes: {raw.hex()}"
    )
    return 0


def raw_thread():
    global raw_hwnd
    cls = "HookTypeDetail"
    ref = WNDPROC(raw_proc)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ref
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = cls
    user32.RegisterClassW(ctypes.byref(wc))
    raw_hwnd = user32.CreateWindowExW(0, cls, cls, 0, 0, 0, 0, 0,
                                      None, None, wc.hInstance, None)
    rid = RAWINPUTDEVICE()
    rid.usUsagePage = 0x01
    rid.usUsage = 0x02
    rid.dwFlags = RIDEV_INPUTSINK
    rid.hwndTarget = raw_hwnd
    user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                   ctypes.sizeof(RAWINPUTDEVICE))
    raw_ready.set()
    msg = wintypes.MSG()
    while not raw_stop.is_set():
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.002)
    user32.PostMessageW(raw_hwnd, WM_QUIT, 0, 0)


def send(flags: int, dx: int = 0, dy: int = 0, data: int = 0, tag: bool = False):
    i = INPUT(type=0)
    i.union.mi.dx = dx
    i.union.mi.dy = dy
    i.union.mi.dwFlags = flags
    i.union.mi.mouseData = data & 0xFFFFFFFF
    i.union.mi.dwExtraInfo = InputInjector.TAG if tag else 0
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def main() -> int:
    # 生产代码的监听器(对照: 它是否也会漏掉按钮?)
    lst = RawInputListener(on_event=lambda ev: None)
    lst.start()
    lst.wait_ready(2.0)

    threading.Thread(target=raw_thread, daemon=True).start()
    raw_ready.wait(2.0)
    threading.Thread(target=hook_thread, daemon=True).start()
    hook_ready.wait(2.0)
    print(f"raw 读取器 hwnd={raw_hwnd}  钩子={'OK' if hook_ref else 'FAILED'}")
    time.sleep(0.25)

    # ============ 1) 无标记(会被钩子吞) ============
    print("\n" + "=" * 70)
    print("1) 无标记注入(钩子会吞掉) —— 看 RawInput 是否收到按钮/滚轮")
    print("=" * 70)
    records.clear()
    eaten["n"] = 0
    b0 = lst.diag()
    send(MOUSEEVENTF_MOVE, 11, 7)
    time.sleep(0.06)
    send(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.06)
    send(MOUSEEVENTF_LEFTUP)
    time.sleep(0.06)
    send(MOUSEEVENTF_WHEEL, data=120)
    time.sleep(0.2)
    b1 = lst.diag()
    print(f"钩子吞掉 {eaten['n']} 次")
    print(f"raw 读取器收到 {len(records)} 条:")
    for r in records:
        print("   ", r)
    print(f"\n生产 RawInputListener: total {b0['total']}->{b1['total']}, "
          f"mouse {b0['mouse']}->{b1['mouse']}, "
          f"synthetic {b0['synthetic']}->{b1['synthetic']}")

    # ============ 2) 带标记(钩子放行) ============
    print("\n" + "=" * 70)
    print("2) 带标记注入(钩子放行) —— 对照组: 按钮/滚轮长什么样")
    print("=" * 70)
    records.clear()
    eaten["n"] = 0
    c0 = lst.diag()
    send(MOUSEEVENTF_MOVE, 11, 7, tag=True)
    time.sleep(0.06)
    send(MOUSEEVENTF_LEFTDOWN, tag=True)
    time.sleep(0.06)
    send(MOUSEEVENTF_LEFTUP, tag=True)
    time.sleep(0.06)
    send(MOUSEEVENTF_WHEEL, data=120, tag=True)
    time.sleep(0.2)
    c1 = lst.diag()
    print(f"钩子吞掉 {eaten['n']} 次(应为 0)")
    print(f"raw 读取器收到 {len(records)} 条:")
    for r in records:
        print("   ", r)
    print(f"\n生产 RawInputListener: total {c0['total']}->{c1['total']}, "
          f"mouse {c0['mouse']}->{c1['mouse']}, "
          f"synthetic {c0['synthetic']}->{c1['synthetic']}")

    raw_stop.set()
    hook_stop.set()
    time.sleep(0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
