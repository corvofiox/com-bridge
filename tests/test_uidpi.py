# -*- coding: utf-8 -*-
"""界面缩放(uidpi)的回归测试。

背景: 本进程是 per-monitor DPI 感知的(输入坐标需要), 代价是 tkinter 不再
自动放大界面 —— 200% 缩放的笔记本屏上, 96 DPI 设计好的窗口只有四分之一
面积(用户实测: "副机 2560x1600, GUI 看起来很小")。所以缩放自己算、自己套。
"""
from __future__ import annotations

import os

import pytest

from com_bridge import uidpi


def test_scale_is_within_bounds():
    assert 1.0 <= uidpi.SCALE <= 3.0


def test_sp_rounds_to_int():
    old = uidpi.SCALE
    try:
        uidpi.SCALE = 1.0
        assert uidpi.sp(620) == 620
        assert isinstance(uidpi.sp(20), int)
        uidpi.SCALE = 2.0
        assert uidpi.sp(620) == 1240
        assert uidpi.sp(1) == 2          # 四舍五入, 不丢成 0
        uidpi.SCALE = 1.5
        assert uidpi.sp(21) == 32        # 31.5 -> 32
    finally:
        uidpi.SCALE = old


def test_clamp_bounds():
    assert uidpi._clamp(0.5) == uidpi._MIN_SCALE
    assert uidpi._clamp(9.0) == uidpi._MAX_SCALE
    assert uidpi._clamp(1.5) == 1.5


@pytest.mark.parametrize("raw,expect", [
    ("2", 2.0), ("1.5", 1.5), ("150%", 1.5), (" 1.25 ", 1.25),
])
def test_env_override_parsed(monkeypatch, raw, expect):
    monkeypatch.setenv("COM_BRIDGE_UI_SCALE", raw)
    assert uidpi._from_env() == pytest.approx(expect)


def test_env_garbage_ignored(monkeypatch):
    monkeypatch.setenv("COM_BRIDGE_UI_SCALE", "big")
    assert uidpi._from_env() is None


def test_desc_mentions_factor():
    text = uidpi.desc()
    assert "界面缩放" in text and "×" in text


def test_theme_constants_are_scaled():
    """主题常量必须已经过缩放(否则 100%/200% 下窗口一样大 = 没生效)。"""
    old = uidpi.SCALE
    try:
        uidpi.SCALE = 1.0
        assert uidpi.sp(620) == 620
    finally:
        uidpi.SCALE = old
    from com_bridge import ui_theme as T
    assert T.WINDOW_W == T.sp(620)
    assert T.PAGE_PAD == T.sp(20)
