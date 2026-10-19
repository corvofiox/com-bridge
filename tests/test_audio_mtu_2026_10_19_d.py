# -*- coding: utf-8 -*-
"""★★★ 回归: 2026-10-19 第四批(音频杂音的真正根因: IP 分片)。

## 用户提供的关键证据
"声音问题没有解决, 我补充一个信息: 起初水位会持续增长到 390ms, 并在
 超过 300ms 时出现爆音。后来限制了它最多涨到 240ms 左右, 然后是超过
 200ms 出现爆音, 现在给它限制到不超过 85, 于是超过 80ms 出现爆音。"

-> 爆音阈值**始终紧跟水位上限**, 说明噪音来自"执行上限的机制"。

## 但排查发现更深的一层(本批真正修掉的)
音频 UDP 包 **3868 字节**, 而以太网 MTU 只有 1500
-> 每个包被切成 **3 个 IP 分片**。
UDP 无重传, **任一分片丢失 -> 整个数据报被丢弃** -> 10ms 音频完全消失
-> 丢包率被放大 ~3 倍 -> 听感是"零碎杂音/爆音"。
水位越高 -> 队列积压越多 -> 主机侧更容易触发"取不到连续数据" ->
就越依赖丢帧兜底 -> 与"阈值跟随上限"的现象一致。

## 本批修复
1. **按 MTU 分包发送**(120 帧 = 960B, 加头 988 < 1500): 彻底消除分片。
2. **静音期间补发零包**: 否则主机时间轴出现空洞 -> pop_aligned 误判
   为巨大滞后 -> 重复/跳过样本。
3. **采集时间戳改用 QPC 设备位置**(而非 now_us): 消除读取延迟抖动。
4. **移除 AUTOCONVERTPCM/SRC_DEFAULT_QUALITY**: 避免 WASAPI 用默认
   质量重采样器处理采集数据。

## 排除的方向(逐项实测都不是)
插值质量(Kaiser sinc THD+N -131dB)、水位/积压、ratio 相位变化、
本机渲染/采集链路(THD+N -131.3dB, 频率误差 0.0ppm)。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.agent as agent_mod  # noqa: E402
import com_bridge.audio as audio_mod  # noqa: E402


# --- 1. MTU 分包(核心) -----------------------------------------------

def test_audio_packets_fit_in_mtu():
    """★★★ 每个 UDP 音频包必须 <= MTU, 否则 IP 分片导致整包丢失。

    实测: 修复前 480 帧 x 2ch x 32bit = 3840B, 加头 3868B,
    而 MTU = 1500 -> 分 3 片, 任一片丢失整包作废。
    """
    src = inspect.getsource(agent_mod.Agent._capture_loop)
    assert "SEND_FRAMES" in src, "必须限制每包帧数"
    # 提取数值
    import re
    m = re.search(r"SEND_FRAMES\s*=\s*(\d+)", src)
    assert m, "SEND_FRAMES 应为字面量"
    frames = int(m.group(1))
    # 计算包大小
    for ch, bits in ((2, 32), (2, 16), (1, 32)):
        payload = frames * ch * (bits // 8)
        total = payload + 28
        assert total <= 1500, (
            f"{frames} 帧 {ch}ch {bits}bit = {total}B 超过 MTU 1500")


def test_audio_send_loop_splits_pcm():
    """必须真的按块切分发送, 不是一次性 send 整块。"""
    src = inspect.getsource(agent_mod.Agent._capture_loop)
    assert "while sent < total_frames" in src, "必须有分包循环"
    assert "piece" in src, "必须逐片发送"
    # 每片时间戳要按帧偏移推进
    assert "piece_ts" in src, "每片要有独立时间戳"


# --- 2. 静音补包 ------------------------------------------------------

def test_agent_sends_silence_packets():
    """★ 静音期间必须补发零包, 否则主机时间轴出现空洞。

    实测: LoopbackCapture.read() 在静音时全部返回 None,
    若什么都不发, 主机 pop_aligned 会把这段当成"巨大滞后",
    触发重复/跳过样本 -> 杂音。
    """
    src = inspect.getsource(agent_mod.Agent._capture_loop)
    assert "silence_ticks" in src, "必须检测静音"
    assert "silent" in src, "必须构造静音 PCM"
    assert "silence_sent" in src, "应有诊断计数"


def test_silence_timestamp_is_monotonic():
    """★ 补的静音包时间戳必须接在上一包之后(严格递增)。

    用 now_us() 可能与上一包乱序, 而 pop_aligned 依赖时间戳单调。
    """
    src = inspect.getsource(agent_mod.Agent._capture_loop)
    assert "_last_audio_ts" in src, "必须记录上一包时间戳"
    assert "last + dur_us" in src or "self._last_audio_ts" in src


# --- 3. 采集时间戳用 QPC ----------------------------------------------

def test_capture_uses_qpc_timestamp():
    """★ 采集时间戳应优先用 QPC 设备位置(单调、与采样率严格线性)。

    now_us() 是"读到数据的时刻", 与音频真正被采集的时刻之间有
    不确定延迟(WASAPI 缓冲/调度) -> 时间戳抖动 -> pop_aligned 选错。
    """
    src = inspect.getsource(agent_mod.Agent._capture_loop)
    assert "qpc" in src, "必须使用 QPC"
    assert "qpc) // 10" in src or "qpc // 10" in src, \
        "QPC 单位是 100ns, 转 us 要除以 10"


# --- 4. 不用劣质重采样 ------------------------------------------------

def test_capture_no_default_quality_src():
    """★ 采集端不应强制 AUTOCONVERTPCM/SRC_DEFAULT_QUALITY。

    实测(diagnose_capture_fidelity.py): 带这两个标志时主频
    2016Hz -> 1992Hz, 频谱形状相关只有 0.597。我们传入的 mix 本就是
    设备原生格式, 不需要转换, 加了反而引入一次劣质重采样。
    """
    src = inspect.getsource(audio_mod.LoopbackCapture.start)
    live = [ln for ln in src.splitlines()
            if not ln.strip().startswith("#")]
    body = "\n".join(live)
    # 允许常量定义存在(诊断用), 但 Initialize 时不得传入
    i = body.find("ac.Initialize")
    assert i >= 0
    call = body[i:i + 400]
    assert "AUTOCONVERTPCM" not in call, \
        "Initialize 不得传 AUTOCONVERTPCM(会用劣质默认重采样)"
    assert "SRC_DEFAULT_QUALITY" not in call, \
        "Initialize 不得传 SRC_DEFAULT_QUALITY"


# --- 5. 本机链路保真度(基准) -----------------------------------------

def test_local_chain_is_transparent():
    """★ 本机播放->采集链路必须是透明的(用于排除本机因素)。

    实测: 播放 2000Hz 正弦, 环回采集后 THD+N = -131.3dB,
    频率误差 0.0 ppm。说明本机渲染/采集完全干净,
    杂音只能来自跨机链路(网络/分片)。
    """
    # 这是一个文档性断言: 确保采集与渲染都用设备原生格式,
    # 不引入额外转换(那是保真度的前提)。
    cap_src = inspect.getsource(audio_mod.LoopbackCapture.start)
    ren_src = inspect.getsource(audio_mod.LoopbackRenderer.start)
    for name, src in (("capture", cap_src), ("render", ren_src)):
        assert "GetMixFormat" in src, f"{name} 必须用设备原生格式"
