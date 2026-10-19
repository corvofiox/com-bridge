"""终极对齐诊断: 同时捕获 (a) write() 流 和 (b) 声卡实际播出的流,
用互相关对齐后逐样本比较, 精确找出渲染端破坏波形的位置与形态。
"""

from __future__ import annotations

import io
import sys
import time
import wave
from contextlib import redirect_stdout

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import LoopbackCapture, LoopbackRenderer
from com_bridge.server import Server
from com_bridge.agent import Agent


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

    writes: list[np.ndarray] = []
    orig_write = LoopbackRenderer.write

    def spy(self, pcm):
        n = orig_write(self, pcm)
        if n > 0:
            writes.append(
                np.frombuffer(pcm[: n * self.block_align], dtype="<f4")
                .reshape(-1, self.channels)[:, 0].copy()
            )
        return n

    LoopbackRenderer.write = spy

    buf = io.StringIO()
    with redirect_stdout(buf):
        srv = Server(name="diag", port=24821, side="right", jitter_ms=60)
        srv.start()
        time.sleep(0.4)
        ag = Agent(server_host="127.0.0.1", name="d", mute_local=False,
                   discover_timeout_s=1.0, control_port=24821, tone_hz=440.0,
                   enable_return=False)
        ag.start()

        cap = LoopbackCapture()
        cap.start()
        chunks: list[bytes] = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            r = cap.read(timeout_s=0.1)
            if r and not (r[1] & 0x2):
                chunks.append(r[0])
        cap.stop()
        ag.stop()
        srv.stop()

    write_stream = np.concatenate(writes)
    captured = np.frombuffer(b"".join(chunks), dtype="<f4").reshape(-1, 2)[:, 0]
    print(f"[diag] write 流 {len(write_stream)} 帧, 录到 {len(captured)} 帧")

    # 互相关对齐: 取 write 流中段 1 秒在 captured 里滑窗找最佳匹配
    seg = write_stream[len(write_stream)//3: len(write_stream)//3 + cap.rate]
    seg = seg - seg.mean()
    best_off, best_c = 0, -2.0
    step = 480  # 10ms 粗扫
    for off in range(0, max(1, len(captured) - len(seg)), step):
        win = captured[off: off + len(seg)]
        if len(win) < len(seg):
            break
        win = win - win.mean()
        c = float(np.corrcoef(seg, win)[0, 1])
        if c > best_c:
            best_c, best_off = c, off
    # 细扫
    for off in range(max(0, best_off - step), min(len(captured) - len(seg), best_off + step)):
        win = captured[off: off + len(seg)] - np.mean(captured[off: off + len(seg)])
        c = float(np.corrcoef(seg, win)[0, 1])
        if c > best_c:
            best_c, best_off = c, off
    print(f"[diag] 对齐: write→captured 偏移 {best_off} 帧, 相关系数 {best_c:.4f}")

    # 逐样本比较
    wseg = write_stream[len(write_stream)//3: len(write_stream)//3 + cap.rate]
    cseg = captured[best_off: best_off + cap.rate]
    diff = cseg - wseg
    print(f"[diag] 差值: 均值 {diff.mean():.5f} 最大 {np.abs(diff).max():.4f}")
    bad = np.where(np.abs(diff) > 0.05)[0]
    print(f"[diag] |差|>0.05 的样本数: {len(bad)} / {len(diff)}")
    if len(bad):
        # 跳变形态分类
        for j in bad[:5]:
            print(f"  @{j}: write={wseg[j]:.4f} captured={cseg[j]:.4f} "
                  f"(前后: {wseg[j-1]:.4f},{wseg[j+1] if j+1 < len(wseg) else float('nan'):.4f})")

    # 保存两个流供人工比对
    with wave.open("diag_write.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(cap.rate)
        w.writeframes((np.clip(write_stream[len(write_stream)//3:], -1, 1)
                       * 32767).astype("<i2").tobytes())
    with wave.open("diag_capture.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(cap.rate)
        w.writeframes((np.clip(captured, -1, 1) * 32767).astype("<i2").tobytes())
    print("[diag] 已保存 diag_write.wav / diag_capture.wav")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
