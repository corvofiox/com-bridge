"""频谱分析录到的输出: 确认里面到底有什么声音。

若只有 440Hz 及其谐波 -> 纯测试音, 断裂是我们产生的;
若出现其它频率成分 -> 有别的声源混入(系统音/残留进程), 诊断结论作废。
"""

from __future__ import annotations

import sys
import wave

sys.path.insert(0, ".")

import numpy as np


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "diag_capture.wav"
    w = wave.open(path, "rb")
    n, ch, sr, sw = w.getnframes(), w.getnchannels(), w.getframerate(), w.getsampwidth()
    raw = w.readframes(n)
    dtype = {2: "<i2", 4: "<f4"}.get(sw)
    if dtype is None:
        print(f"不支持的位宽 {sw*8}bit")
        return 2
    arr = np.frombuffer(raw, dtype=dtype).astype(np.float64)
    if sw == 2:
        arr /= 32768.0
    a = arr.reshape(-1, ch)[:, 0]
    print(f"文件 {path}: {n} 帧, {ch}ch {sw*8}bit @{sr}Hz, {n/sr:.1f}s")

    # 取稳态中段 2 秒做 FFT
    seg = a[int(sr * 1.5): int(sr * 3.5)]
    seg = seg - seg.mean()
    win = np.hanning(len(seg))
    spec = np.abs(np.fft.rfft(seg * win))
    freqs = np.fft.rfftfreq(len(seg), 1.0 / sr)
    # 找峰值
    idx = np.argsort(spec)[::-1][:20]
    peaks = sorted(((float(freqs[i]), float(spec[i])) for i in idx),
                   key=lambda t: -t[1])
    print("前 10 个频谱峰 (Hz, 幅度):")
    for f, m in peaks[:10]:
        print(f"   {f:8.1f} Hz   {m:12.1f}")
    # 440Hz 及其谐波的能量占比
    total = float((spec ** 2).sum())
    harmonic = 0.0
    for k in range(1, 12):
        f0 = 440.0 * k
        if f0 > sr / 2:
            break
        lo, hi = int(f0 - 8), int(f0 + 8)
        harmonic += float((spec[lo:hi] ** 2).sum())
    print(f"440Hz 前 11 次谐波能量占比: {harmonic/total*100:.1f}%")
    if harmonic / total > 0.9:
        print("=> 基本是纯测试音, 断裂由我们产生")
    else:
        print("=> 存在明显非谐波成分, 可能有其它声源混入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
