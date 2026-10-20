# -*- coding: utf-8 -*-
"""验证重采样器的右端越界诊断计数: _right_edge_hits 是否为 0?

## 为什么测这个
代码里有一处专门的注释 (com_bridge/audio.py `_interp_sinc`):
    "若窗口**右端越界**(i0+8 >= len): 必须是**上游保证不发生**,
     否则每次块边界都会丢抽头 -> **周期性咔哒声**。"

配套还有诊断计数器 `_right_edge_hits`, 但它**从未被输出到日志**,
所以没人知道它到底是 0 还是非 0 —— 这正是"周期性噼啪声"长期
查不出根因的原因: 判据存在但没人看。

## 测法
用**生产同样的调用序列**驱动 Resampler:
  * ratio 在生产范围(0.99~1.01)内缓慢变化
  * 每轮输入块大小按生产的节奏变化(2ms/10ms/20ms 混合)
  * 每轮 max_out 按生产的 budget 逻辑给
然后读 `_right_edge_hits` 与 `_starved_frames`。

若 _right_edge_hits > 0 -> 找到了根因: 抽头窗口右端被零填充,
   每次块边界产生一个台阶 = 周期性咔哒声。
若 == 0 -> 右翼安全, 根因在别处(上游/网络/渲染缓冲)。

用法: python tools/diag_resampler_edges.py
"""
from __future__ import annotations

import math
import sys
import time

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import Resampler


def main() -> int:
    rate = 48000
    ch = 2
    rs = Resampler(channels=ch, block_align=4 * ch, bits=32)

    # 生成 1kHz 正弦(连续相位), 便于观察台阶
    t = 0.0
    dt = 1.0 / rate
    phase = 0.0

    def make(frames: int) -> bytes:
        nonlocal t, phase
        w = 2 * math.pi * 1000.0
        arr = np.empty((frames, ch), dtype=np.float32)
        for i in range(frames):
            arr[i, 0] = math.sin(w * t)
            arr[i, 1] = math.sin(w * t) * 0.5
            t += dt
        return arr.tobytes()

    # 生产节奏: ratio 在 ±1% 内缓慢摆动, 块大小混合
    ratios = []
    for i in range(4000):
        ratios.append(1.0 + 0.008 * math.sin(i * 0.003))

    # 块大小序列: 2ms(渲染轮询最坏情况) / 10ms / 20ms 混合
    block_sizes = [96, 480, 960, 96, 480, 96, 960, 480]

    max_out_seq = []
    total_out = 0
    for i, r in enumerate(ratios):
        n = block_sizes[i % len(block_sizes)]
        pcm = make(n)
        # 模拟上游 budget: 产出不超过本轮可用
        max_out = 480
        out = rs.process(pcm, r, max_out=max_out)
        if out:
            frames_out = len(out) // (4 * ch)
            total_out += frames_out
            max_out_seq.append(frames_out)

    right = getattr(rs, "_right_edge_hits", 0)
    starved = rs._starved_frames
    backlog_resets = getattr(rs, "_backlog_resets", 0)
    print()
    print("=== 重采样器边界诊断 ===")
    print(f"  产出总帧数      : {total_out}")
    print(f"  右端越界次数    : {right}   <-- 必须为 0")
    print(f"  饥饿帧数        : {starved:.1f}")
    print(f"  积压回退次数    : {backlog_resets}")
    print(f"  内部缓冲残留    : {len(rs._buf) if rs._buf is not None else 0} 帧")
    print()
    if right:
        print("✗ 右端越界! 抽头窗口被零填充 -> 每次块边界一个台阶")
        print("  = 周期性咔哒声。这是根因。")
        return 1
    print("✓ 右端无越界 —— 重采样器边界安全, 根因不在这里")
    return 0


if __name__ == "__main__":
    sys.exit(main())
