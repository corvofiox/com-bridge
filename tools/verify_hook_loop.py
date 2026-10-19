# -*- coding: utf-8 -*-
"""活体验证: 钩子线程的消息循环 + 重装自愈(BUG1 的运行时部分)。

前面那些 verify_* 都是"直接调回调"的纯逻辑验证, 它们**测不到**:
  1. _run() 里的消息循环是否真的分派了 WM_APP_KEY / WM_APP_REINSTALL;
     (PostThreadMessageW 投递的消息 hwnd 为 NULL, 若只靠 DispatchMessageW
      就会被丢弃 —— 这正是本次改动的关键落点)
  2. 重装路径在真实 SetWindowsHookEx 下能否工作。

做法: 真启动 LowLevelHook, 用 PostThreadMessageW 投递消息, 观察
_drain_key_queue / _reinstall 是否被消息循环调用。

⚠ 本脚本**不吞任何真实输入**: 全程 relay=False(默认), 钩子只观察。
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")

from com_bridge import hook as hook_mod
from com_bridge.hook import LowLevelHook
from com_bridge.protocol import EV_KEY, KEY_DOWN

FAILED = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


print("== 1. 钩子安装 ==")
produced = []
h = LowLevelHook(on_key_event=lambda *a: produced.append(a))
ok = h.start()
check("钩子安装成功", ok, f"last_error={h.last_error}")
if not ok:
    print("\n!! 钩子装不上(可能被 Deskflow/Synergy 占用), 无法继续")
    sys.exit(1)

print("== 2. 消息循环唤醒独立转发线程(核心: 不在钩子线程里做重活) ==")
# 手工塞一个事件进队列, 然后靠**消息循环**唤醒 worker —— 不手工调 drain
with h._key_lock:
    h._key_queue.append((EV_KEY, KEY_DOWN, 0x41, 0x1E, 0, 0))
hook_mod.user32.PostThreadMessageW(h._thread_id, hook_mod.WM_APP_KEY, 0, 0)
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline and not produced:
    time.sleep(0.02)
check("消息循环唤醒了转发线程并交付事件", bool(produced),
      f"produced={produced}")
check("事件内容正确", produced and produced[0][:3] == (EV_KEY, KEY_DOWN, 0x41),
      f"got {produced}")
check("转发线程存活", h.worker_alive)

print("== 3. 消息循环分派 WM_APP_REINSTALL(自愈路径) ==")
before = h.stats.get("reinstalls", 0)
h.request_reinstall()
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline and \
        h.stats.get("reinstalls", 0) <= before:
    time.sleep(0.02)
check("重装被消息循环执行", h.stats.get("reinstalls", 0) > before,
      f"reinstalls {before} -> {h.stats.get('reinstalls', 0)}")
check("重装后仍处于已安装状态", h.installed)

print("== 4. 重装后钩子仍能工作 ==")
produced.clear()
with h._key_lock:
    h._key_queue.append((EV_KEY, KEY_DOWN, 0x42, 0x30, 0, 0))
hook_mod.user32.PostThreadMessageW(h._thread_id, hook_mod.WM_APP_KEY, 0, 0)
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline and not produced:
    time.sleep(0.02)
check("重装后事件仍能送达", produced and produced[0][2] == 0x42,
      f"got {produced}")

print("== 5. relay 语义(不吞真实输入) ==")
check("全程 relay=False", h.relay is False)
check("diag 可读", isinstance(h.diag(), dict), f"{h.diag()}")

h.stop()
print()
if FAILED:
    print(f"!! {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("全部通过")
