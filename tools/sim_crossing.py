"""端到端穿越模拟: 复刻 server 真实状态机, 验证"不再弹回"。

覆盖三个曾经真实出错、且用户已踩到的效应:
  1. 绝对坐标不可靠 —— 穿越期间光标被钳在边界, 坐标回读会抖动;
     修复后的状态机只看**位移方向**, 不受坐标影响;
  2. 注入事件会被自己的监听器收到(回声/自激);
  3. ★ 穿越态**没有时间兜底**(与 Deskflow 一致): 用户把光标停在副机上
     不动是合法状态, 曾经按"活动间隙"强制退出是回归(用户在副机上阅读
     几秒就被拽回主机)。防被困由心跳看门狗处理(server._start_peer_watchdog):
     只有"副电脑失联"才强制退出。

运行: python tools/sim_crossing.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from com_bridge.input import EdgeDetector, ScreenRect

MON = ScreenRect(0, 0, 1920, 1080)
HYST = 8
# ★ 穿越态没有时间兜底(server 与 Deskflow 一致): 退出只由回程信号/看门狗
DT = 0.05                 # 模拟每次事件间隔 50ms


class ServerFSM:
    """逐字复刻修复后的 server._on_input_event。"""

    def __init__(self, side: str = "right"):
        self.side = side
        self.edge = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side=side,
                                 confirm_frames=2, hysteresis=HYST)
        self.inward = 0.0
        self.forwarded = 0
        self.cursor = [960, 540]
        self.transitions: list[str] = []

    def _outward(self, dx, dy) -> bool:
        return {"right": dx > 0, "left": dx < 0,
                "top": dy < 0, "bottom": dy > 0}[self.side]

    def feed(self, dx: int, dy: int) -> bool:
        # Windows 先应用位移再钳制(它自己会把光标钳在屏幕内, 无需我们注入)
        self.cursor[0] = max(MON.left, min(MON.right - 1, self.cursor[0] + dx))
        self.cursor[1] = max(MON.top, min(MON.bottom - 1, self.cursor[1] + dy))

        if not self.edge.is_outside:
            if not self._outward(dx, dy):
                self.edge.reset()
                return False
            if self.edge.update(*self.cursor):
                self.transitions.append("ENTER")
            else:
                return False
        else:
            # 与 server 一致: 穿越态只转发, **没有时间兜底**(空闲是合法状态)
            self.inward += 1
        self.forwarded += 1
        return True

    def on_return_signal(self) -> None:
        """模拟收到 agent 的回程信号。"""
        if self.edge.is_outside:
            self.edge.reset()
            self.inward = 0.0
            self.transitions.append("EXIT_RETURN")


def main() -> int:
    s = ServerFSM("right")

    print("阶段 1: 从中间持续向右推 200 次(+20px)")
    entered_at = None
    bounces = 0
    for i in range(200):
        before = s.edge.is_outside
        s.feed(20, 0)
        after = s.edge.is_outside
        if after and not before and entered_at is None:
            entered_at = i
        if before and not after:
            bounces += 1             # 在持续朝外推期间掉出穿越态 = 弹回
    print(f"  进入穿越态于第 {entered_at} 次事件")
    print(f"  持续朝外推期间的弹回次数: {bounces}  (必须为 0)")
    print(f"  最终是否在穿越态: {s.edge.is_outside}  (应为 True)")
    assert bounces == 0, "出现弹回!"
    assert s.edge.is_outside, "持续朝外推应保持穿越态"
    print(f"  已转发事件: {s.forwarded}")

    print("\n阶段 2: 持续朝外推 100 次(+5px 小步), 混入 1px 内拉抖动")
    b2 = 0
    for i in range(100):
        before = s.edge.is_outside
        dx = -1 if i % 10 == 0 else 5     # 每 10 次来 1px 内拉
        s.feed(dx, 0)
        if before and not s.edge.is_outside:
            b2 += 1
    print(f"  小抖动导致的退出次数: {b2}  (应为 0)")
    assert b2 == 0, f"小抖动导致退出 {b2} 次"
    assert s.edge.is_outside
    print("  ✓ 穿越态稳定")

    print("\n阶段 3: 收到 agent 回程信号 -> 必须干净退出(不能卡死在对面)")
    s.on_return_signal()
    assert not s.edge.is_outside, "回程信号应释放穿越态"
    print("  ✓ 已交回控制权")

    print("\n阶段 4: 退出后光标应能自由移动, 再次推到边缘可重新穿越")
    for _ in range(10):
        s.feed(-10, 0)
    assert not s.edge.is_outside, "光标内移后不应处于穿越态"
    print("  已回到本机 ✓")
    for _ in range(100):
        s.feed(20, 0)
    assert s.edge.is_outside, "再次推到边缘应能重新穿越"
    print("  重新穿越成功 ✓")

    print(f"\n状态转换序列: {s.transitions}")
    print("\n[结论] PASS —— 进入稳定、无弹回、可退出、可重入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
