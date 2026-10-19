"""真实环境验证: 把光标推到边界, 看 Windows 实际把它钳在哪里。

这决定了 EdgeDetector 的边界条件该写什么, 以及 clamp_to_edge 是否会
把光标送到"真正离开屏幕"的位置(那会触发 Windows 自身的跨屏移动,
表现为"弹回")。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.input import (InputInjector, ScreenRect, enumerate_monitors,
                              get_cursor_pos)


def main() -> int:
    mons = enumerate_monitors()
    print(f"显示器 {len(mons)} 台: {mons}")
    m = mons[0]
    print(f"主显示器: {m}  (right={m.right}, bottom={m.bottom})")

    # 1) 用相对移动把光标往右上推, 看它最终停在哪
    print("\n[1] 连续向右推 500 次(相对移动), 观察最终 x:")
    InputInjector.move_absolute(m.left + 100, m.top + 100)
    time.sleep(0.2)
    for _ in range(500):
        InputInjector.move_relative(20, 0)
    time.sleep(0.3)
    x, y = get_cursor_pos()
    print(f"    最终光标: ({x}, {y})")
    print(f"    -> 可达最大 x = {x} (m.right-1 = {m.right - 1})")
    reach_right = x

    print("\n[2] 连续向下推 500 次:")
    InputInjector.move_absolute(m.left + 100, m.top + 100)
    time.sleep(0.2)
    for _ in range(500):
        InputInjector.move_relative(0, 20)
    time.sleep(0.3)
    x, y = get_cursor_pos()
    print(f"    最终光标: ({x}, {y})")
    print(f"    -> 可达最大 y = {y} (m.bottom-1 = {m.bottom - 1})")
    reach_bottom = y

    # 3) 用绝对坐标直接注入右边界值, 看是否被接受
    print("\n[3] 绝对注入到 (right-1, right, right+10), 看实际落点:")
    for target in (m.right - 2, m.right - 1, m.right, m.right + 10):
        InputInjector.move_absolute(target, m.top + 500)
        time.sleep(0.15)
        ax, _ = get_cursor_pos()
        print(f"    注入 x={target:5d} -> 实际 {ax:5d}"
              f"{'  <-- 被钳制' if ax != target else ''}")

    # 4) 结论
    print("\n[结论]")
    print(f"  Windows 把光标钳在 x∈[{m.left}, {reach_right}], "
          f"y∈[{m.top}, {reach_bottom}]")
    exp_r, exp_b = m.right - 1, m.bottom - 1
    if reach_right == exp_r and reach_bottom == exp_b:
        print(f"  与 right-1/bottom-1 ({exp_r}/{exp_b}) 一致 -> 抵边判定正确")
    else:
        print(f"  !! 与 right-1/bottom-1 ({exp_r}/{exp_b}) 不一致 "
              f"-> 抵边判定会用错坐标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
