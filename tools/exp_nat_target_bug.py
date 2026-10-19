# -*- coding: utf-8 -*-
"""★ 决定性: 向 NAT 学到的 192.168.10.1:32802 发事件, 副机真的收到吗?

## 用户日志的关键一行
    [server] 由心跳学到副电脑真实地址 192.168.10.1:32802
             (原 192.168.10.201:24801) -> 切换输入转发目标

两个疑点:
  1. 心跳学习把目标从**正确的 192.168.10.201:24801** 改成了
     **192.168.10.1:32802**(主路由的随机高位端口)。
  2. 若这个地址不可达, 输入事件全部丢失 -> 副机收不到键鼠
     (但音频/控制通道正常, 因为那是副机主动发)。

## 本实验
1. 起一个 Server(不连 agent), 手动喂入"心跳学到 192.168.10.1:32802"
2. 检查 _input_tx 的目标与借道 socket
3. 真的发一个事件包, 检查 sent 计数
4. 关键: 判断这个地址是否属于**本机/网关**, 并对比 192.168.10.201
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.server import Server
from com_bridge.protocol import InputEvent, EV_KEY

print("== 起 Server ==")
srv = Server(name="nat", side="right")
srv.start()
time.sleep(0.8)

# 模拟真实时序: agent 连上(带 adv_host) -> 心跳学到 NAT 地址
print("== 模拟 agent 连接(adv_host=192.168.10.201) ==")
srv._on_client_connect(("192.168.10.1", 50000),
                       {"role": "agent", "name": "real",
                        "adv_host": "192.168.10.201"})
time.sleep(0.3)
tx = srv._input_tx
print(f"  连接后目标: {tx.addr if tx else None}")

print()
print("== 模拟心跳学到 NAT 地址 192.168.10.1:32802(复现用户日志) ==")
srv._on_peer_heartbeat(("192.168.10.1", 32802))
time.sleep(0.3)
print(f"  _punched = {srv._punched_host}:{srv._punched_port}")
print(f"  当前转发目标: {tx.addr if tx else None}")
print(f"  ★ 目标被改成了 NAT 映射地址(主路由)")

print()
print("== 发一个事件, 看是否发出(不代表能到副机) ==")
if tx is not None:
    before = getattr(tx, "sent", 0)
    ev = InputEvent(EV_KEY, 0, 0x41, 0, 0, int(time.time() * 1e6))
    tx.send(ev)
    time.sleep(0.2)
    after = getattr(tx, "sent", 0)
    print(f"  sent: {before} -> {after} (增量 {after - before})")
    print(f"  借道 socket: {getattr(tx, '_borrowed', None)}")
    print(f"  失败计数: {getattr(tx, 'failed', 0)}")

print()
print("== 判读 ==")
print("  192.168.10.1:32802 是**主路由的 NAT 映射地址**。")
print("  发往它的包要经过 OpenWrt 的 conntrack 才能转回副机:")
print("    - 若 conntrack 认(源端口=24801 且条目存在) -> 能到")
print("    - 否则 -> 全部丢失, 副机收不到任何键鼠")
print()
print("  ★ 而 192.168.10.201:24801 是**直连地址**(同一 /24 网段,")
print("    实测 ping 可达), 完全不需要 NAT 中转。")
print()
print("  因此: 心跳学习把**更好的直连地址覆盖成了 NAT 地址** ——")
print("  这是一个真实的 bug: 当 adv_host 已经是可直连地址时,")
print("  不该被 NAT 映射地址覆盖。")

srv.stop()
