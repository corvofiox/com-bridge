# -*- coding: utf-8 -*-
"""分析**纯正弦测试音**到达主机输出后的失真（最干净的判据）。

## 为什么这个测试是决定性的
源信号是副机合成的 440Hz 纯正弦(幅度 0.25)。理论上它的二阶差分
d2 上限 = 0.25 * (2*pi*440/48000)^2 ≈ 0.00083。
所以主机输出里任何 d2 明显大于 0.002 的点，**必定是管线引入的不连续**，
不可能是"音乐内容"。这就把"数字咔哒 vs 音乐瞬态"的歧义彻底消除了。

输出: 每秒 d2 超限点数、超限点间隔、以及每处前后的样本值。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import wave

SR = 48000
THRESH = float(sys.argv[1]) if len(sys.argv) > 1 else 0.002


def main() -> int:
    path = os.path.join(os.environ["TEMP"], "cb_heard.wav")
    if len(sys.argv) > 2:
        path = sys.argv[2]
    raw = open(path, "rb").read()
    d = raw[44:]
    a = np.frombuffer(d, dtype="<i2")
    a = a[:(a.size // 2) * 2].reshape(-1, 2)[:, 0].astype(np.float32) / 32768.0
    print(f"{path}: {a.size} 帧 = {a.size / SR:.1f}s  RMS={np.sqrt(np.mean(a**2)):.5f}")

    d2 = np.abs(np.diff(a, n=2))
    hit = np.flatnonzero(d2 > THRESH)
    print(f"d2 门限={THRESH}  (440Hz/0.25 幅度理论上限 0.00083)")
    print(f"d2 中位={np.median(d2):.6f}  最大={d2.max():.5f}")

    if hit.size == 0:
        print()
        print("✓ 无超限点: 正弦完整无损 -> 管线干净")
        return 0

    # 合并 1ms 内的连续命中
    merged: list[int] = []
    for i in hit:
        if merged and i - merged[-1] <= int(0.001 * SR):
            continue
        merged.append(int(i))
    dur = a.size / SR
    print()
    print(f"=== 不连续点: {len(merged)} 处 = {len(merged) / dur:.2f} 处/秒 ===")
    for i in merged[:25]:
        ctx = a[max(0, i - 2):i + 4]
        print(f"  t={i / SR:8.3f}s  d2={d2[i]:.5f}  样本={np.round(ctx, 4).tolist()}")

    if len(merged) > 1:
        t = np.array(merged) / SR
        gaps = np.diff(t)
        print()
        print(f"间隔(秒): {np.round(gaps[:20], 3).tolist()}")
        print(f"  中位={np.median(gaps) * 1000:.1f}ms  "
              f"最小={gaps.min() * 1000:.1f}ms 最大={gaps.max() * 1000:.1f}ms")
        cv = float(np.std(gaps) / max(1e-9, np.mean(gaps)))
        print(f"  变异系数={cv:.2f} ({'接近周期性' if cv < 0.4 else '不规则'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
