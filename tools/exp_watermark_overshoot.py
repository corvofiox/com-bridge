# -*- coding: utf-8 -*-
"""★ 为什么水位能突破 max_ms=400 到 490?

## 已确认
* GUI 显示的确实是 `_peer.buffered_ms()`
* AlignedStreamBuffer 默认 `max_ms=400.0`
* push() 里理论上会把水位钳在 400ms

## 疑点排查
`push()`:
    max_frames = int(raw * max_ms / 1000)
    while self._buffered_frames() > max_frames and len(self._buf) > 1:
        self._buf.popleft()

`_buffered_frames()`:
    return sum(len(p) for _, p in self._buf) // self.block_align

★ 关键: `while` 条件是 `len(self._buf) > 1` —— 只有**至少 2 个包**才丢。
  若单个包特别大(例如 agent 一次发送 200ms 的数据), 队列只有 1 个包时
  即使水位远超 max_frames 也不会丢！
  => 若 agent 的包很大(比如累积后一次发 100~200ms),
     水位上界 = max_ms + 单个最大包 —— 400 + 90 = 490 完全对得上!

## 本工具验证
模拟"包很大"的场景(每包 90ms), 看水位上界是多少。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.audio import AlignedStreamBuffer

RATE = 48000
BLOCK = 8          # 2ch * 32bit

print("== 实验: 不同包大小下的水位上界(max_ms=400) ==")
for frame_ms in (10.0, 50.0, 90.0, 150.0, 200.0):
    frames = int(RATE * frame_ms / 1000)
    pcm = b"\x00" * (frames * BLOCK)
    buf = AlignedStreamBuffer(channels=2, block_align=BLOCK, bits=32,
                              rate=RATE, name=f"p{int(frame_ms)}",
                              target_ms=60.0, max_ms=400.0)
    for _ in range(300):        # 只 push, 必然顶到上限
        buf.push(int(time.time() * 1e6), pcm)
    print(f"  每包 {frame_ms:5.0f}ms -> 水位上界 {buf.buffered_ms():6.1f}ms "
          f"(max_ms=400, dropped={buf.dropped})")

print()
print("== 结论 ==")
print("  若上界 = 400 + 包长, 说明 `len(self._buf) > 1` 是元凶:")
print("  队列里最后一包永远不会被丢, 于是水位可以超出 max_ms 一个包长。")
print("  用户看到 490ms ≈ 400 + 90ms 包 -> 与实测吻合。")
