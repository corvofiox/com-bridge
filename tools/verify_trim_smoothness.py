# -*- coding: utf-8 -*-
"""★★★ 验证: 小步滑窗后波形是否连续(不再有硬切)。

## 复刻用户场景
水位持续增长(供给略快), 触发软阈值控制, 测量输出波形的连续性。

## 判据
1. 输出与理想正弦的误差(应保持 -50dB 以下)
2. 最大样本间跳变(应接近理论步进, 不出现台阶)
3. 水位是否被压住(不越过软阈值太多)
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


def run(supply_gain: float, seconds: float = 6.0, step_ms: float = 2.0):
    """supply_gain > 1 表示供给快于消费(制造水位上涨)。"""
    buf = AlignedStreamBuffer(channels=CH, block_align=BLOCK, bits=32,
                              rate=RATE, name="t", target_ms=JITTER,
                              max_ms=400.0)
    FRAMES = int(RATE * 0.010)
    out = []
    phase = 0.0
    t0 = time.monotonic()
    next_push = t0
    render_target = int(RATE * 0.095)
    rendered = 0
    trims = 0
    trimmed_frames = 0
    levels = []

    while time.monotonic() - t0 < seconds:
        now = time.monotonic()
        while now >= next_push:
            tt = (np.arange(FRAMES, dtype=np.float64) + phase) / RATE
            w = 0.5 * np.sin(2 * np.pi * TONE * tt)
            st = np.repeat(w.astype(np.float32)[:, None], CH, axis=1)
            phase += FRAMES
            buf.push(int(time.time() * 1e6), st.tobytes())
            next_push += 0.010 / supply_gain
            now = time.monotonic()

        level = buf.buffered_ms()
        levels.append(level)
        # 小步滑窗
        if level > JITTER * 1.5:
            d = buf.trim_to(target_ms=JITTER, max_skip_ms=step_ms)
            if d:
                trims += 1
                trimmed_frames += d

        avail = max(0, render_target - rendered)
        padding = render_target - avail
        budget = max(0, min(avail, render_target - padding))
        if budget > 0:
            chunk = buf.pop_aligned(int(time.time() * 1e6), budget,
                                    25000, ratio_override=1.0)
            if chunk:
                out.append(np.frombuffer(chunk, dtype=np.float32)
                           .reshape(-1, CH)[:, 0].copy())
                rendered = min(render_target,
                               rendered + len(chunk) // BLOCK)
        rendered = max(0, rendered - int(RATE * 0.002))
        time.sleep(0.002)

    y = np.concatenate(out).astype(np.float64) if out else np.array([])
    return {
        "n": len(y),
        "y": y,
        "trims": trims,
        "trimmed_ms": trimmed_frames / 48.0,
        "max_level": max(levels) if levels else 0,
        "avg_level": sum(levels) / len(levels) if levels else 0,
    }


print("== 供给快 2%(必然触发水位控制) ==")
r = run(1.02, 6.0)
print(f"  输出 {r['n']} 样本, 触发 trim {r['trims']} 次, "
      f"共丢 {r['trimmed_ms']:.1f}ms")
print(f"  水位 均{r['avg_level']:.1f} / 峰{r['max_level']:.1f} ms "
      f"(软阈值 {JITTER*1.5:.0f}ms)")

if r["n"] > 2000:
    y = r["y"]
    d = np.abs(np.diff(y))
    theory = 2 * np.pi * TONE / RATE * 0.5      # 幅值 0.5
    print(f"  最大跳变 {d.max():.5f} / 理论 {theory:.5f} "
          f"= {d.max()/theory:.2f}x")
    # 与理想正弦比较(注意丢弃会造成相位跳, 所以分段比)
    seg = 4000
    worst = -999
    cnt = 0
    for k in range(0, len(y) - seg, seg):
        chunk = y[k:k + seg]
        # 局部拟合频率/相位不易, 用 FFT 峰 + 谐波比
        w = np.hanning(seg)
        spec = np.abs(np.fft.rfft(chunk * w))
        spec[:5] = 0
        fb = int(np.argmax(spec))
        fund = spec[fb]
        masked = spec.copy()
        masked[max(0, fb - 6):fb + 7] = 0
        spur = masked.max()
        dbc = 20 * np.log10(max(spur, 1e-20) / max(fund, 1e-20))
        if dbc > worst:
            worst = dbc
        cnt += 1
    print(f"  分段 SFDR 最坏 {worst:.1f} dBc (共 {cnt} 段)")

print()
print("== 对照: 供给正常(不触发控制) ==")
r2 = run(1.0, 6.0)
print(f"  输出 {r2['n']} 样本, 触发 trim {r2['trims']} 次")
print(f"  水位 均{r2['avg_level']:.1f} / 峰{r2['max_level']:.1f} ms")
if r2["n"] > 2000:
    d2 = np.abs(np.diff(r2["y"]))
    print(f"  最大跳变 {d2.max():.5f} = {d2.max()/theory:.2f}x 理论")

print()
print("== 判读 ==")
print("  若触发控制时最大跳变仍 ≈ 理论步进(不出现台阶) -> 滑窗无硬切")
