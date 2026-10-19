# -*- coding: utf-8 -*-
"""★★★ 回归: GUI 窗口必须**持续**放弃焦点(不能只在启动时交一次)。

## 用户实测确认(2026-10-19 13:4x)
"主机程序在前台, 副机打字会发送到主机的程序窗口。
 主机程序在后台, 副机打字没有发送到主机。"
=> 焦点是唯一开关。

## 上一版的缺陷
只在 server 启动成功后把焦点交给 shell 一次。用户随后点一下 GUI,
窗口又变前台 -> 钩子再次失效, 而且旧代码里还有 `self.root.focus_set()`
反而把焦点设回自己。

## 修复(三层)
1. WS_EX_NOACTIVATE 窗口样式(辅助, Tk 的 focus_force 仍可能绕过)
2. 启动时把前台交给 shell
3. **每 700ms 监控一次**, 自己是前台就立刻交出去(主力)

验证: tools/verify_focus_watch.py 5/5 全过
  无监控有焦点 -> kbd_calls+0
  监控交出焦点 -> kbd_calls+10
  用户反复点窗口 3 次 -> kbd_calls+6/+6/+6(每次都纠正)
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.gui as gui_mod  # noqa: E402


def test_focus_watch_exists_and_self_schedules():
    """必须有周期性焦点监控, 且自己续约(after 链)。"""
    assert hasattr(gui_mod.ComBridgeGui, "_focus_watch_tick")
    src = inspect.getsource(gui_mod.ComBridgeGui._focus_watch_tick)
    assert "GetForegroundWindow" in src, "必须检查当前前台窗口"
    assert "SetForegroundWindow" in src, "抢到前台必须交出去"
    assert "after(" in src, "必须自己续约, 否则只执行一次"
    assert "_closing" in src, "关闭时必须停止续约"


def test_focus_watch_started_once():
    """监控只能启动一次(避免 after 链重复堆积)。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._release_focus_for_hook)
    assert "_focus_watch_started" in src
    assert "after(" in src


def test_no_focus_set_to_self():
    """★ 绝不能调用 root.focus_set() —— 那是把焦点设回自己(旧 bug)。"""
    for name in ("_release_focus_for_hook", "_focus_watch_tick"):
        src = inspect.getsource(getattr(gui_mod.ComBridgeGui, name))
        assert "self.root.focus_set()" not in src, \
            f"{name} 里不得把焦点设回自己"


def test_noactivate_not_applied():
    """★ 不得再设置 WS_EX_NOACTIVATE —— 它让窗口最小化后无法恢复。

    用户实测(2026-10-19): "点最小化后 GUI 完全从桌面消失, 任务栏中也
    消失, 无法再恢复"。
    实测(tools/exp_minimize_vanishing.py): 带该样式的窗口最小化后
    ShowWindow(RESTORE) 能清 iconic, 但 GetForegroundWindow 永远不是它
    —— 用户点任务栏也激活不了。
    让出焦点改由 _focus_watch_tick 在"可见且非最小化"时完成。
    """
    assert hasattr(gui_mod.ComBridgeGui, "_apply_noactivate")
    src = inspect.getsource(gui_mod.ComBridgeGui._apply_noactivate)
    assert "SetWindowLongW" not in src, \
        "不得再设置窗口样式(会导致无法激活/无法恢复)"


def test_watch_period_reasonable():
    """监控周期要够短(用户点窗口后能快速纠正)。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._focus_watch_tick)
    # 找 after( 后面的数字
    import re
    m = re.search(r"after\((\d+)", src)
    assert m, "必须有 after(N, ...) 调用"
    period = int(m.group(1))
    assert period <= 1000, f"周期 {period}ms 太长, 用户点窗口后会长时间失效"


def test_close_stops_watch():
    """真正退出时必须置 _closing, 否则 after 链会在销毁后继续跑(报错)。

    ★ 注意: 点 X 现在只隐藏窗口(托盘程序的标准行为), 所以 _closing
      在 **真正退出** 的路径(_quit_app)里设置, 而不是 _on_close。
    """
    src = inspect.getsource(gui_mod.ComBridgeGui._quit_app)
    assert "_closing = True" in src
    # _on_close 应当转发到 _quit_app 或 merely withdraw
    close_src = inspect.getsource(gui_mod.ComBridgeGui._on_close)
    assert "_quit_app" in close_src or "withdraw" in close_src
