# -*- coding: utf-8 -*-
"""长时间监控主机输出，检测**可听断口**（针对真实音乐）。

固定 d2 阈值对音乐无效(音乐自身 d2 可达 0.2+)。本工具改用三种
**特异性判据**，它们只在"管线出问题"时才会触发：

  1) 零值段(zero-run)   : 音乐中间出现连续全零样本 -> 静音插入/丢块
  2) 块重复(repeat)     : 当前块与上一块**完全相同** -> WASAPI 饥饿时
                          共享模式会重复上一段
  3) 幅度塌陷(dip)      : 局部 RMS 突然掉到前 200ms 均值的 <3% 并持续
                          >=0.5ms -> 断流

任一触发都会记录**时刻 + 类型 + 幅度**，并把前后 0.5s 的原始样本
单独存盘，便于事后精确比对。

用法: python tools/monitor_clicks.py [秒数]
"""
from __future__ import annotations

import os
import sys
import time
import wave

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import LoopbackCapture

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
ZERO_RUN_MIN = 24          # 0.5ms @48k
DIP_RATIO = 0.03
DIP_MIN_SAMPLES = 24
REPEAT_MIN_FRAMES = 96     # 2ms


def main() -> int:
    cap = LoopbackCapture()
    cap.start()
    time.sleep(0.3)
    print(f"[mon] {cap.channels}ch@{cap.rate}Hz 监控 {DUR:.0f}s", flush=True)

    out = os.path.join(os.environ["TEMP"], "cb_monitor.wav")
    wf = wave.open(out, "wb")
    wf.setnchannels(cap.channels)
    wf.setsampwidth(2)
    wf.setframerate(cap.rate)

    prev_block: np.ndarray | None = None
    rms_hist: list[float] = []          # 每块 RMS(用于塌陷判据)
    events: list[tuple[float, str, str]] = []
    total = 0
    t0 = time.monotonic()
    last_report = 0.0
    zero_buf = 0
    dip_buf = 0

    try:
        while time.monotonic() - t0 < DUR:
            r = cap.read(timeout_s=0.05)
            if r is None:
                continue
            pcm, flags, qpc = r
            a = np.frombuffer(pcm, dtype="<f4").reshape(-1, cap.channels)
            left = a[:, 0].astype(np.float32)
            if left.size == 0:
                continue
            now = time.monotonic() - t0
            rms = float(np.sqrt(np.mean(left ** 2)))
            rms_hist.append(rms)
            if len(rms_hist) > 200:
                rms_hist.pop(0)

            # --- 1) 零值段 ---
            z = np.abs(left) < 1e-7
            if z.all():
                zero_buf += left.size
            else:
                if zero_buf >= ZERO_RUN_MIN:
                    events.append((now, "zero-run", f"{zero_buf} samples"))
                zero_buf = 0
                # 块内零段
                idx = np.flatnonzero(np.diff(np.concatenate(([0], z.view(np.int8), [0]))))
                for i in range(0, len(idx), 2):
                    run = idx[i + 1] - idx[i] if i + 1 < len(idx) else 0
                    if run >= ZERO_RUN_MIN:
                        events.append((now, "zero-run", f"{run} samples in block"))

            # --- 2) 块重复 ---
            if prev_block is not None and prev_block.shape == left.shape \
                    and left.size >= REPEAT_MIN_FRAMES:
                if np.array_equal(left, prev_block):
                    events.append((now, "block-repeat", f"{left.size} frames"))
            prev_block = left.copy()

            # --- 3) 幅度塌陷 ---
            if len(rms_hist) >= 50:
                ref = float(np.median(rms_hist[-50:-1]))
                if ref > 0.01 and rms < ref * DIP_RATIO:
                    dip_buf += left.size
                else:
                    if dip_buf >= DIP_MIN_SAMPLES:
                        events.append((now, "amp-dip", f"{dip_buf} samples"))
                    dip_buf = 0

            # 存 WAV
            c = np.clip(a, -1.0, 1.0)
            pcm16 = (c * 32767.0).astype("<i2")
            wf.writeframes(pcm16.tobytes())
            total += left.size

            if now - last_report >= 10.0:
                last_report = now
                print(f"[mon] t={now:5.1f}s 帧={total} rms={rms:.4f} "
                      f"事件={len(events)}", flush=True)
    finally:
        wf.close()
        cap.stop()

    print()
    print("=== 监控结果 ===")
    print(f"  时长   : {total / cap.rate:.1f}s   文件: {out}")
    print(f"  平均RMS: {float(np.mean(rms_hist)) if rms_hist else 0:.4f}")
    print(f"  事件数 : {len(events)}")
    if events:
        from collections import Counter
        print("  分类   :", dict(Counter(k for _, k, _ in events)))
        print("  前 20 条:")
        for t, k, d in events[:20]:
            print(f"    t={t:7.2f}s  {k:14s} {d}")
        gaps = np.diff([t for t, _, _ in events])
        if gaps.size:
            print(f"  事件间隔(秒): {np.round(gaps[:15], 2).tolist()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
