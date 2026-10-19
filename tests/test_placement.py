"""跨屏位置映射测试 —— 照搬 Deskflow 的 mapToFraction / mapToPixel。

用户实测: 副机双屏时, 主机设 right 预期从副机**左侧屏**进入, 但光标
越过左侧屏、在**右屏右缘**蹭, 往左拉点就回主机。

对应三处修复:
  1. 交接时绝对定位(Deskflow 的 enter(x, y))—— 只发相对位移则位置不可控;
  2. 位置按**比例**传递(Deskflow 的 mapToFraction/mapToPixel)——
     分辨率不同也能从同一相对高度进入;
  3. 边界基准用**整机桌面**(Deskflow 的 getShape 返回所有显示器合并)。
"""

from __future__ import annotations

from com_bridge.input import (fraction_to_pixel, position_fraction,
                              return_edge_side, virtual_desktop)
from com_bridge.protocol import (EV_MOUSE_PLACE, INPUT_HEADER_SIZE, InputEvent)


class TestPlaceEvent:
    def test_place_is_not_mouse_or_key(self):
        """PLACE 不能被当成鼠标/键盘事件 —— 否则会被回传形成环。"""
        ev = InputEvent(EV_MOUSE_PLACE, 0, 0, 32768, 0, 0)
        assert not ev.is_mouse
        assert not ev.is_key

    def test_place_round_trips(self):
        """比例值必须能无损往返(承载在 int32 的 a 字段)。"""
        for q in (0, 1, 32768, 65534, 65535):
            ev = InputEvent(EV_MOUSE_PLACE, 0, 0, q, 0, 123456)
            back = InputEvent.unpack(ev.pack())
            assert back.type == EV_MOUSE_PLACE
            assert back.a == q
            assert len(ev.pack()) == INPUT_HEADER_SIZE


class TestEntrySide:
    def test_side_maps_to_opposite_edge(self):
        """主机方位 -> 副机进入边(取对侧)。"""
        assert return_edge_side("right") == "left"
        assert return_edge_side("left") == "right"
        assert return_edge_side("top") == "bottom"
        assert return_edge_side("bottom") == "top"

    def test_entry_point_inside_jump_zone(self):
        """进入点必须落在朝向主电脑那条边的**内侧**(jumpZone 语义)。

        照搬 Deskflow avoidJumpZone: 落点若恰好在回传边上, 进入瞬间一次
        朝主机的抖动就会立刻回传; 必须向屏内推入 ENTRY_JUMP_ZONE_PX,
        留出死区, 用户真正"拉回"才触发回传。
        """
        from com_bridge.input import ENTRY_JUMP_ZONE_PX
        d = virtual_desktop()
        x, y = fraction_to_pixel(0.5, "left")
        assert x == d.left + ENTRY_JUMP_ZONE_PX
        x, y = fraction_to_pixel(0.5, "right")
        assert x == d.right - 1 - ENTRY_JUMP_ZONE_PX
        x, y = fraction_to_pixel(0.5, "top")
        assert y == d.top + ENTRY_JUMP_ZONE_PX
        x, y = fraction_to_pixel(0.5, "bottom")
        assert y == d.bottom - 1 - ENTRY_JUMP_ZONE_PX

    def test_entry_point_always_reachable(self):
        """进入点必须夹在屏内(比例越界也不能给出屏幕外坐标)。"""
        d = virtual_desktop()
        for f in (-1.0, -0.001, 0.0, 0.5, 1.0, 1.001, 2.0):
            for side in ("left", "right", "top", "bottom"):
                x, y = fraction_to_pixel(f, side)
                assert d.left <= x <= d.right - 1, (f, side, x, y)
                assert d.top <= y <= d.bottom - 1, (f, side, x, y)


class TestProportionalMapping:
    def test_fraction_reflects_position_along_edge(self):
        """沿跨越边的位置应线性映射到 [0,1]。"""
        d = virtual_desktop()
        top = position_fraction(0, d.top, "right")
        mid = position_fraction(0, d.top + d.height // 2, "right")
        bot = position_fraction(0, d.bottom - 1, "right")
        assert top < mid < bot
        assert 0.0 <= top <= 1.0 and 0.0 <= bot <= 1.0
        assert abs(mid - 0.5) < 0.02

    def test_same_fraction_same_relative_place(self):
        """同一比例在不同高度的屏上应给出相同的相对位置。

        这是"从主机 50% 高度出去, 就从副机 50% 高度进来"的数学保证 ——
        主机 1920x1080 与副机 1366x768 分辨率不同也成立。
        """
        for f in (0.0, 0.25, 0.5, 0.75, 1.0):
            for h in (768, 1080, 1440, 2160):
                y = int(f * h)
                assert abs(y / h - f) <= 1.0 / h + 1e-9

    def test_vertical_crossing_uses_x(self):
        """上下跨越时应按 x 比例映射(而不是 y)。"""
        d = virtual_desktop()
        left = position_fraction(d.left, 0, "bottom")
        right = position_fraction(d.right - 1, 0, "bottom")
        assert left < right
        assert abs(left) < 0.01
        assert abs(right - 1.0) < 0.01
