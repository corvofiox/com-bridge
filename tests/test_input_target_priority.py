# -*- coding: utf-8 -*-
"""★★ 回归: 输入转发目标必须"直连优先于 NAT 中转"。

## 用户现场实测 bug(日志原文)
    [server] agent 上报地址 192.168.10.201 (TCP 源地址 192.168.10.1 应为
             NAT 网关) -> 用上报地址转发输入
    [server] 由心跳学到副电脑真实地址 192.168.10.1:32802
             (原 192.168.10.201:24801) -> 切换输入转发目标

agent 上报的 192.168.10.201 是**同网段直连**(实测 ping 可达), 而心跳
学习把它覆盖成了**主路由的 NAT 映射地址**。后者需要 OpenWrt conntrack
转回, 多一跳且可能完全不可达。

根因: `_ensure_input_tx` 与 `_on_peer_heartbeat` 都无条件信任
"心跳学到的地址", 把"直连优于 NAT"这条基本网络常识弄反了。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from com_bridge.server import Server  # noqa: E402


def _strip_comments(src: str) -> str:
    return "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith("#"))


def test_pick_target_prefers_lan():
    """同网段直连地址必须优先于 NAT 映射地址。"""
    srv = Server.__new__(Server)      # 不跑 __init__, 只测纯函数
    srv._punched_host = "192.168.10.1"      # 心跳学到的 NAT 网关
    srv._punched_port = 32802
    got = srv._pick_input_target("192.168.10.201")   # agent 上报的直连
    assert got == ("192.168.10.201", 24801), \
        f"应保持直连, 实际 {got}"


def test_pick_target_uses_punch_for_cross_subnet():
    """跨网段部署时, 打洞地址才是唯一出路(不能硬用上报地址)。"""
    srv = Server.__new__(Server)
    srv._punched_host = "100.95.120.31"
    srv._punched_port = 32802
    got = srv._pick_input_target("192.168.10.201")
    assert got == ("100.95.120.31", 32802), \
        f"跨网段应使用打洞地址, 实际 {got}"


def test_pick_target_without_punch():
    """没有心跳信息时用上报地址 + 默认端口。"""
    srv = Server.__new__(Server)
    srv._punched_host = None
    srv._punched_port = None
    got = srv._pick_input_target("192.168.10.201")
    assert got == ("192.168.10.201", 24801)


def test_heartbeat_does_not_override_lan_target():
    """心跳到达时, 若当前已是同网段直连, 必须拒绝切换。"""
    src = _strip_comments(inspect.getsource(Server._on_peer_heartbeat))
    assert "_is_lan_peer" in src, \
        "心跳处理必须检查'当前是否已是直连', 否则会覆盖成 NAT 地址"
    assert "保持直连" in inspect.getsource(Server._on_peer_heartbeat), \
        "拒绝覆盖时应有明确日志"


def test_ensure_input_tx_uses_pick_target():
    """_ensure_input_tx 必须通过 _pick_input_target 决定目标(不能硬编码)。"""
    src = _strip_comments(inspect.getsource(Server._ensure_input_tx))
    assert "_pick_input_target" in src
    # 不得再出现"无条件优先 _punched"的写法
    assert "if self._punched_host and self._punched_port:\n            want" \
        not in src


# ---------------------------------------------------------------------------
# 用户现场(2026-10-19 12:54)日志的完整时序 —— 两个"误认网关"的入口
# ---------------------------------------------------------------------------

def test_empty_caps_does_not_build_target():
    """★★★ caps 为空时**绝不能**用 TCP 源地址建转发目标。

    用户日志第一行就是这个陷阱:
        [server] agent 已连接 192.168.10.1 caps={}
        [server] 输入转发目标 -> 192.168.10.1:24801
    空 caps 说明对方还没上报 adv_host, 此刻 TCP 源地址在 NAT 下**就是网关**。
    """
    srv = Server.__new__(Server)
    srv._input_tx = None
    srv._punched_host = None
    srv._punched_port = None
    srv._on_client_connect(("192.168.10.1", 50000), {})
    assert srv._input_tx is None, \
        "空 caps 时不得建转发目标(会把网关当副机)"


def test_gateway_heartbeat_does_not_build_target():
    """★★★ 心跳来自网关时不得单独建目标(它只是 NAT 映射, 不是副机地址)。"""
    srv = Server.__new__(Server)
    srv._input_tx = None
    srv._pending_src = None
    srv._punched_host = None
    srv._punched_port = None
    srv._last_peer_heartbeat = 0.0
    srv._on_peer_heartbeat(("192.168.10.1", 32802))
    assert srv._input_tx is None, \
        "网关心跳不得单独建目标(会发到路由器)"


def test_is_gateway_like():
    """网关识别: .1 结尾视为网关(本项目现场 192.168.10.1 是 OpenWrt)。"""
    assert Server._is_gateway_like("192.168.10.1") is True
    assert Server._is_gateway_like("10.0.0.1") is True
    assert Server._is_gateway_like("192.168.10.201") is False
    assert Server._is_gateway_like("100.95.120.31") is False
    assert Server._is_gateway_like("") is False


def test_adv_host_wins_over_gateway():
    """adv_host 到达后, 目标必须是它(而不是之前记录的网关源地址)。"""
    srv = Server.__new__(Server)
    srv._input_tx = None
    srv._pending_src = "192.168.10.1"
    srv._punched_host = "192.168.10.1"
    srv._punched_port = 32802
    srv._last_peer_heartbeat = 0.0
    # 直接测决策函数(建 socket 的行为在端到端工具里验证)
    got = srv._pick_input_target("192.168.10.201")
    assert got == ("192.168.10.201", 24801), f"实际 {got}"

