# -*- coding: utf-8 -*-
"""★★★ 回归: 键盘钩子自检结论**必须能更新**(不能永久禁用键盘)。

## 踩过的坑(用户实测)
"鼠标进入副机后, 键盘不会在副机输入, 键盘跨屏完全失效"

原因链:
  1. GUI 启动时窗口**有焦点** -> 自检失败(kbd_calls=0, 实测确认)
  2. 自检失败 -> `_kbd_hook_verified = False` -> 键盘转发被**永久**禁用
  3. GUI 之后主动放弃焦点 -> 钩子恢复(kbd_calls=6, 实测确认)
  4. 但**没有任何代码**把结论更新回 True -> 键盘一直不可用

## 修复
自检结论带**有效期**(KBD_VERIFY_TTL_S), 由 `_keyboard_relay_ready`
在过期时复检; 且 `_acquire_control`(每次穿越开始)强制复检一次。

## 为什么不干脆不做"禁用键盘"的兜底
因为钩子失效时若照样转发, 键会同时落主机和副机(用户最初的症状)。
"禁用"与"恢复"必须成对存在, 缺任一半都会出问题。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from com_bridge.server import Server  # noqa: E402


def test_verify_result_has_ttl():
    """自检结论必须带有效期(否则焦点变化后永远无法恢复)。"""
    assert hasattr(Server, "KBD_VERIFY_TTL_S")
    assert 0 < Server.KBD_VERIFY_TTL_S <= 5.0, \
        "TTL 要足够短以跟随焦点变化, 又不至于在热路径上频繁注入"


def test_reverify_exists():
    """必须有独立复检方法。"""
    assert hasattr(Server, "_reverify_keyboard_hook")


def test_relay_ready_triggers_reverify():
    """★ 判据过期时必须复检 —— 这是从"永久禁用"恢复的唯一途径。"""
    src = inspect.getsource(Server._keyboard_relay_ready)
    assert "_reverify_keyboard_hook" in src, \
        "过期必须复检, 否则焦点交出后键盘永远不可用"
    assert "KBD_VERIFY_TTL_S" in src


def test_acquire_control_forces_reverify():
    """★ 每次穿越开始必须强制复检(不依赖 TTL)。"""
    src = inspect.getsource(Server._acquire_control)
    assert "_reverify_keyboard_hook" in src, \
        "穿越开始是最需要键盘可用的时刻, 必须复检"


def test_reverify_updates_on_success():
    """复检成功必须把结论更新为 True 并打日志(用户可见恢复)。"""
    src = inspect.getsource(Server._reverify_keyboard_hook)
    assert "_kbd_hook_verified = bool(probe)" in src
    assert "重新启用键盘转发" in src


def test_reverify_keeps_result_on_unknown():
    """探针返回 None(注入失败)时不得改变结论(判据不可用≠钩子失效)。"""
    src = inspect.getsource(Server._reverify_keyboard_hook)
    # probe is None 时 return, 不写 _kbd_hook_verified
    assert "if probe is None:" in src
    idx = src.index("if probe is None:")
    after = src[idx:idx + 200]
    assert "return" in after, "None 必须早退, 保持原结论"


def test_reverify_updates_timestamp():
    """复检必须更新时间戳, 否则 TTL 永不过期。"""
    src = inspect.getsource(Server._reverify_keyboard_hook)
    assert "_kbd_verify_mono = time.monotonic()" in src
