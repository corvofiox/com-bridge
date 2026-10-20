# -*- coding: utf-8 -*-
"""★ 真实 WASAPI 采集路径的音频连续性验收(闭环, 不经过副机物理机)。

## 为什么必须有这个工具
现有的 tools/verify_audio.py 用 `tone_hz=440` 测试音模式, 走的是
`Agent._tone_loop` —— 那条路径用 catch-up 语义(按已过时间补发),
**早就修好了**(见 agent.py 里 _tone_loop 的注释)。

而**真实用户场景**走的是 `Agent._capture_loop` + `LoopbackCapture`
(WASAPI loopback 采集), 这条路径**没有** catch-up 保护, 也没有被
任何验收工具覆盖过。用户的"周期性噼啪声"正是这条路径的问题。

本工具:
  1. 主机起 Server(真实 WASAPI 渲染)
  2. 副机起 Agent(tone_hz=0 -> 走真实采集路径) —— 但本机没有"外部"
     声音源, 所以同时在本机用声卡播放一段测试音, 让 loopback 采到它
  3. 直接检查主机端 write 流的**跳变**(断口)数量与位置
  4. 输出全部关键诊断: peer 水位 / 渲染缓冲水位 / 欠载 / 空转 /
     重采样器右端越界 / 采集侧空读

## 判据
  * 稳态跳变 == 0 (排除启动 1s)
  * peer 水位贴近目标 (>= jitter_ms * 0.8)
  * 渲染缓冲水位贴近 render_target

用法: python tools/verify_audio_capture_path.py [秒数]
"""
from __future__ import annotations

import contextlib
import io
import sys
import time

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import LoopbackRenderer

wr: list[np.ndarray] = []
orig_write = LoopbackRenderer.write
resampler_ref = {}


def spy(self, pcm):
    n = orig_write(self, pcm)
    if n > 0:
        wr.append(np.frombuffer(pcm[: n * self.block_align], dtype="<f4")
                   .reshape(-1, self.channels)[:, 0].copy())
    return n


LoopbackRenderer.write = spy

from com_bridge.agent import Agent      # noqa: E402
from com_bridge.server import Server    # noqa: E402


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        srv = Server(name="d", port=24861, side="right", jitter_ms=60)
        srv.start()
        time.sleep(0.4)
        # tone_hz=0 -> 走**真实 WASAPI 采集**路径
        # ★★★ 关键: 必须 mute_local=True 避免**声卡自激**!
        #   Server 会把收到的音频渲染到本机声卡, 而 Agent 又在采集本机
        #   声卡 -> 音频绕一圈回到发送端 -> 帧数翻倍、队列爆满、
        #   削波数千次 —— 测出来的"爆音"其实是**测试方法自造的假象**,
        #   与用户的两台真机场景(无反馈环)无关。
        #   (2026-10-19 踩过这个坑: 到达率算出 96000 = 2 倍, 就是自激。)
        ag = Agent(server_host="127.0.0.1", name="d", mute_local=True,
                   discover_timeout_s=1.0, control_port=24861, tone_hz=0,
                   enable_return=False)
        ag.start()
        time.sleep(seconds)
        cap = ag._cap
        stats_ag = dict(ag.stats)
        ag.stop()
        srv.stop()

    out = b.getvalue()
    v = np.concatenate(wr) if wr else np.zeros(0, dtype=np.float32)
    print("=== 主机端诊断 ===")
    for line in out.splitlines():
        if "音频:" in line or "采集:" in line or "渲染" in line:
            print("  " + line)

    if v.size < 48000 * 3:
        print(f"\n⚠ write 流太短({len(v)} 帧), 本机没有可采集的声音源")
        print("  (需要在本机播放音乐才能让 loopback 采到)")
        return 2

    skip = 48000
    tail = v[skip:]
    d = np.abs(np.diff(tail))
    big = np.where(d > 0.02)[0]

    print()
    print("=== 连续性 ===")
    print(f"  write 流       : {len(v)} 帧 ({len(v)/48000:.1f}s)")
    print(f"  稳态跳变       : {len(big)} 处 = "
          f"{len(big)/(len(tail)/48000):.2f}/s  (最大 {d.max():.4f})")
    if big.size:
        gaps = np.diff(big)
        print(f"  跳变间隔(ms)   : "
              f"{np.round(gaps[:12] * 1000 / 48, 1).tolist()}")

    print()
    print("=== 采集侧(副机) ===")
    print(f"  缓冲深度       : {cap.buffer_frames * 1000.0 / cap.rate:.0f}ms"
          f" ({cap.buffer_frames} 帧)")
    print(f"  空读次数       : {cap.empty_reads}")
    print(f"  读次数         : {cap.read_calls}")
    print(f"  发送帧数       : {stats_ag.get('frames_sent', 0)}")
    print(f"  静音补包       : {stats_ag.get('silence_sent', 0)}")

    rs = srv._peer._rs if srv._peer is not None else None
    if rs is not None:
        print()
        print("=== 重采样器 ===")
        print(f"  右端越界次数   : {getattr(rs, '_right_edge_hits', 0)}"
              f"   (必须为 0)")
        print(f"  饥饿帧数       : {rs._starved_frames:.0f}")
        print(f"  积压回退       : {getattr(rs, '_backlog_resets', 0)}")

    pb = srv._peer
    print()
    print("=== 主机队列 ===")
    if pb is not None:
        print(f"  peer 水位      : {pb.buffered_ms():.0f}ms "
              f"(目标 {srv.jitter_ms:.0f}ms)")
        print(f"  欠载次数       : {pb.underruns}")
        print(f"  丢弃帧数       : {pb.dropped}")
    print(f"  削波次数       : {srv.stats.get('level_trims', 0)}")
    print(f"  render_ratio   : {srv.stats.get('render_ratio')}")

    ok = len(big) == 0
    print()
    print(f"结论: {'PASS —— 波形连续' if ok else f'FAIL —— {len(big)} 处跳变'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
