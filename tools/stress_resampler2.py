# -*- coding: utf-8 -*-
"""对齐无关的重采样器失真检测：比较输入/输出的"孤立台阶"数量。

ratio≠1 时输出与输入在时间轴上持续错位，逐样本相减无意义。
但"孤立台阶"(局部 d2 远超邻域)是**平移不敏感**的：无论怎么错位，
一个完美的重采样器都不该凭空造出台阶。

判据: d2[n] > K * max(邻域 ±10ms 的 d2) 且 d2[n] > ABS
"""
from __future__ import annotations

import os
import sys

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

sys.path.insert(0, ".")

from com_bridge.audio import Resampler


def load(path):
    with wave.open(path, "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    return (np.frombuffer(raw, dtype="<i2").reshape(-1, ch).astype(np.float32)
            / 32768.0), sr, ch


def count_steps(a: np.ndarray, sr: int, K=6.0, ABS=0.01) -> int:
    x = a.astype(np.float64)
    if x.size < sr // 10:
        return 0
    d2 = np.abs(np.diff(x, n=2))
    N = int(0.010 * sr)
    g = 10

    def rmax(v):
        pad = np.pad(v, (N, N), mode="constant", constant_values=0)
        return np.max(sliding_window_view(pad, 2 * N + 1), axis=1)

    nb = np.maximum(rmax(np.concatenate((d2[g:], np.zeros(g)))),
                    rmax(np.concatenate((np.zeros(g), d2[:-g]))))
    hit = (d2 > ABS) & (d2 > K * np.maximum(nb, 1e-12))
    # 合并 20ms 内的连续命中为一次
    idx = np.flatnonzero(hit)
    if idx.size == 0:
        return 0
    n, last = 1, idx[0]
    for i in idx[1:]:
        if i - last > int(0.020 * sr):
            n += 1
        last = i
    return n


def main() -> int:
    import wave
    path = sys.argv[1]
    src, sr, ch = load(path)
    print(f"源: {os.path.basename(path)} {src.shape[0]} 帧 {ch}ch@{sr}Hz")

    # 输入自身的台阶数(基线)
    base = count_steps(src[:, 0], sr)
    print(f"输入(源文件)孤立台阶 = {base}")
    print()
    print(f"{'ratio':>9} {'输出帧':>9} {'右越界':>7} {'输出台阶':>9} {'相对输入':>9}")
    for ratio in (1.0, 1.00001, 1.0002, 0.9998, 1.004, 0.996, 1.010):
        rs = Resampler(channels=ch, block_align=4 * ch, bits=32)
        rng = np.random.default_rng(11)
        pos, outs = 0, []
        while pos < src.shape[0]:
            nfr = int(rng.integers(int(0.002 * sr), int(0.010 * sr)))
            chunk = src[pos:pos + nfr]
            if chunk.shape[0] == 0:
                break
            pos += nfr
            out = rs.process(chunk.astype("<f4").tobytes(), ratio,
                             max_out=max(1, int(nfr / ratio) + 8))
            if out:
                outs.append(np.frombuffer(out, dtype="<f4")
                            .reshape(-1, ch)[:, 0].astype(np.float32))
        o = np.concatenate(outs) if outs else np.zeros(0, np.float32)
        cnt = count_steps(o, sr)
        print(f"{ratio:9.5f} {o.size:9d} "
              f"{getattr(rs, '_right_edge_hits', 0):7d} {cnt:9d} "
              f"{(cnt - base):+9d}")
    print()
    print("判读: 若某 ratio 下输出台阶数明显多于输入 -> 重采样器造咔哒")
    return 0


if __name__ == "__main__":
    import wave  # noqa: F401  (load 内用到)
    sys.exit(main())
