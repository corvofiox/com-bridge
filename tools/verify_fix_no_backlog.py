# -*- coding: utf-8 -*-
"""修复验证: 不限制 Resampler 产出后, 任意预算下都不再积压/回退/咔哒。"""
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

    print("修复后: process() 不再接收 max_out(生产新代码就是这样调用)")
    print(f"{'输入块':>8} {'产出帧':>9} {'回退':>5} {'积压残留':>9} "
          f"{'d2max':>9} {'d2>0.05':>8}")
    for step_ms in (2, 5, 20, 50):
        rs = Resampler(channels=2, block_align=8, bits=32)
        pos = 0
        outs: list[np.ndarray] = []
        step = int(step_ms * SR / 1000)
        ratio = 1.0009
        while pos < n:
            chunk = st[pos:pos + step]
            pos += step
            out = rs.process(chunk.astype("<f4").tobytes(), ratio)
            if out:
                outs.append(np.frombuffer(out, dtype="<f4")
                            .reshape(-1, 2)[:, 0])
        o = np.concatenate(outs) if outs else np.zeros(0, np.float32)
        d2 = np.abs(np.diff(o, n=2)) if o.size > 2 else np.zeros(1)
        backlog = len(rs._buf) if rs._buf is not None else 0
        print(f"{step_ms:>6}ms {o.size:9d} {getattr(rs, '_backlog_resets', 0):5d} "
              f"{backlog:9d} {d2.max():9.5f} {int((d2 > 0.05).sum()):8d}")

    print()
    print("判读: 回退应恒为 0, d2 最大应 ≈0.00083(正弦理论极限)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
