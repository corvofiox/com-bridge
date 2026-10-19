# -*- coding: utf-8 -*-
"""★★★ 最终验证: 检查剪贴板与输入是否真的完全隔离。

## 检查项
1. 各接收器的 socket 是否独立(不共享类属性/实例属性)
2. 同时启动所有接收器时, 端口是否各自独立、互不干扰
3. 输入事件在剪贴板收发活跃时是否仍能被正确处理
"""
from __future__ import annotations

import socket
import sys
import time
import traceback

sys.path.insert(0, ".")

from com_bridge.transport import (ClipboardReceiver, InputReceiver)

print("== 1. socket 是否独立 ==")
ir = InputReceiver(port=25801, on_event=lambda e: None)
cr = ClipboardReceiver(port=25803, on_payload=lambda p: None)
print(f"  InputReceiver._sock     = {ir._sock}")
print(f"  ClipboardReceiver._sock = {cr._sock}")
print(f"  都是 None(未绑定): {ir._sock is None and cr._sock is None}")

ir.bind_now()
cr.bind_now()
print(f"  绑定后 Input._sock  = {ir._sock}")
print(f"  绑定后 Clip._sock   = {cr._sock}")
print(f"  ★ 是否同一个对象: {ir._sock is cr._sock}")

print()
print("== 2. 端口是否各自独立 ==")
print(f"  Input 端口     = {ir.port}")
print(f"  Clipboard 端口 = {cr.port}")
try:
    ip = ir._sock.getsockname()
    cp = cr._sock.getsockname()
    print(f"  Input 实际绑定   = {ip}")
    print(f"  Clip 实际绑定    = {cp}")
    print(f"  ★ 端口是否相同: {ip[1] == cp[1]}")
except OSError as exc:
    print(f"  查询失败: {exc}")

print()
print("== 3. 同时运行, 各收各的 ==")
got_input = []
got_clip = []
ir2 = InputReceiver(port=25811, on_event=lambda e: got_input.append(e))
cr2 = ClipboardReceiver(port=25813, on_payload=lambda p: got_clip.append(p))
ir2.bind_now()
cr2.bind_now()
ir2.start()
cr2.start()
time.sleep(0.3)

from com_bridge import clipboard_sync as cs
from com_bridge.protocol import EV_KEY, KEY_DOWN, InputEvent
from com_bridge.transport import ClipboardSender, InputSender

# 发一个输入事件到 25811
i_tx = InputSender("127.0.0.1", 25811)
ev = InputEvent(EV_KEY, KEY_DOWN, 0x41, 0x1E, 0, time.monotonic_ns() // 1000)
i_tx.send(ev)

# 同时发剪贴板到 25813
c_tx = ClipboardSender("127.0.0.1", 25813)
c_tx.send_blob(cs._encode({"text": b"hello"}), 1)
time.sleep(0.6)

print(f"  输入端收到: {len(got_input)} 个事件")
print(f"  剪贴板端收到: {len(got_clip)} 份内容")
print(f"  ★ 输入事件被剪贴板抢走: {len(got_input) == 0 and len(got_clip) > 0}")
print(f"  ★ 剪贴板被输入抢走: {len(got_clip) == 0 and len(got_input) > 0}")

print()
print("== 4. 反向: 剪贴板活跃时输入是否仍能处理 ==")
got_input2 = []
ir3 = InputReceiver(port=25821, on_event=lambda e: got_input2.append(e))
ir3.bind_now()
ir3.start()
time.sleep(0.2)
i_tx2 = InputSender("127.0.0.1", 25821)
# 一边狂发剪贴板, 一边发输入
for k in range(20):
    c_tx.send_blob(cs._encode({"text": bytes([65 + k]) * 500}), k)
    time.sleep(0.005)
i_tx2.send(ev)
time.sleep(0.8)
print(f"  剪贴板压力下, 输入事件收到: {len(got_input2)}")

for o in (i_tx, i_tx2, c_tx, ir2, cr2, ir3, ir, cr):
    try:
        o.stop() if hasattr(o, "stop") else o.close()
    except Exception:  # noqa: BLE001
        pass
print("  已清理")
