# -*- coding: utf-8 -*-
"""★★★ 验证: 绝对定位方案是否实现严格 1:1(不受速度档位/加速影响)。

## 对比
A. 旧的相对注入(MOUSEEVENTF_MOVE)在不同速度档位下放大不同
B. 新的 move_relative(内部改绝对定位)应始终精确 1:1
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")

import com_bridge.input as I
from com_bridge.input import (_get_mouse_speed, _set_mouse_speed,
                              get_cursor_pos)

user32 = ctypes.windll.user32

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def test_new_move_relative(dx: int, dy: int) -> tuple[int, int]:
    """用新的 move_relative, 返回实际位移。"""
    user32.SetCursorPos(800, 500)
    time.sleep(0.12)
    x0, y0 = get_cursor_pos()
    I.InputInjector.move_relative(dx, dy)
    time.sleep(0.10)
    x1, y1 = get_cursor_pos()
    return x1 - x0, y1 - y0


orig_speed = _get_mouse_speed()
print(f"== 原始速度档位 = {orig_speed} ==")
print()

print("== 新的 move_relative(绝对定位) 在各速度档位下的精度 ==")
print()
for sp in (1, 5, 10, 20):
    _set_mouse_speed(sp)
    time.sleep(0.12)
    print(f"  速度档位 = {sp}:")
    for req in (10, 40, 100):
        mdx, mdy = test_new_move_relative(req, 0)
        err = abs(mdx - req)
        ok = err <= 1          # 允许 1px 取整误差
        print(f"    请求 dx={req:3d} -> 实际 {mdx:+4d} "
              f"(误差 {err}) {'OK' if ok else '**偏差!**'}")
        if not ok:
            FAILED.append(f"speed={sp} req={req} -> {mdx}")

# 还原
if orig_speed is not None:
    _set_mouse_speed(orig_speed)
    time.sleep(0.1)

print()
print(f"== 还原速度档位 = {_get_mouse_speed()} ==")
print()
print("== 斜向位移 ==")
for (dx, dy) in ((30, 20), (-40, 25), (60, -35)):
    mdx, mdy = test_new_move_relative(dx, dy)
    ok = abs(mdx - dx) <= 1 and abs(mdy - dy) <= 1
    print(f"  请求 ({dx:+d},{dy:+d}) -> 实际 ({mdx:+d},{mdy:+d}) "
          f"{'OK' if ok else '**偏差!**'}")
    if not ok:
        FAILED.append(f"diag ({dx},{dy}) -> ({mdx},{mdy})")

print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED[:8]:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
