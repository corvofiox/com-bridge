# -*- coding: utf-8 -*-
"""验证托盘图标真实可用: 启动 -> 切换状态 -> 确认图标句柄有效。"""
import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

from com_bridge import tray

user32 = ctypes.windll.user32


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.c_void_p]
user32.GetIconInfo.restype = wintypes.BOOL


def icon_ok(h: int) -> bool:
    if not h:
        return False
    info = ICONINFO()
    if not user32.GetIconInfo(h, ctypes.byref(info)):
        return False
    ok = bool(info.hbmMask) and bool(info.hbmColor)
    for bm in (info.hbmMask, info.hbmColor):
        if bm:
            ctypes.windll.gdi32.DeleteObject(bm)
    return ok


print("== 启动托盘 ==")
t = tray.TrayIcon(tooltip="Com Bridge · 测试")
ok = t.start()
print(f"  启动={ok} running={t.running} hwnd={t._hwnd}")
if not ok:
    sys.exit(1)

print(f"  图标句柄={t._hicon} 有效={icon_ok(t._hicon)}")

print()
print("== 切换到运行中(绿色) ==")
t.set_status(True, "Com Bridge · 主电脑运行中")
time.sleep(0.6)
print(f"  图标句柄={t._hicon} 有效={icon_ok(t._hicon)} "
      f"running={t.running}")

print()
print("== 切换回已停止(灰色) ==")
t.set_status(False, "Com Bridge · 已停止")
time.sleep(0.6)
print(f"  图标句柄={t._hicon} 有效={icon_ok(t._hicon)} "
      f"running={t.running}")

print()
print("== 停止托盘 ==")
t.stop()
print(f"  running={t.running}")
print()
print("判读: 两个状态的图标句柄都必须有效(有效=能在托盘里画出内容)")
