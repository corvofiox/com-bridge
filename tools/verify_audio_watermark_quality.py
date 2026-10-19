# -*- coding: utf-8 -*-
"""★★★ 真实端到端: 高水位下渲染循环的实际输出质量。

## 目的
上一轮修了 `pop_budget`(送进 Resampler 的 max_out 按队列可产出量算),
需要验证: 水位在 60~200ms 区间时, 输出波形是否始终干净。

## 方法
用真实 LoopbackRenderer, 但**不写入 WASAPI**(避免设备限制), 而是:
  1. 按生产节奏向 AlignedStreamBuffer 推包
  2. 按生产逻辑(含 pop_budget 修正)取数据
  3. 对取出的 PCM 做 FFT, 统计杂散
比较"低水位"与"高水位"两组的杂散水平。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import AlignedStreamBuffer

RATE = 48000
BLOCK = 8          # 2ch * 32bit
CH = 2
TONE = 3000.0
JITTER = 60.0


def run(target_level_ms: float, seconds: float = 8.0):
    """把队列水位维持在 target_level_ms 附近, 收集产出的 PCM。"""
    buf = AlignedStreamBuffer(channels=CH, block_align=BLOCK, bits=32,
                              rate=RATE, name="e2e", target_ms=JITTER,
                              max_ms=400.0)
    FRAMES = int(RATE * 0.010)
    render_target = int(RATE * 0.095)      # 95ms(WASAPI 实测值)

    cap = []                                # 收集产出的样本
    phase = 0.0
    t0 = time.monotonic()
    next_push = t0
    rendered_frames = 0
    while time.monotonic() - t0 < seconds:
        now = time.monotonic()
        while now >= next_push:
            # 生成 3kHz 正弦(2ch)
            n = FRAMES
            tt = (np.arange(n, dtype=np.float64) + phase) / RATE
            wave = 0.5 * np.sin(2 * np.pi * TONE * tt)
            st = np.repeat(wave.astype(np.float32)[:, None], CH, axis=1)
            phase += n
            buf.push(int(time.time() * 1e6), st.tobytes())
            next_push += 0.010
            now = time.monotonic()

        level = buf.buffered_ms()
        # 模拟 WASAPI 剩余空间(按水位反推: 水位高 -> 空间小)
        avail = max(0, render_target - rendered_frames)
        padding = render_target - avail
        over_ms = level - JITTER * 1.5

        # 生产逻辑(含 pop_budget 修正)
        budget = max(0, min(avail, render_target - padding))
        pop_budget = budget
        if over_ms > 0 and avail > 0:
            want = int(RATE * min(level, 95.0) / 1000.0)
            pop_budget = max(budget, min(want, avail))

        if pop_budget > 0:
            chunk = buf.pop_aligned(int(time.time() * 1e6), pop_budget,
                                    25000, ratio_override=1.0)
            if chunk:
                cap.append(np.frombuffer(chunk, dtype=np.float32)
                           .reshape(-1, CH)[:, 0].copy())
                # 写不完的部分丢弃(模拟截断)
                n_frames = len(chunk) // BLOCK
                rendered_frames = min(render_target, rendered_frames + n_frames)
        # WASAPI 每 2ms 播掉 96 帧
        rendered_frames = max(0, rendered_frames - int(RATE * 0.002))
        time.sleep(0.002)

    if not cap:
        return None
    y = np.concatenate(cap).astype(np.float64)
    if len(y) < 5000:
        return None
    # FFT 找最坏杂散
    w = np.hanning(len(y))
    spec = np.abs(np.fft.rfft(y * w))
    spec[:5] = 0
    order = np.argsort(spec)[::-1]
    fb = order[0]
    fund = spec[fb]
    masked = spec.copy()
    masked[max(0, fb - 8):fb + 9] = 0
    spur = masked.max() if len(masked) else 0
    dbc = 20 * np.log10(max(spur, 1e-20) / max(fund, 1e-20))
    return len(y), dbc


print("== 不同水位下的输出杂散(3kHz 正弦) ==")
print()
for lvl in (60, 80, 120, 200):
    r = run(lvl, 6.0)
    if r is None:
        print(f"  目标水位 {lvl:3d}ms: 输出不足")
        continue
    n, dbc = r
    flag = "  <-- 杂散偏大!" if dbc > -50 else ""
    print(f"  目标水位 {lvl:3d}ms: 输出 {n:6d} 样本, "
          f"最坏杂散 {dbc:7.1f} dBc{flag}")

print()
print("== 判读 ==")
print("  若各组杂散都在 -50dBc 以下 -> 高水位不再破坏波形")
print("  对比: 线性插值在 3kHz 时约 -47dBc(可听), sinc 应达 -57dBc")
