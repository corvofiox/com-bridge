# -*- coding: utf-8 -*-
"""★★★ 验证: 积分项权限 ±0.2% 是否足够压住水位?

## 推断的故障链
1. WASAPI 共享模式的实际消费速率 != 48000(受设备时钟支配, 可略低)
2. 前馈 ratio_ff = arrival_rate/48000 ≈ 1.000(供给正常)
   -> 前馈**不提供任何纠偏力**
3. 积分项负责压水位, 但权限被限制在 ±0.2%(RATIO_TRIM_LIMIT)
4. 若真实失配 > 0.2%, 积分项饱和 -> 水位持续上涨 -> 撞 max_ms

## 本实验
模拟"消费比供给慢 0.3%"(典型设备时钟偏差), 看:
  A. 积分限幅 ±0.2%(现状) -> 水位能否压住
  B. 积分限幅 ±1.5%(放宽) -> 水位能否压住
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from com_bridge.audio import AlignedStreamBuffer

RATE = 48000
BLOCK = 8
FRAMES = 480
PCM = b"\x00" * (FRAMES * BLOCK)

# 场景: 供给 48000 帧/s, 但消费侧实际只消耗 47856 帧/s(慢 0.3%)
CONSUME_RATE = 48000 * 0.997


def simulate(trim_limit: float, seconds: float = 120.0,
             deadzone: float = 0.15, ki: float = 0.00002) -> list[float]:
    buf = AlignedStreamBuffer(channels=2, block_align=BLOCK, bits=32,
                              rate=RATE, name="sim", target_ms=60.0,
                              max_ms=400.0)
    integral = 0.0
    t = 0.0
    push_acc = 0.0
    # 消费侧: 每 10ms 能"播掉" CONSUME_RATE*0.01 帧
    consume_acc = 0.0
    traces = []
    while t < seconds:
        # 供给: 每 10ms 到一包
        push_acc += 1.0
        if push_acc >= 1.0:
            buf.push(int(t * 1e6), PCM)
            push_acc -= 1.0
        # 消费: 每 10ms 播掉 CONSUME_RATE/100 帧
        consume_acc += CONSUME_RATE * 0.01
        while consume_acc >= FRAMES:
            got = buf.pop_aligned(int(t * 1e6), FRAMES, 0,
                                  ratio_override=1.0)
            if got:
                consume_acc -= FRAMES
            else:
                break
        # 控制器(复刻 server 逻辑)
        level = buf.buffered_ms()
        ratio_ff = 1.0                     # 前馈认为供给正常
        pi_err = (level - 60.0) / 60.0
        if abs(pi_err) > deadzone:
            excess = pi_err - (deadzone if pi_err > 0 else -deadzone)
            integral += excess * ki
            integral = max(-trim_limit, min(trim_limit, integral))
        # 注意: ratio 变大 -> 消费更快(见 exp_ratio_direction 实测)
        if integral > 0:
            # 用额外的消费来体现"ratio 提升"(每次多取 integral 比例的帧)
            extra = int(FRAMES * integral)
            if extra > 0:
                buf.pop_aligned(int(t * 1e6), extra, 0, ratio_override=1.0)
        t += 0.010
        if abs(t % 10.0) < 0.011:
            traces.append(round(level))
    return traces


print("== 场景: 消费比供给慢 0.3%(设备时钟偏差, 前馈无法察觉) ==")
print()
ta = simulate(0.002)      # 现状: ±0.2%
print(f"  A 积分限幅 ±0.2% (现状)")
print(f"    水位轨迹(每10s): {ta}")

print()
tb = simulate(0.015)      # 放宽: ±1.5%
print(f"  B 积分限幅 ±1.5% (放宽)")
print(f"    水位轨迹(每10s): {tb}")

print()
print("== 结论 ==")
print(f"  现状最终水位 {ta[-1]}ms / 放宽后 {tb[-1]}ms (目标 60ms)")
if ta[-1] > 200 and tb[-1] < 150:
    print("  ★★ 确认: 积分权限 ±0.2% 不足以吸收设备时钟失配(0.3%),")
    print("     水位必然撞上限; 放宽到 ±1.5% 可压住。")
elif ta[-1] > 200:
    print("  ★ 两者都失控 -> 需要更强的手段(超阈值主动丢弃)。")
