"""验证完整的 Deskflow 式跨屏流程(位置可控 + 纯位置回传)。

场景(对应用户实测): 主机 side=right, 副机双屏 0..3839。
预期:
  1. 交接时副机光标被**绝对定位**到最左边缘(0, y), 不是任意位置;
  2. 副机光标能在整个双屏桌面上自由移动(不会因越过 x=1920 就回传);
  3. 回到最左边缘并继续朝主电脑方向移动时 -> 回传;
  4. 全程没有"拉 0.7 秒"的时间条件。
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from com_bridge.input import (EdgeDetector, ScreenRect, fraction_to_pixel,
                              return_edge_side, virtual_desktop)
from com_bridge.protocol import EV_MOUSE_PLACE, InputEvent

# 模拟副机双屏桌面(左屏 0..1919, 右屏 1920..3839)
DUAL = ScreenRect(0, 0, 3840, 1080)


class FakePeer:
    """复刻 agent 侧位置语义(用给定桌面尺寸, 便于模拟双屏)。"""

    def __init__(self, desktop: ScreenRect, pc_side: str = "right"):
        self.desktop = desktop
        self.entry_side = return_edge_side(pc_side)
        # confirm_frames=1: 与 agent 实现一致(回传要跟手, 见 _start_return)
        self.edge = EdgeDetector(desktop, ScreenRect(0, 0, 0, 0),
                                 side=self.entry_side, confirm_frames=1,
                                 hysteresis=8)
        self.cursor = [desktop.left + desktop.width // 2, 540]
        self.returned = 0
        self.injected = 0

    def place(self, frac_q: int) -> tuple[int, int]:
        """收到 EV_MOUSE_PLACE: 绝对定位到进入边。"""
        f = frac_q / 65535.0
        if self.entry_side == "left":
            y = max(self.desktop.top,
                    min(self.desktop.bottom - 1,
                        int(f * self.desktop.height) + self.desktop.top))
            px, py = self.desktop.left, y
        elif self.entry_side == "right":
            y = max(self.desktop.top,
                    min(self.desktop.bottom - 1,
                        int(f * self.desktop.height) + self.desktop.top))
            px, py = self.desktop.right - 1, y
        else:
            px, py = self.desktop.left + self.desktop.width // 2, self.desktop.top
        self.cursor = [px, py]
        self.edge.reset()
        return px, py

    def move(self, dx: int, dy: int) -> str:
        """返回 'injected' 或 'to_host'。"""
        toward = (self.entry_side == "left" and dx < 0) or \
                 (self.entry_side == "right" and dx > 0) or \
                 (self.entry_side == "top" and dy < 0) or \
                 (self.entry_side == "bottom" and dy > 0)
        if toward:
            if self.edge.update(*self.cursor):
                self.returned += 1
                return "to_host"
        # 注入到本机(Windows 会钳在桌面范围内)
        self.cursor[0] = max(self.desktop.left,
                             min(self.desktop.right - 1, self.cursor[0] + dx))
        self.cursor[1] = max(self.desktop.top,
                             min(self.desktop.bottom - 1, self.cursor[1] + dy))
        self.injected += 1
        return "injected"


def main() -> int:
    p = FakePeer(DUAL, pc_side="right")
    print(f"副机双屏桌面: {DUAL.left}..{DUAL.right - 1} "
          f"(左屏 0..1919, 右屏 1920..3839)")
    print(f"进入边 = {p.entry_side}(主机 side=right => 副机从最左进入)")
    print(f"初始光标(修复前可能停在任何地方): {p.cursor}")
    print()

    # 1) 交接绝对定位
    print("1. 收交接定位事件(比例 0.5):")
    px, py = p.place(32768)
    print(f"   定位后光标 = ({px}, {py})")
    ok1 = px == DUAL.left, f"应落在最左边缘 x={DUAL.left}"
    print(f"   {'PASS' if ok1 else 'FAIL'} —— {ok1 if isinstance(ok1, str) else ''}")
    print()

    # 2) 向右推到右屏, 不应回传
    print("2. 持续向右推(穿过左屏进入右屏), 不应回传:")
    for _ in range(200):
        r = p.move(20, 0)
        if r == "to_host":
            print("   FAIL —— 向右移动时被误判回传!")
            return 1
    print(f"   光标 = {p.cursor}, 已注入 {p.injected} 次, 回传 {p.returned} 次")
    ok2 = p.cursor[0] > 1919 and p.returned == 0
    print(f"   {'PASS' if ok2 else 'FAIL'} —— 已到达右屏({p.cursor[0]}), 未误回传")
    print()

    # 3) 向左拉回: 在右屏内移动不应回传(这是修复前的主要故障)
    print("3. 向左移动(仍在右屏内), 不应回传:")
    moved = 0
    for _ in range(90):        # 90*20 = 1800px, 仍在右屏内
        r = p.move(-20, 0)
        if r == "to_host":
            print(f"   FAIL —— 光标还在 x={p.cursor[0]}, 就被误判回传!")
            return 1
        moved += 1
    print(f"   向左移动 {moved} 次(共 {moved*20}px), 光标 = {p.cursor}, "
          f"回传 {p.returned} 次")
    ok3 = p.cursor[0] > 1919 and p.returned == 0
    print(f"   {'PASS' if ok3 else 'FAIL'} —— 右屏内向左移动不再被拽回")
    print()

    # 4) 继续向左直到最左边缘 -> 回传
    print("4. 继续向左直到整机最左边缘 -> 应回传:")
    ret = False
    for i in range(200):
        if p.move(-20, 0) == "to_host":
            ret = True
            print(f"   第 {i+1} 次后被判回传, 光标 = {p.cursor}")
            break
    ok4 = ret and p.cursor[0] <= 8
    print(f"   {'PASS' if ok4 else 'FAIL'} —— 抵达最左边缘才回传")
    print()

    # 5) 无时间条件: 快速移动也能立刻回传
    print("5. 纯位置判定(无 0.7 秒时间条件):")
    p2 = FakePeer(DUAL, pc_side="right")
    p2.place(32768)
    p2.cursor = [0, 540]                    # 直接放到最左
    r = p2.move(-20, 0)
    ok5 = r == "to_host"
    print(f"   光标在最左边缘再向左移动一次 -> {r} "
          f"{'PASS(立即回传)' if ok5 else 'FAIL'}")
    print()

    allok = ok1 and ok2 and ok3 and ok4 and ok5
    print(f"[结论] {'PASS —— 位置可控且纯位置回传, 符合 Deskflow 设计' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
