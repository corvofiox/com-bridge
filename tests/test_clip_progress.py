"""传输进度与取消的测试(审查项 #5)。

现场问题: 传 200MB 文件时旧实现**全程一句话都不打印**, 用户不知道是在传、
卡住了、还是已经失败; 也无法中止, 只能杀进程。
"""
import os
import struct
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from com_bridge import clip_transfer as ct  # noqa: E402


class FakeSock:
    """最小的 socket 替身: 收 sendall 的帧, recv 回预置的 ACK。"""

    def __init__(self, ack=b"\x01", ack_msg=b"ok"):
        self.frames: list[tuple[int, bytes]] = []
        self._in = b""
        self._out = ct.pack_frame(ct.K_ACK, ack + ack_msg) if ack is not None else b""
        self.closed = False
        self.timeouts: list[float] = []

    def settimeout(self, t):
        self.timeouts.append(t)

    def sendall(self, data: bytes):
        self._in += data
        while len(self._in) >= 5:
            kind = self._in[0]
            ln = struct.unpack("<I", self._in[1:5])[0]
            if len(self._in) < 5 + ln:
                return
            self.frames.append((kind, self._in[5:5 + ln]))
            self._in = self._in[5 + ln:]

    def recv(self, n: int) -> bytes:
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk

    def close(self):
        self.closed = True

    def kinds(self):
        return [k for k, _ in self.frames]


@pytest.fixture
def fake_sock(monkeypatch):
    sock = FakeSock()
    monkeypatch.setattr(ct.socket, "create_connection",
                        lambda *a, **k: sock)
    return sock


def test_progress_reports_total_and_monotonic_bytes(fake_sock):
    blob = b"x" * (ct.CHUNK * 3)
    seen = []
    ok, sent, msg = ct.push_clipboard(
        "127.0.0.1", 1234, blob, on_progress=lambda d, t: seen.append((d, t)))
    assert ok is True
    assert sent == len(blob)
    assert seen, "必须至少回调一次进度"
    assert all(t == len(blob) for _d, t in seen)      # 总量口径 = 内容 + 文件
    assert [d for d, _t in seen] == sorted(d for d, _t in seen)


def test_progress_total_includes_file_bytes(fake_sock, tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"y" * 1024)
    blob = b"clip"
    entries = [{"src": str(f), "name": "a.bin", "size": 1024}]
    seen = []
    ok, _sent, _msg = ct.push_clipboard(
        "127.0.0.1", 1234, blob, entries,
        on_progress=lambda d, t: seen.append(t))
    assert ok is True
    assert seen and seen[-1] == len(blob) + 1024


def test_cancel_mid_transfer_returns_cancelled(fake_sock):
    blob = b"z" * (ct.CHUNK * 4)
    ev = threading.Event()
    seen = []

    def prog(done, total):
        seen.append(done)
        if len(seen) >= 2:            # 传了两块后按"取消"
            ev.set()

    ok, sent, msg = ct.push_clipboard("127.0.0.1", 1234, blob,
                                      on_progress=prog, cancel=ev)
    assert ok is False
    assert msg == "已取消"
    assert 0 < sent < len(blob), "取消必须保留已发字节数, 便于界面显示"
    assert ct.K_DONE not in fake_sock.kinds()


def test_cancel_already_set_sends_nothing(fake_sock):
    ev = threading.Event()
    ev.set()
    ok, sent, msg = ct.push_clipboard("127.0.0.1", 1234, b"q" * ct.CHUNK,
                                      cancel=ev)
    assert (ok, sent, msg) == (False, 0, "已取消")


def test_progress_callback_exception_is_ignored(fake_sock):
    def boom(_d, _t):
        raise RuntimeError("ui 挂了也不能影响传输")
    ok, sent, _msg = ct.push_clipboard("127.0.0.1", 1234, b"a" * 10,
                                       on_progress=boom)
    assert ok is True and sent == 10


def test_ack_rejection_is_reported(monkeypatch):
    sock = FakeSock(ack=b"\x00", ack_msg="对端满了".encode())
    monkeypatch.setattr(ct.socket, "create_connection", lambda *a, **k: sock)
    ok, _sent, msg = ct.push_clipboard("127.0.0.1", 1234, b"b" * 10)
    assert ok is False
    assert "对端满了" in msg


# ------------------------------------------------------------ ClipOutbox
def test_outbox_stats_shape():
    ob = ct.ClipOutbox("127.0.0.1", 1234)
    st = ob.stats()
    for key in ("sent", "failed", "cancelled", "bytes_sent", "queued",
                "busy", "last_error"):
        assert key in st
    assert st["busy"] is False and st["sent"] == 0


def test_outbox_cancel_current_sets_flag():
    ob = ct.ClipOutbox("127.0.0.1", 1234)
    assert not ob._cancel.is_set()
    ob.cancel_current()
    assert ob._cancel.is_set()


def test_outbox_stop_also_cancels_inflight():
    ob = ct.ClipOutbox("127.0.0.1", 1234)
    ob.stop()
    assert ob._cancel.is_set() and ob._stop.is_set()


def test_outbox_progress_line_is_throttled(capsys):
    ob = ct.ClipOutbox("127.0.0.1", 1234)
    total = 1000
    for done in range(0, 900, 10):        # 90 次回调
        ob._on_progress(done, total)
    out = capsys.readouterr().out
    assert out.count("发送中") <= 2, "进度必须节流, 否则会把日志刷爆"


def test_outbox_progress_prints_again_after_interval(capsys, monkeypatch):
    """时间到了就该再出一行(节流不能变成"只打一行")。"""
    ob = ct.ClipOutbox("127.0.0.1", 1234)
    ob._on_progress(10, 1000)
    ob._prog_t -= 1.0                       # 假装过了 1 秒
    ob._on_progress(200, 1000)
    out = capsys.readouterr().out
    assert out.count("发送中") == 2
    assert "20%" in out


def test_outbox_log_done_success_and_failure(capsys):
    ob = ct.ClipOutbox("127.0.0.1", 1234)
    import time as _t
    ob._t0 = _t.monotonic() - 2.0
    ob._log_done(True, 4 * 1048576, "已送达")
    ob._t0 = _t.monotonic() - 1.0
    ob._log_done(False, 1048576, "传输中断: 连接重置")
    out = capsys.readouterr().out
    assert "已发送 4.0 MB" in out and "MB/s" in out
    assert "发送未完成" in out and "连接重置" in out


def test_file_size_cap_raised_above_2gb():
    """审查项 #5: 原来 2GB 是随意门槛, 不是技术限制。"""
    assert ct.MAX_FILE_BYTES >= 8 * 1024 * 1024 * 1024


def test_oversize_still_rejected(monkeypatch):
    big = [{"src": "x", "name": "x", "size": ct.MAX_FILE_BYTES + 1}]
    ok, sent, msg = ct.push_clipboard("127.0.0.1", 1, b"x", big)
    assert ok is False and sent == 0 and "过大" in msg


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
