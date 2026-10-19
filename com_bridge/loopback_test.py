"""单机端到端自环测试。

在一台机器上同时跑 server + agent(连 127.0.0.1), 验证:
  1. UDP 信标发现 + TCP 控制通道 + 时钟同步
  2. agent loopback 采集 -> 发送 -> server 接收 -> 时钟对齐 -> 混音 -> 渲染
  3. 延迟统计(采集->播出)

由于两台角色跑在同一进程, 采集的都是同一块声卡, 所以渲染输出会被
server 自己的 loopback 再次捕获 —— 这是预期的回声, 测试只统计帧数
与延迟, 不做主观听音。

用法:
    python -m com_bridge.loopback_test [--seconds 10]
"""

from __future__ import annotations

import argparse
import threading
import time

from .agent import Agent
from .clock import now_us
from .server import Server


def run(seconds: float = 10.0, jitter_ms: float = 60.0,
        tone_hz: float = 440.0) -> int:
    srv = Server(name="loopback-server", port=24812, side="right",
                 jitter_ms=jitter_ms)
    srv.start()
    time.sleep(0.5)

    ag = Agent(server_host="127.0.0.1", name="loopback-agent",
               mute_local=False, discover_timeout_s=1.0,
               control_port=24812, tone_hz=tone_hz,
               # 单机自环: server 与 agent 同进程, 会争抢 INPUT_PORT
               # (独占绑定下必然失败), 且单机上回传毫无意义
               enable_return=False)
    ag.start()

    print(f"[test] 运行 {seconds}s ...", flush=True)
    t0 = time.monotonic()
    last = 0
    try:
        while time.monotonic() - t0 < seconds:
            time.sleep(1.0)
            d = {**srv.stats}
            peer_rx = srv._audio_rx.frames_received if srv._audio_rx else 0
            peer_lost = srv._audio_rx.lost if srv._audio_rx else 0
            peerbuf = srv._peer.buffered_ms() if srv._peer else -1.0
            print(
                f"[test] t+{time.monotonic() - t0:4.0f}s "
                f"peer={d['peer_frames']} "
                f"rendered={d['rendered_frames']} silence={d.get('silence_frames', 0)} "
                f"udp_rx={peer_rx} udp_lost={peer_lost} "
                f"peerbuf={peerbuf:.0f}ms "
                f"ratio={srv._peer.ratio if srv._peer else 0:.6f}",
                flush=True,
            )
            last = d["rendered_frames"]
    finally:
        ag.stop()
        srv.stop()

    # 完整性核对:
    #   silence 起步阶段一次性填充, 之后应只随取不到数据而缓慢增长
    #   popped(缓冲交出的声音帧) 应约等于 rendered - silence
    #   peerbuf(缓冲内未播帧) 应稳定在 jitter 目标附近的一倍以内
    frames_out = srv._peer.frames_out if srv._peer else 0
    live_frames = last - srv.stats.get("silence_frames", 0)
    ok = (last > 100000 and srv.stats["peer_frames"] > 100
          and abs(frames_out - live_frames) < 40000)
    print(
        f"[test] {'PASS' if ok else 'FAIL'}: rendered={last} "
        f"peer_packets={srv.stats['peer_frames']} popped={frames_out} "
        f"live={live_frames} silence={srv.stats.get('silence_frames', 0)} "
        f"dropped={srv._peer.dropped if srv._peer else -1}",
        flush=True,
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="com-bridge-loopback-test")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--jitter-ms", type=float, default=60.0)
    p.add_argument("--tone-hz", type=float, default=440.0,
                   help="agent 发送的测试音频率; 0 = 用真实 loopback 采集")
    args = p.parse_args(argv)
    return run(args.seconds, args.jitter_ms, args.tone_hz)


if __name__ == "__main__":
    raise SystemExit(main())
