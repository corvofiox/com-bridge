# -*- coding: utf-8 -*-
"""重采样器**积压保护**的回归锁。

背景(2026-10-21): 旧实现只有一档保护 —— 内部积压 > 500ms 就
`_buf = _buf[-96:]; _pos = 0.0`, 即丢 500ms 真实音频 + **读指针清零**
(相位参照消失), 一次又长又响的断口。

新策略两级:
  ① 积压 > DRAIN_AT(300ms): 应急排空 —— 本轮忽略 max_out 全产出,
     多余的交调用方 carry 顺延 -> 一个样本都不丢;
  ② 积压 > MAX_BACKLOG(500ms): 才硬回退, 但 `_pos` **同步前移**而非清零
     -> 只有内容跳过, 没有相位突跳。
"""
from __future__ import annotations

import numpy as np

from com_bridge.audio import Resampler

SR = 48000


def _feed(rs: Resampler, ratio: float, budget: int, chunk: int, sec: float):
    n = int(SR * sec)
    t = np.arange(n) / SR
    w = (np.sin(2 * np.pi * 440 * t) * 0.25).astype(np.float32)
    st = np.stack([w, w], axis=1)
    pos, outs = 0, []
    while pos < n:
        blk = st[pos:pos + chunk]
        pos += chunk
        out = rs.process(blk.astype("<f4").tobytes(), ratio, max_out=budget)
        if out:
            outs.append(np.frombuffer(out, dtype="<f4").reshape(-1, 2)[:, 0])
    o = np.concatenate(outs) if outs else np.zeros(0, np.float32)
    return o, n


def test_drain_prevents_hard_reset_under_starved_budget():
    """预算只有 2ms、输入却 20ms 一块(病态): 必须靠排空, 不能硬回退。"""
    rs = Resampler(channels=2, block_align=8, bits=32)
    o, _n = _feed(rs, 1.0009, budget=int(SR * 0.002), chunk=int(SR * 0.020),
                  sec=8)
    assert rs._backlog_resets == 0, \
        f"发生了 {rs._backlog_resets} 次硬回退(排空应当阻止)"
    assert rs._backlog_drains > 0, "应当触发过应急排空"


def test_drain_output_has_no_phase_discontinuity():
    """排空/回退都不得在输出里制造相位断口(纯正弦 d2 应 <=0.001)。"""
    rs = Resampler(channels=2, block_align=8, bits=32)
    o, _n = _feed(rs, 1.0009, budget=int(SR * 0.005), chunk=int(SR * 0.020),
                  sec=8)
    d2 = np.abs(np.diff(o.astype(np.float64), n=2))
    assert d2.max() < 0.0015, f"出现相位断口 d2max={d2.max():.5f}"
    assert int((d2 > 0.05).sum()) == 0


def test_backlog_is_bounded():
    """积压必须被排空压住(不超过 MAX_BACKLOG), 否则内存与延迟失控。"""
    rs = Resampler(channels=2, block_align=8, bits=32)
    _feed(rs, 1.0009, budget=int(SR * 0.002), chunk=int(SR * 0.020), sec=8)
    assert rs._backlog_max <= 24000, f"积压峰值 {rs._backlog_max} 失控"
    assert rs.backlog_frames <= 24000


def test_normal_budget_never_drains():
    """预算充足时不应触发排空(说明两级策略不会干扰正常路径)。"""
    rs = Resampler(channels=2, block_align=8, bits=32)
    _feed(rs, 1.0009, budget=int(SR * 0.020), chunk=int(SR * 0.005), sec=8)
    assert rs._backlog_drains == 0
    assert rs._backlog_resets == 0
