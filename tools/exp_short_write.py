# -*- coding: utf-8 -*-
"""★★★ 验证: write() 是否可能写不完(超出部分被静默丢弃)?

## 嫌疑代码(server.py)
    budget = max(0, min(avail, render_target - padding))
    peer = peer_buf.pop_aligned(now, budget, lookahead, ratio_override=ratio)
    written = self._ren.write(peer)
`written` 可能 < peer 的帧数吗? 若会, 超出部分已经从队列摘除
却写不进设备 —— 数据流中间断一刀, 听感就是"噼啪/沙沙"。

## 两个放大因素
1. `pop_aligned` 内部 budget 用 `max_frames * ratio`, 当 ratio > 1 时
   输入预算超过 max_frames, 重采样后产出仍受 max_out 限制 —— 表面安全
2. **但 `pop_aligned` 的 max_out 传的是 budget**, 而 `write()` 的可写
   空间是**调用前**测的 avail。若 pop 与 write 之间有别的线程写了
   WASAPI(或 padding 自己变了), avail 会小于 budget -> 写不完。

## 本工具: 用真实渲染器 + 真实 pop_aligned, 统计 written < frames 的次数
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.audio import AlignedStreamBuffer, LoopbackRenderer

RATE = 48000

print("== 真实渲染器: 统计 write 写不完的次数 ==")
ren = LoopbackRenderer()
ren.start()
block = ren.block_align
render_target = int(ren.buffer_frames) - int(ren.rate * 0.005)

buf = AlignedStreamBuffer(channels=ren.channels, block_align=block,
                          bits=ren.bits, rate=ren.rate, name="t",
                          target_ms=60.0, max_ms=400.0)
FRAMES = int(ren.rate * 0.010)
PCM = b"\x00" * (FRAMES * block)

short_writes = 0
total_writes = 0
max_deficit = 0
t0 = time.monotonic()
next_push = t0

while time.monotonic() - t0 < 15.0:
    now = time.monotonic()
    while now >= next_push:
        buf.push(int(time.time() * 1e6), PCM)
        next_push += 0.010
        now = time.monotonic()

    avail = ren.available_frames()
    padding = ren.buffer_frames - avail
    budget = max(0, min(avail, render_target - padding))
    if budget > 0:
        chunk = buf.pop_aligned(int(time.time() * 1e6), budget, 0,
                                ratio_override=1.0)
        if chunk:
            n = len(chunk) // block
            written = ren.write(chunk)
            total_writes += 1
            if written < n:
                short_writes += 1
                max_deficit = max(max_deficit, n - written)
    time.sleep(0.002)

ren.stop()
print(f"  总写入次数   : {total_writes}")
print(f"  写不完次数   : {short_writes}")
print(f"  最大缺口(帧) : {max_deficit}  ({max_deficit/block/48:.1f}ms)")
print()
if short_writes:
    print("  ★★ 确认: write() 会写不完 -> 超出部分静默丢失")
    print("     这就是突发一段杂音的直接来源: 每次写不完都在波形中间断一刀。")
else:
    print("  ✓ 未发生写不完(budget 计算与 avail 一致)")
    print("     杂音另有来源, 需继续排查。")
