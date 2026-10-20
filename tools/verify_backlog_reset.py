# -*- coding: utf-8 -*-
"""验证假设: 渲染预算 max_out 远小于输入块时, Resampler 内部积压是否会
     触发 MAX_BACKLOG 回退(把 _pos 清零) -> 相位跳变 -> 可听咔哒。

用 440Hz 纯正弦, 检查输出自身的 d2 尖峰(不比较输入, 避开时间错位问题)。
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, ".")

from com_bridge.audio import Resampler

SR = 48000
DUR = 40


def main() -> int:
    n = SR * DUR
    t = np.arange(n) / SR
    w = (np.sin(2 * np.pi * 440 * t) * 0.25).astype(np.float32)
    st = np.stack([w, w], axis=1)

    print(f"{'max_out':>10} {'产出帧':>9} {'回退':>5} {'右越界':>7} "
          f"{'饥饿帧':>10} {'d2max':>8} {'d2>0.05':>8} {'积压残留':>9}")
    for budget_ms in (1, 2, 5, 10, 20, 100, 0):
        rs = Resampler(channels=2, block_align=8, bits=32)
        max_out = None if budget_ms == 0 else int(SR * budget_ms / 1000)
        pos = 0
        outs: list[np.ndarray] = []
        step = int(0.020 * SR)          # 每 20ms 喂一块(接近生产)
        ratio = 1.0009
        while pos < n:
            chunk = st[pos:pos + step]
            pos += step
            kw = {"max_out": max_out} if max_out else {}
            out = rs.process(chunk.astype("<f4").tobytes(), ratio, **kw)
            if out:
                outs.append(np.frombuffer(out, dtype="<f4")
                            .reshape(-1, 2)[:, 0])
        o = np.concatenate(outs) if outs else np.zeros(0, np.float32)
        d2 = np.abs(np.diff(o, n=2)) if o.size > 2 else np.zeros(1)
        label = "None" if max_out is None else f"{budget_ms}ms"
        backlog = len(rs._buf) if rs._buf is not None else 0
        print(f"{label:>10} {o.size:9d} {getattr(rs, '_backlog_resets', 0):5d} "
              f"{getattr(rs, '_right_edge_hits', 0):7d} "
              f"{rs._starved_frames:10.0f} {d2.max():8.5f} "
              f"{int((d2 > 0.05).sum()):8d} {backlog:9d}")
    print()
    print("判读: 若小 max_out 那几行出现 回退>0 且 d2>0.05 有值 -> 假设成立")
    return 0


if __name__ == "__main__":
    sys.exit(main())
