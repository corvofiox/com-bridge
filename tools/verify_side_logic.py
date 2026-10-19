"""直接验证 Agent._on_server_side: 收到主电脑方位后必须重建回传边。

绕开单机端口冲突(不启动完整 Agent), 直接构造回传状态再投喂方位 ——
这正是用户报的"主机 right + 副机 left 就进不去副机"的修复点。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.agent import Agent, _main_monitor
from com_bridge.input import EdgeDetector, ScreenRect


def make_agent(side: str) -> Agent:
    """构造一个只够测方位逻辑的 Agent(不启动任何线程/端口)。"""
    ag = Agent(server_host="127.0.0.1", name="t", mute_local=False,
               discover_timeout_s=0.1, tone_hz=440.0, side=side,
               enable_return=True)
    # 模拟 _start_return 已按本机配置建好回传边
    mon = _main_monitor()
    back_side = {"right": "left", "left": "right",
                 "top": "bottom", "bottom": "top"}[side]
    ag._return_edge = EdgeDetector(
        self_rect=mon, peer_rect=ScreenRect(0, 0, 0, 0), side=back_side)
    return ag


def main() -> int:
    print("场景: 主电脑 side=right, 副电脑本机配置被填成 left(用户实际配错的情形)")
    ag = make_agent("left")
    print(f"  重建前: 副机按本机配置 left 算出回传边 = {ag._return_edge.side} "
          f"(错! 应为 left 才对——见下)")

    # 主电脑下发 right
    ag._on_server_side("right")
    edge_after = ag._return_edge.side
    print(f"  下发 right 后: 回传边 = {edge_after}")

    # 语义: side=right 表示"副电脑在主电脑右侧" => 主电脑在副机**左侧**
    #       => 副机的回传边应为 left
    ok1 = ag._server_side == "right" and edge_after == "left"
    print(f"  回传边应为 left: {'PASS' if ok1 else 'FAIL'}")

    print("\n场景: 主电脑 side=left(副电脑在左) => 副机回传边应为 right")
    ag2 = make_agent("right")
    ag2._on_server_side("left")
    ok2 = ag2._server_side == "left" and ag2._return_edge.side == "right"
    print(f"  回传边 = {ag2._return_edge.side}: {'PASS' if ok2 else 'FAIL'}")

    print("\n场景: 主电脑方位与本机配置一致时不应重复重建")
    ag3 = make_agent("right")
    before = ag3._return_edge
    ag3._on_server_side("right")          # 与配置一致
    ok3 = ag3._return_edge is before      # 对象未变 = 未重建
    print(f"  未重建: {'PASS' if ok3 else 'FAIL'}")

    print("\n场景: 回传边尚未建立(心跳早于 start_return)时应只记录")
    ag4 = Agent(server_host="127.0.0.1", name="t", mute_local=False,
                discover_timeout_s=0.1, tone_hz=440.0, side="left",
                enable_return=True)
    ag4._on_server_side("right")          # _return_edge 还是 None
    ok4 = ag4._server_side == "right" and ag4._return_edge is None
    print(f"  已记录且未崩溃: {'PASS' if ok4 else 'FAIL'}")

    allok = ok1 and ok2 and ok3 and ok4
    print()
    print(f"[结论] {'PASS —— 方位下发能正确决定回传边' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
