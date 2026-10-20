# -*- coding: utf-8 -*-
"""分析**正在写入中**的 WAV 的最近一段，精确定位"数字台阶"。

## 为什么用"孤立台阶"判据
真实音乐里鼓点也是宽带瞬态，但它**周围也有高 d2**（起音过程本身不平滑）。
而数字断口(插入静音/丢样本/重复块)产生的是一次**孤立**的台阶：
该点的 d2 远大于其 ±10ms 邻域内(排除自身)的最大 d2。

判据:
  d2[n] > ISO_K * max(d2[n-N .. n-10] 与 d2[n+10 .. n+N])   且 d2[n] > ABS
  N = 10ms 邻域
这样鼓点(邻域内到处都大)会被排除，孤立台阶会被抓出来。

用法: python tools/analyze_tail.py <wav> [最近秒数] [ISO_K]
"""
from __future__ import annotations

import os
import sys

import numpy as np

TAIL_S = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0
ISO_K = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0
ABS = 0.004


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ["TEMP"], "cb_monitor.wav")
    raw = open(path, "rb").read()
    # 容忍"正在写入"的 WAV: 直接跳过 44 字节头
    data = raw[44:]
    ch = 2
    a = np.frombuffer(data, dtype="<i2")
    a = a[:(a.size // ch) * ch].reshape(-1, ch)[:, 0].astype(np.float32) / 32768.0
    sr = 48000
    total_s = a.size / sr
    take = min(a.size, int(TAIL_S * sr))
    seg = a[-take:]
    print(f"文件总长 {total_s:.1f}s，分析最近 {seg.size / sr:.1f}s")

    d1 = np.abs(np.diff(seg))
    d2 = np.abs(np.diff(seg, n=2))
    if d2.size < sr:
        print("太短")
        return 1

    # 局部邻域最大(排除自身 ±10 样本)
    N = int(0.010 * sr)
    guard = 10
    from numpy.lib.stride_tricks import sliding_window_view
    # 用"前 10ms 与后 10ms"的最大值: 先算两个方向的滚动最大
    def roll_max(x, w):
        pad = np.pad(x, (w, w), mode="constant", constant_values=0)
        return np.max(sliding_window_view(pad, 2 * w + 1), axis=1)

    nb = np.maximum(
        roll_max(np.concatenate((d2[guard:], np.zeros(guard, np.float32))), N),
        roll_max(np.concatenate((np.zeros(guard, np.float32), d2[:-guard])), N),
    )
    ratio = d2 / np.maximum(nb, 1e-9)
    hit = np.flatnonzero((d2 > ABS) & (ratio > ISO_K))

    # 合并相邻命中(<=2ms 视为同一次)
    merged: list[int] = []
    for i in hit:
        if merged and i - merged[-1] <= int(0.002 * sr):
            continue
        merged.append(int(i))

    print(f"最大 d1={d1.max():.4f}  最大 d2={d2.max():.5f}  "
          f"d2 中位={np.median(d2):.6f}")
    print()
    print(f"=== 孤立台阶: {len(merged)} 处 ===")
    for i in merged[:30]:
        ago = (seg.size - i) / sr
        # 该点附近的实际样本值, 便于判断是"跳到零"还是"跳变"
        ctx = seg[max(0, i - 3): i + 4]
        print(f"  t={i / sr:8.3f}s (约 {ago:5.1f}s 前)  d2={d2[i]:.5f} "
              f"倍数={ratio[i]:6.1f}  样本={np.round(ctx, 4).tolist()}")
    if merged:
        print()
        print("判读: 样本序列里出现突跳到 0 -> 静音插入; "
              "出现两个相同值 -> 丢样本; 相邻值连续 -> 相位跳变")
    return 0


if __name__ == "__main__":
    sys.exit(main())
