"""复刻 server 与 agent 两端的完整输入状态机, 找出"疯狂弹回"。

不复用真实 SendInput/UDP, 只把两端**逐字**照抄的判定逻辑串起来跑,
喂入一段"用户把鼠标向右推出屏幕"的真实事件序列, 观察光标轨迹。
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from com_bridge.input import EdgeDetector, ScreenRect, clamp_to_edge

MON = ScreenRect(0, 0, 1920, 1080)


class ServerSide:
    """逐字复刻 server._on_input_event 的逻辑(含 _moving_back)。"""

    def __init__(self, side: str = "right", jitter: int = 8):
        self.side = side
        self.edge = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side=side,
                                 confirm_frames=2, hysteresis=jitter)
        self.forwarded = 0
        self.cursor = [960, 540]      # 模拟本机光标
        self.log: list[str] = []

    def _moving_back(self, dx: int, dy: int) -> bool:
        if self.side == "right":
            return dx < 0
        if self.side == "left":
            return dx > 0
        if self.side == "top":
            return dy > 0
        return dy < 0

    def feed(self, dx: int, dy: int) -> bool:
        """喂入一个真实鼠标相对位移(Windows 已先作用于光标)。"""
        # Windows: 相对位移作用于光标, 并钳制在屏幕内
        self.cursor[0] = max(MON.left, min(MON.right - 1, self.cursor[0] + dx))
        self.cursor[1] = max(MON.top, min(MON.bottom - 1, self.cursor[1] + dy))
        x, y = self.cursor

        if not self.edge.is_outside:
            if self.edge.update(x, y):
                self.log.append(f"越界触发 @({x},{y})")
                cx, cy = clamp_to_edge(x, y)
                self.cursor = [cx, cy]      # 模拟 clamp 注入
            else:
                return False
        else:
            if self._moving_back(dx, dy):
                if not self.edge.update(x, y):
                    self.log.append(f"回到本机 @({x},{y})")
                    return False
            else:
                cx, cy = clamp_to_edge(x, y)
                self.cursor = [cx, cy]
        self.forwarded += 1
        return True


def main() -> int:
    s = ServerSide("right")
    print("模拟: 用户从屏幕中间持续向右推鼠标(每次 +15px)")
    print(f"{'事件':>5} {'dx':>4} {'光标':>12} {'转发':>5}  说明")
    dx = 15
    for i in range(1, 60):
        before = tuple(s.cursor)
        fwd = s.feed(dx, 0)
        note = ""
        if s.log and len(s.log) > 0:
            note = s.log[-1]
            s.log.clear()
        print(f"{i:>5} {dx:>4} {str(tuple(s.cursor)):>12} "
              f"{'是' if fwd else '否':>5}  {note}")
        if i == 50:
            print("\n--- 用户开始往回拉(dx=-15) ---")
            dx = -15

    print(f"\n总共转发 {s.forwarded} 个事件")
    print(f"最终光标 {tuple(s.cursor)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
