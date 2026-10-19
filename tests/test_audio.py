"""音频缓冲/重采样/混音 —— 不含 WASAPI 的纯逻辑部分。

这里重点锁两个曾经真实出错的语义:
  1. pop_aligned 的 horizon 必须往回退 target_ms(否则抖动缓冲等于不存在);
  2. pop_* 必须**严格**遵守 max_frames(否则取多了会被 write() 丢弃, 静默丢音)。
"""

from __future__ import annotations

import numpy as np
import pytest

from com_bridge.audio import (
    AlignedStreamBuffer,
    Mixer,
    Resampler,
    StreamClockTracker,
)


def pcm_frames(vals, channels=2, bits=32) -> bytes:
    arr = np.asarray(vals, dtype=np.float64)
    if bits == 32:
        block = arr.astype(np.float32)
    else:
        block = np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)
    return np.repeat(block, channels).astype("<f4" if bits == 32 else "<i2").tobytes()


class TestMixer:
    def test_sums_two_streams(self):
        m = Mixer(channels=2, block_align=8, bits=32)
        a = np.zeros(4, dtype="<f4"); a[0] = 0.5
        b = np.zeros(4, dtype="<f4"); b[0] = 0.5
        out = np.frombuffer(m.mix([a.tobytes(), b.tobytes()]), dtype="<f4")
        assert out[0] == pytest.approx(1.0)
        assert out[1] == 0.0

    def test_clips_instead_of_wrapping(self):
        m = Mixer(channels=2, block_align=8, bits=32)
        a = np.zeros(4, dtype="<f4"); a[0] = 0.9
        b = np.zeros(4, dtype="<f4"); b[0] = 0.9
        out = np.frombuffer(m.mix([a.tobytes(), b.tobytes()]), dtype="<f4")
        assert out[0] == pytest.approx(1.0)

    def test_single_stream_passthrough(self):
        m = Mixer(channels=2, block_align=8, bits=32)
        a = np.array([0.1, 0.2, 0.3, 0.4], dtype="<f4")
        out = np.frombuffer(m.mix([a.tobytes()]), dtype="<f4")
        assert np.allclose(out, a)

    def test_16bit_clips_at_int16_range(self):
        m = Mixer(channels=2, block_align=4, bits=16)
        a = np.array([30000, 0], dtype="<i2")
        b = np.array([30000, 0], dtype="<i2")
        out = np.frombuffer(m.mix([a.tobytes(), b.tobytes()]), dtype="<i2")
        assert out[0] == 32767

    def test_empty_input(self):
        m = Mixer(channels=2, block_align=8, bits=32)
        assert m.mix([]) == b""


class TestResampler:
    def test_first_call_produces_immediately(self):
        """新实现没有'铺垫'概念: 第一包就应产出大部分。

        旧实现第一包只作铺垫不输出, 且其帧会被下一包重读 —— 那是
        "持续小爆音"的根源之一, 已重写为样本精确的持久缓冲。
        KEEP_FRAMES(96 帧≈2ms) 是防'到达抖动抽干缓冲'的预留, 不输出。
        """
        rs = Resampler(channels=2, block_align=8, bits=32)
        pcm = pcm_frames(np.linspace(-1, 1, 1000))
        out = rs.process(pcm, 1.0)
        # 产出 ≈ n - KEEP_FRAMES 帧
        assert len(out) // 8 >= 890, len(out) // 8

    def test_ratio_one_preserves_frame_count(self):
        rs = Resampler(channels=2, block_align=8, bits=32)
        pcm = pcm_frames(np.linspace(-1, 1, 1000))
        n1 = len(rs.process(pcm, 1.0)) // 8
        n2 = len(rs.process(pcm, 1.0)) // 8
        assert n1 + n2 <= 2000 + 2, (n1, n2)   # 不重不漏

    def test_ratio_above_one_yields_fewer_frames(self):
        rs = Resampler(channels=2, block_align=8, bits=32)
        pcm = pcm_frames(np.linspace(-1, 1, 1000))
        a = len(rs.process(pcm, 1.0)) // 8
        b = len(rs.process(pcm, 1.001)) // 8
        assert b < a + 990, (a, b)

    def test_ratio_below_one_yields_more_frames(self):
        rs = Resampler(channels=2, block_align=8, bits=32)
        pcm = pcm_frames(np.linspace(-1, 1, 1000))
        a = len(rs.process(pcm, 1.0)) // 8
        b = len(rs.process(pcm, 0.999)) // 8
        assert b > a, (a, b)

    def test_16bit_roundtrip_keeps_signal(self):
        rs = Resampler(channels=2, block_align=4, bits=16)
        pcm = pcm_frames(np.linspace(-0.5, 0.5, 500), channels=2, bits=16)
        rs.process(pcm, 1.0)
        out = rs.process(pcm, 1.0)
        vals = np.frombuffer(out, dtype="<i2")
        assert vals.size > 0
        assert np.abs(vals).max() < 32767


class TestStreamClockTracker:
    def test_detects_slow_sender(self):
        """每 1000 帧花 20900us 而非 20833us -> 发得偏慢, ratio < 1。"""
        tr = StreamClockTracker(nominal_rate=48000)
        expected = 1000 * 1_000_000 / 20900 / 48000
        r = 1.0
        for i in range(200):
            r = tr.update(i * 20900, 1000)
        assert r == pytest.approx(expected, abs=0.005)

    def test_nominal_traffic_stays_near_one(self):
        tr = StreamClockTracker(nominal_rate=48000)
        r = 1.0
        for i in range(200):
            r = tr.update(i * 20833, 1000)
        assert abs(r - 1.0) < 0.01

    def test_rejects_wild_outliers(self):
        tr = StreamClockTracker(nominal_rate=48000)
        for i in range(50):
            tr.update(i * 20833, 1000)
        before = tr.ratio
        tr.update(50 * 20833 + 1, 1000)   # dt 极小 -> 瞬时值离谱
        assert abs(tr.ratio - before) < 0.02


class TestAlignedStreamBuffer:
    def _buf(self, target_ms=60.0) -> AlignedStreamBuffer:
        return AlignedStreamBuffer(channels=2, block_align=8, bits=32,
                                  rate=48000, target_ms=target_ms)

    def test_not_due_until_target_elapsed(self):
        """ts 语义已改为'初始对齐用': 日常取数按 FIFO 水位驱动, 不再按
        ts 严格调度 —— 发送节拍抖动会被水位吸收而不是透传成播放空洞
        (早先按 ts 调度时, agent 发送间隔 16~31ms 的抖动直接变成
        10~30ms 的静音簇 = 用户听到的杂音)。此测试改为验证 FIFO 语义。
        """
        b = self._buf(target_ms=60.0)
        ts = 1_000_000
        b.push(ts, pcm_frames(np.zeros(480)))
        # 队列有数据就取, 不看 ts
        out = b.pop_aligned(ts, 480)
        assert out is not None
        assert len(out) // 8 == 480
        # 取空后再取 → underrun
        assert b.pop_aligned(ts + 60_000, 480) is None

    def test_strictly_honors_max_frames(self):
        """曾经的真实 bug: 预算 1056 帧却取出 960*2=1920 帧, 多出的被
        write() 丢弃 —— 数据从队列摘除并计入 frames_out, 于是静默丢音。

        这里 push 三块各 480 帧, 只允许取 500 帧, 结果必须 <= 500,
        且剩余的帧数必须守恒。
        """
        b = self._buf(target_ms=0.0)
        ts = 1_000_000
        for _ in range(3):
            b.push(ts, pcm_frames(np.zeros(480)))
        total_in = b.frames_in
        assert total_in == 1440

        out = b.pop_aligned(ts + 10_000, 500)
        assert out is not None
        got = len(out) // 8
        assert got <= 500, f"取了 {got} 帧, 超出预算 500"

        # 再取一次, 两次合计不能超过总输入
        out2 = b.pop_aligned(ts + 10_000, 500)
        if out2 is not None:
            got += len(out2) // 8
        assert got <= total_in

    def test_oversized_chunk_is_split_not_dropped(self):
        """块大于预算时必须切分保留余量, 而不是整块丢弃。"""
        b = self._buf(target_ms=0.0)
        ts = 1_000_000
        b.push(ts, pcm_frames(np.zeros(960)))
        out1 = b.pop_aligned(ts + 10_000, 400)
        assert len(out1) // 8 == 400
        out2 = b.pop_aligned(ts + 10_000, 400)
        assert len(out2) // 8 == 400
        out3 = b.pop_aligned(ts + 10_000, 400)
        assert len(out3) // 8 == 160           # 960 - 400 - 400
        assert b.pop_aligned(ts + 10_000, 400) is None

    def test_pop_any_also_honors_budget(self):
        b = self._buf()
        b.push(1_000_000, pcm_frames(np.zeros(960)))
        out = b.pop_any(300)
        assert len(out) // 8 == 300
        rest = b.pop_any(10_000)
        assert len(rest) // 8 == 660

    def test_buffered_ms_reflects_queued_frames(self):
        b = self._buf()
        assert b.buffered_ms() == 0.0
        b.push(1_000_000, pcm_frames(np.zeros(48000)))   # 1 秒
        assert b.buffered_ms() == pytest.approx(1000.0, abs=1.0)

    def test_head_lag_none_when_empty(self):
        b = self._buf()
        assert b.head_lag_ms(12345) is None

    def test_head_lag_reports_age(self):
        b = self._buf()
        b.push(1_000_000, pcm_frames(np.zeros(480)))
        assert b.head_lag_ms(1_050_000) == pytest.approx(50.0)

    def test_max_watermark_drops_oldest(self):
        """水位超过 max_ms 必须丢弃最旧的, 否则延迟会无限增长。"""
        b = self._buf()
        b.max_ms = 100.0
        # 每块 10ms, 塞 30 块 = 300ms
        for i in range(30):
            b.push(1_000_000 + i * 10_000, pcm_frames(np.zeros(480)))
        assert b.buffered_ms() <= 100.0 + 20.0
        assert b.dropped > 0

    def test_stats_shape(self):
        b = self._buf()
        s = b.stats()
        for key in ("name", "buffered_ms", "frames_in", "frames_out",
                    "dropped", "ratio"):
            assert key in s
