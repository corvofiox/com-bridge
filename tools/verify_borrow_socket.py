"""验证"借接收 socket 发事件"这条路径 —— 端口限制型 NAT 的必需条件。

背景(用户实测的决定性证据):
    副电脑: 收到 37 包 (来自 192.168.10.12), 心跳 37   ← 心跳全到
    事件: 0                                          ← 事件全丢
两者目标地址完全相同, 唯一区别是发送用的 socket。心跳从接收 socket
(24801)发出所以 NAT 认; 事件从 InputSender 的临时端口发出所以被丢弃。

本测试验证:
  1. 用 InputSender 自建 socket 发 -> 源端口是临时端口(会被 NAT 丢);
  2. use_socket(接收端) 之后发 -> **源端口 == 接收端口**(NAT 认得);
  3. 两种方式对方都能收到(本机无 NAT, 所以都能到; 关键看源端口)。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.protocol import EV_MOUSE_MOVE_REL, InputEvent
from com_bridge.transport import InputReceiver, InputSender

RX_PORT = 24931


def main() -> int:
    got: list[tuple] = []          # (事件, 源地址)
    raw_from: list[tuple] = []

    # 接收端: 记录每个包的**来源端口**(关键指标)。
    # ★ 复用 bind_now() 已经绑好的 socket, 不要再 bind 一次(独占绑定会失败)。
    class SpyRx(InputReceiver):
        def run(self) -> None:
            s = self._sock
            if s is None:
                return
            s.settimeout(0.5)
            while not self._stop.is_set():
                try:
                    data, addr = s.recvfrom(2048)
                except Exception:
                    continue
                self.packets += 1
                self.last_from = addr[0]
                raw_from.append(addr)
                if len(data) >= 19:
                    self.events += 1
                    if self._on_event:
                        self._on_event(InputEvent.unpack(data))

    rx = SpyRx(on_event=got.append)
    rx.port = RX_PORT
    rx.bind_now()
    rx.start()
    time.sleep(0.3)

    # --- 1) 自建 socket 发送: 源端口是临时端口 ---
    tx = InputSender("127.0.0.1", RX_PORT)
    tx.send(InputEvent(EV_MOUSE_MOVE_REL, 0, 0, 1, 0, 0))
    time.sleep(0.3)
    src_own = raw_from[-1][1] if raw_from else None
    print(f"[1] 自建 socket 发送: 源端口 = {src_own} "
          f"(接收端口 = {RX_PORT})")
    ok1 = src_own is not None and src_own != RX_PORT
    print(f"    源端口 != 接收端口: {'符合预期(会被 NAT 丢弃)' if ok1 else '意外'}")

    # --- 2) 借用接收 socket 发送: 源端口必须 == 接收端口 ---
    tx.use_socket(rx._sock, rx)
    before = len(raw_from)
    tx.send(InputEvent(EV_MOUSE_MOVE_REL, 0, 0, 2, 0, 0))
    time.sleep(0.3)
    src_borrowed = raw_from[-1][1] if len(raw_from) > before else None
    print(f"\n[2] 借用接收 socket 发送: 源端口 = {src_borrowed}")
    ok2 = src_borrowed == RX_PORT
    print(f"    源端口 == 接收端口({RX_PORT}): "
          f"{'PASS —— NAT 会认这条回程' if ok2 else 'FAIL'}")

    # --- 3) 事件计数正确(借用路径不应丢事件) ---
    print(f"\n[3] 收到事件数 = {len(got)} (期望 2)")
    ok3 = len(got) == 2
    print(f"    {'PASS' if ok3 else 'FAIL'}")

    # --- 4) 借用后 InputSender 自身统计也应更新 ---
    print(f"\n[4] InputSender: sent={tx.sent} failed={tx.failed}")
    ok4 = tx.sent == 2
    print(f"    {'PASS' if ok4 else 'FAIL'}")

    tx.close()
    rx.stop()

    allok = ok1 and ok2 and ok3 and ok4
    print()
    print(f"[结论] {'PASS —— 借用 socket 后源端口正确, 事件可达' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
