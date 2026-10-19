"""验证本轮两个修复:

A. **方位下发**: 主电脑把自己的 side 编进心跳, 副电脑据此确定回传边 ——
   于是"主机 right + 副机 left"这种配置也能正常工作(此前会完全进不去)。
B. **穿越态可靠退出**: 光标明显回到屏内时必须立即停止转发 ——
   否则副机光标会跟着主机一起动、主机还没到边缘副机就动了。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.input import EdgeDetector, ScreenRect
from com_bridge.transport import (InputReceiver, InputSender, heartbeat_payload,
                                 parse_heartbeat)

MON = ScreenRect(0, 0, 1920, 1080)


def test_side_encoding() -> bool:
    """A1: 方位编解码必须完整往返。"""
    print("A1. 方位编解码往返:")
    ok = True
    for side in ("right", "left", "top", "bottom"):
        payload = heartbeat_payload(side)
        got = parse_heartbeat(payload)
        good = got == side
        ok = ok and good
        print(f"   {'OK ' if good else 'FAIL'} {side:7s} -> {payload!r} "
              f"-> {got}")
    # 不带方位时必须是 NONE(旧版兼容: 副机退回本机配置)
    p = heartbeat_payload(None)
    good = parse_heartbeat(p) is None and p[0] == 5 and len(p) == 4
    ok = ok and good
    print(f"   {'OK ' if good else 'FAIL'} 无方位   -> {p!r} -> None")
    return ok


def test_side_delivery() -> bool:
    """A2: 副机必须能从心跳里收到主机的方位。"""
    print("\nA2. 方位经 UDP 心跳下发:")
    got_sides: list[str] = []
    rx = InputReceiver(on_event=lambda e: None,
                       on_heartbeat=lambda a: None,
                       on_peer_side=got_sides.append)
    rx.port = 24951
    rx.bind_now()
    rx.start()
    time.sleep(0.2)

    tx = InputSender("127.0.0.1", 24951)
    # 主电脑侧: 从接收 socket 发心跳并带方位
    rx_side = "right"
    rx.send_heartbeat_to(("127.0.0.1", 24951), side=rx_side)
    time.sleep(0.4)
    ok = got_sides == [rx_side]
    print(f"   下发 right -> 收到 {got_sides}  {'OK' if ok else 'FAIL'}")
    tx.close()
    rx.stop()
    return ok


def test_reliable_exit() -> bool:
    """B: 穿越态的坐标退出判据。"""
    print("\nB. 穿越态可靠退出(按光标坐标):")
    d = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side="right",
                     confirm_frames=2, hysteresis=8)
    # 进入穿越态
    d.update(1919, 540)
    entered = d.update(1919, 540)
    print(f"   进入穿越态: {entered}  {'OK' if entered else 'FAIL'}")

    # 光标回到屏内(距边界 > 迟滞带) -> 必须退出
    exited = d.maybe_exit(1900, 540)
    print(f"   光标到 1900(距边界 19px) -> 退出={exited} "
          f"{'OK' if exited else 'FAIL'}")
    ok1 = entered and exited and not d.is_outside

    # 光标仍在迟滞带内 -> 不应退出(防抖动)
    d.update(1919, 540)
    d.update(1919, 540)
    still = not d.maybe_exit(1915, 540) and d.is_outside
    print(f"   光标到 1915(带内 4px)  -> 保持穿越="
          f"{d.is_outside} {'OK' if still else 'FAIL'}")
    return ok1 and still


def main() -> int:
    a1 = test_side_encoding()
    a2 = test_side_delivery()
    b = test_reliable_exit()
    print()
    allok = a1 and a2 and b
    print(f"[结论] {'PASS —— 方位下发与穿越退出均正确' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
