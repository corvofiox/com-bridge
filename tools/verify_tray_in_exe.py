# -*- coding: utf-8 -*-
"""验证打包 exe 里的托盘宿主窗口与图标确实存在。

message-only 窗口(HWND_MESSAGE)不被 EnumWindows 枚举, 必须用
FindWindowEx(HWND_MESSAGE, ...) 逐个子窗口找。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

sys.path.insert(0, ".")

user32 = ctypes.windll.user32

user32.FindWindowExW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND,
                                 wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR,
                                 ctypes.c_int]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]

HWND_MESSAGE = wintypes.HWND(-3)

print("== 查找 Com Bridge 的托盘宿主窗口(message-only) ==")
found: list[tuple[int, int, str]] = []
child = wintypes.HWND(0)
while True:
    child = user32.FindWindowExW(HWND_MESSAGE, child, None, None)
    if not child:
        break
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(child, buf, 256)
    if "ComBridgeTray" in buf.value:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(child, ctypes.byref(pid))
        found.append((int(child), pid.value, buf.value))

print(f"  找到 {len(found)} 个托盘宿主窗口")
import subprocess
for hwnd, pid, cls in found:
    # 取进程名
    name = "?"
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV",
                            "/NH"], capture_output=True, text=True, timeout=5)
        name = r.stdout.strip().split(",")[0].strip('"')
    except Exception:  # noqa: BLE001
        pass
    print(f"    hwnd={hwnd}  pid={pid}  进程={name}  类名={cls}")

print()
if found:
    print("  ✓ 托盘宿主窗口存在 -> 图标已注册到托盘")
else:
    print("  ✗ 未找到托盘宿主窗口 -> 托盘未启动!")
sys.exit(0 if found else 1)
