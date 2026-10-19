# -*- coding: utf-8 -*-
"""★ 模拟真实渲染循环: push/pop 混合 60 秒, 看水位是否会突破 max_ms。

## 真实场景参数(从 server.py 读出)
* 渲染轮询: WASAPI 每 ~10ms 一次
* budget = render_target - padding(只补到目标, 不填满)
* peer_buf.pop_aligned(now, budget, lookahead, ratio)
* agent 每 ~10ms 发一包(480 帧 @48kHz)

## 关键怀疑
`pop_aligned` 在预算不足时会**部分消费**:
    keep = room * block_align
    chunks.append(pcm[:keep])
    self._buf[0] = (ts, pcm[keep:])     <- 剩余留在队首
若 block_align 对齐没问题, 不会放大水位。
但如果 `self._buf[0] = ...` 每次留下**极小的碎片**, 碎片累积会让
`_buffered_frames()` 的 `// block_align` 丢弃零头 —— 水位读数偏低,
不会偏高。

## 换个角度: 看 push 的丢弃是否真的生效
若渲染线程**长时间不调用 pop**(例如渲染线程卡住/avail=0),
push 会持续丢弃, 水位停在 max_ms。这也不会到 490。

## 所以 490 只可能来自:
  a) max_ms 被改成更大值(用户配置或新版本)
  b) GUI 显示的是别的量
  c) buffered_ms 计算有误

本工具直接验证 (c): 用真实 block_align + 多种帧数, 比对
buffered_ms 与"实际秒数"是否一致。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.audio import AlignedStreamBuffer

RATE = 48000
BLOCK = 8

print("== 验证 buffered_ms 计算是否准确 ==")
buf = AlignedStreamBuffer(channels=2, block_align=BLOCK, bits=32,
                          rate=RATE, name="acc", target_ms=60.0,
                          max_ms=10_000.0)      # 放大上限以便观察
# 精确推入 100ms 的数据
frames = int(RATE * 0.100)
buf.push(0, b"\x00" * (frames * BLOCK))
print(f"  推入 {frames} 帧(应为 100.0ms) -> buffered_ms = "
      f"{buf.buffered_ms():.2f}")

# 再推 100ms
buf.push(0, b"\x00" * (frames * BLOCK))
print(f"  再推 100ms        -> buffered_ms = {buf.buffered_ms():.2f}")

print()
print("== 验证 pop 部分消费后水位 ==")
got = buf.pop_aligned(0, frames // 2, lookahead_us=0, ratio_override=1.0)
print(f"  取走 {frames//2} 帧后 buffered_ms = {buf.buffered_ms():.2f}")
got2 = buf.pop_aligned(0, frames // 2, lookahead_us=0, ratio_override=1.0)
print(f"  再取走 {frames//2} 帧后 buffered_ms = {buf.buffered_ms():.2f}")

print()
print("== 结论 ==")
print("  若 buffered_ms 与推入量一致, 则计算无误,")
print("  490ms 必然来自 max_ms 被设为更大值 —— 需要看用户的实际配置")
print("  (GUI 的「延迟目标」输入框内容)。")
