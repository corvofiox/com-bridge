"""验证 NAT 打洞的**端口**学习是否正确。

这是上一轮遗漏的关键点: 只学 IP 而沿用固定端口 24801 是无效的 ——
NAT 映射后的源端口几乎总是随机高位端口。

测试方式: 让 agent 侧的接收 socket 绑在**随机端口**(模拟 NAT 映射后的
端口), 从该 socket 发心跳; server 侧应学到 (IP, 该随机端口) 并用它作为
转发目标 —— 随后 server 发来的输入事件必须能被该 socket 收到。
"""

from __future__ import annotations

import socket
import sys
import time

sys.path.insert(0, ".")

from com_bridge.protocol import EV_MOUSE_MOVE_REL, InputEvent
from com_bridge.transport import InputReceiver, InputSender

SERVER_PORT = 24921


def main() -> int:
    # --- agent 侧: 接收端绑随机端口(模拟 NAT 映射出的源端口) ---
    got: list[InputEvent] = []
    hb_from_server: list[tuple] = []
    agent_rx = InputReceiver(on_event=got.append,
                             on_heartbeat=lambda a: hb_from_server.append(a))
    agent_rx.port = 0                    # 0 = 让系统分配随机端口
    agent_rx.bind_now()
    agent_rx.start()
    time.sleep(0.2)
    mapped_port = agent_rx.bound_port
    print(f"[agent] 接收端绑在随机端口 {mapped_port} (模拟 NAT 映射端口)")

    # --- server 侧: 接收端 + 已知的"网关地址"(错误目标) ---
    learned: list[tuple] = []
    srv_rx = InputReceiver(on_event=lambda ev: None,
                           on_heartbeat=lambda a: learned.append(a))
    srv_rx.port = SERVER_PORT
    srv_rx.bind_now()
    srv_rx.start()
    time.sleep(0.2)

    # agent 从**接收 socket** 向 server 发心跳
    ok_send = agent_rx.send_heartbeat_to(("127.0.0.1", SERVER_PORT))
    print(f"[agent] 从接收 socket 发心跳: {'成功' if ok_send else '失败'}")
    time.sleep(0.4)

    # --- server 学到的必须是 (IP, 随机端口) ---
    print(f"\n[server] 收到心跳, 源地址 = {learned}")
    if not learned:
        print("[结论] FAIL —— 没收到心跳")
        return 1
    ip, port = learned[0][0], learned[0][1]
    ok_port = port == mapped_port
    print(f"  学到端口 {port}, agent 接收端口 {mapped_port} "
          f"-> {'PASS' if ok_port else 'FAIL'}")
    if not ok_port:
        print("  !! 端口不一致 —— 打洞后 server 发的包 agent 收不到")

    # --- 关键验证: server 用学到的 (IP, 端口) 发输入, agent 必须收到 ---
    print("\n[关键] server 用学到的 (IP, 端口) 发送输入事件...")
    tx = InputSender(ip, port)
    for i in range(5):
        tx.send(InputEvent(EV_MOUSE_MOVE_REL, 0, 0, 10 + i, 0, 0))
        time.sleep(0.05)
    time.sleep(0.4)
    print(f"  agent 收到 {len(got)} 个事件 (期望 5)")
    ok_recv = len(got) == 5 and [e.a for e in got] == [10, 11, 12, 13, 14]
    print(f"  内容正确: {'PASS' if ok_recv else 'FAIL'} "
          f"({[e.a for e in got]})")

    # --- 反向: server 也从接收 socket 发心跳, agent 应学到 ---
    print("\n[反向] server 从接收 socket 发心跳...")
    srv_rx.send_heartbeat_to((ip, mapped_port))
    time.sleep(0.4)
    print(f"  agent 收到 server 心跳 {len(hb_from_server)} 个 "
          f"(期望 >=1)")
    ok_back = len(hb_from_server) >= 1

    tx.close()
    agent_rx.stop()
    srv_rx.stop()

    print()
    allok = ok_send and ok_port and ok_recv and ok_back
    print(f"[结论] {'PASS —— 端口学习与双向打洞均正确' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
