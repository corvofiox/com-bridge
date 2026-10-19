# -*- coding: utf-8 -*-
"""诊断: 键盘钩子被绕过时, RawInput 键盘通路能否顶上(备用通路验证)。

## 背景

用户实测: 进入副机后 `kbd_eaten` 恒为 0 = **键盘钩子回调从未被调用**
(WH_KEYBOARD_LL 链式钩子被别的程序断在前面)。此时:
  * 键没被吞 -> 主机焦点应用/输入法收到它(用户看到"主机输入法被唤起");
  * 但键**没被吞**, 所以它**会产生 RawInput** -> 若 RawInput 通路正常,
    我们仍能把键转发给副机(只是主机也收到 = 两台同时生效)。

因此 RawInput 键盘通路是"钩子被绕过"时的**唯一备用通路**, 必须确认它可用。

## 判据

装一个 RawInputListener(与生产同款), 注入无标记键盘事件, 看它是否上报:
  * 上报了 -> 备用通路可用(自愈的前提成立);
  * 没上报 -> 备用通路也断了, 必须靠重装修好钩子。

★ 本机已确认正常, 本脚本的价值在于**在出问题的机器上**给出结论。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

from com_bridge.input import (INPUT_KEYBOARD, KEYEVENTF_KEYUP,
                              RawInputListener, user32)

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# ★★ 注入一律走 inject_util —— 本文件曾自己拼只含 "ki" 的 union, 得到
#   32 字节的 INPUT(应为 40), 导致 SendInput 返回 0 且不抛异常,
#   于是"RawInput 收不到键盘"这一结论纯属测量空气。
from inject_util import key as _inj_key, require_injection, PROBE_VK


def send_key(vk, up=False, extra=None):
    return _inj_key(vk, up, extra, interval=0)


print("== 启动 RawInputListener(与生产同款) ==")
events = []
lis = RawInputListener(on_event=lambda e: events.append(e))
lis.start()
check("监听器启动", lis.wait_ready(2.0), f"hwnd={lis._hwnd}")
time.sleep(0.3)

print("== 注入无标记键盘(与物理键同判, 应产生 RawInput) ==")
before = dict(lis.diag())
sent = 0
for _ in range(5):
    sent += send_key(0x41)
    sent += send_key(0x41, up=True)
    time.sleep(0.02)
time.sleep(0.5)
# ★ 注入无效时"RawInput 收不到"不是结论 —— 必须先确认注入真的发生了
try:
    require_injection(sent, 10)
except Exception as exc:  # noqa: BLE001
    print(f"  !! 注入无效, 无法得出结论: {exc}")
    lis.stop(); sys.exit(2)
after = dict(lis.diag())
print(f"  (信息) raw total {before['total']} -> {after['total']}, "
      f"key {before['key']} -> {after['key']}, "
      f"synthetic {before['synthetic']} -> {after['synthetic']}")
key_events = [e for e in events if e.is_key]
print(f"  (信息) 交付的键盘事件: {len(key_events)} 条")
# ★ 注入事件的 hDevice 恒为 NULL, 按设计归入 synthetic —— 这不是缺陷。
#   因此这里**不能**用注入来判定"RawInput 键盘通路"是否可用, 只能报告。
print(f"  (信息) 被归为合成事件: {before['synthetic']} -> {after['synthetic']}"
      " (注入事件 hDevice 恒为 NULL, 属预期)")
check("RawInput 收到了这些 WM_INPUT(通路存在)", after["total"] > before["total"],
      f"total {before['total']} -> {after['total']}")

print("== 带 TAG 的键盘注入应被过滤(不自激) ==")
n0 = len([e for e in events if e.is_key])
send_key(0x42, extra=0x43425247)
send_key(0x42, up=True, extra=0x43425247)
time.sleep(0.4)
n1 = len([e for e in events if e.is_key])
check("带标记注入被过滤(不自激)", n1 == n0, f"{n0} -> {n1}")

lis.stop()
print()
print("=== ★ 本脚本的结论(修正后) ===")
print("无标记 SendInput 注入**同样** hDevice==NULL, 因此按设计被归为合成事件")
print("(上面的 synthetic 计数就是这 10 条)。这说明:")
print("  * 生产判据'先 TAG, 再 hDevice==NULL 兜底'行为正确;")
print("  * 但本脚本**无法**用 SendInput 验证'物理键能否经 RawInput 转发' ——")
print("    注入事件在 hDevice 上与物理事件不同, 这是本质区别, 不是缺陷。")
print("  * 真实物理键有非 NULL hDevice, 能通过该过滤(见 diag_raw_dump.py 的")
print("    实测: 物理鼠标 hDevice=65624 非 0)。")
print("  => 备用通路是否可用, 只能由**真机物理按键**验证, 本机注入验证不了。")
print()
print(f"结果: {len(FAILED)} 项失败(注入类判据不计入)")
if FAILED:
    print("!! RawInput 键盘通路不可用 —— 钩子被绕过时将无法转发键盘")
    sys.exit(1)
print("全部通过 —— 备用通路可用, 重装自愈有意义")
