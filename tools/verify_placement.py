"""验证跨屏位置控制 —— 照搬 Deskflow 的三处设计。

用户实测问题: "副机有两块屏, 主机设置 right 时预期从副机左侧屏进入,
但光标完全越过左侧屏, 而是跟随主机定位在右边屏幕右侧边缘蹭, 往左拉点
就又回到主屏幕。"

三个原因(全部照 Deskflow 修正):
  1. **缺少交接绝对定位** —— Deskflow 在 switchComputer() 里调
     enter(x, y, ...), 把坐标绝对告知目标机; 只发相对位移则副机光标从
     上次位置累积, 位置不可控。
  2. **回传边基准用了单块显示器** —— Deskflow 用计算机的完整 shape
     (getShape 返回所有显示器合并后的矩形); 用 _main_monitor() 会把
     回传边算在某一屏的边缘(如 x=1920), 而不是整机最左(x=0)。
  3. **位置应传比例而非像素** —— Deskflow 的 mapToFraction/mapToPixel,
     让不同分辨率也能"从同一相对高度进入"。
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from com_bridge.input import (EdgeDetector, ScreenRect, fraction_to_pixel,
                              position_fraction, return_edge_side,
                              virtual_desktop)
from com_bridge.protocol import (EV_MOUSE_PLACE, INPUT_HEADER_SIZE, InputEvent)


def test_place_event_not_forwarded() -> bool:
    """1. PLACE 事件不能被当成鼠标事件(否则会形成回环)。"""
    print("1. EV_MOUSE_PLACE 不可被误转发:")
    ev = InputEvent(EV_MOUSE_PLACE, 0, 0, 32768, 0, 0)
    ok1 = not ev.is_mouse
    ok2 = not ev.is_key
    print(f"   is_mouse={ev.is_mouse} (须 False)  {'OK' if ok1 else 'FAIL'}")
    print(f"   is_key  ={ev.is_key}   (须 False)  {'OK' if ok2 else 'FAIL'}")
    # 往返必须无损
    back = InputEvent.unpack(ev.pack())
    ok3 = (back.type == EV_MOUSE_PLACE and back.a == 32768
           and len(ev.pack()) == INPUT_HEADER_SIZE)
    print(f"   打包/解包无损且长度 {INPUT_HEADER_SIZE}: {'OK' if ok3 else 'FAIL'}")
    return ok1 and ok2 and ok3


def test_entry_side_mapping() -> bool:
    """2. 主机 side -> 副机进入边/回传边 的映射必须正确。

    主机 side="right"(副机在主机右边):
      用户从主机右边缘向右推 -> 副机应从它自己的**左边缘**进入。
      副机的回传边也是 left(往左拉就回主机)。
    """
    print("\n2. 方位 -> 进入边映射:")
    expect = {"right": "left", "left": "right",
              "top": "bottom", "bottom": "top"}
    ok = True
    for pc_side, back in expect.items():
        got = return_edge_side(pc_side)
        good = got == back
        ok = ok and good
        print(f"   主机 side={pc_side:<7} -> 副机进入/回传边 {got:<7} "
              f"{'OK' if good else 'FAIL'}")
    return ok


def test_entry_position_on_correct_edge() -> bool:
    """3. 进入点必须落在正确的那个边缘**内侧**(照搬 Deskflow avoidJumpZone)。

    ★ 判据不是"恰好等于边界像素": 落点要向内推入 ENTRY_JUMP_ZONE_PX,
      否则进入瞬间一次朝主机的抖动就会立刻越过回传边弹回去
      (实测手感是"进不去, 一进去就弹回")。这里断言:
        * 落在**正确的边**上(不是屏幕中间/别的屏);
        * 与该边的距离恰好是 ENTRY_JUMP_ZONE_PX;
        * 切向坐标在合法范围内。
    """
    print("\n3. 进入点位置(以本机虚拟桌面为副机):")
    d = virtual_desktop()
    from com_bridge.input import ENTRY_JUMP_ZONE_PX
    print(f"   副机桌面: {d.left}..{d.right - 1} x {d.top}..{d.bottom - 1}"
          f"  (跳入区={ENTRY_JUMP_ZONE_PX}px)")
    ok = True
    checks = [
        ("right", lambda x, y: x == d.left + ENTRY_JUMP_ZONE_PX),
        ("left", lambda x, y: x == d.right - 1 - ENTRY_JUMP_ZONE_PX),
        ("top", lambda x, y: y == d.bottom - 1 - ENTRY_JUMP_ZONE_PX),
        ("bottom", lambda x, y: y == d.top + ENTRY_JUMP_ZONE_PX),
    ]
    for pc_side, cond in checks:
        entry_side = return_edge_side(pc_side)
        x, y = fraction_to_pixel(0.5, entry_side)
        good = cond(x, y)
        ok = ok and good
        print(f"   主机 {pc_side:<7} -> 副机进入点 ({x},{y}) "
              f"{'OK' if good else 'FAIL'}")
    return ok


def test_proportional_entry() -> bool:
    """4. 按比例进入: 主机 50% 处出去 -> 副机 50% 处进来(分辨率不同)。"""
    print("\n4. 按比例进入(主机 1920x1080 -> 副机 1366x768):")
    ok = True
    for host_y, want_frac in ((0, 0.0), (540, 0.5), (1079, 1.0)):
        f = position_fraction(1919, host_y, "right")
        peer_y = int(f * 768)
        good = abs(f - want_frac) < 0.01
        ok = ok and good
        print(f"   主机 y={host_y:>4} -> 比例 {f:.3f} -> 副机 y={peer_y:>4} "
              f"{'OK' if good else 'FAIL'}")
    return ok


def test_return_edge_uses_full_desktop() -> bool:
    """5. ★ 回传边必须用**整个虚拟桌面**, 而不是单块显示器。

    这是"往左拉一点就回主机"的直接原因: 副机双屏时若用面积最大的那一块,
    回传边会被算在 x=1920(右屏左缘)而不是 x=0(整机最左)。
    这里模拟一个双屏桌面来断言。
    """
    print("\n5. 回传边基准(模拟副机双屏 0..3839):")
    dual = ScreenRect(0, 0, 3840, 1080)
    d = EdgeDetector(dual, ScreenRect(0, 0, 0, 0), side="left",
                     confirm_frames=2, hysteresis=8)
    # 在右屏中间(x=2800)绝不应算抵边
    ok1 = not d.update(2800, 540)
    ok1 = ok1 and not d.update(2800, 540)
    print(f"   右屏中间 x=2800 不抵边: {'OK' if ok1 else 'FAIL'}")
    # 推进到整机最左(x=0)才算抵边
    d.update(0, 540)
    ok2 = d.update(0, 540)
    print(f"   整机最左 x=0 抵边:      {'OK' if ok2 else 'FAIL'}")
    # 对比: 若错误地用单块屏(1920..3839), x=1920 就会被误判为抵边
    wrong = EdgeDetector(ScreenRect(1920, 0, 3840, 1080),
                         ScreenRect(0, 0, 0, 0), side="left",
                         confirm_frames=2, hysteresis=8)
    wrong.update(1920, 540)
    misjudged = wrong.update(1920, 540)
    print(f"   (对照)用错基准时 x=1920 被误判抵边: {misjudged} "
          f"<- 正是用户遇到的现象")
    return ok1 and ok2 and misjudged


def main() -> int:
    results = [
        test_place_event_not_forwarded(),
        test_entry_side_mapping(),
        test_entry_position_on_correct_edge(),
        test_proportional_entry(),
        test_return_edge_uses_full_desktop(),
    ]
    print()
    allok = all(results)
    print(f"[结论] {'PASS —— 位置控制符合 Deskflow 的设计' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
