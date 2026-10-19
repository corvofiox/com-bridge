# -*- coding: utf-8 -*-
"""验证 Resampler 积压保护: 长时间驱动下峰值受控、无异常。"""
import sys

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import Resampler

for ratio in (0.985, 1.0, 1.015):
    rs = Resampler(channels=2, block_align=8, bits=32)
    phase = 0.0
    peak = 0
    for _ in range(5000):
        t = (np.arange(480, dtype=np.float64) + phase) / 48000
        w = 0.3 * np.sin(2 * np.pi * 440 * t)
        phase += 480
        st = np.repeat(w.astype(np.float32)[:, None], 2, axis=1)
        rs.process(st.tobytes(), ratio, max_out=480)
        if rs._buf is not None:
            peak = max(peak, len(rs._buf))
    resets = getattr(rs, "_backlog_resets", 0)
    print(f"  ratio={ratio:.3f}: 峰值={peak:6d} 末尾={len(rs._buf):6d} "
          f"极端保护={resets}")

print()
print("判读: 峰值应 <= 24000(约500ms); 极端保护触发次数应很少")
