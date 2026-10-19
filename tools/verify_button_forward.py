"""点击/滚轮**转发链路**验收(基于实测原始字节, 不依赖物理设备)。

背景: 用户症状"进入副机后点击没作用到副机"。已定位两个根因并修复:
  A. `RAWMOUSE` 布局少了 2 字节填充 -> usButtonFlags 永远读成 0
     -> `_emit_mouse` 里所有 `if bf & RI_MOUSE_*` 永不成立 -> **点击从未
        被产出过**。(字节级证据见 tests/test_rawmouse_layout.py)
  B. `button()/wheel()/key()` 注入时**没打 TAG** -> 主机钩子只放行带 TAG
     的事件, 于是自己注入的点击被自己**吞掉**; 监听器也认不出。

本脚本验证**事件产出层**: 把实测捕获的 RAWMOUSE 字节喂给生产代码
`RawInputListener._emit_mouse()`, 断言它产出的 InputEvent 正确。

⚠ 为什么不用 SendInput 造"物理点击"来测:
  SendInput 产生的事件 hDevice 恒为 NULL, 会被监听器正确判为"合成"而丢弃。
  软件**无法**伪造真正的物理事件 —— 因此这一层只能用实测字节离线验证;
  真实物理链路由 tests/test_rawmouse_layout.py + 用户双机实测覆盖。

用法: python -u tools/verify_button_forward.py
"""
from __future__ import annotations

import ctypes
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from com_bridge.input import (
    INPUT,
    RAWMOUSE,
    RawInputListener,
)
from com_bridge.protocol import (
    EV_KEY,
    EV_MOUSE_BUTTON,
    EV_MOUSE_MOVE_REL,
    EV_MOUSE_WHEEL,
    KEY_DOWN,
    MB_DOWN,
    MB_UP,
)
from com_bridge.input import InputInjector

# --- 实测捕获的原始字节(24 字节 RAWMOUSE) -------------------------------
CAPTURED = {
    "位移 (11,7)": "0000000000000000000000000b0000000700000000000000",
    "左键按下": "000000000100000000000000000000000000000000000000",
    "左键抬起": "000000000200000000000000000000000000000000000000",
    "右键按下": "000000000400000000000000000000000000000000000000",
    "中键按下": "000000001000000000000000000000000000000000000000",
    "X1 按下": "000000004000000000000000000000000000000000000000",
    "X2 按下": "000000000001000000000000000000000000000000000000",
    "滚轮上": "000000000004780000000000000000000000000000000000",
    "滚轮下": "00000000000488ff00000000000000000000000000000000",
}


def emit(raw_hex: str) -> list:
    """把原始字节喂给生产代码的 _emit_mouse, 收集它产出的事件。"""
    out = []
    lst = RawInputListener(on_event=out.append)
    rm = RAWMOUSE.from_buffer_copy(bytes.fromhex(raw_hex))
    lst._emit_mouse(rm)
    return out


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    # ---------- 1) 每种事件都能被产出 ----------
    print("=" * 68)
    print("1) 产出层: 实测字节 -> InputEvent (生产代码 _emit_mouse)")
    print("=" * 68)
    expect = {
        "位移 (11,7)": (EV_MOUSE_MOVE_REL, None),
        "左键按下": (EV_MOUSE_BUTTON, (MB_DOWN, 1)),
        "左键抬起": (EV_MOUSE_BUTTON, (MB_UP, 1)),
        "右键按下": (EV_MOUSE_BUTTON, (MB_DOWN, 2)),
        "中键按下": (EV_MOUSE_BUTTON, (MB_DOWN, 3)),
        "X1 按下": (EV_MOUSE_BUTTON, (MB_DOWN, 4)),
        "X2 按下": (EV_MOUSE_BUTTON, (MB_DOWN, 5)),
        "滚轮上": (EV_MOUSE_WHEEL, None),
        "滚轮下": (EV_MOUSE_WHEEL, None),
    }
    for name, hexs in CAPTURED.items():
        got = emit(hexs)
        etype, extra = expect[name]
        if not got:
            ok = False
            detail = "!! 没有产出任何事件(正是用户症状: 点击无效)"
        else:
            primary = [e for e in got if e.type == etype]
            if name == "位移 (11,7)":
                ok = bool(primary) and primary[0].a == 11 and primary[0].b == 7
                detail = f"{[(e.type, e.a, e.b) for e in got]}"
            elif etype == EV_MOUSE_BUTTON:
                ok = bool(primary) and primary[0].code == extra[1] \
                    and (primary[0].flags & extra[0])
                detail = f"code={primary[0].code} " \
                    f"flags=0x{primary[0].flags:x} (期望 code={extra[1]} " \
                    f"flags含0x{extra[0]:x})" if primary else "无按键事件"
            else:
                d = primary[0].b if primary else 0
                exp = 120 if name == "滚轮上" else -120
                ok = bool(primary) and d == exp
                detail = f"滚轮增量={d} (期望 {exp})"
        results.append((f"产出: {name}", ok, detail))
        print(f"  {'OK  ' if ok else 'FAIL'} {name:12s} {detail}")

    # ---------- 2) 所有注入都带 TAG ----------
    print("\n" + "=" * 68)
    print("2) 注入层: 每种注入都必须带 TAG(否则被主机钩子吞掉)")
    print("=" * 68)
    captured: list[tuple[str, int]] = []
    orig = InputInjector._send

    def spy(*inputs):
        for i in inputs:
            if i.type == 0:
                captured.append(("鼠标", i.union.mi.dwExtraInfo or 0))
            else:
                captured.append(("键盘", i.union.ki.dwExtraInfo or 0))
        return orig(*inputs)

    InputInjector._send = staticmethod(spy)
    try:
        InputInjector.button(1, True)
        InputInjector.button(1, False)
        InputInjector.wheel(120)
        InputInjector.move_relative(3, 4)
        InputInjector.key(0x41, 0x1E, True)
    finally:
        InputInjector._send = staticmethod(orig)

    tag = InputInjector.TAG
    for kind, extra in captured:
        ok = extra == tag
        results.append((f"注入带 TAG: {kind} (0x{extra:08x})", ok,
                        "" if ok else "!! 漏打标记 -> 会被主机钩子吞掉"))

    # ---------- 3) 键盘的 TAG 也能被识别 ----------
    print("\n" + "=" * 68)
    print("3) 键盘结构体布局")
    print("=" * 68)
    from com_bridge.input import RAWKEYBOARD
    kbd_ok = (RAWKEYBOARD.ExtraInformation.offset == 12
              and ctypes.sizeof(RAWKEYBOARD) == 16)
    results.append(("RAWKEYBOARD 布局正确(ExtraInformation@12, size=16)",
                    kbd_ok, ""))

    print("\n" + "=" * 68)
    allok = all(ok for _, ok, _ in results)
    npass = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL {name} {detail}")
    print(f"  通过 {npass}/{len(results)}")
    print(f"[结论] {'PASS —— 点击/滚轮/键盘均能产出并带标记转发'
                      if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
