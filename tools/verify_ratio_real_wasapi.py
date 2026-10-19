# -*- coding: utf-8 -*-
"""★★★ 用真实 WASAPI 渲染器验证: ratio 能否真正加快队列消费。

## 之前的困惑
我的模拟脚本里"ratio 变大 -> trim 更多", 但那可能是模拟的 rendered
模型不对(手动维护 rendered 计数器)。这里用**真实 LoopbackRenderer**,
让 WASAPI 自己决定消费速率, 才能看出 ratio 的真实效果。

## 判据
同一供给增益下, 比较 ratio=1.0 vs ratio=1.008 时的水位走势。
若 ratio 有效, 水位应更低、trim 更少。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import AlignedStreamBuffer, LoopbackRenderer

TONE = 1000.0
JITTER = 60.0


def run(ratio: float, gain: float = 1.02, sec: float = 6.0) -> dict:
    ren = LoopbackRenderer()
    ren.start()
    block = ren.block_align
    rate = ren.rate
    render_target = int(ren.buffer_frames) - int(rate * 0.005)
    buf = AlignedStreamBuffer(channels=ren.channels, block_align=block,
                              bits=ren.bits, rate=rate, name="t",
                              target_ms=JITTER, max_ms=400.0)
    FR = int(rate * 0.010)
    phase = 0.0
    trims = 0
    trimmed = 0
    lv = []
    t0 = time.monotonic()
    nxt = t0
    while time.monotonic() - t0 < sec:
        now = time.monotonic()
        while now >= nxt:
            tt = (np.arange(FR, dtype=np.float64) + phase) / rate
            w = 0.5 * np.sin(2 * np.pi * TONE * tt)
            st = np.repeat(w.astype(np.float32)[:, None], ren.channels, axis=1)
            phase += FR
            buf.push(int(time.time() * 1e6), st.tobytes())
            nxt += 0.010 / gain
            now = time.monotonic()
        level = buf.buffered_ms()
        lv.append(level)
        if level > JITTER * 1.15:
            d = buf.trim_to(target_ms=JITTER, max_skip_ms=0.5)
            if d:
                trims += 1
                trimmed += d
        avail = ren.available_frames()
        padding = ren.buffer_frames - avail
        budget = max(0, min(avail, render_target - padding))
        if budget > 0:
            c = buf.pop_aligned(int(time.time() * 1e6), budget, 25000,
                                ratio_override=ratio)
            if c:
                ren.write(c)
        time.sleep(0.002)
    ren.stop()
    return {
        "trims": trims,
        "trimmed_ms": trimmed / 48.0,
        "max_level": max(lv) if lv else 0,
        "avg_level": sum(lv) / len(lv) if lv else 0,
    }


print("== 真实 WASAPI 渲染器: ratio 对水位的影响(供给快 2%) ==")
print()
print(f"{'ratio':>8} {'trim次数':>10} {'丢音频ms':>10} "
      f"{'水位均值':>10} {'水位峰值':>10}")
for r in (1.0, 1.004, 1.008, 1.012):
    res = run(r)
    print(f"{r:>8.3f} {res['trims']:>10} {res['trimmed_ms']:>10.1f} "
          f"{res['avg_level']:>9.1f}ms {res['max_level']:>9.1f}ms")

print()
print("== 判读 ==")
print("  若 ratio 越大 -> 丢音频越少 -> ratio 是有效的水位控制手段")
print("  (因为在真实 WASAPI 上, pop_aligned 的 input_budget=max_frames")
print("   ×ratio 会让队列真正多被取出, 且产出帧数不增加 -> 水位下降)")
