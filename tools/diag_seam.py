"""接缝定位: 分别记录 pop_aligned 出口流与 write 入口流, 比对跳变数。

(a) pop_aligned 出口已脏 -> 问题在 Resampler/pop 接口
(b) pop_aligned 干净而 write 脏 -> 问题在渲染写入路径
"""

from __future__ import annotations

import io
import sys
import time
from contextlib import redirect_stdout

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import AlignedStreamBuffer, LoopbackRenderer

popout: list[np.ndarray] = []
wr: list[np.ndarray] = []

orig_pop = AlignedStreamBuffer.pop_aligned


def pop_spy(self, now, max_frames, lookahead=0, ratio_override=None):
    r = orig_pop(self, now, max_frames, lookahead, ratio_override)
    if r:
        popout.append(np.frombuffer(r, dtype="<f4")
                      .reshape(-1, 2)[:, 0].copy())
    return r


AlignedStreamBuffer.pop_aligned = pop_spy

orig_w = LoopbackRenderer.write


def w_spy(self, pcm):
    n = orig_w(self, pcm)
    if n > 0:
        wr.append(np.frombuffer(pcm[: n * self.block_align], dtype="<f4")
                  .reshape(-1, self.channels)[:, 0].copy())
    return n


LoopbackRenderer.write = w_spy

from com_bridge.server import Server      # noqa: E402
from com_bridge.agent import Agent        # noqa: E402


def main() -> int:
    b = io.StringIO()
    with redirect_stdout(b):
        srv = Server(name="d", port=24853, side="right", jitter_ms=60)
        srv.start()
        time.sleep(0.4)
        ag = Agent(server_host="127.0.0.1", name="d", mute_local=False,
                   discover_timeout_s=1.0, control_port=24853, tone_hz=440.0,
                   enable_return=False)
        ag.start()
        time.sleep(10)
        ag.stop()
        srv.stop()

    for parts, label in ((popout, "pop_aligned 出口"), (wr, "write 入口")):
        if not parts:
            print(f"{label}: 无数据")
            continue
        v = np.concatenate(parts)
        d = np.abs(np.diff(v))
        big = np.where(d > 0.05)[0]
        big = big[big > 48000 * 0.3]
        mx = float(d[big].max()) if len(big) else 0.0
        print(f"{label}: {len(v)} 帧, 跳变 {len(big)} 处 = "
              f"{len(big)/9.7:.1f}/s, 最大 {mx:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
