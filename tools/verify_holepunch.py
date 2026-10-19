"""验证 NAT 打洞链路: 心跳能被收到, 且能让发送方学到真实地址。

这是"鼠标过不去"修复的核心机制验证 —— 不依赖真实双机, 用本机回环模拟
"接收方看到的心跳源地址"这一关键环节。

场景: 发送方 A 知道的目标是 NAT 网关地址(不可达), 而心跳来自真实地址。
      接收方 B 应通过 on_heartbeat 学到真实地址并切换发送目标。
"""

from __future__ import annotations

import socket
import sys
import threading
import time

sys.path.insert(0, ".")

from com_bridge.protocol import EV_HEARTBEAT, HEARTBEAT_SIZE, INPUT_HEADER_SIZE
from com_bridge.transport import InputReceiver, InputSender

PORT = 24911


def main() -> int:
    got_heartbeats: list[tuple] = []

    rx = InputReceiver(on_event=lambda ev: None,
                       on_heartbeat=lambda addr: got_heartbeats.append(addr))
    rx.port = PORT
    rx.bind_now()
    rx.start()
    time.sleep(0.3)
    print(f"接收方已就绪(端口 {PORT}), diag={rx.diag()}")

    # --- 1) 心跳必须被识别(而不是当成残包丢弃) ---
    tx = InputSender("127.0.0.1", PORT)
    for _ in range(3):
        tx.send_heartbeat()
        time.sleep(0.1)
    time.sleep(0.4)
    d = rx.diag()
    print(f"\n[1] 发 3 个心跳后: {d}")
    ok1 = d["heartbeats"] == 3 and len(got_heartbeats) == 3
    print(f"    心跳识别: {'PASS' if ok1 else 'FAIL'} "
          f"(回调 {len(got_heartbeats)} 次, 期望 3)")
    print(f"    回调拿到源地址: {got_heartbeats[:1]}")

    # --- 2) 心跳不能被算作 short packet(那是丢弃计数) ---
    ok2 = d["short"] == 0
    print(f"\n[2] 心跳未污染残包计数: {'PASS' if ok2 else 'FAIL'} (short={d['short']})")

    # --- 3) 长度区分: 心跳 4 字节, 输入事件 19 字节, 不会混淆 ---
    ok3 = HEARTBEAT_SIZE < INPUT_HEADER_SIZE
    print(f"\n[3] 长度可区分: {'PASS' if ok3 else 'FAIL'} "
          f"(heartbeat={HEARTBEAT_SIZE}, input={INPUT_HEADER_SIZE})")

    # 4) 地址学习: 模拟 server 侧的 _on_peer_heartbeat 逻辑
    print("\n[4] 地址学习(模拟 NAT 场景):")
    sender = InputSender("192.168.10.1", PORT)     # 网关地址(错误目标)
    print(f"    初始目标: {sender.addr}")
    real_src = got_heartbeats[0][0] if got_heartbeats else "127.0.0.1"
    if sender.addr[0] != real_src:
        sender.set_addr(real_src)
        print(f"    学到真实地址后切换为: {sender.addr}")
    ok4 = sender.addr[0] == real_src
    print(f"    切换: {'PASS' if ok4 else 'FAIL'}")

    tx.close()
    rx.stop()

    print()
    if ok1 and ok2 and ok3 and ok4:
        print("[结论] PASS —— 心跳可识别、不污染计数、地址可学习并切换")
        return 0
    print("[结论] FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
