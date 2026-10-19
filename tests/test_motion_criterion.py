# -*- coding: utf-8 -*-
"""CursorHider 注入时间戳判据(hide_on_motion)—— 纯数学/接线测试, 不碰真桌面。

背景: 副机 hider 必须区分"注入移动(主机转发, 不得顶掉 hider)"与"物理移动
(用户接管, 必须恢复光标)"。GetMessageExtraInfo 判据实测不可靠(合并取链/
合成消息 extra=0/语义歧义), 已换成注入时间戳判据:
    InputInjector._last_move_ts 由 move_relative/move_absolute 打点,
    CursorHider._is_injected_motion() 查 last_move_age() <= INJECTED_MOVE_WINDOW_S。
本文件把判据锁死为纯函数行为 + 源码接线, 活体行为归 tools/verify_hider.py。
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from com_bridge import cursor as cursor_mod
from com_bridge import input as input_mod

ROOT = Path(__file__).resolve().parents[1]
CURSOR_SRC = (ROOT / "com_bridge" / "cursor.py").read_text(encoding="utf-8")
INPUT_SRC = (ROOT / "com_bridge" "input.py").read_text(encoding="utf-8") \
    if False else (ROOT / "com_bridge" / "input.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 判据行为(纯逻辑: 时间戳是 InputInjector 的类状态, 直接操纵)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_ts():
    old = input_mod.InputInjector._last_move_ts
    yield
    input_mod.InputInjector._last_move_ts = old


class TestLastMoveAge:
    def test_never_injected_is_inf(self):
        input_mod.InputInjector._last_move_ts = 0.0
        assert input_mod.InputInjector.last_move_age() == float("inf")

    def test_just_injected_is_zero(self):
        input_mod.InputInjector._last_move_ts = time.monotonic()
        assert input_mod.InputInjector.last_move_age() < 0.05

    def test_old_injection_grows(self):
        input_mod.InputInjector._last_move_ts = time.monotonic() - 5.0
        assert input_mod.InputInjector.last_move_age() >= 4.9


class TestCursorCriterion:
    def _hider(self):
        # 不 start(): 只测判据函数, 无需窗口/线程
        return cursor_mod.CursorHider(hide_on_motion=True)

    def test_recent_injection_counts_as_injected(self):
        h = self._hider()
        input_mod.InputInjector._last_move_ts = time.monotonic()
        assert h._is_injected_motion() is True
        assert h._injected_seen == 1

    def test_stale_injection_is_physical(self):
        h = self._hider()
        input_mod.InputInjector._last_move_ts = (
            time.monotonic() - cursor_mod.CursorHider.INJECTED_MOVE_WINDOW_S - 0.01)
        assert h._is_injected_motion() is False
        assert h._injected_seen == 0

    def test_window_is_250ms(self):
        assert cursor_mod.CursorHider.INJECTED_MOVE_WINDOW_S == 0.25

    def test_window_tightens_recovery_latency(self):
        # 恢复延迟上界 = 窗口长度(注入流停止后, 物理一动立即生效)
        assert cursor_mod.CursorHider.INJECTED_MOVE_WINDOW_S <= 0.30


# ---------------------------------------------------------------------------
# 接线: 打点必须发生在两条注入移动路径上; 判据必须接在 WM_MOUSEMOVE 分支
# ---------------------------------------------------------------------------

class TestWiring:
    def test_move_relative_records_ts(self):
        tree = ast.parse(INPUT_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "move_relative")
        src = ast.get_source_segment(INPUT_SRC, fn)
        assert "_last_move_ts = time.monotonic()" in src, \
            "move_relative 注入成功后必须打点"

    def test_move_absolute_records_ts(self):
        tree = ast.parse(INPUT_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "move_absolute")
        src = ast.get_source_segment(INPUT_SRC, fn)
        assert "_last_move_ts = time.monotonic()" in src, \
            "move_absolute 注入成功后必须打点"

    def test_ts_recorded_only_on_success(self):
        # 打点行必须在 `if n > 0:` 保护下(注入失败不应刷新窗口)
        tree = ast.parse(INPUT_SRC)
        hits = 0
        for n in ast.walk(tree):
            if isinstance(n, ast.If):
                cond = ast.get_source_segment(INPUT_SRC, n.test) or ""
                if "n > 0" in cond or "n != 0" in cond:
                    body = ast.get_source_segment(INPUT_SRC, n.body[0]) or ""
                    hits += "_last_move_ts = time.monotonic()" in body
        assert hits >= 2, "两条移动路径的打点都应受 n>0 保护"

    @staticmethod
    def _strip_comments(src: str) -> str:
        """去掉注释与 docstring, 只看代码本体(注释里允许记载历史)。"""
        out = []
        for line in src.splitlines():
            i = line.find("#")
            out.append(line if i < 0 else line[:i])
        return "\n".join(out)

    def test_proc_uses_criterion(self):
        tree = ast.parse(CURSOR_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_proc")
        src = self._strip_comments(
            ast.get_source_segment(CURSOR_SRC, fn) or "")
        assert "_is_injected_motion" in src
        assert "GetMessageExtraInfo" not in src, "已废弃的判据不得回流"

    def test_criterion_is_window_based(self):
        tree = ast.parse(CURSOR_SRC)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_is_injected_motion")
        src = ast.get_source_segment(CURSOR_SRC, fn)
        assert "last_move_age" in src
        assert "INJECTED_MOVE_WINDOW_S" in src

    def test_no_hidden_at_grace_left(self):
        # 旧时间宽限(MOTION_GRACE_S/_hidden_at)必须整体移除:
        # MOTION_GRACE_S 只允许出现在历史注释里(非代码行),
        # _hidden_at 字段与赋值必须完全消失。
        assert "_hidden_at" not in CURSOR_SRC
        code = self._strip_comments(CURSOR_SRC)
        assert "MOTION_GRACE_S" not in code, "旧宽限常量不得回流"
