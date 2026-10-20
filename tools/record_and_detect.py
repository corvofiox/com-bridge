# -*- coding: utf-8 -*-
"""在主机侧录制"用户实际听到的音频"，并在线检测样本级断点(咔哒)。

## 为什么用二阶差分(d2)
一阶差分 |x[n]-x[n-1]| 对音乐不可用: 高频内容本身就有大跳变。
二阶差分 d2 = |x[n] - 2x[n-1] + x[n-2]| 对**平滑信号**近乎为 0, 但对
波形断点(拼接/丢块/重复)会产生巨大尖峰:
    f=2kHz @48k, 幅度 0.14 -> d2 ≈ 0.0096
    一次 0.2 幅度的硬拼接      -> d2 ≈ 0.4   (40 倍差距)
所以阈值 0.05 能干净地把"接缝"从正常音乐里分离出来。

用法: python tools/record_and_detect.py [秒数] [阈值]
"""
from __future__ import annotations

import os
import sys
import time
import wave

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import LoopbackCapture

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 240.0
THRESH = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05


def main() -> int:
    cap = LoopbackCapture()
    cap.start()
    time.sleep(0.3)
    print(f"[rec] {cap.channels}ch @{cap.rate}Hz {cap.bits}bit 录 {DUR:.0f}s "
          f"阈值 d2>{THRESH}", flush=True)

    out_path = os.path.join(os.environ["TEMP"], "cb_heard.wav")
    wf = wave.open(out_path, "wb")
    wf.setnchannels(cap.channels)
    wf.setsampwidth(2)
    wf.setframerate(cap.rate)

    tail = np.zeros(2, dtype=np.float64)     # 上一块最后两个样本(左声道)
    total = 0
    spikes: list[tuple[float, float]] = []
    per_sec_rms: list[float] = []
    sec_acc: list[float] = []
    t0 = time.monotonic()
    last_report = 0.0
    d2_max = 0.0

    try:
        while time.monotonic() - t0 < DUR:
            r = cap.read(timeout_s=0.05)
            if r is None:
                continue
            pcm, flags, qpc = r
            if cap.bits != 32:
                print("!! 端点不是 32bit, 本工具只处理 float32")
                break
            a = np.frombuffer(pcm, dtype="<f4").reshape(-1, cap.channels)
            left = a[:, 0].astype(np.float64)
            if left.size == 0:
                continue

            # d2: 把上一块最后 2 个样本接在前面, 保证跨块边界也被检查
            seq = np.concatenate((tail, left))
            d2 = np.abs(seq[2:] - 2.0 * seq[1:-1] + seq[:-2])
            if d2.size:
                m = float(d2.max())
                if m > d2_max:
                    d2_max = m
                idx = np.where(d2 > THRESH)[0]
                if idx.size:
                    now = time.monotonic() - t0
                    for k in idx[:20]:
                        spikes.append((now, float(d2[k])))
            tail = left[-2:] if left.size >= 2 else tail

            # 存 WAV(16bit)
            pcm16 = np.clip(left * 32767.0, -32768, 32767).astype("<i2")
            if cap.channels > 1:
                right = np.clip(a[:, 1].astype(np.float64) * 32767.0,
                                -32768, 32767).astype("<i2")
                inter = np.empty(left.size * 2, dtype="<i2")
                inter[0::2] = pcm16
                inter[1::2] = right
                wf.writeframes(inter.tobytes())
            else:
                wf.writeframes(pcm16.tobytes())
            total += left.size
            sec_acc.append(float(np.sqrt(np.mean(left ** 2))))

            now = time.monotonic() - t0
            if now - last_report >= 10.0:
                last_report = now
                rms = float(np.sqrt(np.mean(np.square(sec_acc[-1000:])))) \
                    if sec_acc else 0.0
                per_sec_rms.append(rms)
                print(f"[rec] t={now:5.1f}s 帧={total} rms={rms:.4f} "
                      f"d2max={d2_max:.3f} 尖峰累计={len(spikes)}", flush=True)
                d2_max = 0.0
    finally:
        wf.close()
        cap.stop()

    dur = (time.monotonic() - t0)
    print()
    print("=== 结果 ===")
    print(f"  录制     : {total} 帧 = {total / cap.rate:.1f}s  (实际耗时 {dur:.1f}s)")
    print(f"  文件     : {out_path}  "
          f"{os.path.getsize(out_path) / 1e6:.1f} MB")
    print(f"  平均 RMS : {float(np.mean(per_sec_rms)) if per_sec_rms else 0:.4f}"
          f"   (0 = 没声音, 说明录制路径没抓到)")
    print(f"  d2 尖峰  : {len(spikes)} 处 (阈值 {THRESH})")
    if spikes:
        print("  前 12 处:")
        for t, v in spikes[:12]:
            print(f"    t={t:7.2f}s  d2={v:.3f}")
        gaps = np.diff([t for t, _ in spikes])
        if gaps.size:
            print(f"  尖峰间隔(秒): {np.round(gaps[:12], 2).tolist()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
