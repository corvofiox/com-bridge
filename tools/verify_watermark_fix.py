# -*- coding: utf-8 -*-
"""★★★ 验证水位修复: 放宽积分权限 + 水位硬保护。

## 复现(修复前)
tools/exp_integral_authority.py 的场景: 消费比供给慢 0.3%
  A 积分限幅 ±0.2%(旧): 水位 10→40→70→...→340→370ms **单调撞顶**
  B 积分限幅 ±1.5%(新): 水位能回落到 17ms

## 本工具验证生产代码的实际行为
直接用 AlignedStreamBuffer.trim_to + 生产同款控制器, 对比:
  1. 只有旧积分限幅(±0.2%)          -> 应撞顶
  2. 新积分限幅(±1.0%)               -> 应压住
  3. 新积分限幅 + trim_to 硬保护     -> 更稳, 且水位不超阈值
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from com_bridge.audio import AlignedStreamBuffer

RATE = 48000
BLOCK = 8
FRAMES = 480
PCM = b"\x00" * (FRAMES * BLOCK)
CONSUME_RATE = RATE * 0.997        # 消费慢 0.3%(设备时钟偏差)


def simulate(trim_limit: float, use_trim: bool,
             seconds: float = 180.0) -> tuple[list[float], int]:
    buf = AlignedStreamBuffer(channels=2, block_align=BLOCK, bits=32,
                              rate=RATE, name="v", target_ms=60.0,
                              max_ms=400.0)
    integral = 0.0
    t = 0.0
    consume_acc = 0.0
    traces = []
    trims = 0
    MAX_LEVEL = 400.0 * 0.6
    while t < seconds:
        buf.push(int(t * 1e6), PCM)          # 每 10ms 一包
        # 消费(设备实际速率)
        consume_acc += CONSUME_RATE * 0.010
        while consume_acc >= FRAMES:
            if buf.pop_aligned(int(t * 1e6), FRAMES, 0,
                               ratio_override=1.0):
                consume_acc -= FRAMES
            else:
                break
        level = buf.buffered_ms()
        # 控制器(生产比例)
        pi_err = (level - 60.0) / 60.0
        if abs(pi_err) > 0.15:
            excess = pi_err - (0.15 if pi_err > 0 else -0.15)
            integral += excess * 0.00002
            integral = max(-trim_limit, min(trim_limit, integral))
        if integral > 0:
            extra = int(FRAMES * integral)
            if extra > 0:
                buf.pop_aligned(int(t * 1e6), extra, 0, ratio_override=1.0)
        # 水位硬保护
        if use_trim and level > MAX_LEVEL:
            if buf.trim_to(60.0):
                trims += 1
        t += 0.010
        if abs(t % 15.0) < 0.011:
            traces.append(round(level))
    return traces, trims


print("== 场景: 消费比供给慢 0.3%, 运行 180 秒(每 15s 记一次) ==")
print()

ta, _ = simulate(0.002, False)
print(f"1. 旧积分限幅 ±0.2%, 无硬保护")
print(f"   轨迹 {ta}")
print(f"   最终 {ta[-1]}ms")
print()

tb, _ = simulate(0.010, False)
print(f"2. 新积分限幅 ±1.0%, 无硬保护")
print(f"   轨迹 {tb}")
print(f"   最终 {tb[-1]}ms")
print()

tc, trims = simulate(0.010, True)
print(f"3. 新积分限幅 ±1.0% + trim_to 硬保护")
print(f"   轨迹 {tc}")
print(f"   最终 {tc[-1]}ms  触发弃帧 {trims} 次")
print()

print("== 结论 ==")
print(f"  旧: 最终 {ta[-1]}ms  (目标 60)")
print(f"  新: 最终 {tb[-1]}ms  (目标 60)")
print(f"  新+硬保护: 最终 {tc[-1]}ms, 弃帧 {trims} 次")
ok = tb[-1] < 200 and tc[-1] < 200
print("  " + ("★ 修复有效: 水位不再单调爬升到上限" if ok
              else "★ 仍需调整"))
sys.exit(0 if ok else 1)
