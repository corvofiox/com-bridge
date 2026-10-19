# -*- coding: utf-8 -*-
"""BUG2 活体验证: 1x1 hider 窗口跟随光标后, 光标在**移动过程中**是否保持隐藏。

## 为什么需要这个脚本

之前的验收(verify_hider.py)只测了:
  * hide() 之后 hider 线程的 ShowCursor 计数器为负 —— 那是**前提**, 不是结果;
  * 屏幕上光标到底有没有消失, 它明确写着"需用户目视确认, 截图不含光标"。

于是"副机退回主机后光标依然存在不消失"(BUG2)一直**没有自动判据**。

## 本脚本的判据

用 `GetCursorInfo().flags & CURSOR_SHOWING` —— 这是系统直接给出的
"光标当前是否显示", 不需要目视、也不需要截图:

  * hider.hide() 后: 应为**不显示**;
  * 注入移动(主机转发的路径)之后: 仍应**不显示** —— 这正是 BUG2 的
    失败点(1x1 窗口不跟随光标, 光标一挪开就现形);
  * hider.show() 后: 恢复显示。

## 环境说明

开发机上 deskflow.exe 会抢走输入, 使 hider 收不到消息 —— 此时注入移动
根本不会经过 hider 线程, 判据不可测, 记 SKIP 而不是 FAIL。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

from com_bridge.cursor import CursorHider
from com_bridge.input import InputInjector, get_cursor_pos

user32 = ctypes.windll.user32
CURSOR_SHOWING = 0x00000001


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hCursor", ctypes.c_void_p),
                ("ptScreenPos", wintypes.POINT)]


def cursor_showing() -> bool | None:
    """系统直接给出"光标是否显示"。None = 查询失败。"""
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        return None
    return bool(ci.flags & CURSOR_SHOWING)


FAILED = []
SKIPPED = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def skip(name, detail):
    print(f"  [SKIP] {name} — {detail}")
    SKIPPED.append(name)


print("== 0. GetCursorInfo 可用性 ==")
base = cursor_showing()
if base is None:
    print("!! GetCursorInfo 不可用, 无法验证")
    sys.exit(1)
print(f"  (信息) 初始 cursor_showing = {base}")

print("== 1. 前置判据: hider 窗口必须成为前台窗口(BUG2 的决定性条件) ==")
h = CursorHider(hide_on_motion=True, name="Bug2Hider")
if not h.start():
    print("!! hider 启动失败")
    sys.exit(1)
time.sleep(0.3)
user32.SetCursorPos(600, 400)
time.sleep(0.1)
h.hide()
time.sleep(0.6)
check("hide() 抢到前台(activated)", h._activated,
      f"activated={h._activated} captured={h._captured}; "
      f"为 False 则本线程收不到鼠标消息, 光标不会消失")

print("== 2. hide() 后光标应不显示 ==")
after_hide = cursor_showing()
print(f"  (信息) hide() 后 cursor_showing = {after_hide}, "
      f"moves_seen={h._moves_seen}")
check("hide() 后光标不显示", after_hide is False,
      f"cursor_showing={after_hide}")

print("== 3. 注入移动过程中光标必须保持不显示(BUG2 的核心) ==")
# 主机转发路径: 一连串 move_relative(带 TAG), 模拟用户在副机上移动
worst = None
for i in range(60):
    InputInjector.move_relative(8, 5)
    time.sleep(0.008)
    s = cursor_showing()
    if s:
        worst = i
time.sleep(0.2)
final = cursor_showing()
print(f"  (信息) 60 次注入后 showing={final} hidden={h.hidden} "
      f"moves={h._moves_seen} inj={h._injected_seen} "
      f"streak={h._physical_streak}")
check("注入移动期间光标始终不显示(BUG2)", worst is None and final is False,
      f"首次现形于第 {worst} 次注入; 最终={final}")

print("== 4. 反向移动后仍隐藏 ==")
for i in range(40):
    InputInjector.move_relative(-8, -5)
    time.sleep(0.008)
back = cursor_showing()
check("反向移动后光标仍不显示", back is False, f"cursor_showing={back}")

print("== 5. show() 后恢复显示 ==")
h.show()
time.sleep(0.5)
after_show = cursor_showing()
check("show() 后光标恢复显示", after_show is True,
      f"cursor_showing={after_show}")

print("== 6. 物理移动仍能恢复(不破坏本机可用性) ==")
h.hide()
time.sleep(0.6)
n = None
try:
    user32.SendInput.restype = ctypes.c_uint
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]

    class MI(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]

    class INP(ctypes.Structure):
        class U(ctypes.Union):
            _fields_ = [("mi", MI)]
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_ulong), ("u", U)]

    # 连续多条无标记注入 = 物理移动同判(extra=0); 单条会被 streak 过滤掉
    for _ in range(6):
        ev = INP(type=0)
        ev.mi.dx, ev.mi.dy = 6, 4
        ev.mi.dwFlags = 0x0001
        ev.mi.dwExtraInfo = None
        n = user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INP))
        time.sleep(0.03)
    time.sleep(0.5)
except Exception as exc:  # noqa: BLE001
    print(f"  (信息) 无标记注入失败: {exc}")
phys = cursor_showing()
print(f"  (信息) 连续无标记移动后 showing={phys} hidden={h.hidden} "
      f"streak={h._physical_streak}")
check("物理同判移动触发恢复", h.hidden is False,
      f"hidden={h.hidden} SendInput={n}")

h.stop()
time.sleep(0.3)
final2 = cursor_showing()
check("销毁后光标恢复显示", final2 is True, f"cursor_showing={final2}")

print()
print(f"结果: {len(FAILED)} 失败, {len(SKIPPED)} 跳过")
if FAILED:
    print(f"!! 失败项: {FAILED}")
    sys.exit(1)
print("全部通过")
