# -*- coding: utf-8 -*-
"""验证完整水位控制链: 比例项(ratio) + 兜底 trim。

用真实 WASAPI 渲染器, 模拟生产逻辑:
  * 比例项: 水位超目标 -> ratio 增大(上限 +0.8%)
  * 兜底:   水位超 1.8x 目标 -> trim_to(0.5ms 小步)
比较"只有兜底"vs"比例项+兜底"的丢音频总量。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import AlignedStreamBuffer, LoopbackRenderer

TONE = 1000.0
JITTER = 60.0
RATIO_P_MAX = 0.008
SLEW = 0.00016


def run(use_p: bool, gain: float = 1.02, sec: float = 8.0) -> dict:
    ren = LoopbackRenderer()
    ren.start()
    block = ren.block_align
    rate = ren.rate
    rt = int(ren.buffer_frames) - int(rate * 0.005)
    buf = AlignedStreamBuffer(channels=ren.channels, block_align=block,
                              bits=ren.bits, rate=rate, name="t",
                              target_ms=JITTER, max_ms=400.0)
    FR = int(rate * 0.010)
    phase = 0.0
    ratio = 1.0
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
        # 比例项
        if use_p:
            want = 1.0
            if level > JITTER:
                over = min(1.0, (level - JITTER) / JITTER)
                want = 1.0 + RATIO_P_MAX * over
            ratio += max(-SLEW, min(SLEW, want - ratio))
        else:
            ratio = 1.0
        # 兜底 trim(阈值 1.8x)
        if level > JITTER * 1.8:
            d = buf.trim_to(target_ms=JITTER, max_skip_ms=0.5)
            if d:
                trims += 1
                trimmed += d
        avail = ren.available_frames()
        padding = ren.buffer_frames - avail
        budget = max(0, min(avail, rt - padding))
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
        "final_ratio": ratio,
    }


print("== 真实 WASAPI: 只有兜底 vs 比例项+兜底(供给快 2%) ==")
print()
print(f"{'策略':<18} {'trim次数':>10} {'丢音频ms':>10} "
      f"{'水位均值':>10} {'水位峰值':>10} {'末态ratio':>10}")
for use_p in (False, True):
    r = run(use_p)
    label = "比例项+兜底" if use_p else "只有兜底"
    print(f"{label:<18} {r['trims']:>10} {r['trimmed_ms']:>10.1f} "
          f"{r['avg_level']:>9.1f}ms {r['max_level']:>9.1f}ms "
          f"{r['final_ratio']:>10.5f}")

print()
print("== 判读 ==")
print("  比例项生效 -> trim 次数和丢音频量应显著下降(理想为 0)")
print("  这才是'不丢样本'的水位控制; trim 只做极端兜底。")
