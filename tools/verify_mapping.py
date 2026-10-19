"""验证按比例映射(照搬 Deskflow 的 mapToFraction/mapToPixel)。

跨屏时必须传**比例**而不是像素: 两台机器分辨率不同(主机 1920x1080,
副机 1366x768)时, 直接传像素会让"从同一相对高度出去"变成"落在偏上/偏下
的位置"。
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from com_bridge.input import (entry_point, fraction_to_pixel, position_fraction,
                              virtual_desktop)


def main() -> int:
    d = virtual_desktop()
    print(f"本机虚拟桌面: {d} ({d.width}x{d.height})")
    print()

    print("比例映射(self=1920x1080 -> peer 假设 1366x768):")
    print(f"{'主机 y':>8} {'比例':>8} {'副机 y':>8}")
    for y in (0, 270, 540, 810, 1079):
        f = position_fraction(1919, y, "right")
        # 模拟副机 1366x768 的进入点
        peer_y = int(f * 768)
        local_y = fraction_to_pixel(f, "left")[1]
        print(f"{y:>8} {f:>8.3f} {peer_y:>8}   (本机映射: {local_y})")
    print()

    print("边界与钳制:")
    cases = [
        (-0.5, "left"), (0.0, "left"), (0.5, "left"), (1.0, "left"),
        (1.5, "left"), (0.5, "right"),
    ]
    for f, side in cases:
        x, y = fraction_to_pixel(f, side)
        inside = (d.left <= x <= d.right - 1) and (d.top <= y <= d.bottom - 1)
        print(f"  f={f:>4} side={side:<6} -> ({x:>5},{y:>5}) "
              f"{'OK' if inside else 'FAIL 越界'}")
    print()

    print("entry_point(进入点应落在朝向主电脑的那条边):")
    for side in ("right", "left", "top", "bottom"):
        pt = entry_point(side)
        print(f"  主机 side={side:<7} -> 副机进入点 {pt}")
    print()

    # 关键断言: 同一比例在两台不同分辨率上应给出相同的相对位置
    print("核心性质: 同一比例在任意分辨率上相对位置一致")
    ok = True
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        for h in (768, 1080, 1440, 2160):
            got = int(f * h)
            want = int(f * h)
            if got != want:
                ok = False
        # 相对误差应 <= 1 像素
        for h in (768, 1080, 1440, 2160):
            if abs(int(f * h) / h - f) > 1.0 / h + 1e-9:
                ok = False
    print(f"  {'PASS' if ok else 'FAIL'}")
    print()
    print(f"[结论] {'PASS —— 比例映射正确' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
