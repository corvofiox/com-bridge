"""输入链路状态机测试 —— 不依赖真实鼠标, 用几何逻辑驱动。

覆盖曾经真实出错、且用户已实际踩到的场景:
  1. 光标抵达屏幕最外一列(可达像素)必须能触发越界 —— 早先用
     `x >= rect.right` 判永不成立, 鼠标完全无法跨屏;
  2. 越界后主电脑光标必须被钉住, 不能随物理鼠标漂走;
  3. 越界后**必须能回来** —— 持续钳制会让"回到屏内"的绝对坐标判定
     永远不成立, 用户被卡死在副电脑上; 因此方向必须由相对位移决定;
  4. **穿越期间不得注入钳制事件**(那会形成 server→agent→server 回声,
     表现为光标在边缘被疯狂弹回);
  5. **穿越态没有时间兜底** —— 用户把光标停在副机上不动是合法状态, 曾经
      按"活动间隙 2.5s"强制退出是回归: 用户在副机上阅读几秒就被拽回主机。
      防被困改由**心跳丢失看门狗**负责: 只有"副电脑失联"才强制退出,
      与用户是否停止活动无关。
"""

from __future__ import annotations

from com_bridge.input import EdgeDetector, ScreenRect, clamp_to_edge


MON = ScreenRect(0, 0, 1920, 1080)      # 光标可达 x∈[0,1919], y∈[0,1079]


class _FakeServerInput:
    """复刻 server._on_input_event 的判定逻辑(不含真实 SendInput/UDP)。

    与实现保持同步的**四条关键语义**(改动前请先看 server 里的长注释):
      1. 穿越判定只看**相对位移方向** + 抵边几何, 不依赖光标绝对坐标
         (绝对坐标受本机钳制注入干扰, 曾导致"边缘疯狂弹回");
      2. **先判方向再喂 EdgeDetector** —— update() 会改内部状态, 顺序反了
         会让"朝内拉"的事件把状态污染成已穿越;
      3. 主电脑**不自行退出**穿越态, 退出由 agent 的回程信号触发
         (_on_return_event);
      4. **空闲绝不退出**: 用户在副机上停止活动是合法状态, 按"活动间隙"
         强制退出是回归(用户在副机上阅读几秒被拽回主机)。"副电脑失联"
         场景由心跳看门狗处理(server._start_peer_watchdog), 不在输入链路里。
    """

    def __init__(self, side: str = "right", jitter: int = 8):
        self.side = side
        self.edge = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side=side,
                                 confirm_frames=2, hysteresis=jitter)
        self.forwarded = 0
        self.clamped_to: tuple[int, int] | None = None
        # 钉住点: 交接那一刻定下, 穿越期间每个事件都钉回**同一个点**
        self.pin_point: tuple[int, int] | None = None
        self.cursor = [960, 540]
        # 测试时钟: 仅用于锁死"空闲绝不退出"这一语义(曾经按它触发兜底)
        self.clock = 0.0

    def _outward(self, dx: int, dy: int) -> bool:
        if self.side == "right":
            return dx > 0
        if self.side == "left":
            return dx < 0
        if self.side == "top":
            return dy < 0
        return dy > 0

    def _pin_to_edge(self, x: int, y: int) -> tuple[int, int]:
        """钉住点 = 边缘坐标 + **交接那一刻**的切向坐标。

        ★ 切向坐标必须来自交接时定下的固定点(_pin_point), 绝不能是每个
          事件读回的当前坐标 —— 后者等于切向零约束, 用户顶墙猛推时手腕
          自然上旋, 光标会沿边缘一路爬到角落(用户实测: "沿着边缘一路向上
          移动, 最终落在屏幕的右上角")。Deskflow 的 deskLeave 钉的是事先
          存好的 m_xCenter/m_yCenter, 从不实时读回。
        """
        if self.pin_point is None:
            if self.side == "right":
                self.pin_point = (MON.right - 1, y)
            elif self.side == "left":
                self.pin_point = (MON.left, y)
            elif self.side == "top":
                self.pin_point = (x, MON.top)
            else:
                self.pin_point = (x, MON.bottom - 1)
        return self.pin_point

    def advance(self, seconds: float) -> None:
        """推进测试时钟(模拟用户一段时间没有动作)。"""
        self.clock += seconds

    def on_return_signal(self) -> None:
        """模拟收到 agent 的回程信号(server._on_return_event)。"""
        self.edge.reset()
        self.pin_point = None

    def feed(self, x: int, y: int, dx: int = 0, dy: int = 0) -> bool:
        """喂入一个位移事件。x,y 为此刻的光标坐标。

        ★ 穿越期间**主机光标被钉在边缘**(注入绝对定位): 位置不再随物理
          鼠标漂移, 用户在副机上可以双向自由移动 —— 这是"两台鼠标协同
          一体"的前提(照搬 Deskflow 的模型)。因此这里模拟 clamped_to。
        """
        self.cursor = [x, y]
        outward = self._outward(dx, dy)
        if not self.edge.is_outside:
            # ★ 先判方向再喂 EdgeDetector(见类文档第 2 条)
            if not outward:
                self.edge.reset()
                return False
            if self.edge.update(x, y):
                # 与 server 一致: 交接那一刻定下钉住点(再发 place 事件)
                self.pin_point = self._pin_to_edge(x, y)
                self.forwarded += 1
                return True
            return False
        # 穿越态: **主机光标被钉回交接时定下的固定点**(注入绝对定位) ——
        # 这是"两台鼠标协同一体"的前提(照搬 Deskflow 的模型)。钉住后用户
        # 在副机上可以双向自由移动, 回程只由"副机光标抵达回传边"触发。
        # ⚠ 钉住目标是**固定的**, 不是读回的当前坐标(那等于切向零约束)。
        # ⚠ **没有时间兜底**: 空闲多久都不退出(见类文档第 4 条)。
        self.clamped_to = self._pin_to_edge(x, y)
        self.cursor = list(self.clamped_to)
        self.forwarded += 1
        return True


class TestCrossScreenReachable:
    def test_cursor_can_cross_at_reachable_pixel(self):
        """核心回归: 用真实可达的最右像素 + 朝外推, 必须能进入穿越态。"""
        s = _FakeServerInput("right")
        assert s.feed(1919, 540, dx=5) is False     # 第 1 帧(confirm_frames=2)
        assert s.feed(1919, 540, dx=5) is True, "抵边并朝外推后应开始转发"
        assert s.forwarded == 1

    def test_interior_never_forwards(self):
        s = _FakeServerInput("right")
        for x in (0, 500, 1500, 1910):
            assert s.feed(x, 540, dx=5) is False
        assert s.forwarded == 0

    def test_edge_but_moving_inward_does_not_cross(self):
        """★ 防弹回: 光标抵边但正在往回拉时, 绝不能进入穿越态。

        这是"疯狂弹回"的直接防线 —— 用户从边缘往内拉时不应被拽进副电脑。
        """
        s = _FakeServerInput("right")
        for _ in range(10):
            assert s.feed(1919, 540, dx=-5) is False
        assert s.forwarded == 0, "朝内拉时不应转发"


class TestNoBouncing:
    """★ 用户实际报的 bug: 光标到边缘被疯狂弹回, 进不去对面。

    机理: 越界后 server 会注入钳制把光标钉在边界, 注入事件(或坐标回读)
    被误判为"用户往回拉" -> 立刻退出穿越态 -> 用户再推 -> 再触发,
    形成高频弹回。以下用例锁死"进入后必须稳定保持"。
    """

    def test_crossing_state_is_stable_under_repeated_outward_pushes(self):
        s = _FakeServerInput("right")
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        # 持续朝外推 50 次: 必须始终保持在穿越态, 一次都不能掉出来
        for i in range(50):
            assert s.feed(1919, 540, dx=8) is True, f"第 {i} 次朝外推掉出了穿越态"
        assert s.edge.is_outside, "持续朝外推后仍应在穿越态"

    def test_no_bounce_when_clamp_reports_cursor_at_edge(self):
        """钳制把光标固定在边界(1919)时, 不应被误判为"已回到屏内"。"""
        s = _FakeServerInput("right")
        s.feed(1919, 540, dx=5)
        s.feed(1919, 540, dx=5)
        # 模拟: 我们注入钳制后, 事件流里仍带着"光标在 1919"的位移事件
        for _ in range(20):
            assert s.feed(1919, 540, dx=1) is True, "坐标停在边界不应导致退出"
        assert s.edge.is_outside

    def test_small_inward_jitter_does_not_exit(self):
        """少量内拉抖动不应退出穿越态(否则边缘噪声会反复切换)。"""
        s = _FakeServerInput("right", jitter=20)
        s.feed(1919, 540, dx=5)
        s.feed(1919, 540, dx=5)
        for _ in range(3):
            assert s.feed(1919, 540, dx=-5) is True
        assert s.edge.is_outside, "内拉抖动不应退出穿越态"


class TestCursorIsPinned:
    def test_injection_during_crossing_pins_cursor(self):
        """★ 穿越期间注入**绝对定位到边缘**来钉住光标(当前设计)。

        演进说明(两次被用户踩到的错误版本):
          * 最初用 clamp_to_edge(x,y) —— 它只把坐标限制在屏幕内, 对已在
            屏内的坐标原样返回, 注入从未改变过光标位置(无效空操作),
            只会制造回声事件(边缘疯狂弹回);
          * 后来完全删掉注入 —— 主机光标随物理鼠标自由移动, 用户在副机
            上往回拉时主机光标也跟着回移, 一超过迟滞带主机就退出穿越态
            (副机只能单向移动)。
        当前: 注入**绝对定位到边缘**(带 TAG 无回声), 钉住主机光标。
        """
        s = _FakeServerInput("right")
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        # 穿越态下反复朝外推: 每次都应记录"钉住目标"(= 边缘)
        for _ in range(20):
            assert s.feed(1919, 540, dx=5) is True
        assert s.clamped_to == (1919, 540), (
            f"穿越期间应注入绝对定位钉住光标, 实际 {s.clamped_to}"
        )

    def test_pin_point_is_fixed_no_corner_creep(self):
        """★ 回归(用户实测): "主机光标沿边缘一路向上爬, 最终落在右上角"。

        成因: 钉住时传的是**读回来的当前切向坐标**, 等于切向零约束 ——
        用户顶墙猛推时手腕自然上旋, dy<0 的分量被原样保留, 光标一路爬。
        正确: 钉回**交接那一刻定下的固定点**(Deskflow 存 m_xCenter/m_yCenter)。
        """
        s = _FakeServerInput("right")
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        assert s.pin_point == (1919, 540)
        # 模拟"顶墙 + 手腕上旋": 事件流里的 y 一路变小
        y = 540
        for _ in range(200):
            y -= 1
            s.feed(1919, y, dx=8, dy=-1)
        # 钉住点必须**始终是交接时的 540**, 而不是被拖到角落
        assert s.pin_point == (1919, 540), (
            f"钉住点漂移了: {s.pin_point} —— 切向必须钉在交接时的固定点"
        )
        assert s.clamped_to == (1919, 540)

    def test_new_handoff_recomputes_pin_point(self):
        """退出穿越态后再次进入, 钉住点要按**新的**交接位置重新计算。"""
        s = _FakeServerInput("right")
        s.feed(1919, 540, dx=5)
        s.feed(1919, 540, dx=5)
        s.on_return_signal()
        # 第二次交接发生在 y=800
        s.feed(1919, 800, dx=5)
        assert s.feed(1919, 800, dx=5) is True
        assert s.pin_point == (1919, 800), (
            f"重新交接应重新定点, 实际 {s.pin_point}"
        )


class TestCanAlwaysComeBack:
    """用户必须能从副电脑回到主电脑。"""

    def test_return_signal_releases_control(self):
        """收到 agent 的回程信号必须立刻释放穿越态。"""
        s = _FakeServerInput("right")
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        assert s.edge.is_outside
        s.on_return_signal()
        assert not s.edge.is_outside, "回程信号应释放穿越态"
        assert s.feed(1800, 540, dx=-5) is False

    def test_idle_never_exits_by_itself(self):
        """★ 回归锁: 用户在副机上停止活动是**合法状态**, 绝不能自动退出。

        曾经按"活动间隙 2.5s"兜底 —— 用户在副机上阅读几秒就被拽回主机
        (实测)。防被困改由心跳看门狗负责(对端失联才退出), 输入链路里
        **没有**任何时间兜底。
        """
        s = _FakeServerInput("right", jitter=8)
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        s.advance(300.0)             # 空闲 5 分钟
        assert s.feed(1919, 540, dx=-5) is True, \
            "空闲再久, 穿越态也不得自行退出(退出只由回程信号/看门狗触发)"
        assert s.edge.is_outside

    def test_outward_push_does_not_trigger_fallback(self):
        """持续朝外推时绝不能掉出穿越态(回归锁, 原兜底场景)。"""
        s = _FakeServerInput("right", jitter=8)
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        for i in range(300):
            s.advance(0.05)          # 时间在流逝
            assert s.feed(1919, 540, dx=10) is True, \
                f"第 {i} 次朝外推掉出了穿越态"
        assert s.edge.is_outside

    def test_slow_inward_pull_does_not_bounce_immediately(self):
        """★ 防"刚过去就弹回": 慢速内拉(事件稀疏)绝不触发退出。

        真实踩到的坑 —— 兜底最早按**像素**累计, 用户慢速拖动时每次事件
        可达 20px, 4 次事件(80px)就超过阈值, 刚把光标挪过去就被弹回来。
        现在任何内拉都不会退出(钉住), 回程只由副机的回程信号触发。
        """
        s = _FakeServerInput("right", jitter=8)
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        # 慢速内拉: 每次 -20px
        for i in range(20):
            s.advance(0.1)
            assert s.feed(1919, 540, dx=-20) is True, \
                f"第 {i} 次慢速内拉就被弹回了(应等回程信号)"
        assert s.edge.is_outside

    def test_long_inactivity_still_alive(self):
        """★ 回归锁: 长时间无活动后第一个事件也必须照常转发。"""
        s = _FakeServerInput("right", jitter=8)
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        s.advance(600.0)             # 空闲 10 分钟
        assert s.feed(1919, 540, dx=3) is True, \
            "长时间空闲后穿越态必须仍然有效"
        assert s.edge.is_outside


class TestCrossingSemantics:
    """★ 当前设计: 钉住主机光标 + 副机决定回程(照搬 Deskflow)。

    对应用户最新反馈:
      "可以正常进入副机, 但两个鼠标没有协同一体 —— 主机进入边缘后副机
       鼠标从对应边缘出现, 但只能朝一个方向移动, 一旦向反方向拉, 主机
       鼠标立马从边缘出来了。预期行为应该是副机鼠标移动到对应边缘时才
       回到主机。这一点没实现。"

    根因: 上一轮修"两台光标同动"用了**按坐标退出**, 而那以"主机光标随
    物理鼠标自由移动"为前提 → 用户在副机上往回拉时主机光标也跟着回移,
    一超过 8px 迟滞带主机就退出穿越态 → 副机只能单向移动。
    修复: 越界期间把主机光标**绝对定位钉在边缘**(注入带 TAG, 无回声),
    回程只由"副机光标抵达回传边"触发 —— 这正是用户描述的预期行为。
    """

    def test_cursor_pinned_while_crossing(self):
        """越界期间主机光标必须被钉在边缘(位置不再随物理鼠标漂移)。"""
        s = _FakeServerInput("right")
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        # 用户物理鼠标往回拉(坐标本会内移), 但钉住会把它送回边缘
        for _ in range(10):
            assert s.feed(1900, 540, dx=-19) is True, \
                "越界期间主机光标应被钉在边缘, 不能被拉回屏内"
        assert s.clamped_to == (1919, 540)

    def test_peer_moves_freely_both_directions(self):
        """副机光标双向移动期间, 主机**保持**转发(不提前退出)。"""
        s = _FakeServerInput("right")
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        # 用户在副机上双向移动: 主机持续转发, 一次都不掉出穿越态
        for i in range(60):
            dx = -20 if i % 2 == 0 else 20     # 左右交替
            assert s.feed(1919, 540, dx=dx) is True, \
                f"第 {i} 次双向移动时主机掉出了穿越态(会误回主机)"
        assert s.edge.is_outside

    def test_return_only_via_signal(self):
        """退出路径只剩: 副机的回程信号(+ 看门狗的失联判定)。"""
        s = _FakeServerInput("right")
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        # 往回拉不再触发退出(被钉住)
        assert s.feed(1900, 540, dx=-19) is True
        # 回程信号触发退出
        s.on_return_signal()
        assert not s.edge.is_outside

    def test_idle_never_releases_when_return_lost(self):
        """★ 回归锁: 回程信号丢失时, 用户停止活动也**不得**自动退出。

        "失联"与"用户空闲"是两回事 —— 空闲由心跳看门狗区分(心跳还在
        就说明副电脑活着), 输入链路绝不能按时间退出。
        """
        s = _FakeServerInput("right")
        s.feed(1919, 540, dx=5)
        assert s.feed(1919, 540, dx=5) is True
        s.advance(300.0)             # 空闲 5 分钟
        assert s.feed(1919, 540, dx=-5) is True
        assert s.edge.is_outside


class TestClampBounds:
    def test_clamp_uses_reachable_pixel(self):
        assert clamp_to_edge(5000, 5000) == (1919, 1079)
        assert clamp_to_edge(-100, -100) == (0, 0)

    def test_clamp_never_returns_unreachable(self):
        """钳制结果必须是光标真能到达的坐标, 否则判定逻辑会错乱。"""
        for x, y in ((1919, 540), (1920, 540), (9999, 9999)):
            cx, cy = clamp_to_edge(x, y)
            assert 0 <= cx <= MON.right - 1
            assert 0 <= cy <= MON.bottom - 1


class TestMultiMonitorReturnEdge:
    """★ 用户实测: 副机有两块屏时"往左拉一点就回到主屏幕"。

    根因: 回传边的基准用了 `_main_monitor()`(面积最大的那一块屏),
    而整台机器的最左边缘是**所有显示器合并**后的左边。
    副机双屏(左屏 0..1919、右屏 1920..3839)时, 误用单块屏会把回传边
    算在 x=1920(右屏左缘), 于是光标一越过 1920 就被判定"已到回传边"
    —— 用户根本无法把光标移到左屏。
    照搬 Deskflow 的做法: 用计算机的完整 shape(所有显示器合并)。
    """

    DUAL = ScreenRect(0, 0, 3840, 1080)      # 双屏合并后的桌面

    def test_middle_of_right_screen_is_not_at_edge(self):
        d = EdgeDetector(self.DUAL, ScreenRect(0, 0, 0, 0), side="left",
                         confirm_frames=2, hysteresis=8)
        # 右屏中部/左缘都不该算"抵到整机最左边"
        for x in (2800, 1920, 1919, 100):
            for _ in range(5):
                assert d.update(x, 540) is False, \
                    f"x={x} 不该被判定为抵达整机最左边缘(会误回主机)"

    def test_only_true_left_edge_triggers(self):
        d = EdgeDetector(self.DUAL, ScreenRect(0, 0, 0, 0), side="left",
                         confirm_frames=2, hysteresis=8)
        assert d.update(0, 540) is False, "第 1 帧只计数"
        assert d.update(0, 540) is True, "整机最左 x=0 应能触发抵边"

    def test_wrong_basis_would_misjudge(self):
        """对照: 用单块显示器作基准时 x=1920 会被误判(记录这只 bug)。"""
        wrong = EdgeDetector(ScreenRect(1920, 0, 3840, 1080),
                             ScreenRect(0, 0, 0, 0), side="left",
                             confirm_frames=2, hysteresis=8)
        wrong.update(1920, 540)
        assert wrong.update(1920, 540) is True, \
            "这是错误基准下的行为 —— 保留它以说明用户现象从何而来"
