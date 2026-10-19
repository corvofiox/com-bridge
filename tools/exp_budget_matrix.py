# -*- coding: utf-8 -*-
"""★★★ 严格对比两种 budget 策略在**真实渲染器**上的表现。

## 背景
注释里记载过: 直接 budget=avail 会让"第一轮把攒下的水位全灌进 WASAPI,
队列随即见底, 之后每轮 pop 取不到数据 -> WASAPI 欠载 -> 每秒几十次爆音"。

但实测又显示: budget=render_target-padding 会让重采样器饥饿、缓冲增长
-> 成段杂音。

两者都不可接受 -> 需要量化对比, 找出真正的折中。

## 指标(全部可量化)
  * underruns   : pop 取不到数据的次数(= WASAPI 欠载风险)
  * 内部缓冲峰值: 重采样器积压程度
  * 跳变次数    : 波形不连续次数(= 杂音)
  * 写入间隔缺口: 两次 write 之间的空档(= 声音断续)
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import AlignedStreamBuffer, LoopbackRenderer

RATE = 48000


def run(budget_mode: str, seconds: float = 12.0,
        ratio: float = 0.985) -> dict:
    ren = LoopbackRenderer()
    ren.start()
    block = ren.block_align
    render_target = int(ren.buffer_frames) - int(ren.rate * 0.005)

    buf = AlignedStreamBuffer(channels=ren.channels, block_align=block,
                              bits=ren.bits, rate=ren.rate, name=budget_mode,
                              target_ms=60.0, max_ms=400.0)
    FRAMES = int(ren.rate * 0.010)

    phase = 0.0
    prev = None
    jumps = 0
    peak_internal = 0
    pops_empty = 0
    writes = 0
    last_write_t = None
    max_gap = 0.0

    t0 = time.monotonic()
    next_push = t0
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
        if budget_mode == "fill":
            budget = max(0, avail)
        elif budget_mode == "target":
            budget = max(0, min(avail, render_target - padding))
        else:   # target + 下限(至少给一包, 防饿死)
            budget = max(0, min(avail, max(render_target - padding, FRAMES)))

        if budget > 0:
            chunk = buf.pop_aligned(int(time.time() * 1e6), budget, 0,
                                    ratio_override=ratio)
            if chunk:
                if buf._rs._buf is not None:
                    peak_internal = max(peak_internal, len(buf._rs._buf))
                ren.write(chunk)
                writes += 1
                now2 = time.monotonic()
                if last_write_t is not None:
                    max_gap = max(max_gap, now2 - last_write_t)
                last_write_t = now2
                arr = np.frombuffer(chunk, dtype=np.float32)[::ren.channels]
                if prev is not None and abs(float(arr[0]) - prev) > 0.1:
                    jumps += 1
                prev = float(arr[-1])
            else:
                pops_empty += 1
        time.sleep(0.002)

    lvl = buf.buffered_ms()
    ren.stop()
    return {
        "mode": budget_mode,
        "peak_internal": peak_internal,
        "jumps": jumps,
        "pops_empty": pops_empty,
        "writes": writes,
        "max_gap_ms": round(max_gap * 1000, 1),
        "level": int(lvl),
    }


print("== 三种 budget 策略对比(ratio=0.985, 真实渲染器, 12 秒) ==")
rows = []
for mode in ("target", "target+floor", "fill"):
    r = run(mode)
    rows.append(r)
    print(f"  {mode:14} 内部峰值={r['peak_internal']:6d}  "
          f"跳变={r['jumps']:4d}  空pop={r['pops_empty']:4d}  "
          f"写={r['writes']:5d}  最大间隔={r['max_gap_ms']:6.1f}ms  "
          f"水位={r['level']}ms")

print()
print("== 判读 ==")
best = min(rows, key=lambda r: (r["jumps"], r["peak_internal"],
                                r["max_gap_ms"]))
print(f"  最优: {best['mode']}")
for r in rows:
    print(f"    {r['mode']:14} 跳变={r['jumps']:4d} 内部峰值={r['peak_internal']:6d}"
          f" 间隔={r['max_gap_ms']:.1f}ms")
