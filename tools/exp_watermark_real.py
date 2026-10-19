# -*- coding: utf-8 -*-
"""★★★ 真实端到端水位实验: 用真实 LoopbackRenderer + 真实渲染循环。

前面的纯数学模拟把 WASAPI 消费建模错了(把自己写的帧当成"自然减少"),
结论不可信。本工具用**真实渲染器**(真 WASAPI 环回)对接 AlignedStreamBuffer,
完全复刻 server._render_loop 的策略, 观察水位真实走势。

对照组:
  A. 现状策略: budget = min(avail, render_target - padding)
  B. 水位感知: 额外乘一个"超目标就少取"的系数

★ 本工具会**播放静音**(全 0 数据), 不会发出声音。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.audio import AlignedStreamBuffer, LoopbackRenderer

RATE = 48000


def run(policy: str, seconds: float = 20.0, mismatch: float = 1.003) -> float:
    ren = LoopbackRenderer()
    ren.start()
    block = ren.block_align
    render_target = int(ren.buffer_frames) - int(ren.rate * 0.005)

    buf = AlignedStreamBuffer(channels=ren.channels, block_align=block,
                             bits=ren.bits, rate=ren.rate, name=policy,
                             target_ms=60.0, max_ms=400.0)
    frames_per_packet = int(ren.rate * 0.010)     # 10ms
    pcm = b"\x00" * (frames_per_packet * block)

    t0 = time.monotonic()
    next_push = t0
    traces = []
    last_report = t0
    primed = False

    while time.monotonic() - t0 < seconds:
        now = time.monotonic()
        # 按 mismatch 节奏推包
        while now >= next_push:
            buf.push(int(time.time() * 1e6), pcm)
            next_push += 0.010 / mismatch
            now = time.monotonic()

        avail = ren.available_frames()
        padding = ren.buffer_frames - avail
        level = buf.buffered_ms()
        if not primed and level > 0:
            primed = True

        if primed:
            budget = max(0, min(avail, render_target - padding))
            if policy == "level-aware" and budget > 0:
                over = level - 60.0
                if over > 0:
                    # 超目标越多, 本轮取越少; over 达到 60ms 时完全不取
                    scale = max(0.0, 1.0 - over / 60.0)
                    budget = int(budget * scale)
            if budget > 0:
                chunk = buf.pop_aligned(int(time.time() * 1e6), budget, 0,
                                        ratio_override=1.0)
                if chunk:
                    ren.write(chunk)

        # 每秒记一次轨迹
        if now - last_report >= 1.0:
            traces.append(round(buf.buffered_ms()))
            last_report = now
        time.sleep(0.002)

    final = buf.buffered_ms()
    ren.stop()
    print(f"  {policy:12} 轨迹 {traces}")
    return final


print("== 真实渲染器 + 复刻渲染策略 ==")
print("  (播放的是全 0 静音, 不会出声)")
print()
fa = run("current", 15.0)
print(f"  A 现状      最终水位 {fa:.1f} ms")
print()
fb = run("level-aware", 15.0)
print(f"  B 水位感知  最终水位 {fb:.1f} ms")

print()
print("== 结论 ==")
print(f"  现状 {fa:.0f}ms / 水位感知 {fb:.0f}ms")
if fa > 100 > fb:
    print("  ★★ 确认: 现状策略水位失控, 水位感知能收敛")
elif fa > 100 and fb > 100:
    print("  ★ 两者都失控 -> 我的修正思路不够, 需要更强的手段(主动丢弃)")
else:
    print("  需要更多数据")
