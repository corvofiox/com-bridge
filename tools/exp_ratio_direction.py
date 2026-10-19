# -*- coding: utf-8 -*-
"""★★★ 决定性实验: ratio 变大时, 队列水位是升还是降?

## 代码里存在**方向矛盾**
1. `pop_aligned`: `input_budget = int(max_frames * max(0.9, eff_ratio))`
   -> ratio 越大, 从队列里取的**输入帧越多** -> 水位应**下降**
2. `Resampler.process(pcm, ratio)`: 文档说"ratio > 1 表示读得更快,
   产出变少"
   -> 同样的输入, ratio 越大产出越少 -> 灌进 WASAPI 的更少
      -> 下一轮 avail 更大 -> 又会多取 -> 间接消耗队列

3. 控制器: `pi_err = (level - target)/target`;
   水位 > 目标时 `_ratio_integral += excess` -> ratio 变大
   若 ratio 变大真的降水位, 这就是**负反馈**(正确)
   若 ratio 变大反而升水位, 就是**正反馈**(发散!)

## 本实验直接测: 固定输入, 不同 ratio 下队列消费速度
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.audio import AlignedStreamBuffer

RATE = 48000
BLOCK = 8
FRAMES = 480              # 10ms 包
PCM = b"\x00" * (FRAMES * BLOCK)

print("== 实验: 相同 push 量, 不同 ratio, 看队列剩余水位 ==")
print("   (每轮: push 1 包(10ms) + pop 固定 max_frames)")
print()

for ratio in (0.985, 1.0, 1.015):
    buf = AlignedStreamBuffer(channels=2, block_align=BLOCK, bits=32,
                              rate=RATE, name=f"r{ratio}",
                              target_ms=60.0, max_ms=100_000.0)  # 不限丢弃
    max_frames = 480                 # 每轮只允许产出 10ms
    total_out = 0
    for i in range(200):             # 200 轮 = 2 秒的 push
        buf.push(i * 10_000, PCM)
        out = buf.pop_aligned(i * 10_000, max_frames, 0,
                              ratio_override=ratio)
        if out:
            total_out += len(out) // BLOCK
    print(f"  ratio={ratio:.3f}: 水位={buf.buffered_ms():7.1f}ms  "
          f"总产出={total_out:6d}帧  欠载={buf.underruns}")

print()
print("== 判读 ==")
print("  若 ratio 越大 -> 水位越低: 控制器方向**正确**")
print("  若 ratio 越大 -> 水位越高: 控制器是**正反馈**, 必然发散!")
