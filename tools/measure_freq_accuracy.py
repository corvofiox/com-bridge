# -*- coding: utf-8 -*-
"""★★★ 用长时间稳定正弦 + 精确频率估计, 判断采集链路是否真的失真。

## 为什么主频会显示 2016 vs 1992Hz
我上一个测试用 4ms 脉冲做 FFT —— 4ms 的频率分辨率只有 250Hz,
测出的"主频"根本不可信。必须用**长时间信号**才能准确测频。

## 本工具
1. 播放 6 秒稳定 2000Hz 正弦
2. 采集后取中间稳定段(不碰边界)
3. 用**零填充 FFT + 抛物线插值**精确估计频率
4. 比较: 频率误差、谐波、噪声底
"""
from __future__ import annotations

import sys
import threading
import time

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import LoopbackCapture, LoopbackRenderer

TONE = 2000.0
AMP = 0.25
SECONDS = 6.0


def precise_freq(x: np.ndarray, rate: int) -> float:
    """零填充 FFT + 抛物线插值, 精确估计单频信号的频率。"""
    n = len(x)
    w = np.hanning(n)
    nfft = 1 << int(np.ceil(np.log2(n * 16)))     # 16x 零填充
    spec = np.abs(np.fft.rfft(x * w, nfft))
    k = int(np.argmax(spec))
    if 1 <= k < len(spec) - 1:
        a, b, c = spec[k - 1], spec[k], spec[k + 1]
        denom = (a - 2 * b + c)
        delta = 0.5 * (a - c) / denom if abs(denom) > 1e-20 else 0.0
    else:
        delta = 0.0
    return (k + delta) * rate / nfft


def main() -> int:
    ren = LoopbackRenderer()
    ren.start()
    rate = ren.rate
    ch = ren.channels
    print(f"  设备 {ch}ch@{rate}Hz, 播放 {TONE}Hz")

    cap = LoopbackCapture()
    cap.start()
    stop = threading.Event()

    def play():
        FR = int(rate * 0.010)
        phase = 0.0
        nxt = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            if now >= nxt:
                tt = (np.arange(FR, dtype=np.float64) + phase) / rate
                w = AMP * np.sin(2 * np.pi * TONE * tt)
                st = np.repeat(w.astype(np.float32)[:, None], ch, axis=1)
                phase += FR
                ren.write(st.tobytes())
                nxt += 0.010
            time.sleep(0.001)

    th = threading.Thread(target=play, daemon=True)
    th.start()
    collected = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < SECONDS:
        r = cap.read(timeout_s=0.1)
        if r is None:
            continue
        pcm, flags, qpc = r
        arr = np.frombuffer(pcm, dtype=np.float32)
        if cap.channels > 1:
            arr = arr.reshape(-1, cap.channels)[:, 0]
        collected.append(arr.copy())
    stop.set()
    time.sleep(0.3)
    ren.stop()
    cap.stop()

    y = np.concatenate(collected).astype(np.float64)
    print(f"  采集 {len(y)} 样本 ({len(y)/cap.rate:.2f}s)")

    # 取中间 4 秒(避开头尾瞬态)
    a = len(y) // 2 - int(cap.rate * 2)
    seg = y[a:a + int(cap.rate * 4)]
    print(f"  分析段 {len(seg)} 样本")

    f = precise_freq(seg, cap.rate)
    print(f"\n== 频率精度 ==")
    print(f"  源 {TONE:.1f} Hz -> 实测 {f:.3f} Hz")
    print(f"  绝对误差 {f - TONE:+.3f} Hz ({(f-TONE)/TONE*1e6:+.1f} ppm)")

    # THD+N
    w = np.hanning(len(seg))
    spec = np.abs(np.fft.rfft(seg * w))
    freqs = np.fft.rfftfreq(len(seg), 1.0 / cap.rate)
    k0 = int(np.argmax(spec))
    band = (freqs > 100) & (freqs < 20000)
    fund = spec[k0]
    tot = (spec[band] ** 2).sum()
    # 基波 ±5 bin
    nb = 5
    lo, hi = max(0, k0 - nb), min(len(spec), k0 + nb + 1)
    fund_e = (spec[lo:hi] ** 2).sum()
    noise = max(0.0, tot - fund_e)
    thdn = 10 * np.log10(noise / fund_e) if fund_e > 0 else -999
    print(f"\n== 失真 ==")
    print(f"  THD+N = {thdn:.1f} dB")
    print(f"  幅度 = {np.abs(seg).max():.4f} (期望 {AMP})")

    # 最强杂散
    masked = spec.copy()
    masked[lo:hi] = 0
    masked[:3] = 0
    kk = int(np.argmax(masked))
    print(f"  最强杂散 {freqs[kk]:.1f}Hz @ "
          f"{20*np.log10(max(masked[kk],1e-20)/fund):.1f} dBc")

    print("\n== 判读 ==")
    if abs(thdn) > 60:
        print("  ★ THD+N 优于 -60dB —— 采集链路干净")
    else:
        print("  ★ THD+N 差于 -60dB —— 链路确实失真")
    return 0


if __name__ == "__main__":
    sys.exit(main())
