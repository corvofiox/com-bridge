"""输入流积压合并(coalesce_moves)与批式交付的回归测试。

背景(2026-10-23 用户实测): 副机 2560x1600 屏上鼠标"非常迟滞, 手停了光标
还在追"。输入是 UDP, 消费端一时跟不上就会在 socket 缓冲里堆事件(默认
64KB ≈ 3400 条), 逐条注入时只能慢慢排空 -> 主观"追手"。

修复: 收到一条事件后先看 socket 里还有没有积压, 有就继续攒, 然后**把相邻
位移合并成一条**再注入 —— 落点等价, 但一轮只付一次 SendInput 的代价。

这里锁死三条不变量:
  1. 位移**总量**守恒(合并丢的是中间站, 不是路程);
  2. 非移动事件(按键/滚轮)**绝不被合并**, 且相对顺序不变;
  3. 没有积压时行为与旧实现完全一致(逐条交付), 不能平白引入延迟。
"""
import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from com_bridge.protocol import (  # noqa: E402
    EV_KEY, EV_MOUSE_BUTTON, EV_MOUSE_MOVE_REL, EV_MOUSE_WHEEL, InputEvent,
)
from com_bridge.transport import InputReceiver, coalesce_moves  # noqa: E402


def mv(dx, dy, ts=0):
    return InputEvent(EV_MOUSE_MOVE_REL, 0, 0, dx, dy, ts)


def btn(code=1, flags=0x01, ts=0):
    return InputEvent(EV_MOUSE_BUTTON, flags, code, 0, 0, ts)


# ------------------------------------------------------------ coalesce_moves
def test_adjacent_moves_are_merged_and_distance_is_conserved():
    out = coalesce_moves([mv(3, 0), mv(4, -1), mv(-2, 5)])
    assert len(out) == 1
    assert (out[0].a, out[0].b) == (5, 4)
    assert out[0].type == EV_MOUSE_MOVE_REL


def test_merged_event_keeps_last_timestamp():
    out = coalesce_moves([mv(1, 0, ts=100), mv(1, 0, ts=250), mv(1, 0, ts=900)])
    assert out[0].ts_us == 900


def test_button_blocks_merging_and_order_is_preserved():
    seq = [mv(5, 0), btn(1, 0x01), mv(5, 0), mv(1, 1), btn(1, 0x02)]
    out = coalesce_moves(seq)
    # 移动-按键-移动(两条合并)-按键
    assert [e.type for e in out] == [
        EV_MOUSE_MOVE_REL, EV_MOUSE_BUTTON, EV_MOUSE_MOVE_REL, EV_MOUSE_BUTTON]
    assert (out[0].a, out[0].b) == (5, 0)
    assert (out[2].a, out[2].b) == (6, 1)


def test_wheel_blocks_merging():
    seq = [mv(2, 0), InputEvent(EV_MOUSE_WHEEL, 0, 0, 0, -1, 0), mv(2, 0)]
    out = coalesce_moves(seq)
    assert len(out) == 3
    assert out[1].type == EV_MOUSE_WHEEL


def test_single_and_empty_passthrough_is_identity():
    one = [mv(7, 8)]
    assert coalesce_moves(one) is one
    empty = []
    assert coalesce_moves(empty) is empty


def test_key_event_not_merged_with_moves():
    seq = [mv(1, 1), InputEvent(EV_KEY, 0x01, 0x41, 0x1E, 0, 0), mv(1, 1)]
    out = coalesce_moves(seq)
    assert len(out) == 3
    assert out[1].type == EV_KEY and out[1].code == 0x41


def test_total_distance_conserved_over_many_moves():
    seq = [mv((i % 7) - 3, (i % 5) - 2) for i in range(200)]
    out = coalesce_moves(seq)
    assert len(out) == 1
    assert (out[0].a, out[0].b) == (sum(e.a for e in seq), sum(e.b for e in seq))


# ------------------------------------------------------------ 批式交付
def _recv(**kw):
    """port=0: 由系统分配端口, 避免和正在运行的本机实例抢 24801。"""
    r = InputReceiver(port=0, **kw)
    r.bind_now()
    r._sock.settimeout(0.2)
    return r


def test_flush_without_pending_is_noop():
    got = []
    r = _recv(on_event=got.append)
    r._flush()
    assert got == [] and r.batches == 0
    r.stop()


def test_flush_delivers_merged_batch_and_counts():
    got = []
    r = _recv(on_event=got.append)
    r._pending = [mv(1, 0), mv(2, 0), btn(1), mv(4, 4)]
    r._flush()
    assert [(e.type, e.a, e.b) for e in got] == [
        (EV_MOUSE_MOVE_REL, 3, 0), (EV_MOUSE_BUTTON, 0, 0),
        (EV_MOUSE_MOVE_REL, 4, 4)]
    st = r.stats()
    assert st["batches"] == 1
    assert st["max_batch"] == 4
    assert st["merged"] == 1          # 两条相邻移动 -> 少一条
    assert st["deliveries"] == 3
    assert st["inject_ms_max"] >= 0.0
    r.stop()


def test_callback_exception_does_not_drop_rest_of_batch():
    seen = []

    def cb(ev):
        seen.append(ev.type)
        if ev.type == EV_MOUSE_BUTTON:
            raise RuntimeError("boom")

    r = _recv(on_event=cb)
    r._pending = [mv(1, 0), btn(1), mv(1, 0)]
    r._flush()
    assert seen == [EV_MOUSE_MOVE_REL, EV_MOUSE_BUTTON, EV_MOUSE_MOVE_REL]
    assert r.callback_errors == 1
    r.stop()


def test_no_backlog_means_immediate_delivery(monkeypatch):
    """select 说"没有积压"时必须立刻交付 —— 不能凭空多一个事件周期的延迟。"""
    got = []
    r = _recv(on_event=got.append)
    monkeypatch.setattr("com_bridge.transport.select.select",
                        lambda *a, **k: ([], [], []))
    r._push_incoming(mv(1, 1))
    assert len(got) == 1 and r.batches == 1
    r.stop()


def test_backlog_is_accumulated_until_drained(monkeypatch):
    got = []
    r = _recv(on_event=got.append)
    monkeypatch.setattr("com_bridge.transport.select.select",
                        lambda *a, **k: ([object()], [], []))
    for i in range(5):
        r._push_incoming(mv(1, 0))
    assert got == []                    # 还在攒
    monkeypatch.setattr("com_bridge.transport.select.select",
                        lambda *a, **k: ([], [], []))
    r._push_incoming(mv(1, 0))
    assert len(got) == 1               # 攒够后一次交付
    assert got[0].a == 6
    assert r.stats()["merged"] == 5
    r.stop()


def test_drain_cap_forces_flush(monkeypatch):
    got = []
    r = _recv(on_event=got.append)
    monkeypatch.setattr("com_bridge.transport.select.select",
                        lambda *a, **k: ([object()], [], []))
    for _ in range(InputReceiver.MAX_DRAIN):
        r._push_incoming(mv(1, 0))
    assert len(got) == 1               # 到上限必须交付, 不能无限攒
    assert got[0].a == InputReceiver.MAX_DRAIN
    r.stop()


# ------------------------------------------------------------ 真 socket 往返
def test_live_udp_burst_preserves_total_distance():
    """真发 60 条位移: 无论被分成几批, 总位移必须一条不少。"""
    got = []
    r = InputReceiver(port=0, on_event=got.append)
    r.bind_now()
    port = r.bound_port
    assert port
    r.start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(60):
            s.sendto(mv(2, -1).pack(), ("127.0.0.1", port))
        s.close()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            total = sum(e.a for e in got)
            if total >= 120:
                break
            time.sleep(0.02)
    finally:
        r.stop()
    assert sum(e.a for e in got) == 120
    assert sum(e.b for e in got) == -60
    assert r.stats()["events"] == 60


def test_live_udp_keeps_button_order():
    got = []
    r = InputReceiver(port=0, on_event=got.append)
    r.bind_now()
    port = r.bound_port
    r.start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for ev in (mv(3, 0), btn(1, 0x01), btn(1, 0x02), mv(4, 0)):
            s.sendto(ev.pack(), ("127.0.0.1", port))
        s.close()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(got) < 3:
            time.sleep(0.02)
    finally:
        r.stop()
    types = [e.type for e in got]
    assert types[0] == EV_MOUSE_MOVE_REL
    assert types[1] == EV_MOUSE_BUTTON and types[2] == EV_MOUSE_BUTTON
    assert (got[0].a, got[-1].a) == (3, 4)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
