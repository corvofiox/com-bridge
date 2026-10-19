# -*- coding: utf-8 -*-
"""★★★ 验证: budget 不受水位约束 -> 水位无法被闭环拉回目标。

## 代码事实(server.py:899)
    budget = max(0, min(avail, render_target - padding))
`budget` 只受 **WASAPI 可用空间** 约束, 与 peer 队列水位**无关**。
于是每一轮只要 WASAPI 有空位, 就把队列里的数据搬过去 ——
队列水位**永远不会因为"太满"而减少取数**。

## 那水位为什么没飞到 max_ms?
因为它被 push() 的丢弃逻辑限制在 400ms(用户看到"加到 490 就不走了"
也是这个钳位在起作用, 只是钳位值与观测点不同)。

## 但用户说"逐渐增加"——说明稳态水位远高于 target_ms(60ms),
## 也就是**闭环失效**: 目标 60ms, 实际爬到 400ms 封顶。

## 本工具
直接构造 Buffer + 模拟 render 循环两种策略, 对比水位收敛:
  A. 现状: budget 只看 WASAPI 空间(不受水位约束)
  B. 修正: budget 还应受"水位超出目标的部分"约束
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.audio import AlignedStreamBuffer

RATE = 48000
BLOCK = 8
FRAMES = 480          # 10ms 包
PCM = b"\x00" * (FRAMES * BLOCK)


def run(policy: str, seconds: int = 30, mismatch: float = 1.0) -> tuple[float, list[float]]:
    """模拟渲染循环。policy: 'current' / 'level-aware'。

    mismatch: 到达速率相对消费的倍数。1.0 = 完全相等(只测闭环能力);
              1.003 = 供给快 0.3%(模拟晶振失配, 需要闭环吸收)。
    """
    buf = AlignedStreamBuffer(channels=2, block_align=BLOCK, bits=32,
                              rate=RATE, name=policy, target_ms=60.0,
                              max_ms=400.0)
    render_target = int(RATE * 0.095)      # 95ms(实测 WASAPI 值)
    rendered = 0
    history = []
    t = 0.0
    push_acc = 0.0
    while t < seconds:
        # agent 每 10ms 推一包
        push_acc += 0.010 * mismatch
        while push_acc >= 0.010:
            buf.push(int(t * 1e6), PCM)
            push_acc -= 0.010
        # 渲染循环每 2ms 一轮
        avail = render_target - rendered
        if policy == "current":
            budget = max(0, min(avail, render_target))
        else:
            level = buf.buffered_ms()
            over = level - 60.0
            if over <= 0:
                budget = max(0, min(avail, render_target))
            else:
                scale = max(0.0, 1.0 - over / 30.0)
                budget = int(max(0, min(avail, render_target)) * scale)
        if budget > 0:
            got = buf.pop_aligned(int(t * 1e6), budget, 0, ratio_override=1.0)
            if got:
                rendered = min(render_target, rendered + len(got) // BLOCK)
        rendered = max(0, rendered - int(RATE * 0.002))
        t += 0.002
        if abs(t % 1.0) < 0.002:
            history.append(buf.buffered_ms())
    return buf.buffered_ms(), history


def fmt(h: list[float]) -> str:
    return "[" + ", ".join(f"{x:.0f}" for x in h[::3]) + "]"


print("== 场景 1: 精确等速(mismatch=1.0) —— 只测闭环能否稳住目标 ==")
fa, ha = run("current", 30, 1.0)
fb, hb = run("level-aware", 30, 1.0)
print(f"  A 现状   30s 后={fa:.1f}ms  轨迹 {fmt(ha)}")
print(f"  B 水位感知 30s 后={fb:.1f}ms  轨迹 {fmt(hb)}")

print()
print("== 场景 2: 供给快 0.3%(mismatch=1.003) —— 模拟晶振失配 ==")
fc, hc = run("current", 30, 1.003)
fd, hd = run("level-aware", 30, 1.003)
print(f"  A 现状   30s 后={fc:.1f}ms  轨迹 {fmt(hc)}")
print(f"  B 水位感知 30s 后={fd:.1f}ms  轨迹 {fmt(hd)}")

print()
print("== 结论 ==")
print(f"  等速:   现状 {fa:.0f}ms / 水位感知 {fb:.0f}ms")
print(f"  快0.3%: 现状 {fc:.0f}ms / 水位感知 {fd:.0f}ms")
