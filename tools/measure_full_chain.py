# -*- coding: utf-8 -*-
"""★★★ 真实链路复现: 带 ratio 重采样 + 队列 + 环回采集。

## 与上一个测试的差别
上一个直接 write 已知正弦 -> 完全干净(信噪比 108dB)。
生产链路多了两件事:
  1. **Resampler**(ratio ≠ 1 的微重采样)
  2. **AlignedStreamBuffer 队列**(按时间戳 pop_aligned)

本工具复刻这两步, 同时环回采集, 看杂音是否出现。
"""
from __future__ import annotations

import sys
import threading
import time

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import (AlignedStreamBuffer, LoopbackCapture,
                              LoopbackRenderer)

TONE = 1000.0
AMP = 0.25
SECONDS = 6.0
JITTER = 60.0


def main() -> int:
    ren = LoopbackRenderer()
    ren.start()
    rate = ren.rate
    ch = ren.channels
    block = ren.block_align
    rt = int(ren.buffer_frames) - int(rate * 0.005)

    buf = AlignedStreamBuffer(channels=ch, block_align=block, bits=ren.bits,
                              rate=rate, name="t", target_ms=JITTER,
                              max_ms=400.0)
    cap = LoopbackCapture()
    cap.start()
    print(f"  渲染 {ch}ch@{rate}Hz, 采集 {cap.channels}ch@{cap.rate}Hz")

    stop = threading.Event()
    stats = {"trims": 0, "trimmed": 0, "max_lv": 0.0}

    def feed_and_render():
        """同时推包与渲染(复刻生产两线程的交互)。"""
        FR = int(rate * 0.010)
        phase = 0.0
        nxt = time.monotonic()
        ratio = 1.0
        while not stop.is_set():
            now = time.monotonic()
            # 每 10ms 推一包(与真实 agent 一致)
            if now >= nxt:
                tt = (np.arange(FR, dtype=np.float64) + phase) / rate
                w = AMP * np.sin(2 * np.pi * TONE * tt)
                st = np.repeat(w.astype(np.float32)[:, None], ch, axis=1)
                phase += FR
                buf.push(int(time.time() * 1e6), st.tobytes())
                nxt += 0.010
            level = buf.buffered_ms()
            stats["max_lv"] = max(stats["max_lv"], level)
            # 比例项(与生产一致)
            if level > JITTER:
                over = min(1.0, (level - JITTER) / JITTER)
                want = 1.0 + 0.008 * over
                ratio += max(-0.00016, min(0.00016, want - ratio))
            else:
                ratio += max(-0.00016, min(0.00016, 1.0 - ratio))
            # 兜底
            if level > JITTER * 1.8:
                d = buf.trim_to(target_ms=JITTER, max_skip_ms=0.5)
                if d:
                    stats["trims"] += 1
                    stats["trimmed"] += d
            avail = ren.available_frames()
            padding = ren.buffer_frames - avail
            budget = max(0, min(avail, rt - padding))
            if budget > 0:
                c = buf.pop_aligned(int(time.time() * 1e6), budget, 25000,
                                    ratio_override=ratio)
                if c:
                    ren.write(c)
            time.sleep(0.002)

    th = threading.Thread(target=feed_and_render, daemon=True)
    th.start()

    collected = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < SECONDS:
        r = cap.read(timeout_s=0.1)
        if r is None:
            continue
        pcm, flags, _qpc = r
        arr = np.frombuffer(pcm, dtype=np.float32)
        if cap.channels > 1:
            arr = arr.reshape(-1, cap.channels)[:, 0]
        collected.append(arr.copy())

    stop.set()
    time.sleep(0.3)
    ren.stop()
    cap.stop()

    print(f"  trim {stats['trims']} 次, 丢 {stats['trimmed']/48:.1f}ms, "
          f"水位峰值 {stats['max_lv']:.1f}ms")

    y = np.concatenate(collected).astype(np.float64)
    print(f"  采集 {len(y)} 样本")

    # 样本级不连续
    d = np.abs(np.diff(y))
    theory = 2 * np.pi * TONE / cap.rate * AMP
    bad = np.where(d > theory * 3.0)[0]
    print(f"\n== 不连续点: {len(bad)} 处 ==")
    for i in bad[:10]:
        print(f"    样本 {i} (t={i/cap.rate:.3f}s) 跳变 {d[i]:.5f} "
              f"= {d[i]/theory:.1f}x")

    # 频谱
    seg = 65536
    mid = len(y) // 2
    chunk = y[mid:mid + seg]
    w = np.hanning(len(chunk))
    spec = np.abs(np.fft.rfft(chunk * w))
    freqs = np.fft.rfftfreq(len(chunk), 1.0 / cap.rate)
    fb = int(np.argmax(spec))
    fund = spec[fb]
    band = (freqs > 200) & (freqs < 8000)
    mask = np.abs(freqs - freqs[fb]) > 50
    tot = (spec[band] ** 2).sum()
    noise = (spec[band & mask] ** 2).sum()
    snr = 10 * np.log10(tot / noise) if noise > 0 else 99
    print(f"  带内信噪比: {snr:.1f} dB")

    print("\n== 判读 ==")
    print("  若本次 SNR 明显低于 108dB(上一个干净测试), 说明")
    print("  Resampler 或队列 pop 环节引入了杂音。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
