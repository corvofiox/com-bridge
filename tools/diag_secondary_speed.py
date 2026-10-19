"""副机侧实测: 副机鼠标"明显比主机快"到底来自哪里。

用户症状 2: "副机鼠标速度明显比主机上快"。

已排除的(实测):
  * **指针速度档位**(SPI_SETMOUSESPEED 的 1..11 滑块)对注入位移**毫无影响**
    —— 实测 speed=1/5/10/15/20 下同一注入的位移都是 1.000 倍。
  * **指针加速**对注入位移有影响(放大 2.29 倍), 已在 `move_relative` 里
    用"临时禁加速 + 立即还原"补偿(实测补偿后 1:1)。
    这一点可用 `tools/diag_accel.py` 在本机复核。

⚠ 本脚本必须**在副机上运行**(它测的是"副机自己注入时, 光标走多远"),
  因为"更快"是**副机**的观感。判断思路:
    1. 副机当前鼠标设置(加速/速度档位)是什么?
    2. 副机的注入增益是多少?(应当 1:1)
    3. 若增益 >1, 是加速还是别的原因?
    4. 副机的**分辨率/缩放比例**是否与主机不同?
       若副机 DPI 缩放更高(如 150% vs 主机 100%), 同样的物理距离在副机上
       占更多"逻辑像素" -> 看起来更快。这一点与代码无关, 但用户会感知为
       "副机更快", 需要在两台机器上对齐缩放或按比例补偿位移。

用法(在副机上): python -u tools/diag_secondary_speed.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import c_int, c_void_p, wintypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from com_bridge.input import (
    INPUT,
    MOUSEEVENTF_MOVE,
    SPI_GETMOUSE,
    SPI_GETMOUSESPEED,
    InputInjector,
    get_cursor_pos,
    user32,
    virtual_desktop,
    _save_mouse_accel,
)

user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         c_void_p, wintypes.UINT]


def get_speed() -> int:
    v = c_int(0)
    user32.SystemParametersInfoW(SPI_GETMOUSESPEED, 0, ctypes.byref(v), 0)
    return v.value


def dpi_scale() -> dict:
    """取主显示器的 DPI 缩放(注入的是逻辑像素, 缩放会影响观感)。"""
    try:
        user32.GetDpiForSystem.restype = wintypes.UINT
        user32.GetDpiForSystem.argtypes = []
        sys_dpi = user32.GetDpiForSystem()
    except Exception:  # noqa: BLE001
        sys_dpi = 0
    try:
        hdc = user32.GetDC(None)
        gdi32 = ctypes.windll.gdi32
        gdi32.GetDeviceCaps.restype = c_int
        gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, c_int]
        LOGPIXELSX = 88
        scale_dpi = gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
        user32.ReleaseDC(None, hdc)
    except Exception:  # noqa: BLE001
        scale_dpi = 0
    return {"system_dpi": sys_dpi, "device_dpi": scale_dpi,
            "scale_pct": round(scale_dpi / 96 * 100) if scale_dpi else 0}


def raw_move(dx, dy, tag=True):
    i = INPUT(type=0)
    i.union.mi.dx = dx
    i.union.mi.dy = dy
    i.union.mi.dwFlags = MOUSEEVENTF_MOVE
    i.union.mi.dwExtraInfo = InputInjector.TAG if tag else 0
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def measure(fn, n, dx, dy) -> tuple[int, int]:
    d = virtual_desktop()
    user32.SetCursorPos(d.left + d.width // 2, d.top + d.height // 2)
    time.sleep(0.15)
    x0, y0 = get_cursor_pos()
    for _ in range(n):
        fn(dx, dy)
        time.sleep(0.01)
    time.sleep(0.15)
    x1, y1 = get_cursor_pos()
    return x1 - x0, y1 - y0


def main() -> int:
    d = virtual_desktop()
    print("=" * 66)
    print("副机鼠标速度诊断")
    print("=" * 66)
    print(f"虚拟桌面      = {d}   ({d.width}x{d.height})")
    print(f"鼠标加速设置  = {_save_mouse_accel()}  (第 3 个值 1=启用加速)")
    print(f"鼠标速度档位  = {get_speed()}  (1..20, 对应设置里的滑块)")
    print(f"DPI 缩放      = {dpi_scale()}")

    N, DX, DY = 40, 13, 7
    exp = (N * DX, N * DY)

    g_raw = measure(lambda x, y: raw_move(x, y, tag=False), N, DX, DY)
    g_prod = measure(InputInjector.move_relative, N, DX, DY)
    print(f"\n注入 {N} 次 ({DX},{DY}) = 请求 {exp}")
    print(f"  未补偿(直接 SendInput): 实际 {g_raw}  "
          f"增益 (x{g_raw[0]/exp[0]:.3f})")
    print(f"  生产 move_relative:     实际 {g_prod}  "
          f"增益 (x{g_prod[0]/exp[0]:.3f})")

    print("\n" + "=" * 66)
    prod_ratio = g_prod[0] / exp[0] if exp[0] else 0
    if prod_ratio > 1.05:
        print(f"[问题] 生产路径增益 {prod_ratio:.3f} > 1 —— 副机的加速补偿"
              f"**没有生效**。\n"
              f"       可能原因: 有另一个程序(Deskflow/Synergy 等 KVM)也在\n"
              f"       改这些全局设置, 与我们的临时改动互相踩。\n"
              f"       请先退出它们再重跑本脚本确认。")
    else:
        print(f"[正常] 生产路径增益 {prod_ratio:.3f} ≈ 1 —— 注入已 1:1。\n"
              f"       若你在副机上仍觉得「鼠标更快」, 请对比两台的 **DPI 缩放**\n"
              f"       (上面已打印): 缩放更高的一侧, 同样的物理移动会占更多\n"
              f"       逻辑像素, 观感即「更快」。"
              f"这需要在 Windows 显示设置里把两台对齐"
              f"(或用相同缩放)。")

    # 对照: 直接看"未补偿"的放大倍数, 说明加速确实是主因
    raw_ratio = g_raw[0] / exp[0] if exp[0] else 0
    print(f"\n[参考] 未补偿注入的放大倍数 = {raw_ratio:.3f} "
          f"(>1 说明该机启用了指针加速)")
    print(f"       缩放 {dpi_scale()['scale_pct']}% —— 若两台缩放不同, "
          f"手感差异主要来自这里")
    return 0


if __name__ == "__main__":
    sys.exit(main())
