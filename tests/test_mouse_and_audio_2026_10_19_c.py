# -*- coding: utf-8 -*-
"""★★★ 回归: 2026-10-19 第三批修复(鼠标速度 + WASAPI 写丢数据)。

## 1. 副屏鼠标显著更快 —— 相对注入被**两层**放大
   用户: "进入副屏后鼠标速度非常快, 显著快于在主屏时"

   实测本机完整映射表(请求 dx=40):
       档位  1 -> 0.215x      档位  5 -> 1.080x
       档位  4 -> 0.860x      档位 10 -> 2.160x  <- Windows 默认!
       档位 20 -> 4.315x
   两层独立放大: 加速曲线 SPI_SETMOUSE + **速度档位 SPI_SETMOUSESPEED**。
   旧实现只关了加速, 漏了速度档位 -> 默认档位 10 放大 2.16 倍。

   而且**没有任何档位能精确 1:1**(档位 5 = 1.08x, 档位 1 = 0.215x),
   所以 Deskflow 那种"临时把速度设为 1"的做法同样不精确
   (它的相对模式实测 0.215x)。

   修复: 改用**绝对定位**(读当前坐标 + dx -> MOUSEEVENTF_ABSOLUTE)。
   绝对坐标不受加速/速度档位影响, 实测所有档位下误差都是 **0 px**,
   且完全不改用户的系统鼠标设置。

## 2. 音频杂音 —— WASAPI 写入静默丢弃尾部数据
   `LoopbackRenderer.write` 旧实现:
       n = min(frames, avail)
       memmove(ptr, pcm[:n*block_align], ...)   # 剩下 n..frames 直接丢!
   而 `avail = buffer_frames - GetCurrentPadding()` 是**近似值**
   (padding 有采样延迟/取整), 实际可用空间常比它多 1~2 帧 ->
   每轮都可能丢几个样本 -> 波形微断裂 -> "细小沙沙声",
   且**高水位时更频繁**(每轮送来帧数多, 越界概率大)。
   这解释了用户"水位超过 80ms 就出现杂音"。

   修复: 未写完的尾部存 `_pending`, 下一轮优先写。实测 40 轮写入
   19200 帧全部到账(修复前丢 10080 帧)。
"""
import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.audio as audio_mod  # noqa: E402
import com_bridge.input as input_mod  # noqa: E402


# --- 1. 鼠标: 绝对定位 -------------------------------------------------

def _code_only(fn) -> str:
    """去掉 docstring 与注释, 只保留可执行代码。

    ★ 必需: 这些函数的 docstring 里会解释历史原因(提到 MOUSEEVENTF_MOVE
      等旧做法), 直接搜源码会误判。
    ★ 必须 dedent: inspect.getsource 返回带缩进的源码, ast.parse 会报错。
    """
    import textwrap
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    node = tree.body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]
    return ast.unparse(node)


def test_move_relative_uses_absolute():
    """★★★ 必须用绝对定位(相对注入会被速度档位放大 2.16 倍)。"""
    code = _code_only(input_mod.InputInjector.move_relative)
    assert "move_absolute" in code, \
        "必须委托给 move_absolute(绝对定位绕过加速与速度档位)"
    # 不得再自己发 MOUSEEVENTF_MOVE
    assert "MOUSEEVENTF_MOVE" not in code, \
        "不得再用相对注入(会被放大)"


def test_move_relative_never_touches_system_settings():
    """★ 绝对定位方案不碰系统鼠标设置(不改用户手感, 崩溃无残留)。"""
    code = _code_only(input_mod.InputInjector.move_relative)
    assert "_set_mouse_accel" not in code
    assert "_set_mouse_speed" not in code


def test_move_relative_clamps_to_virtual_desktop():
    """★ 绝对坐标必须夹在虚拟桌面内(否则被系统钳到边缘)。"""
    code = _code_only(input_mod.InputInjector.move_relative)
    assert "virtual_desktop" in code
    assert "v.left" in code or "v.right" in code


def test_mouse_speed_helpers_removed_or_unused():
    """速度档位读写函数可以保留(诊断用), 但不得在注入路径使用。"""
    # 只要 move_relative 不用它们即可(见上一个测试)
    assert hasattr(input_mod, "_get_mouse_speed")
    assert hasattr(input_mod, "_set_mouse_speed")


# --- 2. 音频: WASAPI 写入不丢数据 --------------------------------------

def test_renderer_keeps_pending_tail():
    """★★★ 写不完的尾部必须缓存到下一轮, 不能丢弃。"""
    src = inspect.getsource(audio_mod.LoopbackRenderer.write)
    assert "_pending" in src, "必须缓存未写完的尾部"
    # 关键: 不能再只有 memmove 前 n 帧就完事
    assert "self._pending = pcm[n * self.block_align:]" in src, \
        "必须把剩余部分存进 _pending"


def test_renderer_init_has_pending_fields():
    """_pending 字段必须初始化(否则首次 write 抛 AttributeError)。"""
    src = inspect.getsource(audio_mod.LoopbackRenderer.__init__)
    assert "_pending" in src
    assert "_pending_carry" in src


def test_renderer_pending_drains_next_round():
    """下一轮必须先写上一轮残留(否则会累积/乱序)。"""
    src = inspect.getsource(audio_mod.LoopbackRenderer.write)
    i_prepend = src.index("self._pending + pcm")
    i_calc = src.index("frames = len(pcm)")
    assert i_prepend < i_calc, "必须先把 pending 接到队首再计算帧数"


def test_renderer_no_gap_when_avail_zero():
    """avail<=0 时必须整段留到下一轮, 不能返回 0 就把数据丢掉。"""
    src = inspect.getsource(audio_mod.LoopbackRenderer.write)
    # 找到 n <= 0 的分支
    i = src.index("if n <= 0:")
    seg = src[i:i + 300]
    assert "_pending = pcm" in seg, \
        "空间为 0 时应把整段留到下一轮, 而不是丢弃"


# --- 3. 音频质量: ratio 变化不破坏相位 --------------------------------

def test_ratio_variation_keeps_quality():
    """★ ratio 缓慢/快速变化都不应显著恶化重建质量(PI 会持续微调 ratio)。"""
    import numpy as np
    rate = 48000
    tone = 3000.0
    n = 48000
    t = np.arange(n) / rate
    sig = np.sin(2 * np.pi * tone * t).astype(np.float32)

    def run(ratio_fn):
        rs = audio_mod.Resampler(channels=1, block_align=4, bits=32)
        out = []
        for i in range(0, n, 480):
            o = rs.process(sig[i:i + 480].tobytes(), ratio_fn(i),
                           max_out=1000)
            if o:
                out.append(np.frombuffer(o, dtype=np.float32))
        y = np.concatenate(out).astype(np.float64)
        seg = 8000
        worst = -999.0
        for k in range(0, max(1, len(y) - seg), seg // 2):
            chunk = y[k:k + seg]
            if len(chunk) < seg:
                break
            w = np.hanning(len(chunk))
            spec = np.abs(np.fft.rfft(chunk * w))
            spec[:5] = 0
            fb = int(np.argsort(spec)[::-1][0])
            fund = spec[fb]
            masked = spec.copy()
            masked[max(0, fb - 8):fb + 9] = 0
            spur = masked.max()
            dbc = 20 * np.log10(max(spur, 1e-20) / max(fund, 1e-20))
            worst = max(worst, dbc)
        return worst

    r_slow = run(lambda i: 1.0 + 0.002 * np.sin((i / 480) * 0.0001))
    r_fast = run(lambda i: 1.0 + 0.001 * np.sin((i / 480) * 0.01))
    assert r_slow < -50, f"ratio 缓慢变化时质量下降: {r_slow:.1f} dBc"
    assert r_fast < -50, f"ratio 快速变化时质量下降: {r_fast:.1f} dBc"
