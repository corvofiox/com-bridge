# -*- coding: utf-8 -*-
"""★★★ 实测: 第一个实例隐藏到托盘后, 第二个实例能否把它唤回前台。

## 用户预期
双击 exe(或再次启动)时, 应该"把那个窗口叫出来", 而不是毫无反应。

## 本工具
假如已有实例子进程:
  1. 启动它(它会显示主窗口)
  2. 让它隐藏到托盘(通过 Win32 隐藏窗口, 模拟点 X)
  3. 调 notify_existing(), 检查窗口是否恢复可见并到前台
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

user32 = ctypes.windll.user32
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


from com_bridge import single_instance as si

# 清掉可能残留的实例
subprocess.run(["taskkill", "/F", "/IM", "python.exe", "/FI",
                "WINDOWTITLE eq Com Bridge*"], capture_output=True)

print("== 启动一个真实 GUI(子进程) ==")
# 用 -c 直接跑 GUI, 避免依赖 cli
code = (
    "import sys; sys.path.insert(0,'.');"
    "from com_bridge import single_instance as s;"
    "assert s.acquire(), 'should acquire';"
    "from com_bridge.gui import ComBridgeGui;"
    "app = ComBridgeGui();"
    "print('GUI_READY', flush=True);"
    "app.run()"
)
proc = subprocess.Popen([sys.executable, "-c", code],
                        stdout=subprocess.PIPE, text=True, cwd=".")
# 等它打印 GUI_READY
deadline = time.time() + 20
ready = False
while time.time() < deadline:
    line = proc.stdout.readline()
    if not line:
        break
    if "GUI_READY" in line:
        ready = True
        break
check("GUI 子进程已就绪", ready)

time.sleep(1.5)
hwnd = si.find_existing_window()
check("能找到已运行的窗口", bool(hwnd), f"hwnd={hwnd}")
if not hwnd:
    proc.kill()
    sys.exit(1)

vis0 = bool(user32.IsWindowVisible(hwnd))
check("窗口初始可见", vis0)

print()
print("== 模拟用户点 X: 隐藏到托盘 ==")
user32.ShowWindow(hwnd, 0)          # SW_HIDE
time.sleep(0.6)
vis1 = bool(user32.IsWindowVisible(hwnd))
check("窗口已隐藏(SW_HIDE 后不可见)", not vis1,
      f"visible={vis1}")

print()
print("== 第二个实例唤起它 ==")
ok = si.notify_existing()
time.sleep(1.0)
vis2 = bool(user32.IsWindowVisible(hwnd))
check("notify_existing 返回成功", ok)
check("★ 窗口被唤回(可见)", vis2, f"visible={vis2}")

print()
print("== 再测最小化场景 ==")
user32.ShowWindow(hwnd, 6)          # SW_MINIMIZE
time.sleep(0.6)
iconic = bool(user32.IsIconic(hwnd))
check("窗口已最小化", iconic)
si.notify_existing()
time.sleep(1.0)
check("★ 窗口从最小化恢复", not bool(user32.IsIconic(hwnd)),
      f"iconic={user32.IsIconic(hwnd)}")

proc.kill()
try:
    proc.wait(timeout=5)
except Exception:  # noqa: BLE001
    pass

print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
