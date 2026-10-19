"""终极定罪: 记录每个 UDP 音频包的 (src_ip, src_port, 首帧号, 帧数)。

如果同一内容(帧 0~766)来自**不同源端口** → 有多个发送方;
如果来自**同一源端口** → 是 server/agent 进程内部的问题。
"""

from __future__ import annotations

import io
import sys
import time
from contextlib import redirect_stdout

sys.path.insert(0, ".")

import numpy as np

from com_bridge.audio import LoopbackRenderer
from com_bridge.protocol import AudioHeader
from com_bridge.server import Server
from com_bridge.agent import Agent
from com_bridge.transport import AudioReceiver


records: list[tuple] = []


class SpyReceiver(AudioReceiver):
    def run(self) -> None:
        s = self._sock
        if s is None:
            import socket as _s
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.setsockopt(_s.SOL_SOCKET, _s.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("0.0.0.0", self.port))
            s.settimeout(0.5)
            self._sock = s
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(65535)
            except Exception:
                continue
            if len(data) < 15:
                continue
            try:
                hdr = AudioHeader.unpack(data)
            except ValueError:
                continue
            v = np.frombuffer(data[15:], dtype="<f4").reshape(-1, 2)[:, 0]
            records.append((
                addr[0], addr[1],
                round(float((v[0] + 1) * 20000), 1),
                round(float((v[-1] + 1) * 20000), 1),
                len(v), hdr.seq,
            ))
            if self._on_frame:
                try:
                    self._on_frame(hdr, data[15:])
                except Exception:
                    pass


def main() -> int:
    orig_cls = Server._start_audio

    def patched(self):
        from com_bridge.audio import LoopbackRenderer
        import threading
        self._ren = LoopbackRenderer()
        self._audio_rx = SpyReceiver(on_frame=self._on_peer_audio)
        self._audio_rx.bind_now()
        self._audio_rx.start()
        t = threading.Thread(target=self._render_loop, daemon=True,
                             name="RenderLoop")
        t.start()
        self._threads = [t]

    Server._start_audio = patched

    b = io.StringIO()
    with redirect_stdout(b):
        srv = Server(name="diag", port=24847, side="right", jitter_ms=60)
        srv.start()
        time.sleep(0.4)
        ag = Agent(server_host="127.0.0.1", name="d", mute_local=False,
                   discover_timeout_s=1.0, control_port=24847, tone_hz=440.0,
                   enable_return=False)
        ag.start()
        time.sleep(3)
        ag.stop()
        srv.stop()

    print(f"共 {len(records)} 包")
    print("前 30 包 (src, sport, 首帧, 尾帧, 帧数, seq):")
    for r in records[:30]:
        print("  ", r)
    srcs = {}
    for r in records:
        key = (r[0], r[1])
        srcs[key] = srcs.get(key, 0) + 1
    print("按来源统计:", srcs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
