"""决定性实验: 低级钩子吞掉鼠标事件后, RawInput 还能收到吗?

这决定整个架构:
  * 若 RawInput **仍能**收到被钩子吞掉的事件 -> 保留现有 RawInput 管线做
    位移来源(它给的是 lLastX/lLastY 原始设备位移, 不受"光标被钳在边缘"
    影响), 钩子只负责**吞**。改动最小、位移精度最高。
  * 若 RawInput **收不到** -> 钩子必须自己产出事件(位移只能从 pt 求差),
    架构要改。

做法: 装 WH_MOUSE_LL, 规则 = "dwExtraInfo == TAG 放行(我们自己注入的),
否则吞掉"; 然后分别注入**无标记**(模拟物理)与**带标记**的位移, 观察三方:
  1) 钩子是否被调用(计数)
  2) RawInputListener 计数是否增长
  3) 光标是否真的移动了(证明吞生效)

⚠ 本机可能跑着 Deskflow(它自己也有 LL 钩子), 它只影响能否"独占", 不影响
  我们观察"钩子吞了 RawInput 是否还收得到"这一事实。
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
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_VIRTUALDESK,
    InputInjector,
    RawInputListener,
    get_cursor_pos,
    kernel32,
    user32,
    virtual_desktop,
)

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_MOUSEWHEEL = 0x020A
LLMHF_INJECTED = 0x00000001
PM_REMOVE = 0x0001


class POINT(ctypes.Structure):
    _fields_ = [("x", c_int), ("y", c_int)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(c_ssize_t, c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [c_int, HOOKPROC, ctypes.c_void_p,
                                     wintypes.DWORD]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.CallNextHookEx.restype = c_ssize_t
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, c_int, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.PeekMessageW.argtypes = [P(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                 wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [P(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                               wintypes.UINT]
user32.GetMessageW.restype = c_int

stats = {"calls": 0, "eaten": 0, "passed": 0, "moves": 0, "buttons": 0}
_hook = None
_stop = threading.Event()
_ready = threading.Event()
_hook_ref = None      # ★ 必须常驻: HOOKPROC 对象被 GC 回收 -> Windows 调用
                      #   野指针 -> 访问违例(进程直接崩溃, 无 traceback)。
                      #   这是 ctypes 回调最常见的崩溃原因。


def _hook_proc(code, wparam, lparam):
    if code < 0:
        return user32.CallNextHookEx(None, code, wparam, lparam)
    info = ctypes.cast(lparam, P(MSLLHOOKSTRUCT)).contents
    tag = info.dwExtraInfo or 0
    stats["calls"] += 1
    if wparam == WM_MOUSEMOVE:
        stats["moves"] += 1
    elif wparam in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN):
        stats["buttons"] += 1
    if tag == InputInjector.TAG:
        stats["passed"] += 1
        return user32.CallNextHookEx(None, code, wparam, lparam)
    stats["eaten"] += 1
    return 1          # 吞掉: 系统不再处理 -> 光标不动


def hook_thread():
    global _hook, _hook_ref
    _hook_ref = HOOKPROC(_hook_proc)          # 常驻引用, 防 GC
    _hook = user32.SetWindowsHookExW(WH_MOUSE_LL, _hook_ref, None, 0)
    if not _hook:
        print(f"  !! SetWindowsHookExW 失败, GetLastError="
              f"{kernel32.GetLastError()}")
    _ready.set()
    if not _hook:
        return
    msg = wintypes.MSG()
    while not _stop.is_set():
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.002)
    user32.UnhookWindowsHookEx(_hook)


def inject_unmarked(dx, dy):
    """无标记相对位移 —— 对钩子而言等同物理输入。"""
    i = INPUT(type=0)
    i.union.mi.dx = dx
    i.union.mi.dy = dy
    i.union.mi.dwFlags = MOUSEEVENTF_MOVE
    i.union.mi.dwExtraInfo = 0        # 不打标记
    return user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def snapshot(lst):
    d = lst.diag()
    return d


def main() -> int:
    import subprocess
    try:
        tl = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                            capture_output=True, text=True, timeout=15).stdout.lower()
        df = "deskflow" in tl
    except Exception:  # noqa: BLE001
        df = None
    print(f"环境: Deskflow 正在运行 = {df} (会影响光标控制权, 不影响本实验结论)")

    lst = RawInputListener(on_event=lambda ev: None)
    lst.start()
    lst.wait_ready(2.0)
    time.sleep(0.3)

    t = threading.Thread(target=hook_thread, daemon=True)
    t.start()
    _ready.wait(2.0)
    print(f"WH_MOUSE_LL 安装: {'OK' if _hook else 'FAILED'}  h={_hook}")
    if not _hook:
        print("!! 装不上钩子, 无法继续")
        return 1
    time.sleep(0.2)

    d = virtual_desktop()
    cx, cy = d.left + d.width // 2, d.top + d.height // 2

    # ---------- 1) 无标记位移(模拟物理) ----------
    user32.SetCursorPos(cx, cy)
    time.sleep(0.15)
    user32.SetCursorPos(cx, cy)     # 二次确认(排除外部干扰)
    time.sleep(0.1)
    before = get_cursor_pos()
    s0 = snapshot(lst)
    stats.update(calls=0, eaten=0, passed=0, moves=0, buttons=0)

    for _ in range(20):
        inject_unmarked(10, 8)
        time.sleep(0.02)
    time.sleep(0.3)
    after = get_cursor_pos()
    s1 = snapshot(lst)

    print("\n== 1) 无标记位移 x20 (钩子应吞掉) ==")
    print(f"  钩子: calls={stats['calls']} moves={stats['moves']} "
          f"eaten={stats['eaten']} passed={stats['passed']}")
    print(f"  RawInput: mouse {s0['mouse']} -> {s1['mouse']}  "
          f"total {s0['total']} -> {s1['total']}")
    print(f"  光标: {before} -> {after}  "
          f"{'(被吞住, 没动)' if after == before else '(动了!)'}")
    print(f"  >> RawInput 是否仍收到被钩子吞掉的事件? "
          f"{'是' if s1['total'] > s0['total'] else '否'}")

    # ---------- 2) 带标记位移(应放行, 光标能动) ----------
    user32.SetCursorPos(cx, cy)
    time.sleep(0.15)
    before2 = get_cursor_pos()
    s2 = snapshot(lst)
    stats.update(calls=0, eaten=0, passed=0, moves=0, buttons=0)
    for _ in range(20):
        InputInjector.move_relative(10, 8)      # 带 TAG
        time.sleep(0.02)
    time.sleep(0.3)
    after2 = get_cursor_pos()
    s3 = snapshot(lst)
    print("\n== 2) 带标记位移 x20 (钩子应放行) ==")
    print(f"  钩子: calls={stats['calls']} moves={stats['moves']} "
          f"eaten={stats['eaten']} passed={stats['passed']}")
    print(f"  光标: {before2} -> {after2}")
    print(f"  >> 放行生效? {'是' if after2 != before2 else '否'}")

    # ---------- 3) 按键(带标记 vs 无标记) ----------
    stats.update(calls=0, eaten=0, passed=0, moves=0, buttons=0)
    for _ in range(5):
        i = INPUT(type=0)
        i.union.mi.dwFlags = 0x0002     # LEFTDOWN
        i.union.mi.dwExtraInfo = 0
        user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
        time.sleep(0.02)
        i.union.mi.dwFlags = 0x0004     # LEFTUP
        user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
        time.sleep(0.02)
    time.sleep(0.2)
    print("\n== 3) 无标记按键 x5 ==")
    print(f"  钩子: calls={stats['calls']} buttons={stats['buttons']} "
          f"eaten={stats['eaten']} passed={stats['passed']}")

    # 复原
    _stop.set()
    time.sleep(0.3)
    lst.stop()
    user32.ReleaseCapture()
    for _ in range(10):
        user32.ShowCursor(True)
    print("\n(结束, 钩子已卸载)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
