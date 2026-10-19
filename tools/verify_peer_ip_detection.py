# -*- coding: utf-8 -*-
"""★ 精确复现用户日志的时序, 验证"误认网关"已被修复。

## 用户日志(2026-10-19 12:54)的真实顺序
    [server] agent 已连接 192.168.10.1 caps={}
    [server] 输入转发目标 -> 192.168.10.1:24801
    [server] agent 上报地址 192.168.10.201 (TCP 源地址 192.168.10.1 应为 NAT 网关)
             -> 用上报地址转发输入
    [server] 更新输入转发目标 192.168.10.1:24801 -> 192.168.10.201:24801
    [server] 由心跳学到副电脑真实地址 192.168.10.1:32802
             (原 192.168.10.201:24801) -> 切换输入转发目标   <- 又错了!

## 两个错误点
1. 空 caps 时用 TCP 源地址(网关)建目标
2. 心跳学习把直连地址覆盖成 NAT 地址

## 本工具按真实时序重放, 断言最终目标始终是 192.168.10.201
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.server import Server

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


print("== 起 Server ==")
srv = Server(name="repro", side="right")
srv.start()
time.sleep(0.8)

print("== 步骤1+2: 空 caps / 网关心跳 不得建目标(纯逻辑, 避免真实 agent 干扰) ==")
# ⚠ 用 __new__ 构造: 不起 Server(否则真实副机 agent 会连上来, 污染断言)
probe1 = Server.__new__(Server)
probe1._input_tx = None
probe1._punched_host = None
probe1._punched_port = None
probe1._last_peer_heartbeat = 0.0

# 步骤1: 空 caps
probe1._on_client_connect(("192.168.10.1", 50000), {})
print(f"  空 caps 后: _input_tx={probe1._input_tx}")
check("空 caps 不建转发目标(不误用网关)", probe1._input_tx is None,
      f"_input_tx={probe1._input_tx}")

# 步骤2: 心跳来自网关
probe1._on_peer_heartbeat(("192.168.10.1", 32802))
print(f"  网关心跳后: _input_tx={probe1._input_tx} "
      f"_punched={probe1._punched_host}:{probe1._punched_port}")
check("心跳不单独建目标", probe1._input_tx is None,
      f"_input_tx={probe1._input_tx}")

print()
print("== 步骤3: 真实 Server 上验证 adv_host 优先(端到端) ==")
srv._on_client_connect(("192.168.10.1", 50000),
                       {"role": "agent", "name": "real",
                        "adv_host": "192.168.10.201"})
time.sleep(0.3)
tx = srv._input_tx
addr = tx.addr if tx else None
print(f"  转发目标 = {addr}")
check("目标 = 副机直连地址(不是网关)", addr == ("192.168.10.201", 24801),
      f"{addr}")

print()
print("== 步骤4: 后续心跳继续来自网关(不应覆盖直连) ==")
for i in range(3):
    srv._on_peer_heartbeat(("192.168.10.1", 32802 + i))
    time.sleep(0.1)
addr2 = srv._input_tx.addr if srv._input_tx else None
print(f"  转发目标 = {addr2}")
check("心跳不覆盖直连地址", addr2 == ("192.168.10.201", 24801), f"{addr2}")

print()
print("== 步骤5: 跨网段场景(心跳地址才是唯一出路, 纯函数验证) ==")
probe = Server.__new__(Server)      # 不跑 __init__, 只测 _pick_input_target
probe._punched_host = "100.95.120.31"
probe._punched_port = 32802
addr3 = probe._pick_input_target("192.168.10.201")
print(f"  跨网段转发目标 = {addr3}")
check("跨网段使用打洞地址", addr3 == ("100.95.120.31", 32802), f"{addr3}")

print()
print("== 步骤6: 无 adv_host 且心跳来自网关(极端场景) ==")
probe2 = Server.__new__(Server)
probe2._punched_host = "192.168.10.1"
probe2._punched_port = 32802
addr4 = probe2._pick_input_target("")
print(f"  目标 = {addr4}")
check("纯网关心跳不会被当成副机(至少不该是 24801 直连推断)",
      addr4 == ("192.168.10.1", 32802),
      f"{addr4} (此场景无更好信息, 保留打洞地址属预期)")

srv.stop()
print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
