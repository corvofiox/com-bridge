# -*- coding: utf-8 -*-
"""★ 复算: 为什么水位能到 490ms(而 max_ms=400)?

## 用户实测
"水位延迟会随着时间逐渐增加, 一直加到 490ms 就不往上走了"

## 代码事实
AlignedStreamBuffer(max_ms=400.0), push() 里:
    max_frames = int(rate * max_ms / 1000)      # 400ms 的帧数
    while self._buffered_frames() > max_frames and len(self._buf) > 1:
        popleft(); dropped += 1

理论上水位应被钳在 400ms(每帧 10ms => 最多 410ms 左右)。

## 疑点
1. 显示的是 **buffered_ms()** 还是别的量?
2. `_buffered_frames()` 用 sum(len(p)) // block_align —— 若有半帧残留会放大?
3. 是否 pop_aligned 里 `self._buf[0] = (ts, pcm[keep:])` 留下了**很小但很多**
   的碎片, 使 len(p) // block_align 为 0 但 sum(len) 不为 0?
   => _buffered_frames 用 sum(len)//block_align, 碎片会被整除掉, 不会放大。

## 本工具直接模拟 push/pop 组合, 看水位上界到底在哪
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.audio import AlignedStreamBuffer

RATE = 48000
BLOCK = 8          # 2ch * 32bit
FRAME_MS = 10.0
FRAMES = int(RATE * FRAME_MS / 1000)      # 每包帧数
PCM = b"\x00" * (FRAMES * BLOCK)

print("== 实验 1: 只 push 不 pop(水位上限) ==")
buf = AlignedStreamBuffer(channels=2, block_align=BLOCK, bits=32,
                          rate=RATE, name="t", target_ms=60.0,
                          min_ms=20.0, max_ms=400.0)
t0 = time.monotonic()
for i in range(200):          # 200 包 = 2 秒
    buf.push(int(time.time() * 1e6), PCM)
print(f"  push 200 包后 buffered_ms = {buf.buffered_ms():.1f} ms")
print(f"  dropped = {buf.dropped}")
print(f"  (max_ms=400 -> 期望 ≈ 400~410)")

print()
print("== 实验 2: push 快于 pop(pop 每 15ms 一次, push 每 10ms 一次) ==")
buf2 = AlignedStreamBuffer(channels=2, block_align=BLOCK, bits=32,
                           rate=RATE, name="t2", target_ms=60.0,
                           min_ms=20.0, max_ms=400.0)
# 模拟: 消费速率比到达慢 33% -> 水位必然顶到上限
for round_ in range(600):
    buf2.push(int(time.time() * 1e6), PCM)
    if round_ % 3 == 0:      # 每 3 个 push 消费 2 包
        buf2.pop_aligned(int(time.time() * 1e6), FRAMES * 2,
                         lookahead_us=0, ratio_override=1.0)
        buf2.pop_aligned(int(time.time() * 1e6), FRAMES * 2,
                         lookahead_us=0, ratio_override=1.0)
print(f"  600 轮后 buffered_ms = {buf2.buffered_ms():.1f} ms")
print(f"  dropped = {buf2.dropped}, underruns = {buf2.underruns}")
print(f"  (若消费慢于到达, 应稳定在 max_ms 附近)")

print()
print("== 实验 3: 消费速率与到达相同(push 10ms, pop 10ms) ==")
buf3 = AlignedStreamBuffer(channels=2, block_align=BLOCK, bits=32,
                           rate=RATE, name="t3", target_ms=60.0,
                           min_ms=20.0, max_ms=400.0)
for round_ in range(600):
    buf3.push(int(time.time() * 1e6), PCM)
    buf3.pop_aligned(int(time.time() * 1e6), FRAMES,
                     lookahead_us=0, ratio_override=1.0)
print(f"  600 轮后 buffered_ms = {buf3.buffered_ms():.1f} ms")
print(f"  dropped={buf3.dropped} underruns={buf3.underruns}")
print(f"  (速率相等应稳定, 不增长)")

print()
print("== 结论 ==")
print("  若实验 1 的上界 ≈ max_ms(400), 而用户看到 490,")
print("  说明用户看到的不是这个 buffer 的 buffered_ms, 或者")
print("  渲染端把水位统计在了别处(见 gui 的显示来源)。")
