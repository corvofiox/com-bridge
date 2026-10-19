# -*- coding: utf-8 -*-
"""★★★ 定位 Resampler 缓冲增长的**真正**机制。

## 已知
ratio=0.985 时内部缓冲从 96 涨到 4094(触上限), 且截断会产生跳变。
ratio=1.0 / 1.015 时稳定在 96~98。

## 代码
    avail_out = (avail - KEEP_FRAMES - self._pos) / ratio
    out_n = int(avail_out)
    if max_out is not None:
        out_n = min(out_n, max_out)          # <-- 关键限幅
    ...
    self._pos += out_n * ratio

若 max_out 长期小于 avail_out, 每轮消费 out_n*ratio 个输入帧,
而新到 480 帧。ratio<1 时 out_n*ratio 可能 < 480 -> 缓冲涨。

## 验证
直接算: 稳态下 avail=480+96=576 左右时, avail_out 是多少?
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

print("== 手算: 每轮 480 帧输入, max_out=480 ==")
KEEP = 96
for ratio in (0.985, 1.0, 1.015):
    print(f"\n  ratio = {ratio}")
    buf = float(KEEP + 480)      # 初始: 上一轮剩余 + 新到
    pos = 0.0
    for round_ in range(6):
        avail_out = (buf - KEEP - pos) / ratio
        out_n = int(avail_out)
        out_n = min(out_n, 480)          # max_out
        consumed = out_n * ratio
        pos += consumed
        drop = int(pos)
        buf = buf - drop
        pos -= drop
        buf += 480                        # 新到一包
        print(f"    轮{round_}: avail_out={avail_out:7.1f} out_n={out_n}"
              f" 消费={consumed:6.1f} 缓冲={buf:7.1f}")

print()
print("== 结论 ==")
print("  若 out_n 被 max_out=480 卡住, 而实际可产出更多,")
print("  则 ratio<1 时消费不足 -> 缓冲增长。")
print("  这解释了为何 ratio<1 才增长: ratio<1 时 avail_out 被放大,")
print("  更容易超过 max_out=480 -> 被卡 -> 消费不足。")
