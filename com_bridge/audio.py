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
        ac.Initialize(
            AUDCLNT_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK
            | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
            0, 0, mix, zero,
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
        while True:
            try:
                ptr, frames, flags, devpos, qpc = self._cap.GetBuffer()
            except OSError:
                return None
            if frames == 0:
                self._cap.ReleaseBuffer(0)
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.003)
                continue
            raw = ctypes.string_at(ptr, frames * self.block_align)
            self._cap.ReleaseBuffer(frames)
            return raw, flags, qpc

    def stop(self) -> None:
        ac, self._ac = self._ac, None
        if ac is not None:
            try:
                ac.Stop()
            except Exception:  # noqa: BLE001
                pass
        # 注意: 不清 self._cap, 不 CoUninitialize —— 渲染线程可能仍在跑


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
        """写入 PCM(必须与本机 mix format 一致), 返回写入帧数。"""
        if self._rc is None:
            return 0
        frames = len(pcm) // self.block_align
        if frames == 0:
            return 0
        pad = self._ac.GetCurrentPadding()
        avail = self.buffer_frames - pad
        n = min(frames, avail)
        if n <= 0:
            return 0
        ptr = self._rc.GetBuffer(n)
        ctypes.memmove(ptr, pcm[: n * self.block_align], n * self.block_align)
        self._rc.ReleaseBuffer(n, 0)
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
        try:
            import numpy as np

            self._np = np
        except ImportError:
            self._np = None

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

        # ★★★ 积压极端保护(必须在计算索引**之前**做, 否则本轮算出的
        #   i0 会指到裁剪后的缓冲之外 -> IndexError, 实测踩到)。
        #   正常生产路径永不触发: 真实渲染器的 budget 足够大, 内部积压
        #   实测只有 97 帧(tools/exp_budget_matrix.py)。只有上游异常
        #   (渲染循环被长时间阻塞、预算持续偏小)才会积压到 500ms。
        #   那时必须回退, 否则每轮 np.concatenate 越来越慢 ->
        #   渲染超时 -> WASAPI 欠载 -> 成段杂音并持续恶化。
        KEEP_FRAMES = 96
        MAX_BACKLOG = 24000          # ≈500ms @48kHz
        if len(self._buf) > MAX_BACKLOG:
            self._backlog_resets = getattr(self, "_backlog_resets", 0) + 1
            self._buf = self._buf[-KEEP_FRAMES:].copy()
            self._pos = 0.0
            self._starved_frames = 0.0

        avail = len(self._buf)
        # 留最后一帧作插值右端点; 另保留 KEEP_FRAMES 帧不过度抽取。
        # 之前掏空到只剩 1 帧: 渲染轮询与网络到达的相位略有错位时, 偶尔
        # 醒来恰好缓冲只剩末端 → out_n=0 → 上层写 1 段静音 → **簇状杂音**
        # (实测 diag: 簇宽 10~30ms, 内含十几次小跳变, 每 2~3s 一簇)。
        # 保留约 2ms 的余量让到达抖动被吸收, 播出延迟只增加 2ms。
        # 「按能力算」本轮最多能产出多少(受剩余样本与 ratio 决定)
        avail_out = int((avail - KEEP_FRAMES - self._pos) / ratio)
        if avail_out <= 0:
            return b""
        # 「按预算算」本轮允许产出多少(受调用方 max_out 决定)
        out_n = avail_out if max_out is None else min(avail_out, max_out)
        if out_n <= 0:
            return b""

        # 诊断: 因预算不足而未产出的量(持续增大说明上游预算偏小,
        # 见本函数开头 ★★★ 的说明)。
        skip_n = avail_out - out_n
        if skip_n > 0:
            self._starved_frames += skip_n * ratio

        idx = self._pos + np.arange(out_n, dtype=np.float64) * ratio
        i0 = np.floor(idx).astype(np.int64)
        i0 = np.clip(i0, 0, avail - 2)          # 防浮点误差越界
        frac = (idx - i0).astype(np.float32)[:, None]

        out = self._buf[i0] * (1.0 - frac) + self._buf[i0 + 1] * frac

        # 推进读指针并丢弃已消费前缀
        self._pos += out_n * ratio
        drop = int(self._pos)
        if drop > 0:
            self._buf = self._buf[drop:]
            self._pos -= drop
        return self._from_frames(out)


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
        # max_out: 渲染缓冲可写帧数 —— Resampler 的产出必须 ≤ 此值,
        # 否则尾部被 write() 丢弃(已从队列摘除) → 周期性爆音。
        ratio = (ratio_override if ratio_override is not None
                 else self._tracker.ratio)
        if abs(ratio - 1.0) > 1e-5:
            out = self._rs.process(merged, ratio, max_out=max_frames)
            return out if out else None
        return merged

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

    def trim_to(self, target_ms: float) -> int:
        """主动弃帧: 把水位削回 target_ms, 返回丢弃的帧数。

        ★ 与 push() 里的被动丢弃的区别(这决定了听感):
          push() 的丢弃是**按包**丢, 且丢在最老的一端 —— 当水位触到
          max_ms 时它已经在持续丢包, 每一次都在数据流中间切一刀,
          听感是"突然一段杂音/卡顿"(用户实测的周期杂音特征之一)。
          本方法由**上层按需调用**, 它在水位达到阈值时**一次性**削
          一大段, 之后相当长时间都不再触发 —— 把多次小断裂换成
          一次极短空隙, 听感明显更干净。

        只保留最新的一批数据(丢弃最旧的), 因为音频直播场景下
        "旧数据"没有价值, 播放它只会让延迟越来越大。
        """
        keep_frames = int(self.rate * target_ms / 1000)
        with self._lock:
            total = self._buffered_frames()
            if total <= keep_frames:
                return 0
            dropped = 0
            while self._buf and self._buffered_frames() > keep_frames:
                pcm = self._buf.popleft()[1]
                dropped += len(pcm) // self.block_align
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
                # 丢弃第一个完整窗口: 起播时 agent 会预发一批数据把水位
                # 垫满, 那一瞬间的到达率会虚高, 采信它会把队列抽干。
                if self._rate_windows >= 1:
                    inst = self._rate_frames / span
                    self._arrival_rate = (0.7 * self._arrival_rate + 0.3 * inst
                                          if self._arrival_rate > 0 else inst)
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
