"""诊断: SendInput 合成事件的 hDevice 到底是什么值?

之前的过滤条件是 `if not hdr.hDevice: return`, 但实测注入 20 次后
raw_mouse 仍增长了 11 —— 说明合成事件的 hDevice 并非总是 NULL。
这里把每个 WM_INPUT 的 hDevice / dwType / 按钮标志打出来看真相。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import byref

sys.path.insert(0, ".")

from com_bridge.input import (  # noqa: E402
    RAWINPUTDEVICE,
    RAWINPUTHEADER,
    RAWKEYBOARD,
    RAWMOUSE,
    RID_INPUT,
    RIDEV_INPUTSINK,
    RIM_TYPEKEYBOARD,
    RIM_TYPEMOUSE,
    WM_INPUT,
    WNDCLASSW,
    WNDPROC,
    InputInjector,
    kernel32,
    user32,
)


records: list[dict] = []


def make_proc() -> WNDPROC:
    def _proc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            size = ctypes.c_uint(0)
            user32.GetRawInputData(lparam, RID_INPUT, None, byref(size),
                                   ctypes.sizeof(RAWINPUTHEADER))
            if size.value:
                buf = ctypes.create_string_buffer(size.value)
                got = user32.GetRawInputData(lparam, RID_INPUT, buf, byref(size),
                                             ctypes.sizeof(RAWINPUTHEADER))
                if got == size.value:
                    hdr = RAWINPUTHEADER.from_buffer_copy(
                        buf.raw[: ctypes.sizeof(RAWINPUTHEADER)])
                    base = ctypes.sizeof(RAWINPUTHEADER)
                    rec = {
                        "hDevice": hdr.hDevice,
                        "dwType": hdr.dwType,
                        "wParam": hdr.wParam,
                    }
                    if hdr.dwType == RIM_TYPEMOUSE:
                        rm = RAWMOUSE.from_buffer_copy(
                            buf.raw[base: base + ctypes.sizeof(RAWMOUSE)])
                        rec.update({"usFlags": rm.usFlags,
                                    "btn": rm.usButtonFlags,
                                    "dx": rm.lLastX, "dy": rm.lLastY})
                    elif hdr.dwType == RIM_TYPEKEYBOARD:
                        rk = RAWKEYBOARD.from_buffer_copy(
                            buf.raw[base: base + ctypes.sizeof(RAWKEYBOARD)])
                        rec.update({"vk": rk.VKey})
                    records.append(rec)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    return WNDPROC(_proc)


def main() -> int:
    proc = make_proc()          # 常驻防 GC
    wc = WNDCLASSW()
    wc.lpfnWndProc = proc
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = "DiagRawInputSink"
    user32.RegisterClassW(byref(wc))
    hwnd = user32.CreateWindowExW(0, "DiagRawInputSink", "diag", 0,
                                  0, 0, 0, 0, None, None, wc.hInstance, None)
    rid = RAWINPUTDEVICE()
    rid.usUsagePage = 0x01
    rid.usUsage = 0x02
    rid.dwFlags = RIDEV_INPUTSINK
    rid.hwndTarget = hwnd
    ok = user32.RegisterRawInputDevices(byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE))
    print("注册鼠标原始输入:", bool(ok), "hwnd=", hwnd)
    rid.usUsage = 0x06
    print("注册键盘原始输入:",
          bool(user32.RegisterRawInputDevices(byref(rid), 1,
                                              ctypes.sizeof(RAWINPUTDEVICE))))

    # 抽消息(没有消息循环, 手动 PeekMessage)
    class MSG(ctypes.Structure):
        pass

    import ctypes.wintypes as wt

    def pump(seconds: float) -> None:
        msg = wt.MSG()
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            while user32.PeekMessageW(byref(msg), hwnd, 0, 0, 1):
                user32.TranslateMessage(byref(msg))
                user32.DispatchMessageW(byref(msg))
            time.sleep(0.005)

    print()
    print("=== 注入 10 次相对移动(合成) ===")
    records.clear()
    for _ in range(10):
        InputInjector.move_relative(3, 0)
        time.sleep(0.02)
    pump(0.5)
    for r in records:
        print("  ", r)
    synth = [r for r in records if r.get("dwType") == RIM_TYPEMOUSE]
    print(f"  共 {len(records)} 条, 其中鼠标 {len(synth)} 条")
    if synth:
        vals = {r["hDevice"] for r in synth}
        print("  鼠标事件的 hDevice 取值集合:", vals)
        print("  全部为 NULL?", vals == {None} or vals == {0})

    user32.DestroyWindow(hwnd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
