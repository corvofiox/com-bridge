"""追踪 captured 相对 write 流的时间轴漂移曲线。

每 1 秒段在 captured 里做互相关定位, 偏移随时间的变化 = write 供给速率
与设备消费速率的累计失配。失配速率恒定且非零 → 周期性 overflow → 爆音。
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
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0

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
        srv = Server(name="diag", port=24830, side="right", jitter_ms=60)
        srv.start()
        time.sleep(0.4)
        ag = Agent(server_host="127.0.0.1", name="d", mute_local=False,
                   discover_timeout_s=1.0, control_port=24830, tone_hz=440.0,
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

    wstream = np.concatenate(writes)
    cstream = np.frombuffer(b"".join(chunks), dtype="<f4").reshape(-1, 2)[:, 0]
    print(f"[diag] write {len(wstream)} 帧, captured {len(cstream)} 帧")

    # 初始对齐
    w0 = len(wstream) // 3
    seg = wstream[w0: w0 + cap.rate]
    seg = seg - seg.mean()
    best_off, best_c = 0, -2.0
    for off in range(0, len(cstream) - len(seg), 480):
        win = cstream[off: off + len(seg)]
        c = float(np.corrcoef(seg, win - win.mean())[0, 1])
        if c > best_c:
            best_c, best_off = c, off
    for off in range(max(0, best_off - 480), best_off + 480):
        win = cstream[off: off + len(seg)]
        c = float(np.corrcoef(seg, win - win.mean())[0, 1])
        if c > best_c:
            best_c, best_off = c, off
    print(f"[diag] 初始对齐: 偏移 {best_off}, 相关 {best_c:.4f}")

    # 每秒跟踪偏移
    win_len = 4800
    offsets = []
    base = best_off
    k = 0
    while True:
        ws = wstream[w0 + k * cap.rate: w0 + k * cap.rate + win_len]
        if len(ws) < win_len:
            break
        ws = ws - ws.mean()
        center = base + k * cap.rate
        lo = max(0, center - 4800)
        hi = min(len(cstream) - win_len, center + 4800)
        best, bo = -2.0, center
        for off in range(lo, hi, 48):
            cs = cstream[off: off + win_len]
            c = float(np.corrcoef(ws, cs - cs.mean())[0, 1])
            if c > best:
                best, bo = c, off
        for off in range(max(lo, bo - 48), min(hi, bo + 48)):
            cs = cstream[off: off + win_len]
            c = float(np.corrcoef(ws, cs - cs.mean())[0, 1])
            if c > best:
                best, bo = c, off
        offsets.append(bo - center)
        k += 1
    print("[diag] 每秒偏移(帧, 相对初始):", offsets)
    if len(offsets) > 2:
        drift = (offsets[-1] - offsets[0]) / (len(offsets) - 1)
        print(f"[diag] 漂移速率: {drift:+.1f} 帧/s ({drift/48000*1e6:+.0f} ppm)")
        print(f"[diag] → 每 {48000/abs(drift):.1f}s 累计 4800 帧(=缓冲满/空) → 一次溢出事件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
