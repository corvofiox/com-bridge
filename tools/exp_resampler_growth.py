# -*- coding: utf-8 -*-
"""★★★ 排查杂音: 重采样器内部缓冲是否无界增长、是否产生跳变?

## 用户描述
"音频同步每隔一段时间就会出现持续的杂音, 杂音持续一小段时间后就彻底
 消失, 再过一段时间又会出现一小段杂音, 如此往复。"
特征: **成段出现 → 消失 → 再出现**, 是累积-释放型故障。

## 嫌疑
Resampler 的 `_buf` 是持久缓冲:
    self._buf = np.concatenate((self._buf, cur))     # 每次追加
    丢弃已消费前缀: self._buf = self._buf[drop:]      # 只丢 _pos 之前的
若 ratio 长期 < 1(产出多于输入), `_pos` 推进慢, 前缀丢弃跟不上,
`_buf` 会持续增长 -> 内存与拷贝开销上升 -> 渲染循环偶尔超时 ->
WASAPI 欠载 -> **一段杂音**; 之后追上又恢复正常 -> 杂音消失。
与"成段/周期性"高度吻合!

## 本工具
用真实音频(正弦)驱动 Resampler, 统计:
  * 内部缓冲长度随时间的变化
  * 输出波形是否连续(检测跳变)
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import Resampler

RATE = 48000
CH = 2
BLOCK = CH * 4          # 32bit


def make_sine(frames: int, phase: float) -> tuple[bytes, float]:
    """生成 float32 立体声正弦。返回 (pcm, 新相位)。"""
    t = (np.arange(frames, dtype=np.float64) + phase) / RATE
    wave = 0.3 * np.sin(2 * np.pi * 440.0 * t)
    stereo = np.repeat(wave.astype(np.float32)[:, None], CH, axis=1)
    return stereo.tobytes(), phase + frames


print("== 用正弦驱动 Resampler, 观察内部缓冲与输出连续性 ==")
for ratio in (0.985, 1.0, 1.015):
    rs = Resampler(channels=CH, block_align=BLOCK, bits=32)
    phase = 0.0
    buf_lens = []
    prev_sample = None
    max_jump = 0.0
    jumps = 0

    for i in range(200):        # 200 轮, 每轮 10ms
        pcm, phase = make_sine(480, phase)
        out = rs.process(pcm, ratio, max_out=480)
        if out:
            arr = np.frombuffer(out, dtype=np.float32)
            mono = arr[::CH]
            if prev_sample is not None:
                # 相邻两次输出的连接处是否连续
                d = abs(float(mono[0]) - prev_sample)
                max_jump = max(max_jump, d)
                if d > 0.1:        # 正弦单步最大变化约 0.03
                    jumps += 1
            prev_sample = float(mono[-1])
        buf_lens.append(len(rs._buf) if rs._buf is not None else 0)

    print(f"  ratio={ratio:.3f}: 内部缓冲 起始={buf_lens[0]} "
          f"末尾={buf_lens[-1]} 最大={max(buf_lens)}")
    print(f"                 输出跳变 {jumps} 次, 最大跳变幅度 {max_jump:.4f}")
