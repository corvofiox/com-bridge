"""钉住注入验收: 公式幂等(纯数学) + 活体漂移对比(环境感知)。

用户实测症状: "主机的鼠标会沿着边缘一路向上移动, 最终落在屏幕的右上角"。

两个成因(都已修复):
  (a) 归一化公式除以 width 而非 width-1 且截断 —— 每次注入欠一点;
  (b) 钉住传的是**读回来的当前坐标** —— 切向(垂直方向)零约束, 用户顶墙
      猛推时手腕自然上旋, 光标就顺着边缘一路爬到角落。

★ 活体部分必须**先确认环境可用**:
  开发机上若跑着别的 KVM(实测: Deskflow 本体在跑, 它有窗口类
  DeskflowDesk), 它会在屏幕边缘抢占光标并把它移到自己算的中心
  (Deskflow 的 deskLeave 调 deskMouseMove(m_xCenter, m_yCenter)) ——
  于是所有"推到边缘再读回坐标"的测量全部失真
  (实测 SetCursorPos(1919,540) 之后读到 (960,540))。
  这种情况下本脚本报 SKIP 而不是 FAIL: 失败的是测量环境, 不是我们的代码。

用法: python tools/verify_pin.py
"""

from __future__ import annotations

import ctypes
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from com_bridge.input import (
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_VIRTUALDESK,
    InputInjector,
    get_cursor_pos,
    user32,
    virtual_desktop,
)

user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]


def deskflow_norm(v: int, origin: int, span: int) -> int:
    return int(65535.0 * (v - origin) / max(1, span - 1) + 0.5)


def old_norm(v: int, origin: int, span: int) -> int:
    return int((v - origin) * 65535 / max(1, span))


def env_polluted() -> tuple[bool, str]:
    """检测是否有外部 KVM/覆盖层在抢占光标(会毁掉边缘测量)。"""
    import subprocess
    reasons = []
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10).stdout.lower()
        for name in ("deskflow", "synergy", "barrier", "input-leap"):
            if name in out:
                reasons.append(f"检测到 {name} 正在运行")
    except Exception:  # noqa: BLE001
        pass
    # 实测法: 把光标推到右边缘, 看是否被拽走
    d = virtual_desktop()
    target = (d.right - 1, d.top + d.height // 2)
    user32.SetCursorPos(*target)
    time.sleep(0.18)
    got = get_cursor_pos()
    if abs(got[0] - target[0]) > 4 or abs(got[1] - target[1]) > 4:
        reasons.append(
            f"光标推到边缘 {target} 后被拽到 {got}(外部程序占用边缘)")
    # 恢复光标到屏内
    user32.SetCursorPos(d.left + d.width // 2, d.top + d.height // 2)
    return bool(reasons), "; ".join(reasons)


def part_math() -> list[tuple[str, bool, str]]:
    """纯数学: 公式与 Deskflow 一致 + 幂等 + 旧公式确有系统性偏差。"""
    out = []
    for rect in (  # (left, top, right, bottom)
            (0, 0, 1920, 1080), (0, 0, 3840, 1080), (-1920, 0, 0, 1080)):
        left, top, right, bottom = rect
        w, h = right - left, bottom - top
        bad = 0
        for x in range(left, right, max(1, w // 200)):
            if old_norm(x, left, w) != deskflow_norm(x, left, w):
                bad += 1
        out.append((f"旧公式在 {w}x{h} 上有系统性偏差 ({bad} 个采样点)",
                    bad > 0, ""))
    # 幂等
    ok = all(
        len({deskflow_norm(x, 0, 1920) for _ in range(8)}) == 1
        for x in range(0, 1920, 37))
    out.append(("新公式幂等(同坐标反复换算值恒定)", ok, ""))
    # 边缘满量程
    out.append(("右边缘像素映射到 65535",
                deskflow_norm(1919, 0, 1920) == 65535, ""))
    out.append(("下边缘像素映射到 65535",
                deskflow_norm(1079, 0, 1080) == 65535, ""))
    return out


def part_live_math() -> list[tuple[str, bool, str]]:
    """活体: 注入 N 次后停在同一点(幂等), 用真实 SendInput。"""
    out = []
    d = virtual_desktop()
    probe = (d.left + d.width // 2, d.top + d.height // 2)
    for (tx, ty) in ((d.left + 100, d.top + 100),
                     (d.right - 100, d.bottom - 100)):
        user32.SetCursorPos(probe[0], probe[1])
        time.sleep(0.15)
        for _ in range(30):
            InputInjector.move_absolute(tx, ty)
        time.sleep(0.15)
        got = get_cursor_pos()
        ok = abs(got[0] - tx) <= 1 and abs(got[1] - ty) <= 1
        out.append((f"注入 30 次落在 {tx},{ty} (实测 {got})", ok, ""))
    return out


def part_drift() -> list[tuple[str, bool, str]]:
    """活体: 顶墙猛推 + 手腕上旋, 钉固定点 vs 不钉住。"""
    out = []
    d = virtual_desktop()
    ex, ey = d.right - 1, d.top + d.height // 2

    def run(pinned: bool) -> int:
        user32.SetCursorPos(ex, ey)
        time.sleep(0.15)
        y0 = get_cursor_pos()[1]
        pin = (ex, ey)
        for _ in range(120):
            InputInjector.move_relative(6, -1)
            if pinned:
                InputInjector.move_absolute(*pin)
        time.sleep(0.15)
        return get_cursor_pos()[1] - y0

    d_free = run(pinned=False)
    d_pin = run(pinned=True)
    out.append((f"钉固定点后 y 漂移 {d_pin}px (应≈0)", abs(d_pin) <= 2, ""))
    out.append((f"对照不钉住漂移 {d_free}px (应显著<0)",
                d_free < -20, "证明该场景可复现"))
    return out


def main() -> int:
    print("=== 钉住注入验收 ===\n")
    print("-- 纯数学: 归一化公式 --")
    math_results = part_math()
    for name, ok, _ in math_results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")

    polluted, why = env_polluted()
    print("\n-- 环境检查 --")
    if polluted:
        print(f"  !! 环境不可用于活体测量: {why}")
        print("  → 活体部分 SKIP(测量环境失效, 非代码问题)")
    else:
        print("  OK  环境可测量")

    live_results = []
    if not polluted:
        print("\n-- 活体: 幂等性 --")
        live_results += part_live_math()
        for name, ok, _ in live_results:
            print(f"  {'OK  ' if ok else 'FAIL'} {name}")
        print("\n-- 活体: 顶墙漂移对比 --")
        drift = part_drift()
        live_results += drift
        for name, ok, _ in drift:
            print(f"  {'OK  ' if ok else 'FAIL'} {name}")

    allok = all(ok for _, ok, _ in math_results + live_results)
    if polluted:
        verdict = ("PASS(数学层全绿; 活体层 SKIP —— 请关闭 Deskflow 等 KVM 后重跑)"
                   if allok else "FAIL(数学层有失败)")
    else:
        verdict = "PASS" if allok else "FAIL"
    print(f"\n[结论] {verdict}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
