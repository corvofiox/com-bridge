# -*- coding: utf-8 -*-
"""压测 Resampler：用真实音乐 + 生产同款分块节奏，检测**块边界失真**。

## 为什么做这个
用户描述爆音"持续 1~10 秒、周期性不固定"，这不是单个咔哒，而是**一段
持续的毛刺/沙沙声**。重采样器在"抽头窗口右翼越界"时会对缺失样本零填充，
产生**宽带噪声**，且会随内部缓冲状态持续若干轮 —— 与症状吻合。

## 做法
把一段真实音乐按生产的分块节奏(2~10ms 随机)喂给 Resampler，
ratio 取生产实测范围(0.99~1.015)，然后用两种方式看失真：
  1. Resampler 自带的 `_right_edge_hits` / `_starved_frames`
  2. **输出 vs 输入的逐样本偏差**(ratio≈1 时输出应几乎等于输入，
     块边界若有台阶，偏差会尖峰)
"""
from __future__ import annotations

import sys
import wave

import numpy as np

sys.path.insert(0, ".")

from com_bridge.audio import Resampler


def load(path: str):
    with wave.open(path, "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        n = w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").reshape(-1, ch).astype(np.float32) / 32768.0
    return a, sr, ch


def run(src: np.ndarray, sr: int, ch: int, ratio: float, seed: int = 7):
    rs = Resampler(channels=ch, block_align=4 * ch, bits=32)
    rng = np.random.default_rng(seed)
    pos = 0
    outs: list[np.ndarray] = []
    while pos < src.shape[0]:
        # 生产节奏: 2~10ms 一块
        nfr = int(rng.integers(int(0.002 * sr), int(0.010 * sr)))
        chunk = src[pos:pos + nfr]
        if chunk.shape[0] == 0:
            break
        pos += nfr
        pcm = chunk.astype("<f4").tobytes()
        max_out = max(1, int(nfr / ratio) + 8)
        out = rs.process(pcm, ratio, max_out=max_out)
        if out:
            outs.append(np.frombuffer(out, dtype="<f4").reshape(-1, ch).copy())
    res = np.concatenate(outs) if outs else np.zeros((0, ch), np.float32)
    return rs, res


def main() -> int:
    path = sys.argv[1]
    src, sr, ch = load(path)
    print(f"源: {path}  {src.shape[0]} 帧 = {src.shape[0] / sr:.1f}s  {ch}ch")
    print()

    print(f"{'ratio':>9} {'输出帧':>9} {'右越界':>7} {'饥饿帧':>9} "
          f"{'最大偏差':>9} {'RMS偏差':>9}")
    worst = None
    for ratio in (1.0, 1.00001, 1.0002, 0.9998, 1.004, 0.996, 1.010, 0.990):
        rs, out = run(src, sr, ch, ratio)
        n = min(out.shape[0], src.shape[0])
        diff = np.abs(out[:n, 0] - src[:n, 0])
        # 只统计有内容的区域(排除静音)
        mask = np.abs(src[:n, 0]) > 0.01
        d = diff[mask] if mask.any() else diff
        print(f"{ratio:9.5f} {out.shape[0]:9d} "
              f"{getattr(rs, '_right_edge_hits', 0):7d} "
              f"{rs._starved_frames:9.0f} "
              f"{float(d.max()) if d.size else 0:9.5f} "
              f"{float(np.sqrt(np.mean(d ** 2))) if d.size else 0:9.6f}")
        if worst is None or (d.size and float(d.max()) > worst[1]):
            worst = (ratio, float(d.max()) if d.size else 0.0)

    print()
    if worst and worst[1] > 0.02:
        print(f"!! ratio={worst[0]} 时输出与输入偏差达 {worst[1]:.4f} "
              f"-> 块边界有明显台阶(可听毛刺)")
    else:
        print("✓ 各 ratio 下输出与输入几乎一致, 无块边界台阶")
    return 0


if __name__ == "__main__":
    import os
    default = os.path.join(os.environ["TEMP"], "cb_test_audio.wav")
    main_path = sys.argv[1] if len(sys.argv) > 1 else default
    sys.argv = sys.argv[:1] + [main_path]
    sys.exit(main())
