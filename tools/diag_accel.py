"""量化鼠标加速对**注入相对位移**的影响, 验证补偿是否有效。

背景(实测发现): 请求 move_relative(40,25), 实际移动 (+91,+57) —— 放大约
2.29 倍。这是 Windows 鼠标加速的指纹: 注入的相对位移同样走加速曲线, 于是
副机鼠标手感与主机不一致(用户会觉得"飘/不听使唤")。

Deskflow 的 deskMouseRelativeMove 专门处理它:
  "relative moves are subject to cursor acceleration which we don't want.
   so we disable acceleration, do the relative move, then restore
   acceleration."

⚠ 注意: `InputInjector.move_relative` 现在**自带补偿**(临时禁加速),
  所以直接用它测不出放大。本脚本用**两条独立路径**对比:
    * raw_move(): 直接 SendInput, **不**禁加速 -> 测得放大比例(原始症状)
    * InputInjector.move_relative(): 生产代码, 自带补偿 -> 应测得 1:1

三段:
  1) 未补偿注入的放大比例(复现症状)
  2) 手工禁用加速后, 注入位移是否 1:1(验证补偿原理有效)
  3) 生产代码(自带补偿)是否 1:1, 且用户设置是否分毫未动

用法: python -u tools/diag_accel.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import c_int, c_void_p, wintypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from com_bridge.input import (
    INPUT,
    MOUSEEVENTF_MOVE,
    SPI_GETMOUSE,
    SPI_SETMOUSE,
    SPI_GETMOUSESPEED,
    SPI_SETMOUSESPEED,
    InputInjector,
    get_cursor_pos,
    user32,
    virtual_desktop,
    _save_mouse_accel,
)

# SPI 常量: 从 input.py 导入(那里已定义), 这里补齐 speed 两个
SPI_GETMOUSESPEED = SPI_GETMOUSESPEED
SPI_SETMOUSESPEED = SPI_SETMOUSESPEED

user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         c_void_p, wintypes.UINT]

Arr3 = c_int * 3


def get_mouse() -> tuple[int, int, int]:
    a = Arr3(0, 0, 0)
    user32.SystemParametersInfoW(SPI_GETMOUSE, 0, ctypes.byref(a), 0)
    return a[0], a[1], a[2]


def get_speed() -> int:
    v = c_int(0)
    user32.SystemParametersInfoW(SPI_GETMOUSESPEED, 0, ctypes.byref(v), 0)
    return v.value


def set_mouse(t1: int, t2: int, accel: int) -> None:
    a = Arr3(t1, t2, accel)
    user32.SystemParametersInfoW(SPI_SETMOUSE, 0, ctypes.byref(a), 0)


def set_speed(v: int) -> None:
    c = c_int(v)
    user32.SystemParametersInfoW(SPI_SETMOUSESPEED, 0, ctypes.byref(c), 0)


def raw_move(dx: int, dy: int) -> None:
    """**未补偿**的原始注入(直接 SendInput), 用于测出放大比例本身。"""
    i = INPUT(type=0)
    i.union.mi.dx = dx
    i.union.mi.dy = dy
    i.union.mi.dwFlags = MOUSEEVENTF_MOVE
    i.union.mi.dwExtraInfo = InputInjector.TAG
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def measure(step, dx: int, dy: int, n: int = 1) -> tuple[int, int]:
    d = virtual_desktop()
    cx, cy = d.left + d.width // 2, d.top + d.height // 2
    user32.SetCursorPos(cx, cy)
    time.sleep(0.12)
    x0, y0 = get_cursor_pos()
    for _ in range(n):
        step(dx, dy)
        time.sleep(0.02)
    time.sleep(0.12)
    x1, y1 = get_cursor_pos()
    return x1 - x0, y1 - y0


def main() -> int:
    t1, t2, accel = get_mouse()
    speed = get_speed()
    print(f"当前鼠标设置: threshold1={t1} threshold2={t2} "
          f"acceleration={accel} speed={speed}")

    RX, RY = 40, 25
    N = 5
    expect = (RX * N, RY * N)

    print(f"\n1) **未补偿**原始注入 ({RX},{RY}) x{N}  (期望 {expect})")
    g1 = measure(raw_move, RX, RY, N)
    r1 = (g1[0] / expect[0]) if expect[0] else 0
    print(f"   实际 {g1}   比例 ({r1:.2f}, {g1[1]/expect[1]:.2f})")

    print(f"\n2) 手工禁用加速后, 原始注入 ({RX},{RY}) x{N}")
    set_mouse(0, 0, 0)
    time.sleep(0.15)
    g2 = measure(raw_move, RX, RY, N)
    print(f"   实际 {g2}   比例 ({g2[0]/expect[0]:.2f}, {g2[1]/expect[1]:.2f})")
    exact_nomock = (abs(g2[0] - expect[0]) <= 4 and abs(g2[1] - expect[1]) <= 4)
    print(f"   >> 1:1? {'是' if exact_nomock else '否'}")
    set_mouse(t1, t2, accel)
    set_speed(speed)
    time.sleep(0.15)

    print(f"\n3) **生产代码** InputInjector.move_relative ({RX},{RY}) x{N}"
          f"  (自带禁加速补偿)")
    g3 = measure(InputInjector.move_relative, RX, RY, N)
    print(f"   实际 {g3}   比例 ({g3[0]/expect[0]:.2f}, {g3[1]/expect[1]:.2f})")
    exact_prod = (abs(g3[0] - expect[0]) <= 4 and abs(g3[1] - expect[1]) <= 4)
    print(f"   >> 1:1? {'是' if exact_prod else '否'}")

    print("\n4) 用户设置是否被污染")
    after = _save_mouse_accel()
    print(f"   before={(t1, t2, accel)} after={after} "
          f"-> {'未污染' if after == (t1, t2, accel) else '!! 被改了'}")

    print("\n" + "=" * 62)
    amplified = r1 > 1.2
    print(f"[发现] 未补偿注入受加速放大: {amplified} (比例 {r1:.2f})")
    print(f"[验证] 禁加速可使注入 1:1: {exact_nomock}")
    print(f"[生产] 自带补偿后 1:1: {exact_prod}")
    print(f"[安全] 用户设置未被污染: {after == (t1, t2, accel)}")
    return 0 if (amplified and exact_nomock and exact_prod) else 1


if __name__ == "__main__":
    sys.exit(main())

