"""重采样器流式连续性 —— "持续小爆音"的回归测试。

背景: 漂移比曾恒为 1.0(时钟分辨率 bug), 重采样器从未真正运行; 修复时钟
后它开始工作, 两个隐藏缺陷随即暴露, 听感为持续的小爆音小杂音:

  1. 旧实现按"本次输入 n 帧 → 产出 n/ratio 帧"计算产出, 读位置跨包错位:
     相邻块帧数不同时(渲染端合并的块大小天然不一), 个别样本被重复读取
     或跳过 —— 波形不连续即爆音。
  2. 上层按"输入帧数"做取数预算, 而渲染缓冲按"输出帧数"预留; ratio≠1
     时产出超出预算, 超出部分被 write() 丢弃(实测 12 秒丢 1378 帧)。

本文件的测试用"连续正弦的样本间跳变"作为爆音的数学判据:
  440Hz/0.25 幅度正弦在 48kHz 下每样本最大变化 ≈ 0.0144;
  丢弃/重复一个样本会造成最高 0.5 的跳变, 二者相差 30 倍以上,
  用 max|out[i+1]-out[i]| < 0.05 即可可靠区分。
"""

from __future__ import annotations

import numpy as np
import pytest

from com_bridge.audio import Resampler


SR = 48000
FREQ = 440.0
AMP = 0.25
# 连续正弦的每样本最大变化(留 3 倍裕量覆盖重采样的间距伸缩)
MAX_SLOPE = AMP * 2 * np.pi * FREQ / SR * 1.05
CRACKLE_SLOPE = 0.05


def _sine_pcm(start: int, n: int, channels: int = 2, bits: int = 32) -> bytes:
    idx = np.arange(start, start + n, dtype=np.float64)
    wave = np.sin(2 * np.pi * FREQ * idx / SR) * AMP
    if bits == 32:
        block = wave.astype(np.float32)
    else:
        block = np.clip(wave * 32767.0, -32768, 32767).astype(np.int16)
    return np.repeat(block, channels).astype("<f4" if bits == 32 else "<i2").tobytes()


def _feed_varying_blocks(rs: Resampler, sizes, ratio: float,
                         channels: int = 2, bits: int = 32):
    """按给定块大小序列连续馈入正弦, 返回(通道0 样本, 总输入帧数)。

    注意口径: 返回的是**通道0**的样本序列(不是交织全样本), 这样
    len(out) == 帧数, 与 total_in 同单位可比。
    """
    outs = []
    pos = 0
    for n in sizes:
        pcm = _sine_pcm(pos, n, channels, bits)
        out = rs.process(pcm, ratio)
        if out:
            dtype = "<f4" if bits == 32 else "<i2"
            outs.append(np.frombuffer(out, dtype=dtype).reshape(-1, channels)[:, 0])
        pos += n
    return np.concatenate(outs), pos


class TestStreamingContinuity:
    """核心回归: 任意块大小序列下输出必须波形连续(不重不漏)。"""

    @pytest.mark.parametrize("ratio", [0.998, 1.0, 1.002])
    def test_no_crackle_with_varying_block_sizes(self, ratio):
        """块大小交替变化 —— 旧实现在此场景下必然产生跳变。"""
        rs = Resampler(channels=2, block_align=8, bits=32)
        sizes = [480, 1056, 960, 300, 1056, 480] * 10
        out, total_in = _feed_varying_blocks(rs, sizes, ratio)
        assert len(out) > 1000
        d = np.abs(np.diff(out))
        assert d.max() < CRACKLE_SLOPE, (
            f"检测到波形不连续: 最大样本间跳变 {d.max():.4f} "
            f"(连续正弦应 < {MAX_SLOPE:.4f}) —— 存在样本重复/丢失(爆音)"
        )
        assert d.max() >= MAX_SLOPE * 0.5, "正弦应真实变化( sanity )"

    @pytest.mark.parametrize("ratio", [0.998, 1.002])
    def test_total_output_matches_stream_length(self, ratio):
        """总产出帧数应 ≈ 总输入/ratio(±0.5%), 不得系统性多产或少产。"""
        rs = Resampler(channels=2, block_align=8, bits=32)
        sizes = [1056] * 50
        out, total_in = _feed_varying_blocks(rs, sizes, ratio)
        expect = total_in / ratio
        assert abs(len(out) - expect) < expect * 0.005, (
            f"产出 {len(out)} 帧, 期望 ≈{expect:.0f}"
        )

    def test_16bit_path_also_continuous(self):
        """16bit 判据 = 正弦单样本最大步进 × 1.05 ≈ 495。

        曾经误写 60 —— 那是把 32bit 的幅度步进(0.0144)直接当成了
        16bit 码值步进; 440Hz/0.25 在 16bit 下每样本最大跳 471 个码,
        判据 60 会把正常正弦误报为爆音。
        """
        rs = Resampler(channels=2, block_align=4, bits=16)
        sizes = [480, 1056, 960, 300] * 10
        out, _ = _feed_varying_blocks(rs, sizes, 0.998, bits=16)
        d = np.abs(np.diff(out.astype(np.float64)))
        assert d.max() < 495, f"最大样本间跳变 {d.max():.0f} 超出正弦理论步进"

    def test_slow_start_then_large_block(self):
        """小帧起步 + 大帧跟进: 旧实现 _pos 变负后会重读样本。"""
        rs = Resampler(channels=2, block_align=8, bits=32)
        sizes = [48, 96, 1056, 1056, 1056, 480, 1056] * 5
        out, _ = _feed_varying_blocks(rs, sizes, 0.998)
        d = np.abs(np.diff(out))
        assert d.max() < CRACKLE_SLOPE


class TestOutputBudget:
    """上层按输出帧数预留渲染缓冲, 单次产出不得系统性超出预算。"""

    def test_output_per_call_bounded_by_input_over_ratio(self):
        rs = Resampler(channels=2, block_align=8, bits=32)
        ratio = 0.998
        pos = 0
        for n in (1056, 1056, 1056):
            out = rs.process(_sine_pcm(pos, n), ratio)
            frames = len(out) // 8
            # 产出应 ≈ n/ratio, 且不得大幅超出(否则被 write() 丢弃)
            assert frames <= int(n / ratio) + 2, (
                f"单次产出 {frames} 帧, 预算上限 {int(n/ratio)+2}"
            )
            pos += n

    def test_empty_input_is_noop(self):
        rs = Resampler(channels=2, block_align=8, bits=32)
        assert rs.process(b"", 0.998) == b""

    def test_tiny_blocks_eventually_produce(self):
        rs = Resampler(channels=2, block_align=8, bits=32)
        produced = 0
        for i in range(200):
            out = rs.process(_sine_pcm(i * 4, 4), 0.998)
            produced += len(out) // 8
        assert produced > 100, "小块流式馈入也应持续产出"
