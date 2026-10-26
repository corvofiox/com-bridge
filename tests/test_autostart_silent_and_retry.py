"""回归: 开机自启必须静默 + 握手探测必须固定 5 秒(2026-10-26 用户报障)。

两条都不是"手感问题", 而是明确的规格:

1. **自启不许弹窗口**: HKCU Run 那条路一直带 `--autostart`; 但**管理员自启**
   (登录计划任务)当初只注册了 `gui` 子命令 —— 登录后直接弹出 GUI。
   另外 `USERDOMAIN` 在工作组机器上是 `WORKGROUP`, 用它拼任务主体会
   注册失败(HRESULT 0x80070534), 必须退回计算机名。
2. **握手探测固定 5 秒一格**: 旧实现是 1→2→4→5 的指数退避, 而且
   `ControlClient` 默认 5s 超时会让"连接尝试"自己阻塞, 实测间隔 6.5~9.5s,
   冷启动握手能拖到 40s+。用户原话: "我之前定的不是每5秒请求一次么？"
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from com_bridge import agent as agent_mod  # noqa: E402
from com_bridge import elevated as el  # noqa: E402
from com_bridge.agent import Agent  # noqa: E402
from com_bridge.transport import ControlClient  # noqa: E402


# ---------------------------------------------------------------- 自启静默
def test_elevated_task_action_includes_silent_flag():
    _exe, args = el._exe_path()
    assert el.SILENT_ARG == "--autostart"
    assert el.SILENT_ARG in args.split(), f"计划任务参数缺少静默标志: {args!r}"
    assert "gui" in args.split() or "-m" in args.split()


def test_silent_flag_matches_gui_autostart_parser():
    """两处必须用同一个标志, 否则"带了标志"也可能被 GUI 忽略。"""
    from com_bridge.gui import _is_autostart_argv
    assert _is_autostart_argv([el.SILENT_ARG]) is True
    assert _is_autostart_argv(["--silent"]) is True
    assert _is_autostart_argv(["gui"]) is False


def test_full_user_uses_computer_name_in_workgroup():
    r"""工作组机器: WORKGROUP\user 会注册失败, 必须换成 计算机名\user。"""
    assert el._full_user({"USERNAME": "gaoxi", "USERDOMAIN": "WORKGROUP",
                          "COMPUTERNAME": "CORVO-LAPTOP"}) == \
        "CORVO-LAPTOP\\gaoxi"


def test_full_user_keeps_real_domain():
    assert el._full_user({"USERNAME": "u", "USERDOMAIN": "CORP",
                          "COMPUTERNAME": "PC"}) == "CORP\\u"


def test_full_user_without_domain():
    assert el._full_user({"USERNAME": "u"}) == "u"


def test_register_uses_full_user_helper():
    src = inspect.getsource(el._do_register)
    assert "_full_user()" in src, "必须走 _full_user, 别再直接读 USERDOMAIN"


def test_task_is_silent_parses_arguments(monkeypatch):
    monkeypatch.setattr(el, "task_action_args",
                        lambda: "gui --autostart")
    assert el.task_is_silent() is True


def test_task_is_silent_detects_old_task(monkeypatch):
    monkeypatch.setattr(el, "task_action_args", lambda: "gui")
    assert el.task_is_silent() is False


def test_task_is_silent_unknown_when_query_fails(monkeypatch):
    monkeypatch.setattr(el, "task_action_args", lambda: None)
    assert el.task_is_silent() is None


def test_task_action_args_parses_xml(monkeypatch):
    xml = ("<Task><Actions><Exec><Command>cb.exe</Command>"
           "<Arguments>gui --autostart</Arguments></Exec></Actions></Task>")

    class R:
        returncode = 0
        stdout = xml.encode("utf-8")

    monkeypatch.setattr(el.subprocess, "run", lambda *a, **k: R())
    assert el.task_action_args() == "gui --autostart"


def test_task_action_args_none_on_failure(monkeypatch):
    class R:
        returncode = 1
        stdout = b""

    monkeypatch.setattr(el.subprocess, "run", lambda *a, **k: R())
    assert el.task_action_args() is None


def test_gui_selfheals_legacy_task():
    """GUI 启动时必须尝试自愈旧的"会弹窗"任务(源码守卫)。"""
    from com_bridge.gui import ComBridgeGui
    src = inspect.getsource(ComBridgeGui._selfheal_elevated_autostart)
    assert "task_is_silent" in src
    assert "_do_register" in src
    assert "is_elevated" in src, "非管理员时改不了任务, 应只提示"


# ---------------------------------------------------------------- 5 秒节拍
def test_retry_interval_is_five_seconds():
    assert Agent.HOST_RETRY_INTERVAL_S == pytest.approx(5.0)
    assert Agent.PROBE_CONNECT_TIMEOUT_S <= 1.0, "探测要短超时, 否则会吃掉整个间隔"


@pytest.mark.parametrize("elapsed,expected", [
    (0.0, 5.0),      # 探测瞬时失败 -> 等满 5s
    (0.3, 4.7),
    (1.0, 4.0),      # 短超时耗尽
    (4.9, 0.2),
    (5.0, 0.2),      # 探测比间隔还久(主机半死): 不再额外等待
    (9.9, 0.2),
])
def test_retry_delay_is_measured_from_attempt_start(elapsed, expected):
    assert Agent.retry_delay(elapsed) == pytest.approx(expected)


def test_retry_delay_interval_override():
    assert Agent.retry_delay(0.5, interval_s=2.0) == pytest.approx(1.5)


def test_wait_loops_use_retry_delay():
    """两条等待路径都必须用 retry_delay, 不能又写回自己的退避。"""
    for fn in (Agent._wait_control, Agent._wait_discover):
        src = inspect.getsource(fn)
        assert "retry_delay" in src, f"{fn.__name__} 没走固定节拍"
        assert "backoff" not in src, f"{fn.__name__} 又出现退避了"


def test_reconnect_uses_fixed_interval():
    src = inspect.getsource(Agent._resync_loop)
    assert "HOST_RETRY_INTERVAL_S" in src
    assert "backoff * 2" not in src, "不要指数退避(会超过 5s)"


def test_probe_uses_short_connect_timeout():
    for fn in (Agent._wait_control, Agent._resync_loop):
        src = inspect.getsource(fn)
        assert "connect_timeout" in src, f"{fn.__name__} 探测没用短超时"


def test_control_client_connect_timeout_defaults_to_timeout():
    c = ControlClient("127.0.0.1", 1, timeout=7.0)
    assert c.timeout == 7.0
    assert c.connect_timeout == 7.0
    c2 = ControlClient("127.0.0.1", 1, timeout=7.0, connect_timeout=1.0)
    assert c2.connect_timeout == 1.0 and c2.timeout == 7.0


def test_control_client_restores_normal_timeout_after_handshake():
    """握手用短超时, 成功之后必须恢复正常超时, 否则后续收发容易误超时。"""
    src = inspect.getsource(ControlClient.connect)
    assert "connect_timeout" in src
    i = src.rindex("settimeout(self.timeout)")
    assert i > src.index("HELLOACK"), "恢复超时必须放在握手之后"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
