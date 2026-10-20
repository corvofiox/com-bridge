# -*- coding: utf-8 -*-
"""离线分析录到的音频，找出**真正的瞬态断口**（音乐中间极短的塌陷）。

## 为什么不能只看"有没有零/有没有重复"
换曲/暂停/静音段会产生大量全零块和完全相同的块 —— 那是正常内容。
真正的断口特征是**短**(典型 2~30ms)、且**前后都是响亮的音乐**。

## 判据(缺一不可)
  1. 塌陷区间长度 在 [0.3ms, 50ms] 之间(更长的多半是换曲)
  2. 塌陷前 200ms 的中位 RMS > 0.02 (前后确实在放音乐)
  3. 塌陷后 200ms 的中位 RMS > 0.02
  4. 塌陷区间 RMS < 前窗口的 8%

输出: 每个事件的时刻、长度、前后 RMS，并给出间隔分布(判断是否周期性)。

用法: python tools/analyze_dropouts.py <wav路径>
"""
from __future__ import annotations

import sys
import wave

import numpy as np

SR = 48000
SHORT_MIN = int(0.0003 * SR)      # 0.3ms
SHORT_MAX = int(0.050 * SR)       # 50ms
CTX = int(0.200 * SR)             # 前后各 200ms 上下文
LOUD_MIN = 0.02
DROP_RATIO = 0.08


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "cb_monitor.wav"
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(n)
    print(f"文件: {path}  {n} 帧 = {n / sr:.1f}s  {ch}ch @{sr}Hz")

    a = np.frombuffer(raw, dtype="<i2").reshape(-1, ch)[:, 0].astype(np.float32) / 32768.0

    # 逐 240 帧(5ms)窗的 RMS
    win = 240
    m = (a.size // win) * win
    env = np.sqrt(np.mean(a[:m].reshape(-1, win) ** 2, axis=1))
    nw = env.size
    print(f"窗数: {nw} (每窗 {win} 帧 = {win / sr * 1000:.0f}ms)")

    events = []
    i = 0
    while i < nw:
        if env[i] > LOUD_MIN:
            i += 1
            continue
        j = i
        while j < nw and env[j] <= LOUD_MIN:
            j += 1
        # 塌陷区间 [i, j)
        dur = (j - i) * win
        if SHORT_MIN <= dur <= SHORT_MAX:
            pre = env[max(0, i - CTX // win):i]
            post = env[j:min(nw, j + CTX // win)]
            if pre.size and post.size:
                pre_med = float(np.median(pre))
                post_med = float(np.median(post))
                dip = float(np.max(env[i:j]))
                if pre_med > LOUD_MIN and post_med > LOUD_MIN \
                        and dip < pre_med * DROP_RATIO:
                    events.append((i * win / sr, dur / sr * 1000, pre_med, post_med))
        i = j

    print()
    print(f"=== 瞬态断口: {len(events)} 处 ===")
    if events:
        for t, ms, pre, post in events[:30]:
            print(f"  t={t:8.3f}s  长度={ms:6.2f}ms  前={pre:.4f} 后={post:.4f}")
        gaps = np.diff([t for t, *_ in events])
        if gaps.size:
            print(f"\n间隔(秒): {np.round(gaps[:25], 2).tolist()}")
            print(f"间隔统计: 中位={np.median(gaps):.2f}s "
                  f"最小={gaps.min():.2f}s 最大={gaps.max():.2f}s")
            cv = float(np.std(gaps) / max(1e-9, np.mean(gaps)))
            print(f"变异系数={cv:.2f}  ({'接近周期性' if cv < 0.4 else '不规则'})")
        rate = len(events) / max(1e-9, n / sr)
        print(f"\n发生率: {rate:.2f} 处/秒")
    else:
        print("  未发现")
    return 0


if __name__ == "__main__":
    sys.exit(main())
