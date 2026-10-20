"""NTP 风格四时间戳对时 + PI 控制的抖动缓冲目标水位。

模型(server 为时间基准):
    t1 = 从机发送请求的时刻(从机时钟)
    t2 = 主机收到请求的时刻(主机时钟)
    t3 = 主机发送回复的时刻(主机时钟)
    t4 = 从机收到回复的时刻(从机时钟)

    RTT    = (t4 - t1) - (t3 - t2)
    offset = ((t2 - t1) + (t3 - t4)) / 2

**offset 的符号(极易写反, 且单机自环测试永远发现不了)**

把上式在"对称延迟 + 主机比从机快 C"下代入, 得到

    offset = remote_clock - local_clock

即 **对端减本地**, 不是反过来。推导(设单程延迟 d, 主机快 C):

    t1 = T            从机发
    t2 = T + d + C    主机收
    t3 = T + d + C    主机回
    t4 = T + 2d       从机收
    ((t2-t1) + (t3-t4)) / 2 = ((d+C) + (C-d)) / 2 = C   ✓ 恰为 C

因此换算方向是:

    local_to_remote(t) = t + offset     从机时间 -> 主机时间
    remote_to_local(t) = t - offset     主机时间 -> 从机时间

单机自环时 offset ≡ 0, 符号写反毫无症状; 而两台机器的 time.monotonic()
纪元不同(各自的开机时刻), offset 可能是数小时, 一旦写反, 从机打出的
时间戳会偏离 2*offset, 音频缓冲不是永久静音就是疯狂丢弃。改公式前先看
tests/test_clock.py::TestClockSyncFormula 里的固定向量。
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field


def now_us() -> int:
    """单调递增的微秒时钟, 用于所有跨机时间戳与本地间隔。

    ★ 必须用 perf_counter_ns, **不能用 monotonic_ns**:
      实测本机 time.monotonic_ns() 的分辨率只有 **15ms**(Windows 默认时钟
      节拍; 50ms 内只产生 4 个不同值), 而 perf_counter_ns 是微秒级。
      用 15ms 量化的时钟做对时与打戳会连锁破坏两处:
        * rtt 恒为 0.00ms(收发两次采样常落在同一个 tick 上);
        * 漂移跟踪器算出的瞬时比值被量化成 1.33 / 0.67 之类的离谱值,
          全部被"0.9~1.1 才算合理"的过滤挡掉, ratio 永远停在 1.0,
          晶振偏差完全得不到补偿 —— 长时间运行后缓冲缓慢漂移直至爆音。
      perf_counter_ns 同样单调, 换掉后跨机 offset 依然成立(纪元不同不影响
      NTP 式偏移量)。
    """
    return time.perf_counter_ns() // 1000


def enable_high_res_timer() -> None:
    """把本进程的系统定时器分辨率调到 1ms(Windows)。

    Windows 默认节拍是 15.6ms, 影响**一切 sleep/wait/Event.wait 的实际时长**:
    实测 agent 测试音的 20ms 发送间隔被量化成 15.6~31.2ms 抖动, 生产速率
    超出消费速率 25%, 回环测试水位飙到 380ms、丢弃 222 块。
    timeBeginPeriod(1) 是进程级全局开关, 退出时 timeEndPeriod 恢复。

    perf_counter 的**读数**不受节拍影响(它走 QPC), 但所有基于 Event.wait
    / time.sleep 的**节拍调度**都受影响 —— 所以打戳换 perf_counter 之后
    还必须调这一步, 水位才能稳定。
    """
    try:
        winmm = ctypes.windll.winmm  # type: ignore[attr-defined]
        winmm.timeBeginPeriod(1)
    except (AttributeError, OSError):
        pass  # 非 Windows 或调用失败: 退化为系统默认节拍


def disable_high_res_timer() -> None:
    """恢复系统默认定时器分辨率(与 enable_high_res_timer 配对)。"""
    try:
        winmm = ctypes.windll.winmm  # type: ignore[attr-defined]
        winmm.timeEndPeriod(1)
    except (AttributeError, OSError):
        pass


def raise_audio_thread_priority() -> str:
    """把**当前线程**提到音频级优先级(Windows), 返回结果描述。

    ★ 为什么必须做(2026-10-21):
      渲染/采集循环是 Python 轮询线程, 每 2ms 醒一次写一小块。
      只要 GIL 被别的线程(剪贴板/托盘/焦点监控/hider 100ms 保活)占住
      数十毫秒, 本线程就写不进去 -> 声卡缓冲周期性见底 -> 可听爆音。
      用户症状正是"周期性、不固定、每次持续 1~10 秒"。
      把线程优先级提上去, 能让调度器优先给它 CPU(虽不能完全绕开 GIL,
      但能显著缩短被抢占的时间)。
      用 TIME_CRITICAL 有风险(极端情况下会饿死界面), 因此取 HIGHEST。
    """
    try:
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # ★★★ 必须声明返回类型: 默认 restype=c_int 会把 x64 的线程句柄
        #   **截断成 32 位**, SetThreadPriority 拿着错误句柄必然返回 0
        #   (实测就是这么失败的。本项目已被同类 ctypes 问题咬过多次:
        #    重复声明结构体/漏 argtypes, 症状永远很隐晦)。
        kernel32.GetCurrentThread.restype = wintypes.HANDLE
        kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        kernel32.SetThreadPriority.restype = wintypes.BOOL
        THREAD_PRIORITY_HIGHEST = 2
        h = kernel32.GetCurrentThread()
        ok = kernel32.SetThreadPriority(h, THREAD_PRIORITY_HIGHEST)
        return "已提升到 HIGHEST" if ok else "提升失败(SetThreadPriority=0)"
    except (AttributeError, OSError) as exc:  # noqa: BLE001
        return f"提升失败: {exc}"


@dataclass
class ClockSync:
    """单向对时结果。"""

    offset_us: float = 0.0     # remote_clock - local_clock
    rtt_us: float = 0.0
    samples: int = 0
    history: list[tuple[float, float]] = field(default_factory=list)

    def update(self, t1: int, t2: int, t3: int, t4: int) -> None:
        """t1/t4 为本地(从机)时钟, t2/t3 为对端(主机)时钟。

        算出的 offset 是 (对端 - 本地), 详见模块 docstring。
        """
        rtt = (t4 - t1) - (t3 - t2)
        offset = ((t2 - t1) + (t3 - t4)) / 2.0
        # ★★★ 记录 offset 的**跳变幅度**(诊断时钟阶跃 —— 周期性噼啪的头号
        #   嫌疑之一): 时间戳由 local_to_remote(offset) 打出, offset 阶跃
        #   会让相邻包的 ts 突然错位, 服务端 pop_aligned/水位都会跟着跳。
        #   本地闭环测试时钟域相同, 永远复现不了, 必须在真双机上看此计数。
        _prev = getattr(self, "last_offset_us", None)
        if _prev is not None:
            jump = abs(offset - _prev)
            self.max_offset_jump_us = max(
                getattr(self, "max_offset_jump_us", 0.0), jump)
            if jump > 10_000:  # >10ms 的跳变一定会造成可听断口
                self.offset_jumps = getattr(self, "offset_jumps", 0) + 1
        self.last_offset_us = offset
        self.history.append((offset, rtt))
        if len(self.history) > 16:
            self.history.pop(0)
        # 中位数滤波: 抗单次网络毛刺(离群样本不参与中位数)
        offs = sorted(o for o, _ in self.history)
        rtts = sorted(r for _, r in self.history)
        self.offset_us = offs[len(offs) // 2]
        self.rtt_us = rtts[len(rtts) // 2]
        self.samples += 1

    @property
    def synced(self) -> bool:
        return self.samples >= 4

    def local_to_remote(self, local_us: int) -> int:
        """本地(从机)时钟读数 -> 对端(主机)时钟读数。"""
        return int(local_us + self.offset_us)

    def remote_to_local(self, remote_us: int) -> int:
        """对端(主机)时钟读数 -> 本地(从机)时钟读数。"""
        return int(remote_us - self.offset_us)


class JitterBufferController:
    """PI 控制器: 根据网络抖动调整目标缓冲水位。

    error = target_level - actual_level
    目标水位在 [min_ms, max_ms] 之间被 PI 输出驱动;
    实际水位偏离目标时, 通过微重采样(不在此模块)吸收。
    """

    def __init__(self, min_ms: float = 20.0, max_ms: float = 200.0,
                 kp: float = 0.002, ki: float = 0.0002, init_ms: float = 60.0):
        self.min_ms = min_ms
        self.max_ms = max_ms
        self.kp = kp
        self.ki = ki
        self.target_ms = init_ms
        self._integral = 0.0

    def update(self, actual_level_ms: float, jitter_ms: float) -> float:
        """actual_level_ms: 当前缓冲内可播放的毫秒数。
        jitter_ms: 最近测得的网络抖动(RTT 方差)。"""
        # 网络抖动大 -> 抬高水位; 缓冲溢出/欠载 -> 回调
        desired = actual_level_ms + jitter_ms
        desired = max(self.min_ms, min(self.max_ms, desired))
        error = desired - self.target_ms
        self._integral += self.ki * error
        self._integral = max(-50.0, min(50.0, self._integral))
        self.target_ms += self.kp * error + self._integral
        self.target_ms = max(self.min_ms, min(self.max_ms, self.target_ms))
        return self.target_ms
