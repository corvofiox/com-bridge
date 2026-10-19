# -*- coding: utf-8 -*-
"""★ 在真实 Server 里打印 peer 缓冲的实际参数与水位变化。

不依赖任何推断 —— 直接读运行中的对象:
  * peer_buf.max_ms / target_ms / min_ms
  * 水位随时间的变化曲线(push/pop 每轮都记录)
跑 20 秒, 每秒打印一次。若有 peer 连上(副机在发音频), 会看到真实水位。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.server import Server

print("== 起 Server(真实配置) ==")
srv = Server(name="watermark-diag", side="right")
srv.start()
time.sleep(1.5)

peer = srv._peer
if peer is None:
    print("  _peer 还没创建(渲染循环未启动?)")
else:
    print(f"  peer 缓冲参数:")
    print(f"    target_ms = {peer.target_ms}")
    print(f"    min_ms    = {peer.min_ms}")
    print(f"    max_ms    = {peer.max_ms}      <-- 水位理论上界")
    print(f"    rate      = {peer.rate}")
    print(f"    block_align = {peer.block_align}")
    print(f"    max_frames = {int(peer.rate * peer.max_ms / 1000)} 帧")
    print()
    print("  观测 20 秒(每秒一次):")
    for i in range(20):
        with peer._lock:
            n_chunks = len(peer._buf)
        bm = peer.buffered_ms()
        print(f"    t={i+1:2d}s  水位={bm:7.1f}ms  包数={n_chunks:4d}  "
              f"丢={peer.dropped}  欠载={peer.underruns}  "
              f"ratio={peer.ratio:.6f}")
        time.sleep(1.0)

    print()
    print(f"  最终: 水位={peer.buffered_ms():.1f}ms  max_ms={peer.max_ms}")
    if peer.buffered_ms() > peer.max_ms + 1.0:
        print("  ★★ 水位突破 max_ms —— push() 的丢弃逻辑有漏洞")
    else:
        print("  ✓ 水位未突破 max_ms")

srv.stop()
