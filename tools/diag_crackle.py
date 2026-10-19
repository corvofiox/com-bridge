"""端到端爆音量化: 起一个 GUI 同款的 server+agent 回环(合成测试音),
同时用 WASAPI loopback 把主电脑默认输出录下来, 对录到的波形做跳变分析。

这能直接回答"测试音下的小杂音"到底在哪一层:
  - 录到波形里有 >0.05 的样本跳变  → 渲染/对齐层在产生断裂
  - 录到的音量包络有周期性塌陷      → 写入端欠载(underrun)
  - 录到波形干净但用户仍听到杂音    → 问题在声卡/驱动层

用法: python tools/diag_crackle.py [秒数]
"""

from __future__ import annotations

import io
import sys
import time
import wave
from contextlib import redirect_stdout

sys.path.insert(0, ".")

import numpy as np


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

    # --- 1. 启动回环(server+agent, 440Hz 测试音) ---
    from com_bridge.server import Server
    from com_bridge.agent import Agent

    buf = io.StringIO()
    with redirect_stdout(buf):
        srv = Server(name="diag", port=24818, side="right", jitter_ms=60)
        srv.start()
        time.sleep(0.4)
        ag = Agent(server_host="127.0.0.1", name="diag-agent", mute_local=False,
                   discover_timeout_s=1.0, control_port=24818, tone_hz=440.0,
                   enable_return=False)
        ag.start()

    # --- 2. 录制默认输出 ---
    from com_bridge.audio import LoopbackCapture

    cap = LoopbackCapture()
    cap.start()
    print(f"[diag] 录制 {seconds:.0f}s 的主电脑输出 ...")
    chunks: list[bytes] = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        r = cap.read(timeout_s=0.1)
        if r is not None:
            pcm, flags, _qpc = r
            if not (flags & 0x2):
                chunks.append(pcm)
    cap.stop()
    ag.stop()
    srv.stop()

    if not chunks:
        print("[diag] !! 没录到任何音频(输出设备静默?)")
        return 1
    raw = b"".join(chunks)
    # ★ 先自检采集格式假设, 再谈跳变 —— 若位深/通道数判断错误,
    #   解析出来的就是乱码, 跳变统计全部作废(曾因此浪费大量时间)。
    print(f"[diag] 采集格式: {cap.channels}ch {cap.bits}bit "
          f"blockAlign={cap.block_align} rate={cap.rate}")
    if cap.bits != 32:
        print(f"[diag] !! 位深不是 32(float), 本工具的 <f4 解析无效, 退出")
        return 2
    frames = np.frombuffer(raw, dtype="<f4").reshape(-1, cap.channels)
    samples = frames[:, 0]
    print(f"[diag] 录到 {len(samples)} 帧 ({len(samples)/cap.rate:.1f}s)")
    if cap.channels >= 2:
        lr = np.abs(frames[:, 0] - frames[:, 1])
        print(f"[diag] L==R 样本占比: {(lr < 1e-6).mean()*100:.1f}% "
              f"(测试音 L=R; 远低于 100% 说明有其它声源混入)")

    # --- 3. 跳变分析: 找波形不连续点 ---
    d = np.abs(np.diff(samples))
    thresh = 0.05
    big = np.where(d > thresh)[0]
    print(f"[diag] 样本间跳变 > {thresh}: {len(big)} 处 "
          f"(440Hz/0.25 正弦的理论步进 0.0144)")
    if len(big):
        # 跳变的时间分布(秒)
        times = np.unique((big // cap.rate).astype(int))
        print(f"[diag]   分布在 {len(times)} 个不同的秒: {times[:20]}")
        i = int(big[0])
        print(f"[diag]   首处: {samples[i]:.4f} -> {samples[i+1]:.4f} "
              f"@ {i/cap.rate:.3f}s")

    # --- 4. 包络分析: 检测周期性塌陷(欠载特征) ---
    # RMS 每 10ms 一窗
    win = cap.rate // 100
    n_win = len(samples) // win
    rms = np.sqrt(np.mean(samples[: n_win * win].reshape(n_win, win) ** 2,
                          axis=1))
    # 正弦 RMS 应 ≈ AMP/√2 ≈ 0.177; 塌陷 = RMS < 一半
    collapsed = np.where(rms < 0.08)[0]
    print(f"[diag] RMS 窗: {n_win}, 塌陷窗(RMS<0.08): {len(collapsed)}")
    if len(collapsed):
        print(f"[diag]   塌陷时间(s): {collapsed[:20] / 100}")

    # --- 5. 存 wav 供人工试听 ---
    # ★ 必须写成**双通道**(设备格式), 不能把已取单通道的数组当双通道写:
    #   帧数会被当成 2 倍样本, 回放时频率翻倍、频谱分析全错
    #   (曾把 440Hz 分析成 871Hz, 白查了很久)。
    with wave.open("diag_capture.wav", "wb") as w:
        w.setnchannels(cap.channels)
        w.setsampwidth(4)                      # float32 才是原始格式
        w.setframerate(cap.rate)
        w.writeframes(np.clip(frames, -1, 1).astype("<f4").tobytes())
    print(f"[diag] 已保存 diag_capture.wav "
          f"({cap.channels}ch float32 @{cap.rate}Hz)")

    # --- 6. 判定 ---
    # ★ 排除启动期(前 0.3s): 起播瞬间缓冲未建立, 会出现短暂静音/跳变,
    #   那是正常现象, 不代表持续杂音。
    # ★ 统计稳态跳变才是有效判据; 之前只看总数会把启动期算进去。
    skip = int(cap.rate * 0.3)
    d_steady = np.abs(np.diff(samples[skip:]))
    big_steady = np.where(d_steady > thresh)[0]
    rms_steady = rms[3:] if len(rms) > 3 else rms
    collapsed_steady = np.where(rms_steady < 0.08)[0]
    print()
    print(f"[diag] === 稳态判定(排除前 0.3s) ===")
    print(f"[diag] 稳态跳变: {len(big_steady)} 处 "
          f"({len(big_steady)/(len(samples)-skip)*cap.rate:.2f}/s)")
    print(f"[diag] 稳态塌陷窗: {len(collapsed_steady)} 个")

    # 环境干净性提示: L==R 是测试音的特征, 占比低说明有其它声源混入
    if cap.channels >= 2:
        print(f"[diag] 提示: L==R 占比 62.9% 以下通常意味着录制期间"
              f"设备上还有别的应用在出声, 结论会被污染。")

    ok = len(big_steady) == 0 and len(collapsed_steady) == 0
    print(f"[diag] 结论: {'音频连续, 无爆音' if ok else '存在断裂(可能有其它声源或真实爆音)'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
