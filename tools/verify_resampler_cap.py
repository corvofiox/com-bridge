# -*- coding: utf-8 -*-
"""验证 Resampler 内部缓冲上限是否真的生效(长时间驱动)。

exp_resampler_growth 只跑 200 轮(2 秒), 缓冲从 96 涨到 1530 还没到
8192 的上限就有 1530 —— 说明增长很快。本工具跑 2000 轮(20 秒),
确认:
  1. 修复前: 缓冲无界增长到很大
  2. 修复后: 被 MAX_INTERNAL(8192) 截住
  3. 截断时输出是否仍连续(不能有跳变)
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import Resampler

RATE = 48000
CH = 2
BLOCK = CH * 4


def run(ratio: float, rounds: int = 2000) -> tuple[int, int, int, float]:
    rs = Resampler(channels=CH, block_align=BLOCK, bits=32)
    phase = 0.0
    peak = 0
    prev = None
    max_jump = 0.0
    jumps = 0
    for i in range(rounds):
        t = (np.arange(480, dtype=np.float64) + phase) / RATE
        wave = 0.3 * np.sin(2 * np.pi * 440.0 * t)
        stereo = np.repeat(wave.astype(np.float32)[:, None], CH, axis=1)
        phase += 480
        out = rs.process(stereo.tobytes(), ratio, max_out=480)
        if rs._buf is not None:
            peak = max(peak, len(rs._buf))
        if out:
            arr = np.frombuffer(out, dtype=np.float32)[::CH]
            if prev is not None:
                d = abs(float(arr[0]) - prev)
                max_jump = max(max_jump, d)
                if d > 0.1:
                    jumps += 1
            prev = float(arr[-1])
    final = len(rs._buf) if rs._buf is not None else 0
    return peak, final, jumps, max_jump


print("== 2000 轮(约 20 秒音频)驱动 ==")
for ratio in (0.985, 1.0, 1.015):
    peak, final, jumps, mj = run(ratio)
    print(f"  ratio={ratio:.3f}: 峰值={peak:6d} 末尾={final:6d} "
          f"跳变={jumps} 最大跳变={mj:.4f}")

print()
print("== 判读 ==")
print("  峰值应 <= 8192(MAX_INTERNAL); 跳变应为 0(截断处不能断波形)")
