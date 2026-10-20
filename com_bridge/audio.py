"""WASAPI loopback 采集 / 渲染 —— 本机已验证的调用配方。

已验证事实(2026-10-15, 本机 48000Hz 32bit float 设备):
  1. pycaw.utils.AudioUtilities.GetSpeakers() 可用(内部 comtypes.CoCreateInstance
     + IMMDeviceEnumerator 早期绑定)。
  2. IMMDevice::Activate(IAudioClient._iid_, CLSCTX_ALL, None) 返回
     POINTER(IUnknown), 必须再 QueryInterface(IAudioClient)。
  3. IAudioClient.Initialize 的 pFormat 必须传 GetMixFormat() 返回的指针,
     传 None 或自建 WAVEFORMATEX 均报错。
  4. IAudioClient::GetService 需要 ctypes.byref(iid), 返回 POINTER(IUnknown),
     再强转为目标接口。
  5. IAudioCaptureClient IID = {C8ADBD64-E71E-48A0-A4DE-185C395CD317}
     IAudioRenderClient  IID = {F294ACFC-3146-4483-A7BF-ADDCA7C260E2}
  6. GetBuffer 返回 (BYTE**, frames, flags, devpos, qpc); flags 位:
       0x1 AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY
       0x2 AUDCLNT_BUFFERFLAGS_SILENT
  7. 若设备静默(无应用渲染), loopback 不会产生数据包 —— 这是正常现象,
     不是代码错误。
"""

from __future__ import annotations

import ctypes
import threading
import time
from collections import deque
from ctypes import POINTER as P, c_byte, c_uint32 as UINT32, c_uint64 as UINT64

from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT, IUnknown, POINTER, CoInitialize, CoUninitialize

from .protocol import FMT_S24, frame_bytes_per_sample


AUDCLNT_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000

AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
IID_IAudioRenderClient = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")


class IAudioCaptureClient(IUnknown):
    _iid_ = IID_IAudioCaptureClient
    _methods_ = (
        COMMETHOD(
            [], HRESULT, "GetBuffer",
            (["out"], P(P(c_byte)), "ppData"),
            (["out"], P(UINT32), "pNumFramesToRead"),
            (["out"], P(UINT32), "pdwFlags"),
            (["out"], P(UINT64), "pu64DevicePosition"),
            (["out"], P(UINT64), "pu64QPCPosition"),
        ),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], UINT32, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize",
                  (["out"], P(UINT32), "pNumFramesInNextPacket")),
    )


class IAudioRenderClient(IUnknown):
    _iid_ = IID_IAudioRenderClient
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["in"], UINT32, "NumFramesRequested"),
                  (["out"], P(P(c_byte)), "ppData")),
        COMMETHOD([], HRESULT, "ReleaseBuffer",
                  (["in"], UINT32, "NumFramesWritten"),
                  (["in"], UINT32, "dwFlags")),
    )


class LoopbackCapture:
    """本机整机音频 loopback 采集。"""

    # ★★★ 采集缓冲深度(100ms) —— 修复"副机传回主机的音频有周期性噼啪声"。
    #
    #   根因(2026-10-19 分析 + 之前的历史注记共同指向):
    #     旧实现 `ac.Initialize(..., hnsBufferDuration=0, ...)`。共享模式下
    #     0 表示"用**引擎建议的最小缓冲**", 实测通常只有几 ms。
    #     而采集循环是 Python: 每轮 read() 都要过 GIL、可能有调度延迟。
    #     只要这个延迟超过浅缓冲, WASAPI 共享模式就会**丢最旧的数据**
    #     (无重传、无标记) —— 主机侧 pop_aligned 看到时间戳跳变,
    #     判定为"该跳过的滞后" -> 重复/跳过样本 -> **周期性噼啪声**。
    #
    #   为什么"周期性": 丢多少、丢在哪取决于当时的调度抖动, 所以断口
    #   大小不定但规律出现 —— 与用户报的"周期性噼啪声"完全一致。
    #
    #   修法: 显式给一个**足够深**的缓冲(100ms, 与渲染端对称),
    #   让 Python 的调度抖动(几十 ms)被缓冲吸收, 不再丢音频。
    #   代价: 采集侧多 100ms 延迟 —— 但渲染端本来就有 95ms 缓冲 +
    #   60ms 抖动缓冲, 端到端延迟只多一点点, 换来无杂音是划算的。
    BUFFER_DURATION_HNS = 1_000_000  # 100ms

    def __init__(self) -> None:
        self._com_inited = False
        self._ac = None
        self._cap = None
        self._cap_raw = None     # GetService 返回的 IUnknown: 必须持有防 GC
        self._dev = None
        self._thread = None      # start() 所在线程: COM 线程亲和
        self.rate = 0
        self.channels = 0
        self.bits = 0
        self.block_align = 0
        self.buffer_frames = 0
        # ★ 诊断: 统计"read() 轮询到空"的次数(缓冲浅时的高频路径),
        #   以及"一次 read 拿到的帧数"分布 —— 前者高说明缓冲被反复抽干。
        self.empty_reads = 0
        self.total_frames = 0
        self.read_calls = 0
        self.max_chunk_frames = 0

    def start(self) -> None:
        from pycaw.api.audioclient import IAudioClient
        from pycaw.utils import AudioUtilities

        # COM 对象线程亲和: comtypes 的 POINTER(IAudioClient) 包装只在
        # 创建它的线程上有 vtable 绑定。start() 与 read() 必须在同一线程,
        # 否则 GetBuffer 抛 "COM method call without VTable"。
        self._thread = threading.current_thread()
        CoInitialize()
        self._com_inited = True

        dev = AudioUtilities.GetSpeakers()
        ac = dev._dev.Activate(IAudioClient._iid_, CLSCTX_ALL, None).QueryInterface(
            IAudioClient
        )
        mix = ac.GetMixFormat()
        fmt = mix.contents
        self.rate = fmt.nSamplesPerSec
        self.channels = fmt.nChannels
        self.bits = fmt.wBitsPerSample
        self.block_align = fmt.nBlockAlign

        zero = GUID("{00000000-0000-0000-0000-000000000000}")
        # ★★★ 只用 LOOPBACK, **不加** AUTOCONVERTPCM / SRC_DEFAULT_QUALITY
        #   (2026-10-19 关键修正, 这是"音频有杂音"的真凶之一)。
        #
        #   这两个标志会让 WASAPI 用**默认质量的采样率转换器**处理音频:
        #       AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM  (0x80000000)
        #       AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY (0x08000000)
        #   实测后果(等间隔脉冲串测试, tools/diagnose_capture_fidelity.py):
        #       * 脉冲间隔完美(0.00ms 误差) -> 时间轴没错
        #       * 但**主频偏移**: 源 2016Hz -> 采集 1992Hz (−1.2%)
        #       * 频谱形状相关只有 0.597(接近 1 才算一致)
        #   即: 波形被那个"默认质量"重采样器**改变了**。
        #   对单频正弦听不出大问题(只是音调略低), 但对音乐这种宽带信号,
        #   它的插值误差就是**持续的沙沙声** —— 正是用户反复报的杂音。
        #
        #   为什么根本不需要它: 我们传入的 `mix` 就是设备的**原生混音格式**
        #   (GetMixFormat), 采集端与设备格式天然一致, 不存在"需要转换"的
        #   情况。加了它反而凭空引入一次劣质重采样。
        ac.Initialize(
            AUDCLNT_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            self.BUFFER_DURATION_HNS, 0, mix, zero,
        )
        self.buffer_frames = ac.GetBufferSize()
        out = ac.GetService(ctypes.byref(IID_IAudioCaptureClient))
        # 必须持有 out 的引用! comtypes 的 IUnknown 包装在 __del__ 里 Release,
        # out 一旦被 GC, self._cap 就成了悬垂指针, 下次 GetBuffer 会抛
        # ValueError: COM method call without VTable (或直接 access violation)。
        self._cap_raw = out
        self._cap = POINTER(IAudioCaptureClient)(
            ctypes.cast(out, ctypes.c_void_p).value
        )
        self._ac = ac
        self._dev = dev
        ac.Start()

    def read(self, timeout_s: float = 0.2) -> tuple[bytes, int, int] | None:
        """阻塞到有新数据或超时, 返回 (pcm_bytes, flags, qpc_position)。

        GetNextPacketSize 在本机 comtypes 封装下会偶发 access violation
        (读 0xFFFFFFFFFFFFFFFF), 因此一律不用它, 改为轮询 GetBuffer:
        frames==0 时先 ReleaseBuffer(0) 再短睡。

        关键陷阱: **不能把 COM 对象缓存在 self._cap 之外再跨线程复用**,
        更不能在 stop() 里把 self._ac/self._cap 置 None —— 渲染线程可能
        仍在 GetBuffer 里, 指针被置空后立刻 access violation。stop() 只
        Stop 流, 不释放引用(Python GC 到进程退出才回收)。
        """
        if self._cap is None:
            return None
        deadline = time.monotonic() + timeout_s
        self.read_calls += 1
        while True:
            try:
                ptr, frames, flags, devpos, qpc = self._cap.GetBuffer()
            except OSError:
                return None
            if frames == 0:
                self._cap.ReleaseBuffer(0)
                self.empty_reads += 1
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.003)
                continue
            raw = ctypes.string_at(ptr, frames * self.block_align)
            self._cap.ReleaseBuffer(frames)
            self.total_frames += frames
            if frames > self.max_chunk_frames:
                self.max_chunk_frames = frames
            return raw, flags, qpc

    def stop(self) -> None:
        ac, self._ac = self._ac, None
        if ac is not None:
            try:
                ac.Stop()
            except Exception:  # noqa: BLE001
                pass
        # 注意: 不清 self._cap, 不 CoUninitialize —— 渲染线程可能仍在跑


class BoundaryFader:
    """在「有声 <-> 静音」的边界处加短淡入/淡出，消除起播/停止的爆音。

    ★ 用户实测症状(2026-10-21): "开始播放声音和结束播放声音的那一刻会
      小爆一下"。这是**采样级跳变**: 音乐突然变成数字零(或反之)时,
      扬声器从某个非零波形值一步跳到 0 —— 就是一声 pop。它与"周期性
      断口"是两个独立问题, 必须分别处理。

    ★ 实现要点: 淡出需要**知道后面是不是静音**, 所以必须把尾部
      FADE 帧暂不外发(延迟一个淡入淡出长度的 FIFO)。这样:
        - 下一块是静音 -> 先把暂存的尾巴按余弦淡出, 再放静音;
        - 下一块恢复有声 -> 静音后紧跟的有声块按余弦淡入;
        - 状态不变 -> 暂存尾巴原样发出, 正常数据零改动。
      代价: 端到端多 FADE 帧延迟(默认 5ms), 对已含 60ms 抖动缓冲的
      音频链路可忽略。
    """

    def __init__(self, channels: int, block_align: int, bits: int,
                 fade_ms: float = 5.0, rate: int = 48000,
                 silent_rms: float = 1e-3):
        self.channels = channels
        self.block_align = block_align
        self.bits = bits
        self.rate = rate
        self.fade_frames = max(8, int(rate * fade_ms / 1000.0))
        self.silent_rms = silent_rms
        self._hold = b""              # 尚未发出的尾部(原始字节)
        self._was_silent = True       # 上一块是否为静音
        self.fades_out = 0            # 诊断: 淡出次数
        self.fades_in = 0             # 诊断: 淡入次数

    # -- 内部 ----------------------------------------------------------
    def _frames(self, pcm: bytes):
        np = _np()
        if self.bits == 32:
            return np.frombuffer(pcm, dtype="<f4").reshape(-1, self.channels)
        return (np.frombuffer(pcm, dtype="<i2")
                .astype(np.float32).reshape(-1, self.channels) / 32768.0)

    def _bytes(self, arr) -> bytes:
        np = _np()
        if self.bits == 32:
            return arr.astype("<f4").tobytes()
        return (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

    def _is_silent(self, arr) -> bool:
        np = _np()
        if arr.size == 0:
            return True
        return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2))) \
            < self.silent_rms

    @staticmethod
    def _ramp(n: int, rising: bool):
        np = _np()
        # 升余弦: 0->1(rising) 或 1->0(falling), 两端导数为 0, 无拐角
        t = np.linspace(0.0, np.pi, n, dtype=np.float32)
        r = (1.0 - np.cos(t)) * 0.5 if rising else (1.0 + np.cos(t)) * 0.5
        return r[:, None].astype(np.float32)

    # -- 对外 ----------------------------------------------------------
    def push(self, pcm: bytes) -> bytes:
        """喂入新数据, 返回**现在可以安全写入设备**的 PCM(可能为空)。"""
        np = _np()
        if not pcm:
            return b""
        cur = self._frames(pcm)
        now_silent = self._is_silent(cur)
        out_parts: list[bytes] = []

        hold = self._frames(self._hold) if self._hold else None
        if hold is not None and hold.size:
            if self._was_silent and not now_silent:
                # 静音 -> 有声: 暂存的静音原样放, 新数据要淡入
                out_parts.append(self._bytes(hold))
                n = min(self.fade_frames, cur.shape[0])
                cur = cur.copy()
                cur[:n] = cur[:n] * self._ramp(n, rising=True)
                self.fades_in += 1
            elif (not self._was_silent) and now_silent:
                # 有声 -> 静音: 暂存的尾巴必须淡出
                n = min(self.fade_frames, hold.shape[0])
                h = hold.copy()
                if n > 0:
                    h[-n:] = h[-n:] * self._ramp(n, rising=False)
                out_parts.append(self._bytes(h))
                self.fades_out += 1
            else:
                out_parts.append(self._bytes(hold))

        self._was_silent = now_silent
        # 新的尾部暂存起来(等下一块决定淡入/淡出)
        if cur.shape[0] <= self.fade_frames:
            self._hold = self._bytes(cur)
            return b"".join(out_parts)
        head = cur[:-self.fade_frames]
        self._hold = self._bytes(cur[-self.fade_frames:])
        out_parts.append(self._bytes(head))
        return b"".join(out_parts)

    def flush(self) -> bytes:
        """把暂存的尾巴放掉(停止渲染时调用)。"""
        out = self._hold
        self._hold = b""
        return out


_np_cache: list = []


def _np():
    """惰性拿 numpy(本模块多处需要, 统一入口避免重复 import 开销)。"""
    if not _np_cache:
        import numpy as np
        _np_cache.append(np)
    return _np_cache[0]


class LoopbackRenderer:
    """向本机默认渲染端点写数据(用于主电脑把混音结果输出)。"""

    # WASAPI 渲染缓冲时长(100ns 单位的 100 倍万 = 1ms; 这里 100ms)。
    # 默认请求 0 会得到 ~22ms 的缓冲 —— 我们的渲染循环是 Python 轮询
    # (2ms 睡眠 + numpy 拷贝), 偶发调度延迟 >12ms 就 underrun, 共享模式
    # 下 WASAPI 插零/重复样本 → 每秒几十次的均匀小爆音。
    # 100ms 缓冲只是**抗调度抖动的余量**: 实际延迟由"写多少播多少"的消费
    # 逻辑决定, 不随缓冲变大而增加。
    BUFFER_DURATION_HNS = 1_000_000  # 100ms

    def __init__(self) -> None:
        self._com_inited = False
        self._ac = None
        self._rc = None
        self._rc_raw = None      # GetService 返回的 IUnknown: 必须持有防 GC
        self._dev = None
        self._thread = None      # start() 所在线程: COM 线程亲和
        self.rate = 0
        self.channels = 0
        self.bits = 0
        self.block_align = 0
        self.buffer_frames = 0
        # ★ 上一轮没写完的 PCM 尾部(见 write 的说明: **绝不丢弃数据**)。
        #   WASAPI 的 GetCurrentPadding 是近似值, avail 常比实际少 1~2 帧,
        #   直接丢弃会造成波形微断裂 -> 可听沙沙声, 且高水位时更明显。
        self._pending: bytes = b""
        self._pending_carry = 0      # 发生"写不完"的次数(诊断)

    def start(self) -> None:
        from pycaw.api.audioclient import IAudioClient
        from pycaw.utils import AudioUtilities

        # 同 LoopbackCapture: 必须与 write() 在同一线程
        self._thread = threading.current_thread()
        CoInitialize()
        self._com_inited = True

        dev = AudioUtilities.GetSpeakers()
        ac = dev._dev.Activate(IAudioClient._iid_, CLSCTX_ALL, None).QueryInterface(
            IAudioClient
        )
        mix = ac.GetMixFormat()
        fmt = mix.contents
        self.rate = fmt.nSamplesPerSec
        self.channels = fmt.nChannels
        self.bits = fmt.wBitsPerSample
        self.block_align = fmt.nBlockAlign

        zero = GUID("{00000000-0000-0000-0000-000000000000}")
        ac.Initialize(AUDCLNT_SHARED, 0,
                      self.BUFFER_DURATION_HNS, 0, mix, zero)
        self.buffer_frames = ac.GetBufferSize()
        out = ac.GetService(ctypes.byref(IID_IAudioRenderClient))
        # 同 LoopbackCapture: 必须持有 out 引用, 否则悬垂指针
        self._rc_raw = out
        self._rc = POINTER(IAudioRenderClient)(
            ctypes.cast(out, ctypes.c_void_p).value
        )
        self._ac = ac
        self._dev = dev
        ac.Start()

    def write(self, pcm: bytes) -> int:
        """写入 PCM(必须与本机 mix format 一致), 返回写入帧数。

        ★★★ 绝不丢弃尾部数据(实测杂音源之一):
          旧实现 `n = min(frames, avail)` 只写入 n 帧, **剩下的直接丢**:
              ctypes.memmove(ptr, pcm[:n*block_align], ...)
          而 `avail = buffer_frames - GetCurrentPadding()` 是**近似值**
          (WASAPI 的 padding 有采样延迟/取整), 实际可用空间经常比它
          多 1~2 帧。于是每轮都可能丢掉几个样本 —— 波形被切断一小段,
          听感是"细小沙沙声", 而且**只在高水位时明显**(那时每轮送来的
          帧数多, 越界概率大)。
          修法: 把 remainder 暂存起来, 下一轮**优先写它**。
          这样任何数据都不会丢, 只是延后一轮写入。
        """
        if self._rc is None:
            return 0
        # 先把上一轮没写完的尾部接上
        if self._pending:
            pcm = self._pending + pcm
            self._pending = b""
        frames = len(pcm) // self.block_align
        if frames == 0:
            return 0
        pad = self._ac.GetCurrentPadding()
        avail = self.buffer_frames - pad
        n = min(frames, avail)
        if n <= 0:
            # 一点空间都没有: 整段留到下一轮(不能丢)
            self._pending = pcm
            return 0
        ptr = self._rc.GetBuffer(n)
        ctypes.memmove(ptr, pcm[: n * self.block_align], n * self.block_align)
        self._rc.ReleaseBuffer(n, 0)
        # ★ 没写完的尾部留到下一轮, 绝不丢弃
        if n < frames:
            self._pending = pcm[n * self.block_align:]
            self._pending_carry += 1
        return n

    def available_frames(self) -> int:
        if self._ac is None:
            return 0
        return self.buffer_frames - self._ac.GetCurrentPadding()

    def stop(self) -> None:
        ac, self._ac = self._ac, None
        if ac is not None:
            try:
                ac.Stop()
            except Exception:  # noqa: BLE001
                pass
        # 不清 self._rc, 不 CoUninitialize —— 渲染线程可能仍在跑


class Resampler:
    """线性插值重采样, 用于抵消两机晶振偏差(典型 10~100ppm)。

    100ppm 不补偿时, 每 6 分钟累积 6ms 偏移, 会造成周期性爆音。
    这里只做微调(ratio 接近 1.0), 线性插值足够; 大比例重采样请用
    scipy.signal.resample_poly。
    """

    def __init__(self, channels: int, block_align: int, bits: int):
        self.channels = channels
        self.block_align = block_align
        self.bits = bits
        self._buf = None         # 持久样本缓冲(帧, 通道) — 见 process docstring
        self._pos = 0.0          # 缓冲内的浮点读位置(帧)
        # 累积"因 max_out 预算不足而未产出"的样本量(诊断用)。
        # 若它持续增大, 说明**上游给的单轮预算长期小于可产出量** ——
        # 那会让内部缓冲缓慢增长, 应在 server._render_loop 的 budget
        # 计算里修正(见 process() 里 ★★★ 的说明)。
        self._starved_frames = 0.0
        # 诊断: 内部积压峰值 / 应急排空次数 / 硬回退次数
        #   backlog_max    : 内部缓冲历史最大帧数(正常应只有 ~100 帧)
        #   backlog_drains : 触发"应急排空"(忽略预算先泄压)的次数
        #   backlog_resets : 触发"硬回退"(丢最旧前缀)的次数 —— 应为 0
        self._backlog_max = 0
        self._backlog_drains = 0
        self._backlog_resets = 0
        try:
            import numpy as np

            self._np = np
        except ImportError:
            self._np = None

    @property
    def backlog_frames(self) -> int:
        """当前内部积压帧数(供水位/GUI 诊断)。"""
        return len(self._buf) if self._buf is not None else 0

    def diag(self) -> dict:
        return {
            "backlog": self.backlog_frames,
            "backlog_max": self._backlog_max,
            "drains": self._backlog_drains,
            "resets": self._backlog_resets,
            "pos": round(float(self._pos), 3),
        }

    def _to_frames(self, pcm: bytes):
        if self._np is None:
            raise RuntimeError("Resampler 需要 numpy")
        if self.bits == 32:
            return self._np.frombuffer(pcm, dtype="<f4").reshape(-1, self.channels)
        if self.bits == 16:
            return (
                self._np.frombuffer(pcm, dtype="<i2")
                .astype(self._np.float32)
                .reshape(-1, self.channels)
                / 32768.0
            )
        raise ValueError(f"Resampler 仅支持 16/32bit, got {self.bits}")

    def _from_frames(self, frames) -> bytes:
        if self.bits == 32:
            return frames.astype("<f4").tobytes()
        if self.bits == 16:
            return (
                (self._np.clip(frames, -1.0, 1.0) * 32767.0)
                .astype("<i2")
                .tobytes()
            )
        raise ValueError(self.bits)

    def process(self, pcm: bytes, ratio: float, max_out: int | None = None) -> bytes:
        """流式微重采样: ratio > 1 表示"读得更快"(产出变少), 用于消耗多余缓冲。

        max_out: 本次产出的帧数上限(渲染缓冲可写帧数)。内部缓冲会把
                 产出不了的样本留到下次, 保证不重不漏且永不超预算。

        ★ 实现为**样本精确的持久缓冲**: 新样本追加到内部缓冲, 读指针
          `_pos` 以浮点帧为单位连续推进, 消费过的前缀随即丢弃。

        旧实现(prev/cur 双块拼接 + `_pos` 负值钳 0)有两个真实缺陷, 听感
        为持续小爆音:
          1. 第一包作"铺垫"不输出, 但它的帧仍会被下一包重读(一次性偏移);
          2. 块大小变化时 `_pos` 会变负, 钳到 0 后 cur 开头一段被**重复
             输出** —— 每次块边界处一次波形跳变, 正是用户听到的杂音。
        新设计没有铺垫概念, 也没有负位置, 任意块大小序列下输出严格
        不重不漏(见 tests/test_resampler_stream.py 的斜坡/正弦连续性测试)。
        """
        if not pcm:
            return b""
        np = self._np
        cur = self._to_frames(pcm)

        if self._buf is None:
            self._buf = cur.copy()
            self._pos = 0.0
        else:
            self._buf = np.concatenate((self._buf, cur))

        # ★★★ 积压保护(必须在计算索引**之前**做, 否则本轮算出的 i0 会
        #   指到裁剪后的缓冲之外 -> IndexError, 实测踩到)。
        #
        #   2026-10-21 重做: 旧实现只有一个档位 —— 一旦超过 500ms 就
        #   `_buf = _buf[-96:]; _pos = 0.0`, 也就是**丢掉 500ms 真实音频
        #   并把读指针清零**(相位参照消失)。那是一次又长又响的断口。
        #
        #   新策略分两级:
        #     ① 积压 > DRAIN_AT(300ms): **应急排空** —— 本轮忽略 max_out,
        #        能产多少产多少, 让调用方(server)的 _render_carry 顺延
        #        到后续轮次。这样积压会自己塌下去, **一个样本都不丢**。
        #     ② 积压 > MAX_BACKLOG(500ms, 说明连排空都救不回来, 上游已
        #        严重异常): 才做硬回退, 但**保留相位参照** —— 丢掉最旧的
        #        前缀同时把 `_pos` 同步前移, 而不是清零; 这样读指针指向的
        #        仍是同一段音频的延续, 只有内容跳过、没有相位突跳。
        BACKLOG_KEEP = 96
        DRAIN_AT = 14400             # ≈300ms @48kHz: 开始应急排空
        MAX_BACKLOG = 24000          # ≈500ms @48kHz: 最后手段
        n_buf = len(self._buf)
        if n_buf > getattr(self, "_backlog_max", 0):
            self._backlog_max = n_buf
        if n_buf > MAX_BACKLOG:
            self._backlog_resets = getattr(self, "_backlog_resets", 0) + 1
            drop = n_buf - BACKLOG_KEEP
            self._buf = self._buf[drop:].copy()
            # ★ 相位连续: 同步前移读指针(清零才会造成相位突跳)
            self._pos = max(0.0, self._pos - drop)
            self._starved_frames = 0.0

        avail = len(self._buf)
        # 留最后一帧作插值右端点; 另保留 KEEP_FRAMES 帧不过度抽取。
        # ★ sinc 抽头半宽 = TAPS/2; 右端要留够整个右翼, 否则末端被零填充
        #   会产生周期性咔哒声。KEEP_FRAMES 取 max(96, 半宽+余量)。
        KEEP_FRAMES = max(96, self._SINC_TAPS // 2 + 32)
        # 「按能力算」本轮最多能产出多少(受剩余样本与 ratio 决定)
        avail_out = int((avail - KEEP_FRAMES - self._pos) / ratio)
        if avail_out <= 0:
            return b""
        # 「按预算算」本轮允许产出多少(受调用方 max_out 决定)。
        # ★ 积压达到应急阈值时**忽略预算**先排空 —— 宁可让调用方多拿一
        #   批(它有 carry 逻辑), 也不要攒到 500ms 后一声不响丢掉半秒音频。
        _draining = avail > DRAIN_AT
        if max_out is None or _draining:
            out_n = avail_out
            if _draining:
                self._backlog_drains = getattr(self, "_backlog_drains", 0) + 1
        else:
            out_n = min(avail_out, max_out)
        if out_n <= 0:
            return b""

        idx = self._pos + np.arange(out_n, dtype=np.float64) * ratio
        i0 = np.floor(idx).astype(np.int64)
        i0 = np.clip(i0, 0, avail - 2)          # 防浮点误差越界
        frac = (idx - i0).astype(np.float32)

        out = self._interp_sinc(i0, frac)

        # ★★★ 预算不足时必须**同步推进读指针**, 不能只记诊断。
        #
        #   实测 bug(diag_sinc_threshold.py): `max_out` 小于可产出量时
        #   重建误差从 -57.9dB 恶化到 **+5dB**(完全失真)。
        #   机制: out_n 被 max_out 卡住 -> 每轮消费的输入帧
        #   (out_n*ratio)少于到达量 -> 缓冲持续积压 -> 抽头窗口永远
        #   贴着缓冲末端 -> 零填充比例失控 -> 波形被截断成噪声。
        #   这也解释了用户观察到的"水位超过某阈值就出现杂音":
        #   水位越高, 积压越多, 末端截断越严重。
        #
        #   ★ 修法尝试记录: 直接 `_pos += skip_n*ratio` 是**错的** ——
        #     那会让下一轮的相位与已产出样本脱节, 误差反而更大
        #     (实测所有 max_out 都变成 +5dB)。
        #     正确做法是让**上游给足预算**(server 侧 budget 已保证
        #     >= 每轮可产出量, 见 exp_budget_matrix 的结论), 这里只
        #     记录诊断; 若真的长期不足, 说明上游有问题, 应由上游修。
        skip_n = avail_out - out_n
        if skip_n > 0:
            self._starved_frames += skip_n * ratio

        # 推进读指针并丢弃已消费前缀。
        #
        # ★★★ 必须保留**抽头余量**(实测 bug: ratio≠1 时每 479 个样本
        #   出现一次 1.35x 过冲, 间距恰好等于块大小 480):
        #   sinc 窗口是 [i0-15, i0+16]。若把 _pos 之前的所有样本都丢掉,
        #   下一轮的 i0 从 0 附近开始时, 窗口左半部分(最多 15 帧)已经
        #   不存在了 —— 被零填充, 重建波形在块边界处产生台阶。
        #   修复: 丢弃到 _pos - TAPS_HALF, 让窗口左翼始终有样本可用。
        self._pos += out_n * ratio
        keep_back = self._SINC_TAPS // 2
        drop = int(self._pos) - keep_back
        if drop > 0:
            self._buf = self._buf[drop:]
            self._pos -= drop
        return self._from_frames(out)

    # --- 插值内核: 加窗 sinc(替代线性插值) ----------------------------
    #
    # ★★★ 为什么必须换掉线性插值(这是"有细小杂音"的根因, 已查证):
    #   线性插值的频响是 sinc²(f), 阻带衰减极差 —— 它是**弱抗镜像**的。
    #   48kHz 下的实际后果: 一个 3.4kHz 的带内音会被调制出 4.6kHz 的
    #   **可听镜像**(见 GopherTrunk《Client-Side Resampling》的实测:
    #   "Linear interpolation is not good enough. It's weak anti-imaging:
    #    a 3.4 kHz in-band tone upsampled to 48 kHz leaks an audible
    #    4.6 kHz image. That's the 'tinny' character."）。
    #   这类镜像在**每一次 ratio 微调**时相位都在变, 于是听起来不是
    #   稳定的失真, 而是"细小的、沙沙的、随水位波动出现又消失"的杂音
    #   —— 与用户描述完全一致。
    #
    # ★ 方案: Blackman 加窗 sinc, **逐相位预计算**系数表。
    #   - 阻带衰减 ~74dB(Blackman 窗), 远超线性插值的 ~-30dB 量级;
    #   - 预计算 N_PHASE 个相位 × TAPS 个抽头, 运行时只做查表 + 点积;
    #   - 每个相位系数**归一化到 DC 增益 1**, 避免不同相位音量起伏
    #     (那会表现为周期性的"嗡"声)。
    #   - 抽头数 16(半宽 8): 在 48kHz 下覆盖约 ±8kHz 过渡带,
    #     计算量可控(每输出帧 16 次乘加, numpy 向量化后整块处理)。
    #
    # ★ 为什么不用 polyphase FIR 分解(更高效的经典做法):
    #   我们的 ratio 是**连续微调**的(±1.5%, 每轮 slew 限幅), 不是
    #   固定有理数比 —— 标准多相分解要求 L/M 固定的整数比。查表式
    #   分数延迟 sinc 正好适配"任意浮点 ratio + 连续相位"的场景。
    _SINC_TAPS = 32          # 抽头数(偶数, 中心在两侧之间)
    _SINC_PHASES = 512       # 相位量化档数
    _KAISER_BETA = 8.6       # Kaiser 窗 β: 旁瓣 ~-90dB, 主瓣可控
    _sinc_table = None       # (PHASES, TAPS) float32, 惰性构建

    @classmethod
    def _build_sinc_table(cls, np):
        """构建 Kaiser 加窗 sinc 系数表, 每行对应一个分数相位。

        ★ 为什么用 Kaiser 窗而不是 Blackman(实测依据):
          Blackman 旁瓣低(-58dB)但**主瓣很宽**, 用在分数延迟插值上时,
          相位变化会让重建波形的过冲达到 32%(实测: 1kHz 正弦在
          ratio=1.002 下最大跳变 0.173, 理论 0.131)。那 32% 过冲
          本身听不出(它在高频), 但说明**过渡带太宽** —— 靠近 Nyquist
          的内容会被过度衰减, 与线性插值相比只是换了种失真。
          Kaiser 窗的 β 可调: β=8.6 时旁瓣约 -90dB, 且主瓣宽度与
          旁瓣是**连续折中**的, 16->32 抽头配合 β=8.6 能把过冲压到
          接近理论步进, 同时阻带仍很深。
        """
        taps = cls._SINC_TAPS
        phases = cls._SINC_PHASES
        half = taps // 2
        beta = cls._KAISER_BETA
        table = np.empty((phases, taps), dtype=np.float32)
        # I0 级数展开(Kaiser 窗需要零阶修正贝塞尔函数, numpy 没有自带)
        def i0(x):
            # 标准级数: I0(x) = sum((x/2)^(2k) / (k!)^2)
            s = np.ones_like(x, dtype=np.float64)
            term = np.ones_like(x, dtype=np.float64)
            for k in range(1, 32):
                term = term * (x / (2.0 * k)) ** 2
                s = s + term
                if np.all(term < 1e-16 * np.maximum(s, 1e-30)):
                    break
            return s

        k = np.arange(taps, dtype=np.float64) - (half - 1)
        denom = i0(np.float64(beta))
        for p in range(phases):
            frac = p / phases
            x = k - frac                       # 与插值位置的距离
            s = np.sinc(x)
            # Kaiser: w = I0(β·sqrt(1-(n/half)²)) / I0(β)
            n = x / half
            inside = np.abs(n) <= 1.0
            arg = beta * np.sqrt(np.maximum(0.0, 1.0 - n * n))
            w = np.where(inside, i0(arg) / denom, 0.0)
            coef = s * w
            # DC 归一化(每相位增益必须为 1, 否则随 ratio 变化产生嗡声)
            total = coef.sum()
            if abs(total) > 1e-12:
                coef = coef / total
            table[p] = coef.astype(np.float32)
        return table

    def _interp_sinc(self, i0, frac):
        """用加窗 sinc 表做分数延迟插值(i0/frac 为 int64/float32 数组)。

        ★★★ 边界处理是最容易出爆音的地方(实测踩到):
          16 抽头半宽 8, 插值位置 i0 的窗口是 [i0-7, i0+8]。
          - 若窗口**左端越界**(i0 < 7): 只能用 0 填充缺失样本。
            生产路径不会发生(缓冲前端总有 KEEP_FRAMES=96 帧余量),
            但进程刚启动、缓冲刚开始填充时会 —— 那时截断会造成一次
            明显爆音。
          - 若窗口**右端越界**(i0+8 >= len): 必须是**上游保证不发生**,
            否则每次块边界都会丢抽头 -> 周期性咔哒声。
            上游用 `avail_out = (avail - KEEP_FRAMES - _pos)/ratio` 且
            KEEP_FRAMES=96 >= 8, 已保证。
          这里对左右越界都做零填充(不 clip 索引), 并把"右端越界"
          记为诊断计数, 便于验证上游保证是否真的成立。
        """
        np = self._np
        cls = Resampler
        if cls._sinc_table is None:
            cls._sinc_table = cls._build_sinc_table(np)
        table = cls._sinc_table
        taps = self._SINC_TAPS
        half = taps // 2
        n_buf = len(self._buf)

        phase = np.clip((frac * self._SINC_PHASES).astype(np.int64),
                        0, self._SINC_PHASES - 1)
        coefs = table[phase]                   # (out_n, taps)

        # 抽头绝对索引: base + [0..taps-1], base = i0 - (half-1)
        base = i0 - (half - 1)
        idx = base[:, None] + np.arange(taps)[None, :]
        valid = (idx >= 0) & (idx < n_buf)
        # 统计右端越界(上游保证应使其为 0; 非 0 说明预算算错了)
        right_over = int(np.count_nonzero(idx[valid.shape[0] - 1:] >= n_buf))
        if right_over:
            self._right_edge_hits = getattr(self, "_right_edge_hits", 0) \
                + right_over
        # ★ 不做 clip, 而是把越界样本"安全索引 + 置零": clip 会把首/尾
        #   样本复制到窗口里(相当于把 DC 值混入), 那才是爆音根源。
        safe = np.clip(idx, 0, n_buf - 1)
        samples = self._buf[safe]              # (out_n, taps, ch)
        samples = samples * valid[:, :, None].astype(samples.dtype)
        out = (samples * coefs[:, :, None]).sum(axis=1)
        return out.astype(np.float32)


class StreamClockTracker:
    """跟踪单路音频流的采样率偏差, 输出微重采样 ratio。

    依据相邻两帧的 ts_us 差与帧数差计算实际到达速率:
        measured_rate = frames * 1e6 / (ts2 - ts1)
    ratio = measured_rate / nominal_rate, 收敛后应稳定在 1 ± ppm。
    """

    def __init__(self, nominal_rate: int, alpha: float = 0.05):
        self.nominal_rate = nominal_rate
        self.alpha = alpha
        self._ratio = 1.0
        self._last_ts: int | None = None
        self._last_frames: int = 0

    def update(self, ts_us: int, frames: int) -> float:
        if self._last_ts is not None:
            dt = ts_us - self._last_ts
            if dt > 0:
                measured = frames * 1_000_000.0 / dt
                inst = measured / self.nominal_rate
                # 只接受合理范围内的瞬时值, 防单点毛刺
                if 0.9 < inst < 1.1:
                    self._ratio = (1 - self.alpha) * self._ratio + self.alpha * inst
        self._last_ts = ts_us
        self._last_frames = frames
        return self._ratio

    @property
    def ratio(self) -> float:
        return self._ratio

class AlignedStreamBuffer:
    """按 server 时钟域时间戳对齐的音频流缓冲。

    解决的问题:
      1. 两机采样率不完全一致(晶振偏差 10~100ppm)。若不补偿, 每 6 分钟
         累积 6ms, 表现为周期性爆音/断流。这里用 StreamClockTracker 估 ratio,
         由 Resampler 做微重采样。
      2. 网络抖动。ts 表示"这帧数据在 server 时钟域下应当被播出的时刻",
         渲染时以本机渲染时钟做基准, 只播 ts <= now + lookahead 的帧,
         从而自动吸收抖动; 水位过低时补静音, 过高时丢弃。

    ts 语义: 数据在**发送端**的 server-时钟域时刻(见 Agent._capture_loop
    用 clock.local_to_remote 打戳)。因此本地流与远端流的 ts 在同一时间轴上,
    可以直接混音。
    """

    # 到达速率统计的窗口长度(秒)。见 note_packet 的说明: 窗口太短则噪声
    # 大于要修正的失配量, 前馈会变成噪声注入。
    RATE_WINDOW_S = 4.0

    def __init__(self, channels: int, block_align: int, bits: int,
                 rate: int, name: str = "stream",
                 target_ms: float = 60.0, min_ms: float = 20.0,
                 max_ms: float = 400.0):
        self.channels = channels
        self.block_align = block_align
        self.bits = bits
        self.rate = rate
        self.name = name
        self._buf: deque[tuple[int, bytes]] = deque()
        self._lock = threading.Lock()
        self._rs = Resampler(channels=channels, block_align=block_align, bits=bits)
        self._tracker = StreamClockTracker(nominal_rate=rate)
        self.target_ms = target_ms
        self.min_ms = min_ms
        self.max_ms = max_ms
        self.frames_in = 0
        self.frames_out = 0
        self.dropped = 0
        self.underruns = 0
        # 到达速率统计(前馈控制依据): 用接收端本地时钟测, 避免被发送端
        # 时钟误差污染 —— 详见 note_packet 的说明。
        self._rate_lock = threading.Lock()
        self._rate_t0: float | None = None
        self._rate_frames = 0
        self._rate_windows = 0
        self._arrival_rate = 0.0

    # -- 写入 ----------------------------------------------------------

    def push(self, ts_us: int, pcm: bytes) -> None:
        """推入一帧 PCM 及其 server-时钟域时间戳。"""
        if not pcm:
            return
        frames = len(pcm) // self.block_align
        if frames == 0:
            return
        with self._lock:
            self._buf.append((ts_us, pcm))
            self.frames_in += frames
            # 水位过高: 丢弃最旧的(宁可丢旧的也别让延迟无限增长)
            max_frames = int(self.rate * self.max_ms / 1000)
            while self._buffered_frames() > max_frames and len(self._buf) > 1:
                self._buf.popleft()
                self.dropped += 1

    # -- 读取 ----------------------------------------------------------

    def _buffered_frames(self) -> int:
        return sum(len(p) for _, p in self._buf) // self.block_align

    def buffered_ms(self) -> float:
        with self._lock:
            return self._buffered_frames() * 1000.0 / self.rate

    def head_lag_ms(self, now_us_server: int) -> float | None:
        """队首包相对当前时刻的"年龄"(毫秒): now - ts。返回 None 表示缓冲为空。

        稳态下 ts 记录采集时刻, 而播出时刻是 ts + target_ms, 所以队首包的
        年龄约等于 target_ms(这正是缓冲深度)。年龄远大于 target_ms 才说明
        时间戳异常(对时被重置、长时间卡顿)。

        用途: 区分两种"pop_aligned 取不出数据"——
          * 队首还没到期(年龄 < target_ms): 抖动缓冲在正常工作, 必须等;
          * 队首年龄已远超 target_ms: 时间戳异常, 此时才该 pop_any 兜底,
            否则会永久静音。
        若不加区分地对两者都用 pop_any, 缓冲会被立刻抽干, 抖动缓冲形同虚设。
        """
        with self._lock:
            if not self._buf:
                return None
            return (now_us_server - self._buf[0][0]) / 1000.0

    @property
    def ratio(self) -> float:
        return self._tracker.ratio

    def pop_aligned(self, now_us_server: int, max_frames: int,
                    lookahead_us: int = 0,
                    ratio_override: float | None = None) -> bytes | None:
        """取一批 PCM(已做微重采样), 供渲染缓冲补位。

        now_us_server: 本机时钟(server 域), 用于**首次对齐**与漂移监测。
        max_frames:     本次最多输出多少帧(渲染缓冲可写字节数)。
        ratio_override: 上层闭环控制器给出的产出比例。

        ★ 调度语义(重要, 曾两度改错):
          早期按 ts 严格调度(只取 ts<=horizon 的帧), 期望"播出时刻=采集
          时刻+target_ms"。但 agent 的发送节拍有 ±10ms 抖动(Event.wait
          受调度影响), 严格 ts 调度会把抖动**透传**成播放流的时间空洞
          —— 每个空洞=一次静音插入=爆音簇(实测簇宽 10~30ms, 每 2~3s 一簇)。
          正确语义: **ts 只负责初始水位校准, 日常取数按 FIFO 水位驱动**
          —— 队列里有就取, 没有才写静音。抖动被 60ms 水位吸收, 不透传。
          漂移(两机时钟速率差)由 ratio 闭环修正, 与取数解耦。
        """
        with self._lock:
            if not self._buf:
                self.underruns += 1
                return None
            horizon = now_us_server + lookahead_us - int(self.target_ms * 1000)
            # 取出的帧数受 max_frames **严格**限制, 从队首连续取。
            # ★ 预算必须按**重采样后的产出帧数**计, 而不是输入帧数:
            #   ratio<1 时产出 = 输入/ratio > 输入, 若按输入取,
            #   产出会超出 max_frames, write() 只写前 max_frames 帧,
            #   超出的尾部(已从队列摘除)被静默丢弃 → 周期性爆音
            #   (实测 6.4 处/s, 恰为渲染轮询频率)。
            #   输入预算 = max_frames × ratio(保守取 ratio≥0.99 的下界)。
            eff_ratio = ratio_override if ratio_override is not None \
                else self._tracker.ratio
            input_budget = int(max_frames * max(0.9, eff_ratio))
            out_frames = 0
            chunks: list[bytes] = []
            while self._buf and out_frames < input_budget:
                ts, pcm = self._buf[0]
                n = len(pcm) // self.block_align
                room = input_budget - out_frames
                if n > room:
                    # 只取够预算的部分, 剩余留在队列里(含原时间戳)
                    keep = room * self.block_align
                    chunks.append(pcm[:keep])
                    self._buf[0] = (ts, pcm[keep:])
                    out_frames += room
                    break
                chunks.append(pcm)
                out_frames += n
                self._buf.popleft()
            if not chunks:
                # ★★★ 区分"真欠载"与"假信号"(2026-10-20 实测定案):
                #   budget=0/1(渲染缓冲满, 本轮不需要数据)时 input_budget=0,
                #   队列**明明有数据**也会走到这里 —— 旧代码把这种情形也计入
                #   underruns, 实测 20 秒虚报 1282 次, 严重污染排障视线。
                #   真欠载 = 队列真空(已在函数入口单独计数)。
                if input_budget <= 0:
                    self.starved_budget = getattr(self, "starved_budget", 0) + 1
                else:
                    self.underruns += 1
                return None

        # 合并成一帧
        if len(chunks) == 1:
            merged = chunks[0]
        else:
            merged = b"".join(chunks)
        n = len(merged) // self.block_align
        self.frames_out += n

        # 漂移补偿: 上层闭环 ratio 优先, 否则用开环估计。
        #
        # ★★★ 2026-10-21 关键修复(周期性爆音的真因, 已用纯正弦实测证实):
        #   **绝不能**把重采样器的产出卡在 max_out(渲染缓冲剩余空间)。
        #
        #   机制: Resampler 内部有持久缓冲。若每轮要求产出的帧数 < 本轮
        #   喂进去的帧数, 内部缓冲就会**持续积压**; 一旦超过 MAX_BACKLOG
        #   (24000 帧 ≈ 500ms), 代码会执行 `_buf = _buf[-96:]; _pos = 0.0`
        #   —— 读指针清零 = **相位突跳** = 一声可听的咔哒。
        #
        #   实测(tools/verify_backlog_reset.py, 440Hz 纯正弦 40 秒):
        #     max_out= 1ms -> 回退 74 次, d2 尖峰 118 个
        #     max_out= 2ms -> 回退 71 次, d2 尖峰 142 个
        #     max_out= 5ms -> 回退 60 次, d2 尖峰  96 个
        #     max_out=10ms -> 回退 40 次, d2 尖峰  80 个
        #     max_out>=20ms 或不限 -> 回退 **0** 次, d2 尖峰 **0** 个,
        #                             d2 最大 = 0.00083(正弦理论极限)
        #   生产里 budget 常只有 2~10ms(渲染缓冲接近满时), 于是周期性发作
        #   —— 与用户"周期性、每次持续 1~10 秒"的描述吻合: 积压到 500ms
        #   才回退一次, 回退前的那段时间波形都是错的。
        #
        #   修法: 让 Resampler **能产多少就产多少**, 多余的帧交给调用方的
        #   `_render_carry` 顺延到下一轮(server._render_loop 已有该逻辑,
        #   不丢样本)。这样内部缓冲永远不积压, 相位连续, 从根本上消除咔哒。
        ratio = (ratio_override if ratio_override is not None
                 else self._tracker.ratio)
        # ★★★ 2026-10-21 第二处关键修复: **永远走重采样器, 绝不旁路**。
        #
        #   旧代码是 `if abs(ratio-1.0) > 1e-5: 走重采样; 否则 return merged`。
        #   而生产 ratio 长期在 1.0 附近微调(实测 0.99969~1.00231), 会**反复
        #   跨过 1e-5 这个阈值**, 于是两条路径来回切换:
        #     - 走重采样时, 它内部有持久缓冲 + 浮点读指针;
        #     - 旁路时把原始 merged 直接返回, 内部缓冲**不再被消费**;
        #   再切回来时, 内部还压着旁路期间没消费的旧样本, 与新数据错位,
        #   叠加 `_pos` 的相位差 -> **相位突跳 = 咔哒**(实测纯正弦 40 秒
        #   33 处, 间隔不规则, 与用户"周期性、不固定"完全吻合)。
        #
        #   修法: 无条件经过 Resampler。ratio=1.0 时它等价于直通(且不产生
        #   任何虚的相位偏移, 见 stress_resampler 的 ratio=1.0 用例: d2 尖峰
        #   0 个), 代价只是一次 32 抽头点积, 可忽略。
        #
        #   ⚠ max_out 仍按**调用方预算**传入: pop_aligned 的契约是"本轮产出
        #     不超过 max_frames", 上层(server._render_loop)依赖它算写入量。
        #     曾试过传 None(让重采样器倾巢而出), 实测水位/ratio 环路失稳
        #     (水位冲到 180ms、ratio 撞下限), 因此不采用。
        out = self._rs.process(merged, ratio, max_out=max_frames)
        return out if out else None

    def pop_any(self, max_frames: int) -> bytes | None:
        """不看时间戳, 直接取(用于启动阶段/低水位兜底)。

        同样**严格**遵守 max_frames: 超出部分留在队列里, 不能摘除后丢弃。
        """
        with self._lock:
            if not self._buf:
                return None
            chunks: list[bytes] = []
            frames = 0
            while self._buf and frames < max_frames:
                ts, pcm = self._buf[0]
                n = len(pcm) // self.block_align
                room = max_frames - frames
                if n > room:
                    keep = room * self.block_align
                    chunks.append(pcm[:keep])
                    self._buf[0] = (ts, pcm[keep:])
                    frames += room
                    break
                chunks.append(pcm)
                frames += n
                self._buf.popleft()
        merged = b"".join(chunks) if len(chunks) > 1 else chunks[0]
        self.frames_out += len(merged) // self.block_align
        return merged

    def trim_to(self, target_ms: float, max_skip_ms: float = 2.0) -> int:
        """把水位削回目标值的**平滑滑窗**(每次最多跳过约 max_skip_ms)。

        ★★★ 为什么不能一次性大段丢弃(用户实测的关键证据):
          用户报告: "起初水位会持续增长到 390ms, 并在超过 300ms 时出现
          爆音。后来限制它最多涨到 240ms 左右, 然后超过 200ms 出现爆音。
          现在限制到不超过 85, 于是超过 80ms 出现爆音。"
          —— **爆音阈值始终紧跟水位上限**。这证明噪音**来自执行水位
          上限的机制本身**, 而不是重采样/插值/积压。

          旧实现从队首 `popleft()` 整包丢弃, 一次可能削掉几十毫秒音频
          —— 波形被硬切一刀, 听感就是爆音/咔哒。触发频率 = 水位越上限
          的频率, 所以阈值总跟着上限走。

        ★ 新做法: 每次只丢 **max_skip_ms 以内**的量(默认 2ms ≈ 1~2 个包),
          由渲染循环**每轮调用一次**。这样:
            * 每次的波形断裂极小(2ms 的接缝, 听感接近无缝);
            * 削水位变成"持续小步滑动", 而不是"很久一次大跳";
            * 仍然能保证水位不越过上限(每轮削 2ms, 而每轮只播 2ms,
              净效果足以把多出的部分追回来)。

        ★ 为什么这样能真正压住水位: 渲染循环每秒约 500 轮, 每轮最多削
          2ms -> 理论上限削 1000ms/s, 远大于任何实际积压速率。
          即使每轮只触发一小部分, 也足以稳定。
        """
        keep_frames = int(self.rate * target_ms / 1000)
        cap_frames = max(1, int(self.rate * max_skip_ms / 1000))
        with self._lock:
            total = self._buffered_frames()
            if total <= keep_frames:
                return 0
            # ★ 关键: 本轮**最多**丢 cap_frames, 绝不一次削到底。
            excess = total - keep_frames
            budget = min(excess, cap_frames)
            dropped = 0
            while self._buf and dropped < budget:
                ts, pcm = self._buf[0]
                n = len(pcm) // self.block_align
                want = budget - dropped
                if n > want:
                    # 只消费这个包的前 want 帧, 剩余留在队首(相位连续)
                    keep = want * self.block_align
                    self._buf[0] = (ts, pcm[keep:])
                    dropped += want
                    break
                self._buf.popleft()
                dropped += n
            self.dropped += dropped
            return dropped

    def note_packet(self, ts_us: int, frames: int) -> None:
        """外部喂入采样率观测点(由 AudioReceiver 每帧调用)。"""
        self._tracker.update(ts_us, frames)
        # ★ 同时维护"按本地时钟计的到达速率" —— 这是前馈控制的依据。
        #   仅凭包内 ts 差算速率会被发送端时钟误差污染; 用接收端本地
        #   时钟统计"每秒实际到多少帧"才是闭环/前馈真正需要的信息。
        now = time.monotonic()
        with self._rate_lock:
            if self._rate_t0 is None:
                self._rate_t0 = now
                self._rate_frames = 0
                return
            self._rate_frames += frames
            span = now - self._rate_t0
            # ★ 用**长窗口**(默认 4s)统计到达率。
            #   1s 窗口的读数噪声高达 ±0.7%(实测 47919~48625), 比需要修正
            #   的失配量(通常 <0.5%)还大 —— 拿它做前馈等于把噪声注入
            #   读取速率, 表现为 ratio 持续小幅乱跳(音调抖动)。
            #   窗口拉长到 4s 后噪声按 √N 下降到 ±0.35%, 再叠加 EWMA 平滑
            #   (α=0.3)可到 ±0.1% 量级, 足以支撑前馈。
            if span >= self.RATE_WINDOW_S:
                inst = self._rate_frames / span
                # ★★★ 2026-10-21: **钳制到合理范围**再喂养前馈。
                #   实测启动阶段的窗口读数只有 26000~42000(远低于 48000):
                #   连接握手、采集线程起来、首批缓冲回填都会让前 1~2 个
                #   窗口严重偏低。若不钳制, ratio_ff 会被拖到 0.55, 而 ratio
                #   下限是 0.985 -> 控制器"故意放慢消费" -> 水位降不下来,
                #   一直压在削波阈值上 -> 持续丢样本 -> 持续爆音, 几十秒后
                #   读数收敛才恢复(正是用户描述的"刚连接时离谱、随后好转")。
                #   设备晶振失配实际不可能超过 ±2%, 超出部分一律视为测量噪声。
                lo, hi = 0.98 * self.rate, 1.02 * self.rate
                inst = min(max(inst, lo), hi)
                if self._rate_windows >= 1:
                    self._arrival_rate = (0.7 * self._arrival_rate + 0.3 * inst
                                          if self._arrival_rate > 0 else inst)
                else:
                    # 第一个窗口仍然丢弃: 它最容易受启动突发影响。
                    pass
                self._rate_windows += 1
                self._rate_t0 = now
                self._rate_frames = 0

    @property
    def arrival_rate(self) -> float:
        """本机时钟实测的到达帧率(帧/秒); 未知时返回 0。"""
        with self._rate_lock:
            return self._arrival_rate

    def stats(self) -> dict:
        return {
            "name": self.name,
            "buffered_ms": round(self.buffered_ms(), 1),
            "frames_in": self.frames_in,
            "frames_out": self.frames_out,
            "dropped": self.dropped,
            "ratio": round(self._tracker.ratio, 6),
            "arrival_rate": round(self.arrival_rate, 1),
        }


class Mixer:
    """多路 PCM 混音(32bit float 或 16bit int, 按帧对齐)。"""

    def __init__(self, channels: int, block_align: int, bits: int):
        self.channels = channels
        self.block_align = block_align
        self.bits = bits
        self._bytes = frame_bytes_per_sample(bits) if bits in (16, 24, 32) else 4

    def mix(self, streams: list[bytes]) -> bytes:
        """streams: 每路一帧 PCM(长度必须一致且为 block_align 整数倍)。"""
        if not streams:
            return b""
        n = min(len(s) for s in streams)
        if n == 0:
            return b""
        n -= n % self.block_align
        if n == 0:
            return b""
        try:
            import numpy as np

            if self.bits == 32:
                acc = np.zeros(n // 4, dtype=np.float32)
                for s in streams:
                    acc += np.frombuffer(s[:n], dtype="<f4")
                acc = np.clip(acc, -1.0, 1.0)
                return acc.astype("<f4").tobytes()
            elif self.bits == 16:
                acc = np.zeros(n // 2, dtype=np.int32)
                for s in streams:
                    acc += np.frombuffer(s[:n], dtype="<i2").astype(np.int32)
                acc = np.clip(acc, -32768, 32767)
                return acc.astype("<i2").tobytes()
            else:
                # 24bit / 其它: 退化为逐个样本 int 累加(慢路径, 仅正确性保证)
                out = bytearray(n)
                step = self._bytes
                for i in range(0, n, step):
                    total = 0
                    for s in streams:
                        chunk = s[i : i + step]
                        total += int.from_bytes(chunk, "little", signed=True)
                    limit = (1 << (self.bits - 1)) - 1
                    total = max(-limit - 1, min(limit, total))
                    out[i : i + step] = total.to_bytes(step, "little", signed=True)
                return bytes(out)
        except ImportError:
            # 无 numpy: 逐样本 int 混音
            out = bytearray(n)
            step = self._bytes
            for i in range(0, n, step):
                total = 0
                for s in streams:
                    chunk = s[i : i + step]
                    total += int.from_bytes(chunk, "little", signed=True)
                limit = (1 << (self.bits - 1)) - 1
                total = max(-limit - 1, min(limit, total))
                out[i : i + step] = total.to_bytes(step, "little", signed=True)
            return bytes(out)
