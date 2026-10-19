# -*- coding: utf-8 -*-
"""★★★ 回归: 音频水位与重采样器积压。

## 用户报告(2026-10-19)
1. "水位延迟会随着时间逐渐增加, 一直加到上限就不往上走了"
2. "音频同步每隔一段时间就会出现持续的杂音, 杂音持续一小段时间后就
   彻底消失, 再过一段时间又会出现一小段杂音, 如此往复"

## 根因一: 水位单调爬升(已修)
前馈 `ratio_ff = arrival_rate/48000` 测的是"agent 每秒发来多少帧",
而**消费速率由 WASAPI 设备时钟决定**, 两者可差 0.2~0.5%, 前馈看不见。
这个失配只能靠积分项吸收, 但原来积分权限被限死在 ±0.2%
(RATIO_TRIM_LIMIT=0.002) —— 一旦真实失配超过它, 积分饱和,
水位单向爬升直到 max_ms。
实测(tools/verify_watermark_fix.py, 消费慢 0.3%):
    ±0.2%: 水位 10→390ms 单调撞顶  (正是用户描述)
    ±1.0%: 水位稳定在 70ms 附近

## 根因二: 重采样器内部积压(已修)
`Resampler.process(max_out=...)` 在 max_out 长期小于可产出量时,
数据只能积压在内部缓冲(实测 20 秒涨到 14490 帧 ≈ 300ms)。积压使
每轮 np.concatenate 越来越慢 -> 渲染循环超时 -> WASAPI 欠载 ->
**成段杂音**; 消费追上后消失, 再积压再出现 —— 完全对应症状 2。
修法: 积压超过约 500ms 时强制回退(极端保护), 正常路径永不触发
(实测生产 budget 下内部积压只有 97 帧)。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.audio as audio_mod  # noqa: E402
from com_bridge.server import Server  # noqa: E402


def test_ratio_trim_limit_is_large_enough():
    """★ 积分权限必须能覆盖设备时钟失配(实测 0.2~0.5%)。

    原值 0.002(±0.2%)导致水位撞顶; 现为 0.010(±1.0%)。
    """
    src = inspect.getsource(Server._render_loop)
    assert "RATIO_TRIM_LIMIT = " in src
    # 提取数值
    import re
    m = re.search(r"RATIO_TRIM_LIMIT = ([0-9.]+)", src)
    assert m, "RATIO_TRIM_LIMIT 必须是字面量"
    val = float(m.group(1))
    assert val >= 0.005, (
        f"积分权限 {val} 太小, 无法吸收设备时钟失配(实测 0.3% 时"
        f"水位会单调爬升到 max_ms)")


def test_level_hard_guard_exists():
    """必须有水位硬保护(逼近上限时主动弃帧)。"""
    assert hasattr(Server, "max_level_ms")
    src = inspect.getsource(Server.max_level_ms)
    assert "max_ms" in src
    # 阈值应低于 max_ms(留余量)
    assert "0.6" in src, "阈值必须低于 max_ms, 否则触顶后才救"


def test_trim_to_exists_and_shrinks():
    """trim_to 必须真的把水位削到目标。"""
    assert hasattr(audio_mod.AlignedStreamBuffer, "trim_to")
    buf = audio_mod.AlignedStreamBuffer(
        channels=2, block_align=8, bits=32, rate=48000,
        name="t", target_ms=60.0, max_ms=10000.0)
    pcm = b"\x00" * (480 * 8)
    for _ in range(10):          # 100ms
        buf.push(0, pcm)
    before = buf.buffered_ms()
    dropped = buf.trim_to(60.0)
    after = buf.buffered_ms()
    assert dropped > 0, "应当丢弃了一些帧"
    assert after <= before, "水位应当下降"
    assert after <= 70.0, f"应削到约 60ms, 实际 {after}"


def test_resampler_backlog_guard():
    """★ 重采样器必须有积压保护(否则内部缓冲无界增长 -> 周期性杂音)。"""
    src = inspect.getsource(audio_mod.Resampler.process)
    assert "MAX_BACKLOG" in src, "必须限制内部积压"
    import re
    m = re.search(r"MAX_BACKLOG = (\d+)", src)
    assert m, "阈值应为字面量"
    val = int(m.group(1))
    # 48000Hz 下 500ms = 24000 帧; 允许 300~800ms 的合理区间
    assert 14400 <= val <= 38400, f"积压阈值 {val} 帧不合理"


def test_backlog_guard_runs_before_index_math():
    """★★ 保护必须在**计算索引之前**(否则会 IndexError, 实测踩到)。"""
    src = inspect.getsource(audio_mod.Resampler.process)
    i_guard = src.index("MAX_BACKLOG")
    i_idx = src.index("np.arange(out_n")
    assert i_guard < i_idx, (
        "积压裁剪必须在计算 i0 之前, 否则裁剪后索引越界")
