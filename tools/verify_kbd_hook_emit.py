# -*- coding: utf-8 -*-
"""回归验证: 键盘链路 —— 钩子自产事件 -> Server 转发 -> Agent 注入。

覆盖 tools/diag_hook_kbd_rawinput.py 实测出的根因: 被键盘钩子吞掉的
按键不产生 RawInput, 必须由钩子回调自产 InputEvent。

不依赖真实桌面/钩子安装, 纯逻辑层验证(可在 CI 跑)。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from com_bridge.hook import LowLevelHook
from com_bridge.protocol import (
    EV_KEY,
    EV_MOUSE_MOVE_REL,
    InputEvent,
    KEY_DOWN,
    KEY_EXTENDED,
    KEY_UP,
)
from com_bridge.server import Server

FAILED = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------- 纯逻辑
print("== decide_key_eat 基线(不变) ==")
from com_bridge.hook import PASSTHROUGH_VKS, decide_key_eat
check("relay=False 放行", not decide_key_eat(False, 0x41, 0))
check("relay=True 吞普通键", decide_key_eat(True, 0x41, 0))
check("Caps 指示灯键放行", not decide_key_eat(True, 0x14, 0))
check("TAG 注入放行", not decide_key_eat(True, 0x41, 0x43425247))

# ---------------------------------------------------------------- _kbd_proc
print("== LowLevelHook._kbd_proc 自产键盘事件 ==")

hook = LowLevelHook(on_key_event=None)
produced = []
hook._relay = True
hook._on_key_event = lambda *a: produced.append(a)


def make_kbd_ref():
    """直接用 _HOOKPROC 包装 _kbd_proc, 构造 KBDLLHOOKSTRUCT 内存调用。"""
    return hook._kbd_ref


import ctypes
from com_bridge.hook import KBDLLHOOKSTRUCT, _HOOKPROC

info = KBDLLHOOKSTRUCT()
info.vkCode = 0x41          # 'A'
info.scanCode = 0x1E
info.flags = 0              # 非扩展键
info.dwExtraInfo = 0        # 无 TAG(物理键)
ref = _HOOKPROC(hook._kbd_proc)  # 常驻, 防止测试期间被 GC

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105


def feed(msg):
    """调一次回调并**同步**排空队列(测试专用确定性路径)。

    ★ 回调只**入队**(BUG1: 回调里做转发工作会超时被系统静默摘钩), 真正的
      交付在生产中由**独立转发线程** `_key_worker_loop` 完成(不能在钩子
      线程里做, 否则键盘钩子会被拖到超时被摘)。验收要确定性, 故用
      `drain_sync()` 在当前线程排空。
    """
    r = ref(0, msg, ctypes.addressof(info))
    hook.drain_sync()
    return r


produced.clear()
r = feed(WM_KEYDOWN)
check("吞掉返回 1", r == 1)
check("down 事件自产", len(produced) == 1 and
      produced[0][:3] == (EV_KEY, KEY_DOWN, 0x41),
      f"got {produced}")
check("扫描码承载在 a 字段", produced and produced[0][3] == 0x1E)

produced.clear()
feed(WM_KEYUP)
check("up 事件自产", len(produced) == 1 and
      produced[0][:3] == (EV_KEY, KEY_UP, 0x41), f"got {produced}")

# 扩展键: 右 Ctrl 走 LLKHF_EXTENDED(0x01)
info.vkCode = 0xA3          # VK_RCONTROL
info.scanCode = 0x1D
info.flags = 0x01           # LLKHF_EXTENDED
produced.clear()
feed(WM_KEYDOWN)
check("扩展键带 KEY_EXTENDED", produced and
      produced[0][1] & KEY_EXTENDED, f"got {produced}")

# 系统键: Alt+F4 走 WM_SYSKEYDOWN/WM_SYSKEYUP
info.vkCode = 0x73          # F4
info.scanCode = 0x3E
info.flags = 0x20           # LLKHF_ALTDOWN
produced.clear()
feed(WM_SYSKEYDOWN)
check("WM_SYSKEYDOWN → KEY_DOWN", produced and
      produced[0][1] & KEY_DOWN and not (produced[0][1] & KEY_UP))
feed(WM_SYSKEYUP)
check("WM_SYSKEYUP → KEY_UP", produced[-1][1] & KEY_UP)

# ★ BUG1: 回调本身必须只入队, 不得直接交付
produced.clear()
hook._key_queue.clear()
ref(0, WM_KEYDOWN, ctypes.addressof(info))    # 故意不排空
check("回调只入队不直接交付(BUG1)", not produced and hook._key_queue,
      f"produced={produced} queued={len(hook._key_queue)}")
hook._key_queue.clear()

# 放行路径不产生事件
hook._relay = False
produced.clear()
r = feed(WM_KEYDOWN)
check("relay=False 放行(不吞不产事件)", r != 1 and not produced)

# 回调异常不外泄、仍吞掉
def boom(*a):
    raise RuntimeError("boom")


hook._relay = True
hook._on_key_event = boom
r = feed(WM_KEYDOWN)
check("回调异常时仍返回 1(吞)", r == 1)

del ref  # 解除常驻引用

# ---------------------------------------------------------------- 接线
print("== Server 接线(on_key_event -> _on_input_event) ==")

import inspect

src = inspect.getsource(Server._ensure_hook)
check("_ensure_hook 传入 on_key_event", "on_key_event=" in src)
check("接的是 _on_hook_key_event", "_on_hook_key_event" in src)
src2 = inspect.getsource(Server._on_hook_key_event)
check("组装 InputEvent 喂 _on_input_event", "_on_input_event" in src2
      and "InputEvent(" in src2)

# 端到端逻辑: 伪造 Server, 验证钩子自产事件走完整转发链
srv = Server.__new__(Server)
srv._edge = None
srv._input_tx = None
sent = []


class FakeTx:
    def send(self, ev):
        sent.append(ev)


srv.stats = {"events_forwarded": 0}


class FakeEdge:
    is_outside = True
    self_rect = None


srv._edge = FakeEdge()
srv._input_tx = FakeTx()
ev = InputEvent(EV_KEY, KEY_DOWN, 0x41, 0x1E, 0, 123456)
srv._on_hook_key_event(ev.type, ev.flags, ev.code, ev.a, ev.b, ev.ts_us)
check("自产事件经 _on_input_event 转发", len(sent) == 1 and sent[0].is_key,
      f"sent={sent}")

print()
if FAILED:
    print(f"!! {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("全部通过")
