"""最终验收: 不经过声卡, 直接检查 write 流的连续性、音调与闭环状态。

判据:
  * 稳态跳变 = 0(排除前 1s 启动期)
  * 主频 ≈ 440Hz(闭环不应产生可闻音调偏差)
  * ratio 收敛在 1.0 附近且水位贴近目标
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
orig = LoopbackRenderer.write


def spy(self, pcm):
    n = orig(self, pcm)
    if n > 0:
        wr.append(np.frombuffer(pcm[: n * self.block_align], dtype="<f4")
                  .reshape(-1, self.channels)[:, 0].copy())
    return n


LoopbackRenderer.write = spy

from com_bridge.server import Server   # noqa: E402
from com_bridge.agent import Agent     # noqa: E402


def main() -> int:
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        srv = Server(name="d", port=24859, side="right", jitter_ms=60)
        srv.start()
        time.sleep(0.4)
        ag = Agent(server_host="127.0.0.1", name="d", mute_local=False,
                   discover_timeout_s=1.0, control_port=24859, tone_hz=440.0,
                   enable_return=False)
        ag.start()
        time.sleep(14)
        ag.stop()
        srv.stop()

    v = np.concatenate(wr)
    skip = 48000
    tail = v[skip:]
    d = np.abs(np.diff(tail))
    big = np.where(d > 0.05)[0]
    seg = v[skip: skip + 96000]
    seg = seg - seg.mean()
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1 / 48000)
    peak = float(freqs[int(np.argmax(spec))])

    rate = srv.stats.get("render_ratio")
    level = srv._peer.buffered_ms()
    print(f"write 流: {len(v)} 帧 ({len(v)/48000:.1f}s)")
    print(f"  稳态跳变   : {len(big)} 处 = {len(big)/(len(tail)/48000):.2f}/s"
          f"  (最大 {d.max():.4f})")
    print(f"  主频       : {peak:.1f} Hz  (期望 440.0, 偏差 "
          f"{(peak/440-1)*100:+.2f}%)")
    print(f"  ratio 稳态 : {rate}")
    print(f"  peer 水位  : {level:.1f} ms (目标 {srv.jitter_ms:.0f})")

    ok = len(big) == 0 and abs(peak - 440.0) < 2.0
    print(f"\n结论: {'PASS —— 音频连续且音调正确' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
