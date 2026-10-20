# -*- coding: utf-8 -*-
"""检测音频里的**宽带突刺**(咔哒/噼啪的真正形态)。

## 为什么之前漏了
咔哒不是"静音"，而是**波形不连续**。它表现为极短时间内的高频能量爆发，
但**总 RMS 可能不变甚至上升**，所以"幅度塌陷"判据完全看不见它。

## 判据
对每 5ms 窗计算二阶差分的 RMS(等价于高频能量)，得到 hf[]。
再用**滑动中位数**做基线，标出 hf > K×baseline 且绝对量足够大的窗。
二阶差分对断点极敏感、对平滑乐音几乎不响应。

用法: python tools/analyze_clicks.py <wav> [K]
"""
from __future__ import annotations

import sys
import wave

import numpy as np

K = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
WIN = 240          # 5ms
MED_WIN = 200      # 基线窗口 = 1s


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "cb_monitor.wav"
    with wave.open(path, "rb") as w:
        n, sr, ch = w.getnframes(), w.getframerate(), w.getnchannels()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").reshape(-1, ch)[:, 0].astype(np.float32) / 32768.0
    m = (a.size // WIN) * WIN
    blocks = a[:m].reshape(-1, WIN)
    # 二阶差分(跨窗也要连续, 故对整段算)
    d2 = np.abs(np.diff(a[:m], n=2))
    hf = np.sqrt(np.mean(d2[:blocks.shape[0] * (WIN - 2)].reshape(blocks.shape[0], WIN - 2) ** 2, axis=1))

    # 滑动中位数基线(左右 pad 之和 = MED_WIN-1, 保证输出长度一致)
    from numpy.lib.stride_tricks import sliding_window_view
    pl = MED_WIN // 2
    pr = MED_WIN - 1 - pl
    hp = np.pad(hf, (pl, pr), mode="edge")
    win = sliding_window_view(hp, MED_WIN)
    base = np.median(win, axis=1)

    abs_floor = 0.002                      # 太安静的区域不判(避免底噪误报)
    ratio = hf / np.maximum(base, 1e-9)
    hit = np.flatnonzero((ratio > K) & (base > abs_floor * 0.1) & (hf > abs_floor))

    print(f"文件 {path}: {n} 帧 = {n / sr:.1f}s, 窗 {hf.size} 个(5ms)")
    print(f"K={K}  绝对门限={abs_floor}")
    print(f"基线中位数={float(np.median(base)):.5f}  hf 中位={float(np.median(hf)):.5f} "
          f"最大={float(hf.max()):.5f}")
    print()
    print(f"=== 宽带突刺: {hit.size} 处 ===")
    if hit.size:
        for i in hit[:40]:
            print(f"  t={i * WIN / sr:8.3f}s  hf={hf[i]:.5f}  "
                  f"基线={base[i]:.5f}  倍数={ratio[i]:6.1f}")
        if hit.size > 1:
            t = hit * WIN / sr
            gaps = np.diff(t)
            print(f"\n间隔(秒): {np.round(gaps[:20], 2).tolist()}")
            print(f"间隔: 中位={np.median(gaps):.2f}s 最小={gaps.min():.2f}s "
                  f"最大={gaps.max():.2f}s")
            cv = float(np.std(gaps) / max(1e-9, np.mean(gaps)))
            print(f"变异系数={cv:.2f} ({'接近周期性' if cv < 0.4 else '不规则'})")
        print(f"\n发生率: {hit.size / (n / sr):.2f} 处/秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
