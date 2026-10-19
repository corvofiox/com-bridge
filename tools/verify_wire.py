"""验证"server 发出 -> agent 收到并解包"的完整线路径。

单机无法同时让两端绑定同一个 INPUT_PORT, 因此这里用**独立的接收端口**
模拟 agent 的接收端, 专门验证:
  1. server 的 InputSender 发到目标端口;
  2. 接收端能解包成正确的事件(而不是被当成残包丢弃);
  3. 心跳与输入事件在同一端口上互不干扰。

这三条正是"主电脑转发 2159、副电脑注入 0"故障所涉及的链路。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.protocol import (EV_HEARTBEAT, EV_MOUSE_MOVE_REL,
                                 HEARTBEAT_SIZE, INPUT_HEADER_SIZE, InputEvent)
from com_bridge.transport import InputReceiver, InputSender

RX_PORT = 24913


def main() -> int:
    got: list[InputEvent] = []
    rx = InputReceiver(on_event=got.append, on_heartbeat=lambda a: None)
    rx.port = RX_PORT
    rx.bind_now()
    rx.start()
    time.sleep(0.3)

    tx = InputSender("127.0.0.1", RX_PORT)

    # 1) 混发: 心跳 + 输入事件, 验证互不干扰
    print("[1] 混发心跳与输入事件...")
    for i in range(10):
        tx.send_heartbeat()
        tx.send(InputEvent(EV_MOUSE_MOVE_REL, 0, 0, 20 + i, -5, 123456))
        time.sleep(0.03)
    time.sleep(0.5)

    d = rx.diag()
    print(f"    diag={d}")
    ok1 = d["heartbeats"] == 10 and d["events"] == 10
    ok2 = d["short"] == 0 and d["bad"] == 0
    print(f"    心跳 10 / 事件 10: {'PASS' if ok1 else 'FAIL'}")
    print(f"    无残包/坏包:      {'PASS' if ok2 else 'FAIL'}")

    # 2) 事件内容必须正确解出(而非长度对但内容错)
    print("\n[2] 事件内容校验...")
    if got:
        first = got[0]
        print(f"    首个事件: type={first.type} a={first.a} b={first.b} "
              f"ts={first.ts_us}")
    ok3 = bool(got) and got[0].type == EV_MOUSE_MOVE_REL and got[0].a == 20 \
        and got[0].b == -5 and got[0].ts_us == 123456
    print(f"    字段正确(type/a/b/ts): {'PASS' if ok3 else 'FAIL'}")

    # 3) 序号/位移连续性: 10 个事件的 a 应是 20..29
    ok4 = [e.a for e in got] == list(range(20, 30))
    print(f"\n[3] 位移序列连续(20..29): {'PASS' if ok4 else 'FAIL'}")
    if not ok4:
        print(f"    实际: {[e.a for e in got]}")

    # 4) 长度契约
    print(f"\n[4] 长度契约: 心跳 {HEARTBEAT_SIZE} < 输入 {INPUT_HEADER_SIZE} "
          f"= {'PASS' if HEARTBEAT_SIZE < INPUT_HEADER_SIZE else 'FAIL'}")
    ok5 = HEARTBEAT_SIZE < INPUT_HEADER_SIZE and len(InputEvent(
        EV_MOUSE_MOVE_REL, 0, 0, 1, 1, 1).pack()) == INPUT_HEADER_SIZE
    print(f"    输入事件打包长度 == {INPUT_HEADER_SIZE}: "
          f"{'PASS' if ok5 else 'FAIL'}")

    tx.close()
    rx.stop()

    allok = ok1 and ok2 and ok3 and ok4 and ok5
    print(f"\n[结论] {'PASS —— 线路径完好' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
