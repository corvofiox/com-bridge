# -*- coding: utf-8 -*-
"""交接去抖 + 隐藏幂等 + 输入线程不阻塞的回归测试(2026-10-22)。

## 现场(副机真机日志, 修前)

副机日志 886 行里有 712 行是同一句
`[agent] 光标离开回传边 -> 交回主电脑控制权`, 按时间聚簇后同一手势
最多 42ms 内触发 **145 次**。每一次都会:

  * 往主机回传一遍位移(主机还要再注入一次, 等于把手速放大两倍);
  * `hider.hide()` 一次 —— 而单次 hide ≈ 50ms(全屏定位 + 抢前台 +
    抓捕获 + 两次 ±1px 注入 + 两个 sleep), 145 次 ≈ 7 秒。

用户看到的是"指针被往主机那一侧边缘吸、拉不出来、回来之后光标迟迟
不出现"。根因是**回传分支没有去抖**, 而 `_do_hide` 也不幂等, 于是
管线里一批陈旧事件把开销放大了两个数量级。

另外主机侧 `_acquire_control` 原先在**原始输入回调线程**上同步调用
`_reverify_keyboard_hook()`, 而探针要轮询等最多 0.5s —— 每次越界都把
输入线程堵住, 期间的事件排队后一次性倒给副机(指针先僵住再猛窜)。

这些测试不依赖真实桌面: 纯状态机 + 源码断言 + 记录型假 user32。
"""
from __future__ import annotations

import inspect
import threading
import time

import pytest

import com_bridge.agent as agent_mod
import com_bridge.cursor as cursor_mod
from com_bridge.agent import Agent
from com_bridge.cursor import CursorHider
from com_bridge.input import EdgeDetector, InputInjector, ScreenRect
from com_bridge.protocol import (
    EV_MOUSE_MOVE_REL,
    EV_MOUSE_PLACE,
    EV_MOUSE_RETURN,
    InputEvent,
)
from com_bridge.server import Server


# --- 工具 ---------------------------------------------------------------

def _strip_comments(src: str) -> str:
    """去掉注释, 只看代码本体(注释里允许记载历史与根因)。"""
    out = []
    for line in src.splitlines():
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def _func_ast(fn):
    """取函数的 AST(注释与文档字符串都不参与"是否调用了 X"的判断)。"""
    import ast
    import textwrap
    return ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]


def _call_names(fn) -> set[str]:
    """函数体里实际调用到的名字(方法名或函数名)。"""
    import ast
    node = _func_ast(fn)
    out = set()
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute):
            out.add(f.attr)
        elif isinstance(f, ast.Name):
            out.add(f.id)
    return out


class _FakeTx:
    """记录型 InputSender: 只关心发了什么。"""

    def __init__(self):
        self.sent = []

    def send(self, ev):
        self.sent.append(ev)

    def types(self):
        return [ev.type for ev in self.sent]


class _FakeHider:
    """记录型 CursorHider: 统计 hide/show 次数。"""

    def __init__(self):
        self.hide_calls = 0
        self.show_calls = 0

    def hide(self):
        self.hide_calls += 1

    def show(self):
        self.show_calls += 1


class _FakeUser32:
    """任何 API 调用都只记录名字并返回 0(绝不碰真实系统光标)。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _rec(*args, **kwargs):
            self.calls.append(name)
            return 0
        return _rec


def _move(dx: int, dy: int = 0) -> InputEvent:
    # InputEvent 字段序: type, flags, code, a, b, ts_us
    return InputEvent(EV_MOUSE_MOVE_REL, 0, 0, dx, dy, 0)


@pytest.fixture()
def agent_at_edge(monkeypatch):
    """副机光标顶在回传边(左侧 x=0)上的一套最小环境。"""
    a = Agent(server_host="127.0.0.1", enable_return=False)
    a._return_edge = EdgeDetector(
        self_rect=ScreenRect(0, 0, 4480, 1600),
        peer_rect=ScreenRect(0, 0, 0, 0),
        side="left", confirm_frames=1,
    )
    a._input_tx = _FakeTx()
    a._hider = _FakeHider()
    a._return_listener = "enabled"
    # 注入既不能碰真实系统光标, 也要能计数
    injected: list[tuple[int, int]] = []
    monkeypatch.setattr(agent_mod, "get_cursor_pos", lambda: (0, 800))
    monkeypatch.setattr(InputInjector, "move_relative",
                        staticmethod(lambda dx, dy: injected.append((dx, dy))))
    monkeypatch.setattr(InputInjector, "move_absolute",
                        staticmethod(lambda x, y: 1))
    a._injected_moves = injected          # 供测试断言
    return a


# ===========================================================================
# 1. 回传去抖: 一轮穿越只回传一次
# ===========================================================================

class TestReturnDebounce:

    def test_backlog_triggers_return_exactly_once(self, agent_at_edge):
        """145 个陈旧位移只回传一次(而不是 145 次)。

        这就是"吸在边缘"的直接成因: 修前每个事件都回传 + hide。
        """
        a = agent_at_edge
        for _ in range(50):
            a._on_input_event(_move(-5))
        assert a.stats["events_returned"] == 1, "一轮穿越只能回传一次"
        assert a._hider.hide_calls == 1, "hide() 只能调用一次(单次 ≈50ms)"
        # 1 条回程方位(EV_MOUSE_RETURN) + 1 条位移
        assert a._input_tx.types() == [EV_MOUSE_RETURN, EV_MOUSE_MOVE_REL]
        assert a.stats.get("events_dropped_stale", 0) == 49

    def test_stale_moves_are_not_replayed_to_host(self, agent_at_edge):
        """陈旧位移必须丢弃: 主机早已用物理鼠标实时走过同样的路程。"""
        a = agent_at_edge
        for _ in range(10):
            a._on_input_event(_move(-5))
        # 丢弃的事件既不再回传, 也不在本机注入(否则等于把手速放大)
        assert a._injected_moves == []

    def test_place_rearms_debounce(self, agent_at_edge):
        """主机再次越界进入副机(EV_MOUSE_PLACE)必须解除去抖锁。"""
        a = agent_at_edge
        for _ in range(5):
            a._on_input_event(_move(-5))
        assert a.stats["events_returned"] == 1
        a._on_input_event(InputEvent(EV_MOUSE_PLACE, 0, 0, 30000, 0, 0))
        assert a._returned is False
        a._on_input_event(_move(-5))
        assert a.stats["events_returned"] == 2, "新一轮穿越必须能再次回传"
        assert a._hider.hide_calls == 2

    def test_drain_window_is_bounded_and_self_heals(self, agent_at_edge):
        """回程包万一丢了(主机没接管), 冷静期一过必须恢复注入。

        否则副机会**永久吞掉**输入 —— 那比"指针贴着边"严重得多。
        """
        a = agent_at_edge
        a._on_input_event(_move(-5))
        assert a._returned is True
        # 把"交回时刻"拨到冷静期之外
        a._returned_mono = time.monotonic() - (Agent.RETURN_STALE_DRAIN_S + 1.0)
        # 光标已离开回传边 => 这一次应该正常注入
        a._return_edge.reset()
        a._on_input_event(_move(3))
        assert a._returned is False
        assert a._injected_moves == [(3, 0)], "冷静期过后必须恢复注入"

    def test_debounce_constants_present(self):
        assert Agent.RETURN_STALE_DRAIN_S >= 0.2
        assert Agent.RETURN_STALE_DRAIN_S <= 0.5
        assert "_returned" in inspect.getsource(Agent.__init__)
        src = _strip_comments(inspect.getsource(Agent._on_input_event))
        assert "if self._returned:" in src
        assert "RETURN_STALE_DRAIN_S" in src
        assert "events_dropped_stale" in src

    def test_return_logged_once_per_crossing(self, agent_at_edge, capsys):
        """日志里不能再出现"同一手势 145 行"(它占了副机日志的 80%)。"""
        a = agent_at_edge
        for _ in range(40):
            a._on_input_event(_move(-5))
        out = capsys.readouterr().out
        assert out.count("光标离开回传边") == 1


# ===========================================================================
# 2. hide() 幂等: 已隐藏就别重走整套(单次 ≈50ms)
# ===========================================================================

class TestHideIdempotent:

    def test_do_hide_is_noop_when_already_hidden(self, monkeypatch):
        h = CursorHider(name="TestHider")
        fake = _FakeUser32()
        monkeypatch.setattr(cursor_mod, "user32", fake)
        monkeypatch.setattr(InputInjector, "move_relative",
                            staticmethod(lambda dx, dy: 0))
        h._shown = True
        h._hide_mono = 123.0
        h._do_hide()
        assert fake.calls == [], f"已隐藏时不该再碰任何 user32 API: {fake.calls}"
        assert h._hide_mono == 123.0, "静默期起点不该被重复 hide 改写"
        assert h._shown is True

    def test_guard_is_the_first_statement(self):
        import ast
        body = _func_ast(CursorHider._do_hide).body
        # 文档字符串之后的第一条可执行语句必须是 if self._shown: return
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            first = body[1]
        assert isinstance(first, ast.If), ast.dump(first)[:200]
        assert "self._shown" in ast.unparse(first.test)
        assert isinstance(first.body[0], ast.Return)


# ===========================================================================
# 3. 输入线程绝不能被探针的 0.5s 等待阻塞
# ===========================================================================

class TestInputThreadNeverBlocks:

    def test_acquire_control_verifies_in_background(self):
        calls = _call_names(Server._acquire_control)
        assert "_kick_kbd_verify" in calls
        # 同步调用会让每次越界都堵住原始输入线程(最多 0.5s)
        assert "_reverify_keyboard_hook" not in calls
        assert "probe_keyboard_hook" not in calls

    def test_release_shows_cursor_before_verifying(self):
        src = _strip_comments(inspect.getsource(Server._release_control))
        assert src.index("_show_cursor") < src.index("_verify_kbd_on_release")

    def test_release_verify_spawns_thread(self):
        calls = _call_names(Server._verify_kbd_on_release)
        assert "_spawn_kbd_verify" in calls
        assert "probe_keyboard_hook" not in calls

    def test_spawn_is_single_flight(self):
        """探针会改全局计数: 两个并发探针会互相污染判据。"""
        srv = object.__new__(Server)
        srv._kbd_verify_thread = None
        srv._kbd_verify_lock = threading.Lock()
        gate = threading.Event()
        started = []

        def slow():
            started.append(1)
            gate.wait(2.0)

        srv._spawn_kbd_verify(slow)
        srv._spawn_kbd_verify(slow)          # 第二次必须被挡回
        time.sleep(0.05)
        assert len(started) == 1, "同一时刻只允许一轮自检"
        gate.set()
        assert srv._kbd_verify_thread is not None
        srv._kbd_verify_thread.join(timeout=2.0)

# ===========================================================================
# 4. 控制权在主电脑时, 本机指针不得驱动主机光标(2026-10-22 边缘"慢移不动")
# ===========================================================================

class TestLocalPointerDoesNotDriveHost:
    """交回控制权后, 副机本机指针的任何移动都必须被丢弃。

    真机现场(2026-10-22 17:39): 主机光标复位到 (1907,518) 之后, 用户
    慢推了 **56 秒**才越过 12px 的边界; 用力拉一次(+96px)就过去了。
    根因是交回控制权后 `_return_edge.is_outside` 一直是 True(要等下一次
    EV_MOUSE_PLACE), 而本机光标就停在回传边上 —— 于是本机指针的任何
    零点漂移(触摸板/无线接收器)都被当成"拖回主机"转发出去, 主机收到后
    注入到**它自己的光标**上, 与用户手上的物理鼠标正面对抗。
    """

    def _agent(self):
        a = Agent(server_host="127.0.0.1", enable_return=False)
        a._return_edge = EdgeDetector(
            self_rect=ScreenRect(0, 0, 4480, 1600),
            peer_rect=ScreenRect(0, 0, 0, 0),
            side="left", confirm_frames=1,
        )
        a._input_tx = _FakeTx()
        return a

    def test_local_moves_dropped_after_return(self):
        a = self._agent()
        a._host_has_control = True
        a._return_edge._outside = True          # 交回后残留的边缘态
        for _ in range(20):
            a._on_return_event(_move(-1))       # 本机漂移
        assert a._input_tx.sent == [], "控制权在主电脑时不得转发本机位移"
        assert a._local_drift_dropped == 20
        assert a.stats.get("events_returned", 0) == 0

    def test_place_rearms_local_channel(self):
        a = self._agent()
        a._host_has_control = True
        a._on_input_event(InputEvent(EV_MOUSE_PLACE, 0, 0, 30000, 0, 0))
        assert a._host_has_control is False, "主机再次进入时必须解除抑制"

    def test_return_sets_host_has_control(self, agent_at_edge):
        a = agent_at_edge
        a._on_input_event(_move(-5))
        assert a.stats["events_returned"] == 1
        assert a._host_has_control is True


# ===========================================================================
# 5. 慢推必须和快甩一样能过去(2026-10-22 "慢移离不开边缘"的根因回归)
# ===========================================================================

class TestSlowPushEntry:
    """主机侧进入判定: confirm_frames=2 + "dx<=0 就清零" 会把慢推卡死。

    鼠标按固定频率上报, 慢慢移动时绝大多数报告是 dx=0(位移不足一个计数),
    夹着偶尔的 dx=+1。旧实现要求"连续 2 帧外向", 而 dx=0 会把 streak 清零,
    于是慢推几十秒都过不去; 快甩是一连串大 dx, 立刻满足。
    """

    def _model(self, confirm_frames: int):
        """复刻 Server._on_input_event 的喂入规则(只有 dx>0 才喂探测器)。"""
        edge = EdgeDetector(ScreenRect(0, 0, 1920, 1080),
                            ScreenRect(0, 0, 0, 0), side="right",
                            confirm_frames=confirm_frames)

        def feed(dx: int, x: int = 1919) -> bool:
            if dx <= 0:
                edge.reset()
                return False
            return edge.update(x, 0)

        return edge, feed

    def test_slow_push_crosses_with_single_frame(self):
        edge, feed = self._model(1)
        crossed = False
        for _ in range(20):                     # 慢推: 每 10 个上报才有 1 个 +1
            for _ in range(9):
                crossed = feed(0) or crossed
            crossed = feed(1) or crossed
            if crossed:
                break
        assert crossed, "慢推(夹着 dx=0 上报)必须能进穿越态"

    def test_slow_push_stalls_with_two_frames(self):
        _, feed = self._model(2)
        for _ in range(20):
            for _ in range(9):
                feed(0)
            if feed(1):
                break
        assert _is_outside_never_happened(feed), "这是旧 bug 的模型: 慢推永远进不去"

    def test_fast_flick_works_either_way(self):
        for n in (1, 2):
            _, feed = self._model(n)
            assert any(feed(30) for _ in range(2)), "快甩必须立刻过去"

    def test_server_uses_single_confirm_frame(self):
        src = inspect.getsource(Server._start_input)
        assert "confirm_frames=1" in src, \
            "主机侧进入判定必须与回传侧对称(一击即过)"


def _is_outside_never_happened(feed) -> bool:
    """上面的 2 帧模型跑完后, 探测器从未进入过 outside。"""
    # 模型里 feed() 的返回值恒为 False; 这里再喂一串 dx=0 确认不会突然成立
    for _ in range(50):
        if feed(0):
            return False
    return True


# ===========================================================================
# 6. 诊断绝不能改变行为(2026-10-22 事故: 一进副机指针就卡死)
# ===========================================================================

class TestDiagNeverChangesBehavior:
    """诊断开关开启时, 注入行为必须与关闭时**完全一致**。

    事故现场: `_diag_inject` 在行数配额(6000)用满后直接 `return`, 而调用方
    是"命中诊断就交给它, 由它内部注入" —— 于是配额一满, **边缘附近的所有
    位移被静默丢弃**。1000 事件/秒下 6000 行只需几秒, 所以每次重启后边缘
    只正常几秒; 用户看到的是"一进副机指针就卡死不动", 只能按 Ctrl+Alt+Esc
    逃出来(日志里 4 次逃生键)。
    """

    def test_moves_still_injected_when_diag_capped(self, agent_at_edge,
                                                   monkeypatch):
        a = agent_at_edge
        monkeypatch.setattr(Agent, "DIAG_EDGE", True)
        a._diag_lines = Agent.DIAG_MAX_LINES        # 配额已满
        a._on_input_event(_move(7))
        assert a._injected_moves == [(7, 0)], "诊断配额用满后位移仍必须注入"

    def test_moves_still_injected_when_diag_on(self, agent_at_edge,
                                               monkeypatch):
        a = agent_at_edge
        monkeypatch.setattr(Agent, "DIAG_EDGE", True)
        a._on_input_event(_move(7))
        assert a._injected_moves == [(7, 0)], "开启诊断时也必须注入"

    def test_log_helper_never_touches_cursor(self, agent_at_edge,
                                             monkeypatch, capsys):
        a = agent_at_edge
        a._diag_log_inject(_move(5), (0, 800))
        assert a._injected_moves == [], "日志函数不得改动光标"
        assert "inj dx=5" in capsys.readouterr().out

    def test_no_diag_call_site_injects(self):
        """源码约定: 注入与诊断必须是两条独立语句(顺序: 注入在前)。"""
        src = _strip_comments(inspect.getsource(Agent._on_input_event))
        i_inj = src.index("inj.move_relative(ev.a, ev.b)")
        i_diag = src.index("_diag_log_inject(ev, diag_before)")
        assert i_inj < i_diag, "必须先注入再记日志"
        assert "_diag_inject(" not in src, "旧的'诊断内部注入'写法必须消失"



# ===========================================================================
# 7. 全屏程序(游戏)时不许穿越(2026-10-22 用户要求)
# ===========================================================================

class TestGameBlocksCrossing:
    """前台是别的进程的全屏窗口时, 主机不得进入穿越态。

    用户原话: "主机在游戏中鼠标还是能穿梭到副机上。我需要游戏中这种情况时
    主机的鼠标靠近边缘也不要穿梭。" 游戏里鼠标经常贴边平移, 一穿过去
    键鼠就全跑到对面。
    """

    def _srv(self):
        s = object.__new__(Server)
        s._game_check = (False, 0.0)
        s._game_blocks = 0
        s._game_log_mono = 0.0
        return s

    def test_blocks_when_foreground_is_fullscreen(self, monkeypatch):
        import com_bridge.cursor as cur
        s = self._srv()
        monkeypatch.setattr(cur, "foreign_fullscreen_foreground", lambda: True)
        assert s._game_blocks_crossing() is True
        assert s._game_blocks == 1

    def test_allows_when_not_fullscreen(self, monkeypatch):
        import com_bridge.cursor as cur
        s = self._srv()
        monkeypatch.setattr(cur, "foreign_fullscreen_foreground", lambda: False)
        assert s._game_blocks_crossing() is False

    def test_verdict_is_cached(self, monkeypatch):
        """每个事件都查前台窗口太重: 结论必须在 TTL 内复用。"""
        import com_bridge.cursor as cur
        s = self._srv()
        calls = []
        monkeypatch.setattr(cur, "foreign_fullscreen_foreground",
                            lambda: calls.append(1) or True)
        for _ in range(50):
            s._game_blocks_crossing()
        assert len(calls) == 1, f"缓存失效: 查了 {len(calls)} 次"

    def test_entry_is_gated_in_hot_path(self):
        src = _strip_comments(inspect.getsource(Server._on_input_event))
        i_game = src.index("_game_blocks_crossing()")
        i_edge = src.index("self._edge.update(x, y)")
        assert i_game < i_edge, "必须在抵边判定之前拦截"

    def test_fullscreen_helper_exists(self):
        from com_bridge.cursor import (foreign_fullscreen_foreground,
                                       is_foreign_fullscreen)
        assert callable(foreign_fullscreen_foreground)
        assert callable(is_foreign_fullscreen)
