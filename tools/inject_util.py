# -*- coding: utf-8 -*-
"""注入工具的正确写法(所有诊断脚本必须复用, 不得各写一份)。

## ★★ 为什么单独抽出这个模块

本项目**两次**因为诊断脚本里自己拼 `INPUT` 结构而得出错误结论:

    class U(ctypes.Union):
        _fields_ = [("ki", KBD)]        # ← 只放了 keyboard

64 位下 `INPUT` 是 **40 字节**(union 里最大的成员是 32 字节的
`MOUSEINPUT`, 含 8 字节指针 + 对齐), 只放 `KEYBDINPUT`(24B) 会得到
**32 字节**。`SendInput` 的 `cbSize` 传错时**不报异常**, 而是返回 0,
`GetLastError()` = **87 (ERROR_INVALID_PARAMETER)**, 即**注入根本没发生**。

后果极隐蔽: 钩子计数是 0, 于是"钩子收不到按键"看起来像结论,
其实是在**测量空气** —— 并据此推出过完全错误的根因。
(实测踩到两次, 见 tools/diag_hook_chain_order.py 与
 tools/diag_hook_two_instances.py 的修正记录。)

因此: 一律 `from com_bridge.input import INPUT`, 并在使用前断言尺寸,
每次注入都检查返回码。本模块把这两件事都做掉。
"""
from __future__ import annotations

import ctypes
import time

from com_bridge.input import (
    INPUT,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYEVENTF_KEYUP,
    user32,
)

# 结构尺寸不对就**立刻**炸掉, 而不是让后续实验产出假结论
assert ctypes.sizeof(INPUT) == 40, (
    f"sizeof(INPUT)={ctypes.sizeof(INPUT)}, 64 位应为 40; "
    "尺寸错误会让 SendInput 返回 0(GetLastError=87)且不抛异常")

# 未分配的虚拟键, 无字符输出、不影响 Caps/Num/Scroll 指示灯
PROBE_VK = 0xE8


class InjectionFailed(RuntimeError):
    """注入没有发生 —— 此时任何"钩子收不到"的观测都无效。"""


def key(vk: int, up: bool, extra: int | None = None,
        interval: float = 0.012) -> int:
    """注入一次键盘事件, 返回 SendInput 的返回码(1=成功)。"""
    ev = INPUT(type=INPUT_KEYBOARD)
    ev.union.ki.wVk = vk
    ev.union.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    ev.union.ki.dwExtraInfo = extra
    n = user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INPUT))
    if interval:
        time.sleep(interval)
    return n


def mouse_move(dx: int, dy: int, extra: int | None = None,
               interval: float = 0.01) -> int:
    """注入一次相对鼠标移动(无标记 = 与物理移动同判)。"""
    MOUSEEVENTF_MOVE = 0x0001
    ev = INPUT(type=INPUT_MOUSE)
    ev.union.mi.dx = dx
    ev.union.mi.dy = dy
    ev.union.mi.dwFlags = MOUSEEVENTF_MOVE
    ev.union.mi.dwExtraInfo = extra
    n = user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INPUT))
    if interval:
        time.sleep(interval)
    return n


def tap(vk: int = PROBE_VK, extra: int | None = None) -> int:
    """注入一对 down/up, 返回两次返回码之和(成功应为 2)。"""
    return key(vk, False, extra) + key(vk, True, extra)


def burst(n: int = 4, vk: int = PROBE_VK, extra: int | None = None,
          gap: float = 0.03, settle: float = 0.4) -> int:
    """注入 n 组 down/up, 返回返回码总和(成功应为 2n)。

    若总和为 0 说明注入整体失效 —— 调用方必须据此**放弃判定**,
    绝不能把"计数为 0"解释成"钩子收不到"。
    """
    sent = 0
    for _ in range(n):
        sent += tap(vk, extra)
        time.sleep(gap)
    time.sleep(settle)
    return sent


def require_injection(sent: int, expected: int) -> None:
    """注入不足就抛异常, 强制实验失败而不是产出假结论。"""
    if sent < expected:
        raise InjectionFailed(
            f"注入只成功 {sent}/{expected} 次 —— 观测无效。"
            f"GetLastError={ctypes.windll.kernel32.GetLastError()}"
            " (87=参数错误, 通常是 INPUT 结构尺寸不对)")
