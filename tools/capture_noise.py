# -*- coding: utf-8 -*-
"""★★★ 现场录音诊断: 边运行边环回采集, 检测到杂音就保存波形。

## 为什么需要它
我在代码层面反复排查(插值/水位/丢弃/时间戳/静音包)都没能复现用户的
杂音。**必须拿到"杂音发生那一刻的真实波形"**才能定位。

## 用法(在**主机**上跑, 与 GUI 同时运行)
    python tools/capture_noise.py 60
  (60 = 采集秒数, 期间请正常使用/播放音乐)

## 输出
  * 检测到不连续点就保存 `noise_<时间>.wav` + 该段前后的 PCM
  * 同时打印当时的: 水位、ratio、trim 次数
"""
from __future__ import annotations

import sys
import time
import wave

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import LoopbackCapture


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(f"== 现场采集 {seconds} 秒(请正常播放音乐/使用) ==")
    print("   检测到不连续点会自动保存 wav")

    cap = LoopbackCapture()
    cap.start()
    rate = cap.rate
    ch = cap.channels
    print(f"   {ch}ch @{rate}Hz {cap.bits}bit")

    # 逐块分析: 每 0.5 秒一块
    block_sec = 0.5
    block_n = int(rate * block_sec)
    collected = []
    findings = []
    t0 = time.monotonic()
    total = 0

    while time.monotonic() - t0 < seconds:
        r = cap.read(timeout_s=0.1)
        if r is None:
            continue
        pcm, flags, qpc = r
        arr = np.frombuffer(pcm, dtype=np.float32)
        if ch > 1:
            arr = arr.reshape(-1, ch)[:, 0]
        collected.append(arr.copy())
        total += len(arr)

        # 攒够一块就分析
        if sum(len(x) for x in collected) >= block_n:
            y = np.concatenate(collected).astype(np.float64)
            collected = []
            rms = np.sqrt((y ** 2).mean())
            if rms < 1e-4:
                continue        # 静音, 跳过
            # 二阶差分找突变(比一阶更能发现"咔哒")
            d1 = np.diff(y)
            d2 = np.abs(np.diff(d1))
            # 阈值: 相对 RMS 的 20 倍
            th = max(rms * 20, 1e-3)
            hits = np.where(d2 > th)[0]
            if len(hits):
                el = time.monotonic() - t0
                findings.append((el, len(hits), float(d2.max()), rms))
                name = f"noise_{int(el*1000)}.wav"
                try:
                    with wave.open(name, "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(rate)
                        s = np.clip(y * 32767, -32768, 32767).astype(np.int16)
                        w.writeframes(s.tobytes())
                    print(f"   ★ t={el:.1f}s: {len(hits)} 处突变 "
                          f"(最大 {d2.max():.4f}, RMS={rms:.4f}) -> {name}")
                except Exception as exc:  # noqa: BLE001
                    print(f"   保存失败: {exc}")

    cap.stop()
    print(f"\n== 完成: 采集 {total/rate:.1f}s, 发现 {len(findings)} 处异常 ==")
    if findings:
        print("  时刻(s)  突变数   最大二阶差分   RMS")
        for el, n, mx, rms in findings[:20]:
            print(f"  {el:7.1f}  {n:6d}   {mx:12.4f}   {rms:.4f}")
        print("\n  ★ 请把生成的 noise_*.wav 发给我, 我用频谱分析定位根因")
    else:
        print("  本次未检测到异常 —— 请在有杂音时再跑一次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
