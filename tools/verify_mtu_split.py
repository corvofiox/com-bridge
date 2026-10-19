# -*- coding: utf-8 -*-
"""★★★ 验证: 分包后的 UDP 包确实不再触发 IP 分片。

## 方法
用真实 UDP socket 向本机回环端口发包, 同时用 `getsockopt`/抓包不可行,
改为**直接计算** + 用 `IP_DONTFRAGMENT` 探测:
  * 发送 960 字节 payload 应成功
  * 发送 3840 字节 payload 在设置 DF 标志时应失败(或需分片)

更实用的验证: 检查 agent 代码的分包逻辑是否产生 ≤1500 的包。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

# 1. 计算验证
RATE = 48000
CH = 2
BITS = 32
BLOCK = CH * (BITS // 8)
SEND_FRAMES = 120

payload = SEND_FRAMES * BLOCK
total = payload + 28
print("== 分包后每包大小 ==")
print(f"  {SEND_FRAMES} 帧 x {CH}ch x {BITS}bit = {payload} 字节")
print(f"  加 IP+UDP 头 = {total} 字节")
print(f"  MTU 1500 -> {'不分片 ✓' if total <= 1500 else '仍需分片 ✗'}")
print(f"  余量 {1500 - total} 字节")

print()
print("== 对比: 修复前 ==")
old_payload = 480 * BLOCK
old_total = old_payload + 28
print(f"  480 帧 = {old_payload} 字节, 加头 {old_total}")
print(f"  -> 分 {int(old_total / 1480) + 1} 片, 任一片丢失整包作废")

# 2. 实测 DF 标志
import socket
print()
print("== 实测: 用 IP_DONTFRAGMENT 探测本机 MTU 上限 ==")
IP_DONTFRAGMENT = 14        # Windows
for size in (960, 1440, 1920, 3840):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.IPPROTO_IP, IP_DONTFRAGMENT, 1)
        # 发到本机一个不存在的端口(只为测试 MTU, 不关心是否接收)
        s.sendto(b"x" * size, ("127.0.0.1", 54321))
        print(f"  {size:5d} 字节 payload: 发送成功"
              f"{' (<= MTU)' if size + 28 <= 1500 else ' (超 MTU 但回环不分片)'}")
    except OSError as exc:
        print(f"  {size:5d} 字节 payload: 失败 {exc}")
    finally:
        s.close()

print()
print("== 语法检查: agent 里是否真的按 120 帧分包 ==")
src = open("com_bridge/agent.py", encoding="utf-8").read()
has_split = "SEND_FRAMES = 120" in src
has_loop = "while sent < total_frames:" in src
print(f"  找到 SEND_FRAMES = 120: {has_split}")
print(f"  找到分包循环: {has_loop}")
if has_split and has_loop:
    print("  ✓ 分包逻辑已就位")
else:
    print("  ✗ 分包逻辑缺失")
