# -*- coding: utf-8 -*-
"""★★★ 用户场景验证: 光标停在**其他程序窗口上层**时, hide 是否生效。

## 用户原话
"我说的情况是在其他程序上层, 我能很容易注意到鼠标光标没有消失,
 所以并非一定在这个条件下才会触发鼠标光标没有消失"

## 场景
光标放在: (a) 空桌面 (b) 记事本客户区 (c) 浏览器窗口区域
每种位置都 hide(), 检查 GetCursorInfo 的 showing。

提示: 这是修复后的验证。若某个场景仍失败, 说明还有别的机制在
      重置光标(比如该程序自己在调 ShowCursor)。"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

user32 = ctypes.windll.user32

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def vis() -> bool:
    class CI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hCursor", wintypes.HANDLE), ("pt", wintypes.POINT)]
    ci = CI()
    ci.cbSize = ctypes.sizeof(CI)
    user32.GetCursorInfo.argtypes = [ctypes.c_void_p]
    user32.GetCursorInfo(ctypes.byref(ci))
    return bool(ci.flags & 1)


def center_of(hwnd):
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.l + r.r) // 2, (r.t + r.b) // 2


def probe(h, label, hold=1.2):
    h.hide()
    time.sleep(0.5)
    v1 = vis()
    time.sleep(hold)
    v2 = vis()
    print(f"  [{label}] 0.5s={v1} {hold}s={v2} "
          f"fg={h._is_foreground} cap={h._captured} "
          f"cnt={h._counter_after_hide} regrabs={h._fg_regrabs} "
          f"capchg={h._capture_changed}")
    h.show()
    time.sleep(0.4)
    return v1, v2


from com_bridge.cursor import CursorHider

h = CursorHider(hide_on_motion=False, name="ScenarioTest")
check("hider 启动", h.start())
time.sleep(0.3)

print()
print("== (a) 光标在空桌面 ==")
user32.SetCursorPos(1910, 1075)
time.sleep(0.2)
v1, v2 = probe(h, "空桌面")
check("空桌面: 光标隐藏", not v1 and not v2, f"{v1},{v2}")

print()
print("== (b) 光标在记事本客户区 ==")
np = None
try:
    np = subprocess.Popen(["notepad.exe"])
    time.sleep(1.5)
    nh = user32.FindWindowW("Notepad", None)
    if nh:
        user32.SetCursorPos(*center_of(nh))
        time.sleep(0.3)
        v1, v2 = probe(h, "记事本上")
        check("记事本: 光标隐藏", not v1 and not v2, f"{v1},{v2}")
    else:
        print("  (未找到记事本)")
finally:
    if np:
        np.kill()
        np.wait()
time.sleep(0.4)

print()
print("== (c) 光标在浏览器等其它程序窗口上 ==")
found = []
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                 wintypes.LPARAM)


def cb(hwnd, _lp):
    if user32.IsWindowVisible(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 2)
            user32.GetWindowTextW(hwnd, buf, n + 2)
            found.append((hwnd, buf.value))
    return True


user32.EnumWindows(WNDENUMPROC(cb), 0)
# 排除我们自己的窗口
cands = [(hw, t) for hw, t in found
         if "Com" not in t and "hider" not in t.lower()]
if cands:
    hw, t = cands[0]
    print(f"  目标窗口: {t[:40]!r}")
    user32.SetCursorPos(*center_of(hw))
    time.sleep(0.4)
    v1, v2 = probe(h, "其它程序上")
    check("其它程序: 光标隐藏", not v1 and not v2, f"{v1},{v2}")
else:
    print("  (没有可用的其他窗口)")

h.stop()
print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
