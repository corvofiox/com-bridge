# -*- coding: utf-8 -*-
"""★★★ 实测: 不同大小的剪贴板内容在真实 UDP 上的成功重组率。

## 为什么必须实测
算出来 8MB = 8389 块, 理论成功率(0.01% 丢包)只有 43%。
但"理论"不一定等于实际 —— 本机回环几乎不丢包, 真实局域网才有丢包。
本工具先测回环(确认机制本身没问题), 再评估真实环境的可用档位。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge import clipboard_sync as cs
from com_bridge.transport import ClipboardReceiver, ClipboardSender

PORT = 24805


def trial(size_mb: float) -> tuple[bool, float, int]:
    """发一份 size_mb 的内容, 返回 (是否成功, 耗时, 块数)。"""
    got = []
    rx = ClipboardReceiver(port=PORT, on_payload=lambda p: got.append(p))
    rx.start()
    time.sleep(0.25)
    tx = ClipboardSender("127.0.0.1", PORT)

    n = int(size_mb * 1024 * 1024)
    payload = {"text": b"x" * n}
    blob = cs._encode(payload)
    chunks = len(cs.split_payload(1, blob))

    t0 = time.monotonic()
    tx.send_blob(blob, 1)
    # 等到收齐或超时(给足时间)
    deadline = t0 + max(3.0, size_mb * 3.0)
    while time.monotonic() < deadline and not got:
        time.sleep(0.05)
    dt = time.monotonic() - t0
    ok = bool(got) and got[0] == payload
    tx.close()
    rx.stop()
    time.sleep(0.15)
    return ok, dt, chunks


print("== 回环 UDP 上的实测(几乎不丢包, 反映机制上限) ==")
print()
print(f"{'大小':>8} {'块数':>8} {'成功':>6} {'耗时':>8} {'吞吐':>10}")
for mb in (0.1, 0.5, 1, 2, 4, 8):
    ok, dt, chunks = trial(mb)
    thr = (mb / dt) if dt > 0 else 0
    print(f"{mb:>7.1f}MB {chunks:>8,} {'是' if ok else '否':>6} "
          f"{dt:>7.2f}s {thr:>8.1f}MB/s")

print()
print("== 理论成功率(按块丢失独立建模) ==")
print("  内容被切成 N 块, 任一块丢失整份作废。成功率 = (1-p)^N")
print()
print(f"{'大小':>8} {'块数':>8} {'p=0.001%':>10} {'p=0.01%':>10} {'p=0.1%':>10}")
for mb in (0.1, 0.5, 1, 2, 4, 8):
    n = int(mb * 1024 * 1024 / cs.CHUNK_SIZE) + 2
    row = f"{mb:>7.1f}MB {n:>8,}"
    for p in (0.00001, 0.0001, 0.001):
        row += f" {100*(1-p)**n:>9.1f}%"
    print(row)

print()
print("== 结论 ==")
print("  1MB 以内: 即使 0.1% 丢包也有 35% 成功率 -> 可用")
print("  4MB:      0.01% 丢包下 66%, 0.1% 下 1.5% -> 不稳")
print("  8MB:      0.01% 丢包下 43%, 0.1% 下 0.02% -> 基本不可用")
print()
print("  => 上限设 8MB 技术上成立, 但**实际可用区间约 1~2MB**。")
print("     更大的内容需要重传/确认机制(当前没有)。")
