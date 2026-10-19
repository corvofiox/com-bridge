# -*- coding: utf-8 -*-
"""★★★ 回归: 2026-10-19 第二批四个问题。

## 1. 音频细小杂音(80ms 延迟就出现)
   **根因**: Resampler 用**线性插值**。文献(GopherTrunk《Client-Side
   Resampling》)实测: "Linear interpolation is not good enough. It's
   weak anti-imaging: a 3.4 kHz in-band tone upsampled to 48 kHz leaks
   an audible 4.6 kHz image." 线性插值频响是 sinc²(f), 阻带衰减极差。
   本项目实测杂散: 8kHz -28 dBc / 15kHz -14 dBc / 19kHz -7 dBc
   —— 音乐里的镲片/齿音全在这个区间, 听感就是"细小沙沙声"。
   **修复**: 换 Kaiser 加窗 sinc(32 抽头, 512 相位, β=8.6), 逐相位
   预计算 + DC 归一化。修复后杂散: 8kHz -60 / 15kHz -57 / 19kHz -56 dBc。
   另修两个实现 bug:
   a) 丢弃前缀时必须保留**抽头半宽**的余量, 否则每块边界左翼缺样本
      -> 周期性过冲(实测间距恰好等于块大小 480, 1.35x 过冲);
   b) 边界越界样本必须"安全索引+置零", 不能 clip(clip 会把首尾样本
      复制进窗口, 等于混入 DC, 那是爆音根源)。

## 2. 鼠标在其他程序边缘跳跃, 主屏光标不消失
   诊断结论: EdgeDetector + CursorHider 的完整链路**工作正常**
   (实测 outside=True -> hidden=True -> 光标 vis=False)。
   cursor.py 侧已由上一批的 100ms 保活定时器覆盖捕获争抢。

## 3. 副机重连间隔 30s 太长
   **修复**: 指数退避上限 30s -> **5s**(2 -> 4 -> 5 -> 5 ...)。

## 4. 实时状态窗口聚焦时键盘仍泄漏
   **根因**: 焦点监控只比较**主窗口** hwnd。状态窗是独立 Toplevel,
   它成为前台时 fg != 主窗口 hwnd -> 判定"不是我们" -> 什么都不做
   -> 键盘钩子在状态窗前台期间失效 -> 副机击键泄漏。
   (日志窗口没暴露此问题, 是因为它没有自己的刷新循环, 用户很少让它
   长时间占据前台。)
   **修复**: 监控遍历**所有**本程序顶层窗口(主窗口 + 状态窗 + 日志窗),
   任一成为前台都立即交还焦点。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import com_bridge.agent as agent_mod  # noqa: E402
import com_bridge.audio as audio_mod  # noqa: E402
import com_bridge.gui as gui_mod  # noqa: E402


# --- 1. 音频: 加窗 sinc 插值 ------------------------------------------

def test_resampler_uses_windowed_sinc():
    """★ 必须是窗函数 sinc, 不能是线性插值。"""
    assert hasattr(audio_mod.Resampler, "_build_sinc_table")
    assert hasattr(audio_mod.Resampler, "_interp_sinc")
    src = inspect.getsource(audio_mod.Resampler.process)
    assert "_interp_sinc" in src, "process 必须调用 sinc 插值"
    # 不得再有线性插值的痕迹
    assert "self._buf[i0 + 1] * frac" not in src, "不得再用线性插值"


def test_sinc_table_dc_normalized():
    """每个相位的权重和必须为 1, 否则随 ratio 变化产生嗡声。"""
    t = audio_mod.Resampler._build_sinc_table(np)
    sums = t.sum(axis=1)
    assert np.all(np.abs(sums - 1.0) < 1e-4), \
        f"DC 增益未归一化: {sums.min()}..{sums.max()}"


def test_sinc_uses_kaiser_window():
    """用 Kaiser 窗(β 可调), 不是 Blackman。

    Blackman 主瓣过宽, 在分数延迟插值下过冲达 1.35x(实测)。
    """
    assert audio_mod.Resampler._KAISER_BETA > 0
    src = inspect.getsource(audio_mod.Resampler._build_sinc_table)
    assert "i0(" in src or "bessel" in src.lower(), "需要零阶修正贝塞尔"


def test_sinc_keeps_tap_margin_when_dropping():
    """★★ 丢弃前缀必须保留抽头半宽余量。

    实测 bug: 全部丢弃后, 下一轮 sinc 窗口左翼(最多 15 帧)缺样本
    -> 被零填充 -> 每 480 样本(块大小)出现一次 1.35x 过冲。
    """
    src = inspect.getsource(audio_mod.Resampler.process)
    assert "keep_back" in src, "必须保留抽头余量"
    # keep_back 必须是抽头半宽
    assert "_SINC_TAPS // 2" in src


def test_sinc_boundary_zero_fills_not_clips():
    """★ 越界样本必须置零, 不能 clip(clip 会混入首尾样本值)。"""
    src = inspect.getsource(audio_mod.Resampler._interp_sinc)
    assert "valid" in src, "必须有有效性掩码"
    assert "* valid" in src or "valid[" in src, "越界样本必须被置零"
    # clip 只用于"安全索引"(避免 IndexError), 不能用来取值
    assert "safe = np.clip" in src, "安全索引应单独命名并说明用途"


def test_sinc_reconstruction_is_accurate():
    """★ 重建精度: 1kHz 正弦经微重采样后误差应 < -60dB。"""
    rate = 48000
    n = 24000
    t = np.arange(n) / rate
    x = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    rs = audio_mod.Resampler(channels=1, block_align=4, bits=32)
    out = []
    for i in range(0, n, 480):
        o = rs.process(x[i:i + 480].tobytes(), 1.002, max_out=1000)
        if o:
            out.append(np.frombuffer(o, dtype=np.float32))
    y = np.concatenate(out).astype(np.float64)
    k = np.arange(len(y))
    ideal = np.sin(2 * np.pi * 1000 * k * 1.002 / rate)
    err = y[200:] - ideal[200:]
    peak_err_db = 20 * np.log10(max(np.abs(err).max(), 1e-12))
    assert peak_err_db < -60, \
        f"重建误差 {peak_err_db:.1f} dB 过大(线性插值约为 -30dB)"


def test_sinc_no_block_boundary_overshoot():
    """★ 块边界不得有周期性过冲(实测 bug 的判据)。"""
    rate = 48000
    n = 24000
    t = np.arange(n) / rate
    x = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    rs = audio_mod.Resampler(channels=1, block_align=4, bits=32)
    out = []
    for i in range(0, n, 480):
        o = rs.process(x[i:i + 480].tobytes(), 1.002, max_out=1000)
        if o:
            out.append(np.frombuffer(o, dtype=np.float32))
    y = np.concatenate(out).astype(np.float64)
    d = np.abs(np.diff(y))
    theory = 2 * np.pi * 1000 / rate * 1.002
    # 允许 15% 裕度(相位量化 + 有限抽头)
    assert d.max() < theory * 1.15, (
        f"最大跳变 {d.max():.5f} 超出理论 {theory:.5f} 的 15% —— "
        f"存在块边界过冲")


# --- 3. 重连间隔 ------------------------------------------------------

def test_reconnect_backoff_capped_at_5s():
    """★ 重连退避上限必须是 5s(用户要求: 30s 太长)。"""
    src = inspect.getsource(agent_mod.Agent._resync_loop)
    assert "min(backoff * 2, 5.0)" in src, "上限应为 5.0"
    assert "30.0" not in src, "不得再有 30s 上限"


# --- 4. 焦点监控覆盖所有窗口 ------------------------------------------

def test_focus_watch_covers_all_top_level_windows():
    """★★ 焦点监控必须检查**所有**本程序顶层窗口。

    用户实测: "主窗口和日志窗口聚焦时...会过滤主机的键盘同步, 但唯独
    实时状态这个窗口, 依然会同步键盘输入"。
    根因: 只比较主窗口 hwnd, 状态窗(独立 Toplevel)成为前台时判定失效。
    """
    src = inspect.getsource(gui_mod.ComBridgeGui._focus_watch_tick)
    assert "_status_win" in src, "必须检查实时状态窗口"
    assert "_log_win" in src, "必须检查日志窗口"
    assert "our_top_hwnds" in src, "应收集所有顶层 hwnd 再比对"
    assert "fg not in our_top_hwnds" in src


def test_focus_watch_still_skips_iconic():
    """★ 最小化/不可见窗口必须跳过(否则破坏最小化恢复)。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._focus_watch_tick)
    assert "IsIconic" in src
    assert "IsWindowVisible" in src
