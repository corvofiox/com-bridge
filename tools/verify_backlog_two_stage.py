# -*- coding: utf-8 -*-
"""重采样器积压保护的两级策略验证。

## 背景
旧实现只有一档: 内部积压 > 500ms 就 `_buf = _buf[-96:]; _pos = 0.0`
—— 丢掉 500ms 真实音频 + **读指针清零**(相位参照消失), 是一次又长又响
的断口。

## 新策略
① 积压 > 300ms(DRAIN_AT): **应急排空** —— 本轮忽略 max_out, 能产多少
   产多少, 多余的交调用方 carry 顺延 -> **一个样本都不丢**;
② 积压 > 500ms(MAX_BACKLOG, 上游已严重异常) 才硬回退, 且**同步前移
   `_pos`**(而不是清零) -> 只有内容跳过, 没有相位突跳。

## 判据
在"输入 20ms 一块、预算只给 2ms"的病态条件下跑 40 秒:
  * 硬回退次数必须是 **0**(排空应已阻止它)
  * 输出自身的 d2 尖峰必须是 **0**(纯正弦, 理论 d2 ≤ 0.00083)
  * 输出总帧数必须 ≈ 输入(不丢样本)
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

    print(f"{'预算':>6} {'输入块':>6} {'产出帧':>9} {'硬回退':>7} {'排空':>6} "
          f"{'积压峰值':>9} {'d2max':>9} {'d2>0.05':>8}")
    failures = 0
    for budget_ms in (2, 5):
        for chunk_ms in (20,):
            rs = Resampler(channels=2, block_align=8, bits=32)
            max_out = int(SR * budget_ms / 1000)
            step = int(chunk_ms * SR / 1000)
            pos = 0
            outs: list[np.ndarray] = []
            ratio = 1.0009
            while pos < n:
                chunk = st[pos:pos + step]
                pos += step
                out = rs.process(chunk.astype("<f4").tobytes(), ratio,
                                 max_out=max_out)
                if out:
                    outs.append(np.frombuffer(out, dtype="<f4")
                                .reshape(-1, 2)[:, 0])
            o = np.concatenate(outs) if outs else np.zeros(0, np.float32)
            d2 = np.abs(np.diff(o, n=2)) if o.size > 2 else np.zeros(1)
            spikes = int((d2 > 0.05).sum())
            resets = rs._backlog_resets
            print(f"{budget_ms:>4}ms {chunk_ms:>4}ms {o.size:9d} "
                  f"{resets:7d} {rs._backlog_drains:6d} "
                  f"{rs._backlog_max:9d} {d2.max():9.5f} {spikes:8d}")
            if resets > 0:
                print(f"  ✗ 硬回退 {resets} 次(排空未能阻止积压)")
                failures += 1
            if spikes > 0:
                print(f"  ✗ d2 尖峰 {spikes} 个(有相位断口)")
                failures += 1
            # 样本守恒: 注意 ratio>1 时重采样器**本来就会**少产出
            # (输入被 consume 得比产出快, 正是它补偿晶振漂移的原理),
            # 所以期望产出 = (输入 - 残留) / ratio, 而不是直接等于输入。
            residual = rs.backlog_frames
            expected = (n - residual) / ratio
            if abs(o.size - expected) > 300:
                print(f"  ✗ 样本不守恒: out={o.size} 期望≈{expected:.0f} "
                      f"(残留={residual}, ratio={ratio})")
                failures += 1

    print()
    if failures:
        print(f"结论: {failures} 项不达标")
        return 1
    print("结论: ✓ 硬回退 0 次、无相位断口、样本守恒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
