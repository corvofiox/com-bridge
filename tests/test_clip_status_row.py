"""状态窗口"剪贴板"一行(_clip_status)的回归测试(审查项 #5)。

这一行是新加的展示面: 发/收计数、文件数、流量、排队、取消、失败。
它是纯函数(静态方法, 不碰 tkinter), 可以直接验证。
"""
import os
import subprocess
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from com_bridge import clip_transfer as ct  # noqa: E402
from com_bridge.gui import ComBridgeGui  # noqa: E402


class Role:
    """最小角色替身: _clip_status 只通过 getattr 读这几个属性。"""


def _outbox(**kw):
    ob = ct.ClipOutbox("127.0.0.1", 1)
    for k, v in kw.items():
        setattr(ob, k, v)
    return ob


def test_not_enabled_when_no_clipboard_objects():
    text, level = ComBridgeGui._clip_status(Role())
    assert text == "未启用" and level == 0


def test_reports_sent_and_received_with_traffic():
    r = Role()
    r._clip_tx = _outbox(sent=3, bytes_sent=12 * 1048576, _busy=True)
    r._clip_rx = types.SimpleNamespace(
        stats={"payloads": 5, "files": 2, "bytes": 30 * 1048576, "rejected": 0})
    r._clip_sync = types.SimpleNamespace(stats={"skipped_big": 1, "errors": 0})
    text, level = ComBridgeGui._clip_status(r)
    assert "发 3 项" in text and "12.0 MB" in text
    assert "传输中" in text
    assert "收 5 项" in text and "文件 2 个" in text and "30.0 MB" in text
    assert "过大跳过 1" in text
    assert level == 1


def test_send_failure_turns_red():
    r = Role()
    r._clip_tx = _outbox(failed=2)
    r._clip_rx = None
    r._clip_sync = None
    text, level = ComBridgeGui._clip_status(r)
    assert "失败 2" in text
    assert level == 3


def test_receive_rejection_turns_red():
    r = Role()
    r._clip_tx = None
    r._clip_rx = types.SimpleNamespace(
        stats={"payloads": 1, "files": 0, "bytes": 0, "rejected": 3})
    r._clip_sync = None
    text, level = ComBridgeGui._clip_status(r)
    assert "拒收 3" in text and level == 3


def test_queue_and_cancel_are_visible():
    r = Role()
    ob = _outbox(cancelled=1)
    ob._q = [("blob", []), ("blob2", [])]      # stats() 的 queued = len(_q)
    r._clip_tx = ob
    r._clip_rx = None
    r._clip_sync = None
    text, _level = ComBridgeGui._clip_status(r)
    assert "排队 2" in text and "取消 1" in text


def test_collect_sync_errors_raise_warning_level():
    r = Role()
    r._clip_tx = _outbox()
    r._clip_rx = None
    r._clip_sync = types.SimpleNamespace(stats={"skipped_big": 0, "errors": 2})
    text, level = ComBridgeGui._clip_status(r)
    assert "采集错误 2" in text and level == 2


def test_status_window_has_the_clipboard_field():
    """字段表里必须有这一行, 且分组/显示名固定(否则界面上找不到)。"""
    from com_bridge import gui_windows as gw
    assert ("clipboard", "跨机功能", "剪贴板") in gw.StatusWindow.FIELDS


def test_status_window_renders_clipboard_row():
    """真渲染一遍状态窗口, 确认这一行拿到的是数据而不是"—"。

    ★ 放在**子进程**里做: 在同一个 pytest 进程里创建/销毁 Tk 根窗口会污染
      后续 Tk 用例(实测: test_gui_theme_v2 会偶发 3 failed/10 errors, 单跑却
      全过)。子进程彻底隔离图形状态。
    """
    code = (
        "import queue, sys, tkinter as tk\n"
        "sys.path.insert(0, '.')\n"
        "from com_bridge import gui_windows as gw\n"
        "class App:\n"
        "    _log_q = queue.Queue()\n"
        "    def collect_status(self):\n"
        "        return {'clipboard': ('发 1 项 (0.5 MB) ｜ 收 2 项（文件 1 个, 3.0 MB）', 1)}\n"
        "root = tk.Tk(); root.withdraw()\n"
        "win = gw.StatusWindow(root, App()); win._create()\n"
        "print('ROWS=' + ','.join(win._rows))\n"
        "print('TEXT=' + win._rows['clipboard'].cget('text'))\n"
        "win.hide_children(); win.destroy(); root.destroy()\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=120, cwd=ROOT)
    if "ROWS=" not in r.stdout:
        pytest.skip(f"无图形环境或 Tk 不可用: {r.stderr[-300:]}")
    rows = [ln.split("=", 1)[1] for ln in r.stdout.splitlines()
            if ln.startswith("ROWS=")][0].split(",")
    assert "clipboard" in rows, f"状态窗口没渲染出剪贴板这一行: {rows}"
    text = [ln.split("=", 1)[1] for ln in r.stdout.splitlines()
            if ln.startswith("TEXT=")][0]
    assert "发 1 项" in text and "文件 1 个" in text, text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
