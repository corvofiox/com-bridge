"""端到端验证方位下发链路(不用端口 hack)。

做法: 只启动 server 的**输入接收端**(不启动 audio/control, 避开端口冲突),
再用一个独立 socket 模拟副机 —— 它先发心跳让 server 学到地址, 然后
server 的反向心跳应带着 side 回来。

这精确验证"主电脑把自己的方位下发给副电脑"这条生产路径。
"""

from __future__ import annotations

import socket
import sys
import time

sys.path.insert(0, ".")

from com_bridge.protocol import EV_HEARTBEAT
from com_bridge.transport import (InputReceiver, InputSender, heartbeat_payload,
                                 parse_heartbeat)

SRV_PORT = 24981


def main() -> int:
    srv_got_sides: list[str] = []
    srv_got_hb: list[tuple] = []
    rx = InputReceiver(on_event=lambda e: None,
                       on_heartbeat=lambda a: srv_got_hb.append(a),
                       on_peer_side=srv_got_sides.append)
    rx.port = SRV_PORT
    rx.bind_now()
    rx.start()
    time.sleep(0.2)

    # --- 模拟副机: 自己的接收端 + 从该 socket 发心跳 ---
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("0.0.0.0", 0))
    peer.settimeout(1.5)
    peer_port = peer.getsockname()[1]
    print(f"副机模拟端绑定端口 {peer_port}, 主电脑接收端 {SRV_PORT}")

    # 副机发心跳(带自己的"方位"= None, 因为副机不下发方位)
    peer.sendto(heartbeat_payload(None), ("127.0.0.1", SRV_PORT))
    time.sleep(0.3)
    print(f"\n[1] 主电脑收到副机心跳: {len(srv_got_hb)} 个, "
          f"来源 {srv_got_hb[:1]}")

    # --- 主电脑反向发心跳, **带上自己的 side=right** ---
    ok_send = rx.send_heartbeat_to(("127.0.0.1", peer_port), side="right")
    print(f"[2] 主电脑下发方位 right: {'已发出' if ok_send else '发送失败'}")

    # 副机侧收包
    got_side = None
    try:
        data, addr = peer.recvfrom(2048)
        got_side = parse_heartbeat(data)
        print(f"    副机收到 {len(data)} 字节, 解析方位 = {got_side}")
    except socket.timeout:
        print("    副机超时, 未收到心跳")

    # --- 反向验证: 副机也发一次带方位的(实际不会, 但确保解析对称) ---
    peer.sendto(heartbeat_payload("left"), ("127.0.0.1", SRV_PORT))
    time.sleep(0.3)
    print(f"\n[3] 主电脑解析副机方位: {srv_got_sides}")

    peer.close()
    rx.stop()
    ok1 = len(srv_got_hb) >= 1
    ok2 = got_side == "right"
    ok3 = srv_got_sides == ["left"]
    print()
    print(f"主电脑收到副机心跳: {'PASS' if ok1 else 'FAIL'}")
    print(f"副机收到主电脑方位: {'PASS' if ok2 else 'FAIL'}")
    print(f"主电脑能解析方位:   {'PASS' if ok3 else 'FAIL'}")
    allok = ok1 and ok2 and ok3
    print(f"\n[结论] {'PASS —— 方位下发链路完整可用' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
