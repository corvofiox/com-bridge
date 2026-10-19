"""双端往返模拟: 验证"主电脑 -> 副电脑 -> 回主电脑"完整闭环不打架。

把 server 与 agent 两端的状态机串起来, 用同一串"用户物理鼠标位移"驱动,
检查:
  1. 用户向右推 -> 控制权交给 agent, 且**稳定保持**(不弹回);
  2. agent 上的光标能正常移动(注入生效);
  3. 用户向左拉 -> 光标在 agent 上左移到回传边 -> 回传 -> 主电脑取回控制;
  4. 全程没有"两端互相抢控制权"的振荡。
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from com_bridge.input import EdgeDetector, ScreenRect

HOST = ScreenRect(0, 0, 1920, 1080)     # 主电脑(左)
PEER = ScreenRect(0, 0, 1366, 768)      # 副电脑(右), 分辨率故意不同
HYST = 8


class Host:
    """主电脑侧状态机(复刻 server._on_input_event, side='right')。

    退出穿越态**不由本机决定**, 而是等 agent 的回程信号(见 on_return_signal);
    **没有时间兜底**(空闲是合法状态, 防被困由心跳看门狗负责)。
    """

    def __init__(self) -> None:
        self.side = "right"
        self.edge = EdgeDetector(HOST, ScreenRect(0, 0, 0, 0), side="right",
                                 confirm_frames=2, hysteresis=HYST)
        self.cursor = [960, 540]
        self.forwarded = 0
        self.log: list[str] = []
        self.clock = 0.0

    def advance(self, seconds: float) -> None:
        self.clock += seconds

    def on_return_signal(self) -> None:
        if self.edge.is_outside:
            self.edge.reset()
            self.log.append("EXIT_VIA_RETURN_SIGNAL")

    def feed(self, dx: int, dy: int) -> str:
        """返回 'local'(本机处理) 或 'to_peer'(转发给副电脑)。"""
        self.cursor[0] = max(HOST.left, min(HOST.right - 1, self.cursor[0] + dx))
        self.cursor[1] = max(HOST.top, min(HOST.bottom - 1, self.cursor[1] + dy))

        if not self.edge.is_outside:
            if dx <= 0:                       # 不是朝副电脑推
                self.edge.reset()
                return "local"
            if self.edge.update(*self.cursor):
                self.log.append("ENTER_PEER")
                self.cursor = [HOST.right - 1, self.cursor[1]]
            else:
                return "local"
        else:
            # 穿越态只转发, **没有时间兜底**(与 Deskflow 一致)
            self.cursor = [HOST.right - 1, self.cursor[1]]
        self.forwarded += 1
        return "to_peer"


class Peer:
    """副电脑侧(复刻 agent._on_input_event, 回传边='left')。

    回程条件有**两条**, 满足任一即交回控制权:
      1. 光标抵达回传边且继续朝主电脑移动;
      2. 持续朝主电脑方向拉动超过 RETURN_INTENT_S —— 避免用户必须横穿
         整个副电脑屏幕才能回去(副电脑常比主电脑宽)。
    """

    RETURN_INTENT_S = 0.7

    def __init__(self) -> None:
        self.return_edge = EdgeDetector(PEER, ScreenRect(0, 0, 0, 0),
                                        side="left", confirm_frames=2,
                                        hysteresis=HYST)
        self.cursor = [683, 384]              # 副电脑屏幕中央
        self.returned = 0
        self.injected = 0
        self.log: list[str] = []
        self.clock = 0.0
        self._intent_since: float | None = None

    def advance(self, seconds: float) -> None:
        self.clock += seconds

    def feed(self, dx: int, dy: int) -> str:
        """返回 'injected'(在本机注入) 或 'to_host'(回传给主电脑)。"""
        toward_host = dx < 0                 # 回传边是 left
        if toward_host:
            if self._intent_since is None:
                self._intent_since = self.clock
        else:
            self._intent_since = None

        if toward_host:
            at_edge = self.return_edge.update(*self.cursor)
            pulled_long = (self._intent_since is not None
                           and self.clock - self._intent_since
                           >= self.RETURN_INTENT_S)
            if at_edge or pulled_long:
                self.returned += 1
                self.log.append("RETURN_EDGE" if at_edge else "RETURN_INTENT")
                return "to_host"
        else:
            if self.return_edge.is_outside:
                self.return_edge.reset()
                self.log.append("STAY_ON_PEER")

        # 本机注入相对移动
        self.cursor[0] = max(PEER.left, min(PEER.right - 1, self.cursor[0] + dx))
        self.cursor[1] = max(PEER.top, min(PEER.bottom - 1, self.cursor[1] + dy))
        self.injected += 1
        return "injected"


def main() -> int:
    h, p = Host(), Peer()

    print("阶段 1: 主电脑上向右推 120 次(+20px) -> 应进入并稳定保持转发")
    entered = None
    bounces = 0
    for i in range(120):
        before = h.edge.is_outside
        r = h.feed(20, 0)
        if h.edge.is_outside and not before and entered is None:
            entered = i
        if before and not h.edge.is_outside:
            bounces += 1
    print(f"  第 {entered} 次事件进入穿越态, 弹回 {bounces} 次")
    assert bounces == 0, "主电脑侧出现弹回"
    assert h.edge.is_outside
    print("  ✓ 控制权已稳定交给副电脑")

    print("\n阶段 2: 用户继续向右推, 事件被转发并在副电脑上注入")
    for _ in range(30):
        assert h.feed(20, 0) == "to_peer"
        p.feed(20, 0)
    print(f"  副电脑光标: {p.cursor} (从 683 右移, dx>0 远离 left 回传边)")
    assert p.cursor[0] > 683, "副电脑光标应向右移动"
    assert p.returned == 0, "向右移动不应触发回传"
    print("  ✓ 注入生效且未误回传")

    print("\n阶段 3: 用户在副电脑上向左拉回 -> 应回传并让主电脑取回控制")
    steps = 0
    returned = False
    while steps < 400:
        steps += 1
        h.advance(0.05)              # 每次事件间隔 50ms(真实手感)
        p.advance(0.05)
        if h.feed(-20, 0) == "local":
            print(f"  !! 主电脑在第 {steps} 次内拉后自行退出(应为回程信号)")
            break
        # 位移被转发到副电脑; 由副电脑判断是否该回传
        if p.feed(-20, 0) == "to_host":
            h.on_return_signal()
            returned = True
            break
    print(f"  {steps} 次内拉({steps*0.05:.1f}s)后: 回程信号={returned}, "
          f"主电脑穿越态={h.edge.is_outside}")
    assert returned, "副电脑未能在光标抵达回传边时发出回程信号"
    assert not h.edge.is_outside, "主电脑应已释放穿越态"
    print("  ✓ 成功回到主电脑")

    print("\n阶段 4: 回到主电脑后光标应能自由内移, 不残留穿越状态")
    for _ in range(10):
        assert h.feed(-20, 0) == "local"
    print(f"  主电脑光标: {h.cursor} (已内移)")
    assert h.cursor[0] < HOST.right - 1
    print("  ✓ 状态干净")

    print("\n阶段 5: 再次推到边缘应能重新穿越(可重复使用)")
    for _ in range(120):
        h.feed(20, 0)
    assert h.edge.is_outside, "应能重新进入穿越态"
    print("  ✓ 可重复穿越")

    print(f"\n主电脑状态转换: {h.log}")
    print(f"副电脑状态转换: {p.log}")
    print(f"\n转发 {h.forwarded} 事件, 副电脑注入 {p.injected}, 回传 {p.returned}")
    print("\n[结论] PASS —— 往返闭环正常, 两端不抢控制权")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
