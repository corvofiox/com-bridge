# -*- coding: utf-8 -*-
"""交接区"推动式回传" + 去抖 + 隐藏幂等 + 输入线程不阻塞的回归测试。

## 现场一(2026-10-22 上午, 修前)

副机日志 886 行里有 712 行是同一句 `光标离开回传边`, 同一手势最多在
42ms 内触发 **145 次**。每一次都会往主机回传一遍位移、并调一次
`hider.hide()`(单次 ≈50ms: 全屏定位 + 抢前台 + 抓捕获 + 两次 ±1px 注入
+ 两个 sleep)。145 次 ≈ 7 秒, 期间 `show()` 排在几十个 hide 后面。

## 现场二(2026-10-22 下午, 用户复报"边缘粘滞依然存在")

去抖把"7 秒卡死"修掉了, 但手感还是"被吸在边上"。原因是**判定条件本身**:
旧规则是"光标距边 ≤2px 且朝主机方向"就立刻交回控制权, 而进入副机时
光标本来就落在离边 `ENTRY_JUMP_ZONE_PX` 处 —— 于是手一点点向左的漂移
(斜着推, 或者管道滞后导致手已回拉而指针还在往左走)就足以把控制权
甩回主机; 回到主机又同样落在离边 12px 处, 一个向右的抖动又跳回去。

新规则: 光标顶到墙上之后, **继续朝主机方向多推 RETURN_PUSH_OUT_PX 像素**
才交回。边缘的手感变成"硬墙", 只有真的往外顶才穿过去。

## 现场三(主机侧)

`_acquire_control` 原先在**原始输入回调线程**上同步调 `_reverify_keyboard_hook()`,
而探针要轮询等最多 0.5s —— 每次越界都把输入管线堵一次, 排队的事件随后
一次性倒给副机(指针先僵住再猛窜)。

这些测试不依赖真实桌面: 纯状态机 + 源码断言 + 记录型假 user32。
"""
from __future__ import annotations

import ast
import inspect
import threading
import time
import textwrap

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
    return ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]


def _call_names(fn) -> set[str]:
    """函数体里实际调用到的名字(方法名或函数名)。"""
    out = set()
    for n in ast.walk(_func_ast(fn)):
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


# 每次事件推 5px => 达到阈值所需的事件数(向上取整)
_STEP = 5
_PUSH_N = -(-Agent.RETURN_PUSH_OUT_PX // _STEP)


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
    injected: list[tuple[int, int]] = []
    monkeypatch.setattr(agent_mod, "get_cursor_pos", lambda: (0, 800))
    monkeypatch.setattr(InputInjector, "move_relative",
                        staticmethod(lambda dx, dy: injected.append((dx, dy))))
    monkeypatch.setattr(InputInjector, "move_absolute",
                        staticmethod(lambda x, y: 1))
    a._injected_moves = injected          # 供测试断言
    return a


def _push(a, times: int) -> None:
    """朝主机方向推 times 个 5px 的位移事件。"""
    for _ in range(times):
        a._on_input_event(_move(-_STEP))


# ===========================================================================
# 1. 推动式回传: 贴边不等于回传
# ===========================================================================

class TestPushThrough:
    """★ 这是"边缘粘滞"的正面修复: 贴边只把指针停在墙上, 不交回控制权。"""

    def test_threshold_is_sane(self):
        # 太小 = 一碰就走(粘滞); 太大 = 要使劲顶(反而难用)
        assert 16 <= Agent.RETURN_PUSH_OUT_PX <= 40

    def test_hold_at_wall_does_not_return(self, agent_at_edge):
        """推得不够时指针停在墙上, 控制权不动。"""
        a = agent_at_edge
        _push(a, _PUSH_N - 1)
        assert a.stats.get("events_returned", 0) == 0
        assert a._hider.hide_calls == 0
        assert a._returned is False
        assert a._push_acc == (_PUSH_N - 1) * _STEP
        # 这些位移照常注入(系统会把它钳在边界, 指针不越界)
        assert len(a._injected_moves) == _PUSH_N - 1

    def test_push_enough_returns_once(self, agent_at_edge):
        """推够了就交回一次 —— 并且只交回一次。"""
        a = agent_at_edge
        _push(a, _PUSH_N)
        assert a.stats["events_returned"] == 1
        assert a._hider.hide_calls == 1
        assert a._input_tx.types() == [EV_MOUSE_RETURN, EV_MOUSE_MOVE_REL]

    def test_backlog_after_return_is_dropped(self, agent_at_edge):
        """回传之后管线里积压的陈旧位移必须丢弃, 不能回放给主机。

        它们比真实时间晚 0.15~0.3s, 而主机的物理鼠标早已实时走过同样的
        路程(回程还是绝对定位) —— 重放等于把手速放大两倍。
        """
        a = agent_at_edge
        _push(a, _PUSH_N)
        held = len(a._injected_moves)
        _push(a, 40)                      # 积压
        assert a.stats["events_returned"] == 1
        assert a.stats.get("events_dropped_stale", 0) == 40
        assert len(a._injected_moves) == held, "陈旧位移不得再注入"
        assert len(a._input_tx.sent) == 2, "陈旧位移不得再回传给主机"

    def test_inward_motion_resets_push(self, agent_at_edge):
        """往回拉一下(哪怕只 1px)就清空累计: 用户显然不打算走。"""
        a = agent_at_edge
        _push(a, _PUSH_N - 2)
        before = a._push_acc
        assert before > 0
        a._on_input_event(_move(+3))      # 朝屏内
        assert a._push_acc == 0
        assert a._push_t0 == 0.0
        _push(a, _PUSH_N - 2)             # 又要从头推
        assert a.stats.get("events_returned", 0) == 0

    def test_push_memory_window_expires(self, agent_at_edge):
        """贴边停一会儿再轻轻一推, 不该沿用旧的累计(否则会意外回传)。"""
        a = agent_at_edge
        _push(a, _PUSH_N - 1)
        a._push_mono = time.monotonic() - (Agent.RETURN_PUSH_MEMORY_S + 1.0)
        _push(a, 1)
        assert a._push_acc == _STEP, "超过记忆窗口必须从零重新计"
        assert a.stats.get("events_returned", 0) == 0

    def test_drain_window_is_bounded_and_self_heals(self, agent_at_edge):
        """回程包万一丢了(主机没接管), 冷静期一过必须恢复注入。

        否则副机会**永久吞掉**输入 —— 那比"指针贴着边"严重得多。
        """
        a = agent_at_edge
        _push(a, _PUSH_N)
        assert a._returned is True
        a._returned_mono = time.monotonic() - (Agent.RETURN_STALE_DRAIN_S + 1.0)
        a._return_edge.reset()
        a._on_input_event(_move(3))
        assert a._returned is False
        assert a._injected_moves[-1] == (3, 0), "冷静期过后必须恢复注入"

    def test_overshoot_geometry(self):
        """_overshoot_px 是纯函数: 四种边都要算对(可单测)。"""
        a = Agent(server_host="127.0.0.1", enable_return=False)
        for side, rect, args, expect in (
            ("left", ScreenRect(0, 0, 100, 50), (0, 10, -5, 0), 5),
            ("left", ScreenRect(0, 0, 100, 50), (10, 10, -5, 0), 0),
            ("right", ScreenRect(0, 0, 100, 50), (99, 10, 7, 0), 7),
            ("top", ScreenRect(0, 0, 100, 50), (5, 0, 0, -9), 9),
            ("bottom", ScreenRect(0, 0, 100, 50), (5, 49, 0, 4), 4),
        ):
            a._return_edge = EdgeDetector(self_rect=rect,
                                          peer_rect=ScreenRect(0, 0, 0, 0),
                                          side=side, confirm_frames=1)
            assert a._overshoot_px(*args) == expect, side

    def test_debounce_constants_present(self):
        assert "_returned" in inspect.getsource(Agent.__init__)
        assert "_push_acc" in inspect.getsource(Agent.__init__)
        src = _strip_comments(inspect.getsource(Agent._on_input_event))
        assert "if self._returned:" in src
        assert "RETURN_STALE_DRAIN_S" in src
        assert "RETURN_PUSH_OUT_PX" in src
        assert "events_dropped_stale" in src

    def test_return_logged_once_per_crossing(self, agent_at_edge, capsys):
        """日志里不能再出现"同一手势 145 行"(它占了副机日志的 80%)。"""
        a = agent_at_edge
        _push(a, 40)
        out = capsys.readouterr().out
        assert out.count("推过回传边") == 1

    def test_place_rearms_both_gates(self, agent_at_edge):
        """主机再次越界进入副机(EV_MOUSE_PLACE)必须解除去抖锁。"""
        a = agent_at_edge
        _push(a, _PUSH_N)
        assert a.stats["events_returned"] == 1
        a._on_input_event(InputEvent(EV_MOUSE_PLACE, 0, 0, 30000, 0, 0))
        assert a._returned is False
        _push(a, _PUSH_N)
        assert a.stats["events_returned"] == 2, "新一轮穿越必须能再次回传"
        assert a._hider.hide_calls == 2


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
        body = _func_ast(CursorHider._do_hide).body
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
        srv._kbd_verify_thread.join(timeout=2.0)
