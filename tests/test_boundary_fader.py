# -*- coding: utf-8 -*-
"""BoundaryFader 的回归测试: 起播/停止的边界跳变必须被消除。

用户实测症状(2026-10-21): "开始播放声音和结束播放声音的那一刻会小爆一下"。

★ 测试设计要点: 判据不能直接拿"相邻样本差的上限"当阈值 —— 正弦自身
  就有 d1 = amp*2*pi*f/sr。这里用 200Hz/0.5(自然 d1 仅 0.013), 于是
  "突然断到 0"产生的 0.5 级跳变会非常突出; 同时用"边界处 d1 vs 稳态
  d1"的比值做判据, 与信号频率无关。
"""
from __future__ import annotations

import numpy as np
import pytest

from com_bridge.audio import BoundaryFader

SR = 48000
CH = 2
TONE_HZ = 200.0
AMP = 0.5
NAT_D1 = AMP * 2 * np.pi * TONE_HZ / SR      # 正弦自身的相邻差分上限 ≈0.0131


def _tone(frames: int) -> bytes:
    # 用绝对相位, 保证跨块连续
    _tone.n += frames
    t = np.arange(_tone.n - frames, _tone.n) / SR
    w = (np.sin(2 * np.pi * TONE_HZ * t) * AMP).astype(np.float32)
    return np.stack([w, w], axis=1).astype("<f4").tobytes()


_tone.n = 0


def _silence(frames: int) -> bytes:
    return np.zeros((frames, CH), dtype="<f4").tobytes()


def _feed(fader: BoundaryFader, blocks) -> np.ndarray:
    parts = [fader.push(b) for b in blocks]
    parts.append(fader.flush())
    out = b"".join(p for p in parts if p)
    return (np.frombuffer(out, dtype="<f4").reshape(-1, CH)[:, 0]
            .astype(np.float64))


def test_fade_out_before_silence_removes_step():
    """音乐 -> 静音: 边界跳变不得明显超过正弦自身的自然差分。"""
    _tone.n = 0
    f = BoundaryFader(CH, 8, 32, fade_ms=5.0, rate=SR)
    x = _feed(f, [_tone(480) for _ in range(8)] + [_silence(480) for _ in range(8)])
    assert f.fades_out >= 1, "应至少触发一次淡出"
    d = np.abs(np.diff(x))
    assert d.max() < NAT_D1 * 1.5, \
        f"边界仍有跳变 dmax={d.max():.4f}, 自然上限={NAT_D1:.4f}(会听到 pop)"


def test_fade_in_after_silence_removes_step():
    """静音 -> 音乐: 起播那一刻同样不能有跳变。"""
    _tone.n = 0
    f = BoundaryFader(CH, 8, 32, fade_ms=5.0, rate=SR)
    x = _feed(f, [_silence(480) for _ in range(8)] + [_tone(480) for _ in range(8)])
    assert f.fades_in >= 1, "应至少触发一次淡入"
    d = np.abs(np.diff(x))
    assert d.max() < NAT_D1 * 1.5, \
        f"起播处仍有跳变 dmax={d.max():.4f}, 自然上限={NAT_D1:.4f}"


def test_no_alteration_when_continuously_loud():
    """持续有声时: 除 FIFO 延迟外数据必须原样通过(不引入失真)。"""
    _tone.n = 0
    f = BoundaryFader(CH, 8, 32, fade_ms=5.0, rate=SR)
    src = np.frombuffer(_tone(4800), dtype="<f4").reshape(-1, CH)[:, 0]
    _tone.n = 0
    x = _feed(f, [_tone(480) for _ in range(10)])
    n = min(len(x), len(src))
    assert n > 4000, f"只产出 {n} 帧, 数据被吞"
    diff = float(np.abs(x[:n] - src[:n]).max())
    assert diff < 1e-6, f"持续有声时数据被改动 diff={diff}"


def test_audio_is_not_lost():
    """只改样本值, 不得丢帧(允许 FADE 帧尾窗)。"""
    _tone.n = 0
    f = BoundaryFader(CH, 8, 32, fade_ms=5.0, rate=SR)
    got = 0
    for _ in range(10):
        got += len(f.push(_tone(480))) // 8
    got += len(f.flush()) // 8
    total_in = 4800
    assert total_in - f.fade_frames <= got <= total_in, \
        f"帧数不守恒: in={total_in} out={got}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
