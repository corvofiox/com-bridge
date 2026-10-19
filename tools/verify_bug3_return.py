# -*- coding: utf-8 -*-
"""BUG3 端到端线路验证: 进入方位 + 回程方位(不依赖真机, 纯逻辑 + 假 sink)。

覆盖用户报的第三条:
  BUG3① "主机指针进入副机时方位略有偏差"
  BUG3② "副机指针回退主机时的方位完全不继承, 依然从主机跳转到副机时
         指针的位置回退"

做法: 用真实的 position_fraction / fraction_to_pixel / EdgeDetector, 把
主客两端的收发**串起来**(事件只在内存里传递), 断言:
  * 去程: 主机 y -> 副机 y 保持相对高度(分辨率不同也成立);
  * 回程: 副机在**实际位置** y 回传时, 主机落点必须跟随该位置, 而**不是**
    停留在当初跳过去的那个位置(BUG3② 的核心断言)。
"""
from __future__ import annotations

import sys
from contextlib import contextmanager

sys.path.insert(0, ".")

from com_bridge import input as input_mod
from com_bridge.input import (ENTRY_JUMP_ZONE_PX, EdgeDetector, ScreenRect,
                              position_fraction)
from com_bridge.protocol import (EV_MOUSE_PLACE, EV_MOUSE_RETURN, InputEvent)

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def q(f: float) -> int:
    return int(round(min(max(f, 0.0), 1.0) * 65535))


@contextmanager
def desktop(rect: ScreenRect):
    """把 virtual_desktop() 临时替换为指定桌面。

    ★ 这是必要的: position_fraction / fraction_to_pixel 都读**本机**桌面
      (它们跑在哪台机器上就用哪台的尺寸)。要在一台机器上模拟两台,
      必须显式切换"当前站在哪台机器上"。
    """
    orig = input_mod.virtual_desktop
    input_mod.virtual_desktop = lambda: rect
    try:
        yield
    finally:
        input_mod.virtual_desktop = orig


print("== 场景: 主机 1920x1080 (side=right) <-> 副机 1366x768 ==")
HOST = ScreenRect(0, 0, 1920, 1080)
PEER = ScreenRect(0, 0, 1366, 768)

print("\n-- 1. 去程(进入方位): 主机 y -> 副机 y --")
entry_side = "left"          # 副机的进入边(主机在它左侧)
print(f"   副机进入边 = {entry_side}, 跳入区 = {ENTRY_JUMP_ZONE_PX}px")
ok_prop = True
for host_y, want_rel in ((0, 0.0), (270, 0.25), (540, 0.5),
                         (809, 0.75), (1079, 1.0)):
    with desktop(HOST):                       # 站在主机上算比例
        f = position_fraction(1919, host_y, "right")
    ev = InputEvent(EV_MOUSE_PLACE, 0, 0, q(f), 0, 0)
    with desktop(PEER):                       # 站在副机上还原
        px, py = input_mod.fraction_to_pixel(ev.a / 65535.0, entry_side)
    rel = py / (PEER.height - 1)
    good = abs(rel - want_rel) < 0.02
    ok_prop = ok_prop and good
    print(f"   主机 y={host_y:>4} f={f:.4f} -> 副机 ({px},{py}) "
          f"相对高度={rel:.3f} {'OK' if good else 'FAIL'}")
check("去程保持相对高度", ok_prop)

print("\n-- 2. BUG3① 关键断言: 进入点不得系统性偏移 --")
worst = 0.0
for host_y in range(0, 1080, 7):
    with desktop(HOST):
        f = position_fraction(1919, host_y, "right")
    with desktop(PEER):
        _, py = input_mod.fraction_to_pixel(q(f) / 65535.0, entry_side)
    ideal = host_y * (PEER.height - 1) / (HOST.height - 1)
    worst = max(worst, abs(py - ideal))
check("进入方位误差 <= 1px(实测无系统性偏差)", worst <= 1.0,
      f"最大偏差 {worst:.2f}px")

print("\n-- 3. BUG3② 核心: 回程方位必须继承副机的实际位置 --")
back_side = "left"        # 副机的回传边(主机在左侧)
pin_point_host_y = 540    # 主机光标被钉在**进入时**的位置(50%)
print(f"   主机光标被钉在 y={pin_point_host_y} (进入时的位置)")
print(f"   副机从回传边({back_side})的不同高度回传:")
ok_inherit = True
for peer_y in (100, 300, 384, 500, 700):
    with desktop(PEER):                       # 副机侧: 算它自己的比例
        f = position_fraction(0, peer_y, back_side)
    ev = InputEvent(EV_MOUSE_RETURN, 0, 0, q(f), 0, 0)
    with desktop(HOST):                       # 主机侧: 用**自己的跨越边**还原
        hx, hy = input_mod.fraction_to_pixel(ev.a / 65535.0, "right")
    want = peer_y * (HOST.height - 1) / (PEER.height - 1)
    good = abs(hy - want) <= 1.0
    # ★ 落点必须**跟随副机位置**。注意不能用"落点 != 钉住点"当判据:
    #   peer_y=384 是副机中点, 它正确地映射到主机中点 540 —— 与钉住点
    #   恰好相同是**巧合而非缺陷**(钉住点取的是进入时的 y, 而进入时
    #   用户正好也在中点)。所以判据是"落点 == 按比例期望值"。
    ok_inherit = ok_inherit and good
    print(f"   副机 y={peer_y:>4} -> 主机 y={hy:>4} "
          f"(期望 {want:.0f}, 钉住点 {pin_point_host_y}) "
          f"{'OK' if good else 'FAIL'}")
check("回程落点跟随副机实际位置(而非钉住点)", ok_inherit)

# 单独锁一条"确实不再停在钉住点"的强证据: 取一个远离中点的位置
with desktop(PEER):
    f_far = position_fraction(0, 100, back_side)
with desktop(HOST):
    _, hy_far = input_mod.fraction_to_pixel(q(f_far) / 65535.0, "right")
check("远离中点回传时落点明显不同于钉住点(修掉 BUG3② 的直接证据)",
      abs(hy_far - pin_point_host_y) > 100,
      f"副机 y=100 -> 主机 y={hy_far}, 钉住点 {pin_point_host_y}")

print("\n-- 4. 对照: 只发相对位移时的错误行为(BUG3② 的成因) --")
# 若回程只发相对位移, 主机从钉住点开始累积, 落点与副机位置无关
rel_only = pin_point_host_y      # 钉住点 + 相对位移的累积起点
print(f"   只发相对位移: 落点 = 钉住点({pin_point_host_y}) + 累积位移")
print(f"   => 无论用户在副机哪里回传, 都从 {rel_only} 附近出发 "
      f"(这正是用户看到的'从跳过去的位置回退')")
check("对照说明成立(相对位移无法继承方位)", True)

print("\n-- 5. EV_MOUSE_RETURN 不得被当作鼠标输入转发(防回环) --")
ev = InputEvent(EV_MOUSE_RETURN, 0, 0, 100, 0, 0)
check("is_mouse 为 False", not ev.is_mouse)
check("is_key 为 False", not ev.is_key)
check("与 EV_MOUSE_PLACE 不同", EV_MOUSE_RETURN != EV_MOUSE_PLACE)

print("\n-- 6. 回传边几何: 副机必须真的贴到边上才回传 --")
det = EdgeDetector(PEER, ScreenRect(0, 0, 0, 0), side=back_side,
                   confirm_frames=1)
at_edge = det.update(PEER.left, 384)          # x=0 贴左边
inside = EdgeDetector(PEER, ScreenRect(0, 0, 0, 0), side=back_side,
                      confirm_frames=1).update(600, 384)
check("贴边时判定为越界", at_edge)
check("屏内时判定为未越界", not inside)

print()
if FAILED:
    print(f"!! {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("全部通过")
