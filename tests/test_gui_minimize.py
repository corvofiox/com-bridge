# -*- coding: utf-8 -*-
"""★★★ 回归: 窗口最小化后必须能恢复(不能消失)。

## 用户报告(2026-10-19)
"当我点击最小化时, 程序 GUI 完全从桌面消失, 任务栏中也消失了,
 使得窗口无法再恢复"

## 根因(两处, 都是键盘焦点修复引入的回归)

1. **WS_EX_NOACTIVATE**(0x08000000)
   实测(tools/exp_minimize_vanishing.py): 带该样式的窗口最小化后
   `ShowWindow(RESTORE)` 能把 iconic 清掉、visible 也是 True, 但
   **GetForegroundWindow 永远不是它** —— 用户点任务栏也激活不了,
   感知就是"窗口彻底消失了, 无法恢复"。
   => 必须移除该样式。让出焦点改由监控完成。

2. **焦点监控在最小化期间仍抢前台**
   用户点任务栏恢复时, 窗口刚拿到前台就被监控交给 shell,
   表现为"点了没反应/闪一下又没了"。
   => 监控必须跳过"最小化/不可见"的窗口。

3. **监控与用户操作争抢(衍生)**
   用户点开 GUI 想看状态, 700ms 内焦点被夺走 -> 界面用不了。
   => 用户操作后给 8 秒宽限期, 之后自动恢复键盘保护。

## 验证
tools/verify_minimize_fix.py: 8/8 通过(含"恢复后能被激活"与
"宽限期后监控恢复保护"两个关键点)。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.gui as gui_mod  # noqa: E402


def test_noactivate_not_applied():
    """★ 生产代码**不得**再设置 WS_EX_NOACTIVATE(会让窗口无法激活)。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._apply_noactivate)
    # 只允许出现在注释/文档里, 不得有实际的 SetWindowLongW 调用
    assert "SetWindowLongW" not in src, \
        "不得再设置窗口样式 —— 它让窗口最小化后无法恢复"


def test_focus_watch_skips_iconic():
    """★ 监控必须跳过最小化/不可见的窗口。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._focus_watch_tick)
    assert "IsIconic" in src, "必须检查最小化状态"
    assert "IsWindowVisible" in src, "必须检查可见性"


def test_focus_watch_respects_grace():
    """★ 监控必须尊重用户操作宽限期。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._focus_watch_tick)
    assert "_focus_grace_until" in src, \
        "必须有宽限期, 否则用户点开界面立刻被夺走焦点"


def test_grace_granted_on_user_activity():
    """用户交互事件必须绑定到宽限期授予。"""
    assert hasattr(gui_mod.ComBridgeGui, "_on_user_activity")
    assert hasattr(gui_mod.ComBridgeGui, "_grant_focus_grace")
    src = inspect.getsource(gui_mod.ComBridgeGui._on_user_activity)
    assert "_grant_focus_grace" in src
    # 最小化时不该给宽限(监控本来就跳过)
    assert "iconic" in src


def test_grace_initialized():
    """宽限期字段必须初始化(否则首次访问 AttributeError)。"""
    src = inspect.getsource(gui_mod.ComBridgeGui.__init__)
    assert "_focus_grace_until" in src


def test_grace_is_bounded():
    """宽限期不能永久生效(否则键盘保护彻底失效)。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._grant_focus_grace)
    import re
    m = re.search(r"seconds: float = ([0-9.]+)", src)
    assert m, "宽限期应有明确默认值"
    val = float(m.group(1))
    assert 1.0 <= val <= 60.0, f"宽限期 {val}s 不合理(应 1~60s)"
