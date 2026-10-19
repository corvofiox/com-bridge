# -*- coding: utf-8 -*-
"""验证不同 max_skip_ms 下: 水位压制效果 + 丢音频总量 + 波形连续性。

## 判据
* 丢音频总量: 越少越好(丢得越少, 声音越"完整", 不会听起来变快)
* 水位峰值: 应稳定在软阈值附近
* 最大跳变: 应 ≈ 理论步进(无台阶)
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import AlignedStreamBuffer

RATE = 48000
CH = 2
BLOCK = CH * 4
TONE = 1000.0
JITTER = 60.0


def run(gain: float, step_ms: float, soft_ratio: float = 1.15,
        sec: float = 6.0) -> dict:
    buf = AlignedStreamBuffer(channels=CH, block_align=BLOCK, bits=32,
                              rate=RATE, name="t", target_ms=JITTER,
                              max_ms=400.0)
    FR = int(RATE * 0.010)
    rt = int(RATE * 0.095)
    rendered = 0
    trims = 0
    trimmed = 0
    phase = 0.0
    t0 = time.monotonic()
    nxt = t0
    lv = []
    out = []
    while time.monotonic() - t0 < sec:
        now = time.monotonic()
        while now >= nxt:
            tt = (np.arange(FR, dtype=np.float64) + phase) / RATE
            w = 0.5 * np.sin(2 * np.pi * TONE * tt)
            st = np.repeat(w.astype(np.float32)[:, None], CH, axis=1)
            phase += FR
            buf.push(int(time.time() * 1e6), st.tobytes())
            nxt += 0.010 / gain
            now = time.monotonic()
        level = buf.buffered_ms()
        lv.append(level)
        if level > JITTER * soft_ratio:
            d = buf.trim_to(target_ms=JITTER, max_skip_ms=step_ms)
            if d:
                trims += 1
                trimmed += d
        avail = max(0, rt - rendered)
        padding = rt - avail
        budget = max(0, min(avail, rt - padding, 200))
        if budget > 0:
            c = buf.pop_aligned(int(time.time() * 1e6), budget, 25000,
                                ratio_override=1.0)
            if c:
                out.append(np.frombuffer(c, dtype=np.float32)
                           .reshape(-1, CH)[:, 0].copy())
                rendered = min(rt, rendered + len(c) // BLOCK)
        rendered = max(0, rendered - int(RATE * 0.002))
        time.sleep(0.002)
    y = np.concatenate(out).astype(np.float64) if out else np.array([])
    d = np.abs(np.diff(y)) if len(y) > 10 else np.array([0.0])
    theory = 2 * np.pi * TONE / RATE * 0.5
    return {
        "trims": trims,
        "trimmed_ms": trimmed / 48.0,
        "max_level": max(lv) if lv else 0,
        "avg_level": sum(lv) / len(lv) if lv else 0,
        "jump_ratio": (d.max() / theory) if theory else 0,
        "n": len(y),
    }


print("== 不同单步幅度对比(供给快 2%) ==")
print()
print(f"{'单步':>8} {'trim次数':>10} {'丢音频ms':>10} "
      f"{'水位均值':>10} {'水位峰值':>10} {'跳变/理论':>10}")
for step in (0.5, 1.0, 2.0, 5.0):
    r = run(1.02, step)
    print(f"{step:>7.1f}ms {r['trims']:>10} {r['trimmed_ms']:>10.1f} "
          f"{r['avg_level']:>9.1f}ms {r['max_level']:>9.1f}ms "
          f"{r['jump_ratio']:>9.2f}x")

print()
print("== 判读 ==")
print("  单步越小 -> 跳变越接近 1.00x(无台阶), 但 trim 次数更多、")
print("  丢音频总量可能更多。理想: 跳变 ≈1.00x 且丢音频最少。")
