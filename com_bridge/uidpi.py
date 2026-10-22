# -*- coding: utf-8 -*-
"""界面缩放系数 —— 让 GUI 跟随系统缩放(高 DPI 笔记本上不再"整体偏小")。

## 为什么需要单独一个模块

本进程是 **per-monitor DPI 感知** 的(`input._enable_dpi_awareness`) —— 这是
输入坐标必须的: 否则 Windows 会把 `GetCursorPos`/`SetCursorPos` 的坐标按当前
缩放"虚拟化", 跨屏落点会整体偏移(历史 BUG3①)。

代价是 **tkinter 不再自动放大界面**: 它看到的是物理像素, 于是 100% 缩放下
设计好的 620×660 窗口在 200% 的笔记本屏上只占四分之一面积, 字小得看不清。

所以缩放要自己算、自己套:
  * 本模块给出 `SCALE`(1.0 / 1.25 / 1.5 / 2.0 ...), **在导入期**就算好 ——
    `ui_theme` 的 PAD/WINDOW_W 等常量是模块级的, 必须在那之前确定;
  * `ui_theme.sp(v)` 把设计稿里的像素乘上去;
  * `ui_render.font(px)` 把 PIL 字号乘上去(PIL 用的是像素高度, 不受 Tk 缩放影响);
  * Tk 自己的磅值字体靠 `tk scaling` 放大(见 gui.MainWindow.__init__)。

## 取值顺序

1. 环境变量 `COM_BRIDGE_UI_SCALE`(调试/强制用, 例如 `2` 或 `125%`);
2. 系统 DPI: `GetDpiForSystem() / 96`;
3. 兜底 1.0。

⚠ `GetDpiForSystem()` 必须在**声明 DPI 感知之后**调用, 否则拿到的是虚拟化后的
96。这里主动调一次 `_enable_dpi_awareness()`(幂等), 避免依赖导入顺序。

## 换显示器怎么办

缩放系数在进程启动时定一次。把窗口拖到另一块不同缩放的屏幕上不会实时重排 ——
需要重启程序。这是**有意的取舍**: 重建整套控件(PIL 位图全部按新尺寸重画)在
运行期做会闪一下, 而且用户很少来回拖。真要改, 界面上没有任何入口, 只能改环境
变量或重启。
"""
from __future__ import annotations

import os

# 允许的取值范围: 低于 1.0 会把界面缩小(没有意义), 高于 3.0 只会把窗口撑到
# 屏幕外
_MIN_SCALE = 1.0
_MAX_SCALE = 3.0


def _from_env() -> float | None:
    raw = os.environ.get("COM_BRIDGE_UI_SCALE", "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("%"):
            return float(raw[:-1]) / 100.0
        return float(raw)
    except ValueError:
        return None


def _from_system() -> float | None:
    try:
        # 先确保 DPI 感知(幂等; input 模块里也调过)
        try:
            from .input import _enable_dpi_awareness
            _enable_dpi_awareness()
        except Exception:  # noqa: BLE001
            pass

        import ctypes
        user32 = ctypes.windll.user32
        # GetDpiForSystem 是 Win10 1607+; 取不到再退回 GetDeviceCaps(LOGPIXELSX)
        try:
            user32.GetDpiForSystem.restype = ctypes.c_uint
            dpi = int(user32.GetDpiForSystem())
            if dpi > 0:
                return dpi / 96.0
        except (AttributeError, OSError):
            pass
        hdc = user32.GetDC(0)
        try:
            gdi32 = ctypes.windll.gdi32
            LOGPIXELSX = 88
            dpi = int(gdi32.GetDeviceCaps(hdc, LOGPIXELSX))
            if dpi > 0:
                return dpi / 96.0
        finally:
            user32.ReleaseDC(0, hdc)
    except Exception:  # noqa: BLE001
        return None
    return None


def _clamp(s: float) -> float:
    return max(_MIN_SCALE, min(_MAX_SCALE, float(s)))


def detect() -> tuple[float, str]:
    """返回 (缩放系数, 来源说明)。"""
    env = _from_env()
    if env is not None:
        return _clamp(env), f"环境变量 COM_BRIDGE_UI_SCALE={env}"
    sysdpi = _from_system()
    if sysdpi is not None:
        return _clamp(sysdpi), f"系统 DPI {sysdpi * 96:.0f}"
    return 1.0, "兜底 1.0"


SCALE, SCALE_SOURCE = detect()


def sp(px: float) -> int:
    """设计稿像素 -> 实际像素(四舍五入到整数)。"""
    return int(round(float(px) * SCALE))


def desc() -> str:
    """一句话说明当前缩放(打进日志, 便于用户确认)。"""
    return f"界面缩放 ×{SCALE:.2f}（{SCALE_SOURCE}）"
