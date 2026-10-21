# -*- coding: utf-8 -*-
"""活体验证 B: 副机等待主电脑 —— 主机晚到也能自动握手, 且随时可取消。

复现用户的真实场景(两台机器都开机自启, 主机更慢):
  阶段 1  主机还没起来: 副机必须**停留在等待状态**, 不报错、不退出
  阶段 2  主机起来了:     等待中的副机必须在几秒内完成握手(版本协商 + 对时)
  阶段 3  用户按停止:     等待必须立刻结束, 且归类为"取消"而不是"失败"
  阶段 4  设了上限:       超时才报错, 且信息可读

本工具不起音频/输入链路(只测控制通道), 因此**不会影响正在运行的实例**,
也不占用 4010/24801 等正式端口。
"""
from __future__ import annotations

import socket
import threading
import time

from com_bridge.agent import Agent, HostWaitCancelled
from com_bridge.transport import ControlServer


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def make_agent(port: int, wait_s: float = 0.0) -> Agent:
    return Agent(server_host="127.0.0.1", control_port=port,
                 wait_for_host_s=wait_s, enable_return=False,
                 enable_clipboard=False, mute_local=False)


def phase1_and_2() -> bool:
    port = free_port()
    a = make_agent(port, wait_s=0.0)
    result = {}

    def run() -> None:
        t0 = time.monotonic()
        try:
            ctrl = a._wait_control("127.0.0.1")
            result["ok"] = (time.monotonic() - t0, ctrl)
        except BaseException as exc:  # noqa: BLE001
            result["err"] = exc

    print(f"--- 阶段1/2: 先无主机({port}), 2.5s 后再起主机 ---")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    for i in range(5):
        time.sleep(0.5)
        st = a.host_wait_state()
        if st:
            phase, elapsed, attempts = st
            print(f"    t+{i * 0.5 + 0.5:.1f}s  等待中: 阶段={phase} "
                  f"已等={elapsed:.1f}s 探测={attempts}")
            if "err" in result:
                print(f"   !! 提前失败: {result['err']}")
                return False
    srv = ControlServer(port=port, name="late-host")
    srv.bind_now()
    srv.set_shape(0, 0, 1920, 1080)
    srv.start()
    print("    主机已启动 -> 等待中的副机应当自动握手")
    t.join(timeout=20)
    try:
        if "err" in result:
            print(f"   !! 等待期间异常退出: {result['err']}")
            return False
        waited, ctrl = result["ok"]
        minority = ctrl.negotiated_minor
        print(f"    ✅ 握手完成: 共等 {waited:.1f}s, 版本协商={minority}")
        ctrl.close()
        return waited >= 2.0 and minority is not None
    finally:
        srv.stop()


def phase3() -> bool:
    port = free_port()
    a = make_agent(port, wait_s=0.0)
    outcome = []

    def run() -> None:
        try:
            a._wait_control("127.0.0.1")
            outcome.append("connected")
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    print('--- 阶段3: 等待中按停止 -> 必须归类为"取消" ---')
    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.6)
    t0 = time.monotonic()
    a.stop()
    t.join(timeout=10)
    took = time.monotonic() - t0
    cancelled = bool(outcome) and isinstance(outcome[0], HostWaitCancelled)
    print(f"    停止后 {took:.2f}s 退出; 归类={'取消' if cancelled else outcome}")
    return (not t.is_alive()) and cancelled


def phase4() -> bool:
    port = free_port()
    a = make_agent(port, wait_s=0.6)
    print("--- 阶段4: 设上限 0.6s -> 超时必须给出可读错误 ---")
    try:
        a._wait_control("127.0.0.1")
        print("   !! 应当超时却成功了")
        return False
    except RuntimeError as exc:
        text = str(exc)
        good = "等待主电脑超时" in text
        print(f"    {'✅' if good else '!!'} {text.splitlines()[0]}")
        return good


def main() -> int:
    ok12 = phase1_and_2()
    ok3 = phase3()
    ok4 = phase4()
    ok = ok12 and ok3 and ok4
    print("\n==== 汇总 ====")
    print(f"  阶段1/2 等待->握手: {'通过' if ok12 else '失败'}")
    print(f"  阶段3   停止=取消:  {'通过' if ok3 else '失败'}")
    print(f"  阶段4   超时报错:   {'通过' if ok4 else '失败'}")
    return 0 if ok else 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
