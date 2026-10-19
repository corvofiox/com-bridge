"""方位(side)语义与下发测试。

★ 这里锁死用户实际踩到的第一个问题:
    "主机设置 right, 副机设置 right 才能连上; 主机 right + 副机 left
     就发送不到副机。"

根因: side 的语义是"**副电脑位于主电脑的哪一侧**", 与副机自己的方位无关。
      副机若按"主电脑在我哪边"来填, 就会填成相反值, 于是把回传边算反 ——
      主电脑转发来的每一个位移都被判成"用户要回主机"而原样退回, 光标
      完全进不去副机。
修复: 主电脑把自己的 side 编进心跳下发, 副机直接采用(见 Agent.
      _on_server_side), 用户只需在主电脑上设置一次。
"""

from __future__ import annotations

from com_bridge.protocol import (CODE_TO_SIDE, SIDE_CODE_BOTTOM, SIDE_CODE_LEFT,
                                 SIDE_CODE_NONE, SIDE_CODE_RIGHT, SIDE_CODE_TOP,
                                 SIDE_TO_CODE)
from com_bridge.transport import heartbeat_payload, parse_heartbeat


class TestSideEncoding:
    def test_all_sides_round_trip(self):
        """四个方位都必须能编码进心跳并原样解出。"""
        for side in ("right", "left", "top", "bottom"):
            payload = heartbeat_payload(side)
            assert len(payload) == 4, "心跳必须是 4 字节(与 19 字节输入事件区分)"
            assert payload[0] == 5, "首字节必须是 EV_HEARTBEAT"
            assert parse_heartbeat(payload) == side

    def test_no_side_is_compatible(self):
        """不带方位时必须解出 None —— 旧版主机不下发时副机退回本机配置。"""
        payload = heartbeat_payload(None)
        assert len(payload) == 4 and payload[0] == 5
        assert payload[1] == SIDE_CODE_NONE
        assert parse_heartbeat(payload) is None

    def test_unknown_side_is_none(self):
        """未知方位串不能崩, 要退化成 NONE。"""
        assert parse_heartbeat(heartbeat_payload("diagonal")) is None

    def test_side_codes_are_distinct(self):
        """方位码必须互不相同, 否则解出错误方位。"""
        codes = [SIDE_TO_CODE[s] for s in ("right", "left", "top", "bottom")]
        assert len(set(codes)) == 4
        assert SIDE_CODE_NONE not in codes
        assert CODE_TO_SIDE[SIDE_CODE_RIGHT] == "right"
        assert CODE_TO_SIDE[SIDE_CODE_LEFT] == "left"
        assert CODE_TO_SIDE[SIDE_CODE_TOP] == "top"
        assert CODE_TO_SIDE[SIDE_CODE_BOTTOM] == "bottom"

    def test_heartbeat_shorter_than_input_event(self):
        """心跳必须比输入事件短 —— 两端靠长度区分, 否则会被当输入包解析。"""
        from com_bridge.protocol import HEARTBEAT_SIZE, INPUT_HEADER_SIZE
        assert HEARTBEAT_SIZE < INPUT_HEADER_SIZE
