"""协议层: 音频帧头与输入事件的编解码。

这些测试锁死两件事:
  1. 线上字节数 —— 头长度写错会让对方解析错位, 且很难从现象反推;
  2. 往返一致性 —— 含边界值(int32 负数、7 字节时间戳上限)。
"""

from __future__ import annotations

import pytest

from com_bridge.protocol import (
    AUDIO_HEADER_SIZE,
    INPUT_HEADER_SIZE,
    AudioHeader,
    InputEvent,
    decode_rate_index,
    encode_rate_index,
    EV_MOUSE_MOVE_REL,
    EV_KEY,
)


class TestAudioHeader:
    def test_header_is_exactly_15_bytes(self):
        h = AudioHeader(rate=48000, bits=32, channels=2, chan_mask=3,
                        seq=0, ts_us=0)
        assert len(h.pack()) == AUDIO_HEADER_SIZE == 15

    def test_roundtrip_basic(self):
        h = AudioHeader(rate=48000, bits=32, channels=2, chan_mask=3,
                        seq=7, ts_us=1234567890)
        assert AudioHeader.unpack(h.pack()) == h

    @pytest.mark.parametrize("rate", [44100, 48000, 96000, 192000])
    def test_roundtrip_all_supported_rates(self, rate):
        h = AudioHeader(rate=rate, bits=16, channels=1, chan_mask=0,
                        seq=1, ts_us=5)
        assert AudioHeader.unpack(h.pack()).rate == rate

    def test_roundtrip_max_timestamp(self):
        """7 字节时间戳上限是 2^56-1; 超出必须被截断而不是污染相邻字段。"""
        ts = (1 << 56) - 1
        h = AudioHeader(rate=48000, bits=32, channels=2, chan_mask=3,
                        seq=65535, ts_us=ts)
        got = AudioHeader.unpack(h.pack())
        assert got.ts_us == ts
        assert got.seq == 65535

    def test_rejects_short_buffer(self):
        with pytest.raises(ValueError):
            AudioHeader.unpack(b"\x01" * 14)

    def test_rejects_bad_version(self):
        good = AudioHeader(rate=48000, bits=32, channels=2, chan_mask=3,
                           seq=1, ts_us=1).pack()
        bad = bytes([0x02]) + good[1:]
        with pytest.raises(ValueError):
            AudioHeader.unpack(bad)


class TestRateIndex:
    @pytest.mark.parametrize("rate", [44100, 48000, 88200, 96000, 192000])
    def test_roundtrip(self, rate):
        idx = encode_rate_index(rate)
        assert 0 <= idx <= 0xFF
        assert decode_rate_index(idx) == rate

    def test_rejects_unsupported_rate(self):
        with pytest.raises(ValueError):
            encode_rate_index(12345)


class TestInputEvent:
    def test_event_is_19_bytes(self):
        ev = InputEvent(EV_MOUSE_MOVE_REL, 0, 0, 1, 1, 0)
        assert len(ev.pack()) == INPUT_HEADER_SIZE == 19

    def test_roundtrip_negative_deltas(self):
        """鼠标向左上移动是负数; 若误用无符号解析会把 -5 变成 4e9。"""
        ev = InputEvent(EV_MOUSE_MOVE_REL, 0, 0, -5, -12, 999)
        got = InputEvent.unpack(ev.pack())
        assert (got.a, got.b) == (-5, -12)
        assert got == ev

    def test_roundtrip_key_event(self):
        ev = InputEvent(EV_KEY, 0x01, 0x41, 30, 0, 123456)
        assert InputEvent.unpack(ev.pack()) == ev

    def test_roundtrip_int32_extremes(self):
        ev = InputEvent(EV_MOUSE_MOVE_REL, 0, 0, -(2**31), 2**31 - 1, 0)
        got = InputEvent.unpack(ev.pack())
        assert (got.a, got.b) == (-(2**31), 2**31 - 1)

    def test_rejects_short_buffer(self):
        with pytest.raises(ValueError):
            InputEvent.unpack(b"\x00" * 18)

    def test_is_mouse_is_key(self):
        assert InputEvent(EV_MOUSE_MOVE_REL, 0, 0, 0, 0, 0).is_mouse
        assert not InputEvent(EV_MOUSE_MOVE_REL, 0, 0, 0, 0, 0).is_key
        assert InputEvent(EV_KEY, 0, 0, 0, 0, 0).is_key
