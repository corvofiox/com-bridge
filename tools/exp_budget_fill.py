# -*- coding: utf-8 -*-
"""★★★ 验证最终方案: max_out 传足够大的值(≥ 可产出量), 让重采样器不丢弃。

## 问题链
1. `budget = render_target - padding`(约 96~200 帧/轮)
2. `pop_aligned(max_frames=budget)` -> `Resampler.process(max_out=budget)`
3. ratio<1 时可产出量(约 487 帧) > budget -> out_n 被卡
4. 消费不足 -> 内部缓冲增长 -> 拷贝变慢 -> 渲染超时 -> 欠载 -> 杂音

## 方案对比
A. 现状: max_out = budget(小)
B. 修正: 让 max_out >= 可产出量, 但要保证**写进 WASAPI 的量**仍受 avail 限制
   => 调用侧先取够数据, write 时只写 avail 那么多(多余留在哪里?)
      不能留在 write(已 pop), 所以必须让 pop 的产出 **刚好** 等于要写的量。

## 真正的解法(本工具验证)
让 budget 本身足够大: 不要"补到 render_target", 而是"**补满 avail**"
(= 渲染缓冲全部可写空间, 约 4800 帧), 但每轮只 write 实际需要的量?
不行 —— write 超过 avail 会丢。

=> 正确: **每轮把 avail 全部填满**(budget = avail),
   这样 max_out = avail 可能远大于可产出量, 不会饿死重采样器;
   而 WASAPI 每 2ms 消耗 96 帧, 下一轮又有 96 帧空间, 正好补上。
   渲染缓冲会一直保持接近满(约 100ms 延迟), 这是可接受的代价,
   换来的是**重采样器不丢弃、波形连续**。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import AlignedStreamBuffer, LoopbackRenderer

RATE = 48000


def run(policy: str, seconds: float = 15.0) -> tuple[int, int, int]:
    """返回 (内部缓冲峰值, 跳变次数, 最终水位ms)。"""
    ren = LoopbackRenderer()
    ren.start()
    block = ren.block_align
    render_target = int(ren.buffer_frames) - int(ren.rate * 0.005)

    buf = AlignedStreamBuffer(channels=ren.channels, block_align=block,
                              bits=ren.bits, rate=ren.rate, name=policy,
                              target_ms=60.0, max_ms=400.0)
    FRAMES = int(ren.rate * 0.010)

    phase = 0.0
    prev = None
    jumps = 0
    peak_internal = 0
    t0 = time.monotonic()
    next_push = t0
    ratio = 0.985 if policy == "A" else 0.985

    while time.monotonic() - t0 < seconds:
        now = time.monotonic()
        while now >= next_push:
            t = (np.arange(FRAMES, dtype=np.float64) + phase) / ren.rate
            wave = 0.3 * np.sin(2 * np.pi * 440.0 * t)
            stereo = np.repeat(wave.astype(np.float32)[:, None],
                               ren.channels, axis=1)
            phase += FRAMES
            buf.push(int(time.time() * 1e6), stereo.tobytes())
            next_push += 0.010
            now = time.monotonic()

        avail = ren.available_frames()
        padding = ren.buffer_frames - avail
        if policy == "A":
            budget = max(0, min(avail, render_target - padding))
        else:
            budget = max(0, avail)          # 填满全部可写空间

        if budget > 0:
            chunk = buf.pop_aligned(int(time.time() * 1e6), budget, 0,
                                    ratio_override=ratio)
            if chunk:
                if buf._rs._buf is not None:
                    peak_internal = max(peak_internal, len(buf._rs._buf))
                written = ren.write(chunk)
                arr = np.frombuffer(chunk, dtype=np.float32)[::ren.channels]
                if prev is not None:
                    d = abs(float(arr[0]) - prev)
                    if d > 0.1:
                        jumps += 1
                prev = float(arr[-1])
        time.sleep(0.002)

    final_level = buf.buffered_ms()
    ren.stop()
    return peak_internal, jumps, int(final_level)


print("== A. 现状: budget = render_target - padding ==")
pa, ja, la = run("A", 15.0)
print(f"  重采样器内部缓冲峰值={pa}  跳变={ja}  水位={la}ms")

print()
print("== B. 修正: budget = avail(填满可写空间) ==")
pb, jb, lb = run("B", 15.0)
print(f"  重采样器内部缓冲峰值={pb}  跳变={jb}  水位={lb}ms")

print()
print("== 结论 ==")
print(f"  A: 内部峰值 {pa}, 跳变 {ja}")
print(f"  B: 内部峰值 {pb}, 跳变 {jb}")
if jb < ja:
    print("  ★ B 跳变更少 -> budget 给足可消除重采样器饥饿")
