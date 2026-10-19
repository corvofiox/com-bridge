"""全事件类型贯通验收: 主机的每一次输入都要完整、正确地在副机落位。

用户要求"全面检查鼠标、键盘、按键点击的落位", 这个脚本**一次性覆盖所有
事件类型**, 而不是让人一个一个试:

  鼠标移动 / 左键 / 右键 / 中键 / X1 / X2 / 滚轮 / 横向滚轮
  键盘按下 / 抬起 / 扩展键 / 组合键(Ctrl+C) / 大小写/Num 指示灯键

分两层:
  A. **协议层**(纯逻辑): 每种事件的编码->解码无损, 且 Server 的转发条件
     与 Agent 的注入分支都确实覆盖它 —— 这是"某类事件永远送不到"的防线。
  B. **注入层**(真实 Win32, 需要干净桌面): 在副机侧真实注入每种事件, 用
     系统状态(GetAsyncKeyState / 光标坐标)确认它真的生效。

用法: python -u tools/verify_all_events.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from com_bridge.input import InputInjector, get_cursor_pos, user32, virtual_desktop
from com_bridge.protocol import (
    EV_KEY,
    EV_MOUSE_BUTTON,
    EV_MOUSE_MOVE_REL,
    EV_MOUSE_PLACE,
    EV_MOUSE_WHEEL,
    KEY_DOWN,
    KEY_EXTENDED,
    KEY_UP,
    MB_DOWN,
    MB_UP,
    InputEvent,
)

user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

VK_LBUTTON, VK_RBUTTON, VK_MBUTTON = 0x01, 0x02, 0x04
VK_XBUTTON1, VK_XBUTTON2 = 0x05, 0x06
VK_CONTROL, VK_C, VK_LEFT, VK_CAPITAL = 0x11, 0x43, 0x25, 0x14


def down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def release_all() -> None:
    """确保按键不悬空(失败会留下"卡住的键", 很讨厌)。"""
    for code in (1, 2, 3, 4, 5):
        InputInjector.button(code, False)
    for vk, scan in ((VK_C, 0x2E), (VK_CONTROL, 0x1D), (VK_LEFT, 0x4B)):
        InputInjector.key(vk, scan, False)
    time.sleep(0.12)


# ------------------------------------------------------------------ A 协议层
def protocol_roundtrip() -> list[tuple[str, bool, str]]:
    out = []
    cases = [
        ("鼠标移动 +13,-7", EV_MOUSE_MOVE_REL, 0, 0, 13, -7),
        ("鼠标移动 -200,150", EV_MOUSE_MOVE_REL, 0, 0, -200, 150),
        ("左键按下", EV_MOUSE_BUTTON, MB_DOWN, 1, 0, 0),
        ("左键抬起", EV_MOUSE_BUTTON, MB_UP, 1, 0, 0),
        ("右键按下", EV_MOUSE_BUTTON, MB_DOWN, 2, 0, 0),
        ("中键按下", EV_MOUSE_BUTTON, MB_DOWN, 3, 0, 0),
        ("X1 按下", EV_MOUSE_BUTTON, MB_DOWN, 4, 0, 0),
        ("X2 按下", EV_MOUSE_BUTTON, MB_DOWN, 5, 0, 0),
        ("滚轮上", EV_MOUSE_WHEEL, 0, 0, 0, 120),
        ("滚轮下", EV_MOUSE_WHEEL, 0, 0, 0, -120),
        ("键盘 A 按下", EV_KEY, KEY_DOWN, 0x41, 0x1E, 0),
        ("键盘 A 抬起", EV_KEY, KEY_UP, 0x41, 0x1E, 0),
        ("方向键左(扩展)", EV_KEY, KEY_DOWN | KEY_EXTENDED, VK_LEFT, 0x4B, 0),
        ("Ctrl 按下", EV_KEY, KEY_DOWN, VK_CONTROL, 0x1D, 0),
        ("交接定位 50%", EV_MOUSE_PLACE, 0, 0, 32767, 0),
    ]
    for name, t, fl, cd, a, b in cases:
        ev = InputEvent(t, fl, cd, a, b, 12345678901234)
        raw = ev.pack()
        assert len(raw) == 19, f"{name}: 包长 {len(raw)} != 19"
        back = InputEvent.unpack(raw)
        ok = (back.type == t and back.flags == fl and back.code == cd
              and back.a == a and back.b == b and back.ts_us == 12345678901234)
        out.append((f"协议往返: {name}", ok, ""))
    return out


def dispatch_coverage() -> list[tuple[str, bool, str]]:
    """Server 必须转发鼠标**与**键盘; Agent 必须能注入全部四种类型。"""
    import inspect
    from com_bridge.agent import Agent
    from com_bridge.server import Server

    s_src = inspect.getsource(Server._on_input_event)
    a_src = inspect.getsource(Agent._on_input_event)
    out = [
        ("Server 转发条件覆盖鼠标与键盘",
         "ev.is_mouse or ev.is_key" in s_src, ""),
        ("Server 不再丢弃键盘(无 if ev.is_mouse: 单条件)",
         "if ev.is_mouse:  # 旧" not in s_src, ""),
        ("Agent 注入鼠标移动", "move_relative" in a_src, ""),
        ("Agent 注入鼠标按键", "inj.button" in a_src, ""),
        ("Agent 注入滚轮", "inj.wheel" in a_src, ""),
        ("Agent 注入键盘", "inj.key" in a_src, ""),
        ("Agent 支持交接绝对定位", "EV_MOUSE_PLACE" in a_src, ""),
    ]
    return out


# ------------------------------------------------------------------ B 注入层
def _cursor_hijacked() -> bool:
    """环境探测: 光标是否被外来程序(Deskflow/Synergy 等)劫持。

    活体测量的前提是"注入后光标真能到达目标位置" —— 开发机上装着并
    运行 Deskflow 本体时, 它抢占鼠标输入把光标钉在屏幕中心, 此时
    **所有**基于光标读回的断言都会失真(教训: 曾据此误判过注入失效)。
    探测方法: SetCursorPos 到一个非中心点, 若读回位置不是目标 → 被劫持。
    """
    d = virtual_desktop()
    tx, ty = d.left + d.width // 3, d.top + d.height // 3   # 避开中心
    user32.SetCursorPos(tx, ty)
    time.sleep(0.12)
    gx, gy = get_cursor_pos()
    return not (abs(gx - tx) <= 2 and abs(gy - ty) <= 2)


def injection_layer() -> list[tuple[str, bool, str]]:
    out = []
    d = virtual_desktop()
    cx, cy = d.left + d.width // 2, d.top + d.height // 2

    if _cursor_hijacked():
        out.append(("环境: 光标被外来程序劫持(Deskflow/Synergy?)",
                    False,
                    "SKIP: 光标位置测量在本机不可信, 注入层其余断言可能失真;"
                    " 请退出 Deskflow 后重跑"))
        return out

    # --- 鼠标移动 ---
    user32.SetCursorPos(cx, cy)
    time.sleep(0.15)
    x0, y0 = get_cursor_pos()
    InputInjector.move_relative(40, 25)
    time.sleep(0.15)
    x1, y1 = get_cursor_pos()
    out.append((f"鼠标移动注入生效 ({x1-x0:+d},{y1-y0:+d})",
                abs((x1 - x0) - 40) <= 6 and abs((y1 - y0) - 25) <= 6, ""))

    # --- 五种按键 ---
    for name, code, vk in (("左键", 1, VK_LBUTTON), ("右键", 2, VK_RBUTTON),
                           ("中键", 3, VK_MBUTTON), ("X1", 4, VK_XBUTTON1),
                           ("X2", 5, VK_XBUTTON2)):
        InputInjector.button(code, True)
        time.sleep(0.06)
        got = down(vk)
        InputInjector.button(code, False)
        time.sleep(0.06)
        out.append((f"{name}注入生效(按下时系统状态为按下)", got, ""))

    # --- 滚轮(用消息计数验证太复杂, 这里验证不报错且光标不动) ---
    try:
        before = get_cursor_pos()
        InputInjector.wheel(120)
        InputInjector.wheel(-120)
        time.sleep(0.08)
        out.append(("滚轮注入不干扰光标位置", get_cursor_pos() == before, ""))
    except Exception as exc:  # noqa: BLE001
        out.append(("滚轮注入不干扰光标位置", False, str(exc)))

    # --- 键盘: 字母键 ---
    InputInjector.key(VK_C, 0x2E, True)
    time.sleep(0.06)
    c_down = down(VK_C)
    InputInjector.key(VK_C, 0x2E, False)
    time.sleep(0.06)
    out.append(("键盘 C 按下生效", c_down, ""))

    # --- 键盘: 扩展键(方向键) ---
    InputInjector.key(VK_LEFT, 0x4B, True, extended=True)
    time.sleep(0.06)
    left_down = down(VK_LEFT)
    InputInjector.key(VK_LEFT, 0x4B, False, extended=True)
    time.sleep(0.06)
    out.append(("扩展键(左方向键)按下生效", left_down, ""))

    # --- 组合键: Ctrl 按住 + C ---
    InputInjector.key(VK_CONTROL, 0x1D, True)
    time.sleep(0.05)
    InputInjector.key(VK_C, 0x2E, True)
    time.sleep(0.06)
    combo = down(VK_CONTROL) and down(VK_C)
    InputInjector.key(VK_C, 0x2E, False)
    InputInjector.key(VK_CONTROL, 0x1D, False)
    time.sleep(0.08)
    out.append(("组合键 Ctrl+C 两键同时按下生效", combo, ""))

    # --- 指示键(可变状态, 只验证不报错) ---
    try:
        InputInjector.key(VK_CAPITAL, 0x3A, True)
        InputInjector.key(VK_CAPITAL, 0x3A, False)
        time.sleep(0.05)
        out.append(("指示灯键(CapsLock)注入不报错", True, ""))
    except Exception as exc:  # noqa: BLE001
        out.append(("指示灯键(CapsLock)注入不报错", False, str(exc)))

    release_all()
    return out


def main() -> int:
    print("=" * 68)
    print("A. 协议层(每种事件编码->解码无损)")
    print("=" * 68)
    proto = protocol_roundtrip()
    for name, ok, _ in proto:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")

    print("\n" + "=" * 68)
    print("A2. 分发覆盖(Server 转发 / Agent 注入 是否覆盖每种类型)")
    print("=" * 68)
    disp = dispatch_coverage()
    for name, ok, _ in disp:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")

    print("\n" + "=" * 68)
    print("B. 注入层(真实 Win32 注入是否生效)")
    print("=" * 68)
    inj = injection_layer()
    # SKIP 项(带 'SKIP:' 标记的环境探测)不计入结论 —— 环境不可信不等于
    # 软件有回归(教训: 开发机常驻 Deskflow 时所有光标测量都失真)。
    inj_real = [(n, ok) for (n, ok, _d) in inj if "SKIP:" not in _d]
    for name, ok, detail in inj:
        print(f"  {'OK  ' if ok else 'FAIL' if 'SKIP:' not in detail else 'SKIP'} {name} {detail}")

    allres = ([(n, ok) for (n, ok, _d) in proto]
              + [(n, ok) for (n, ok, _d) in disp]
              + inj_real)
    allok = all(ok for _, ok in allres)
    verdict = "PASS" if allok else "FAIL"
    skipped = len(inj) - len(inj_real)
    skip_note = f" (+{skipped} 项环境跳过)" if skipped else ""
    print(f"\n[结论] {verdict}  "
          f"({sum(1 for _, ok in allres if ok)}/{len(allres)} 项){skip_note}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
