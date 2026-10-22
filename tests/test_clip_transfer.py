# -*- coding: utf-8 -*-
"""剪贴板 TCP 通道(clip_transfer.py)的回归测试。

覆盖:
  * 帧读写(长度前缀 + magic 校验 + 截断/畸形拒绝);
  * 接收侧**路径消毒**(``..``、绝对路径、盘符、保留名、深度);
  * 目录展开(collect_sources)与重名改名;
  * 端到端: 文本 / 裸 DIB 图片 / 一组文件(含子目录)从"发送方"推到
    "接收方", 校验落盘内容逐字节一致、且回调里拿到的是**实际落盘路径**;
  * 上限(剪贴板内容、文件总量)与 ACK 失败路径;
  * 发送方串行队列(ClipOutbox)不丢单。

这些都跑在 127.0.0.1 上, 用临时目录, 不碰用户真实剪贴板。
"""
from __future__ import annotations

import os
import socket
import struct
import threading
import time

import pytest

from com_bridge import clipboard as cb
from com_bridge import clip_transfer as ct


# --- 路径消毒 -------------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    ("C:\\Users\\x\\a.txt", "a.txt"),
    ("../../evil.exe", "evil.exe"),
    ("/etc/passwd", "passwd"),
    ("a.txt", "a.txt"),
    ("  spaced.txt  ", "spaced.txt"),
    ("con.txt", "_con.txt"),
    ("....", ""),
    ("", ""),
])
def test_sanitize_name(raw, expect):
    assert ct.sanitize_name(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    ("照片/2024/a.jpg", "照片/2024/a.jpg"),
    ("a\\b\\c.txt", "a/b/c.txt"),
    ("../x.txt", ""),
    ("a/../../x.txt", ""),
    ("/abs/path", "abs/path"),
    ("", ""),
])
def test_sanitize_relpath(raw, expect):
    assert ct.sanitize_relpath(raw) == expect


def test_sanitize_relpath_depth_limit():
    deep = "/".join(f"d{i}" for i in range(ct.MAX_DEPTH + 1)) + "/x.txt"
    assert ct.sanitize_relpath(deep) == ""


# --- 收集(目录展开) ------------------------------------------------------

def test_collect_sources_expands_folder(tmp_path):
    root = tmp_path / "照片"
    (root / "2024").mkdir(parents=True)
    (root / "a.jpg").write_bytes(b"A" * 10)
    (root / "2024" / "b.jpg").write_bytes(b"B" * 20)
    entries, total, warnings = ct.collect_sources([str(root)])
    assert total == 30 and not warnings
    names = sorted(e["name"] for e in entries)
    assert names == ["照片/2024/b.jpg", "照片/a.jpg"]


def test_collect_sources_renames_duplicates(tmp_path):
    a = tmp_path / "x" / "same.txt"
    b = tmp_path / "y" / "same.txt"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    entries, _total, _w = ct.collect_sources([str(a), str(b)])
    names = [e["name"] for e in entries]
    assert len(names) == 2 and len(set(n.lower() for n in names)) == 2


def test_collect_sources_reports_missing():
    entries, total, warnings = ct.collect_sources(["Z:\\does\\not\\exist"])
    assert entries == [] and total == 0 and warnings


# --- 帧 --------------------------------------------------------------

def test_frame_roundtrip_over_socketpair():
    a, b = socket.socketpair()
    try:
        ct.send_frame(a, ct.K_CLIP_DATA, b"hello")
        kind, payload = ct.read_frame(b)
        assert (kind, payload) == (ct.K_CLIP_DATA, b"hello")
    finally:
        a.close()
        b.close()


def test_frame_rejects_bad_magic():
    a, b = socket.socketpair()
    try:
        a.sendall(b"XX" + struct.pack("<BI", 2, 1) + b"z")
        assert ct.read_frame(b) is None
    finally:
        a.close()
        b.close()


def test_frame_rejects_truncated():
    a, b = socket.socketpair()
    try:
        a.sendall(ct.HDR.pack(ct.MAGIC, ct.K_CLIP_DATA, 10) + b"short")
        a.shutdown(socket.SHUT_WR)
        assert ct.read_frame(b) is None
    finally:
        a.close()
        b.close()


# --- 端到端 -----------------------------------------------------------

def _start_inbox(tmp_path, max_clip=ct.MAX_CLIP_BYTES,
                 max_files=ct.MAX_FILE_BYTES):
    got: list[dict] = []
    inbox = ct.ClipInbox(port=0, on_payload=got.append,
                         recv_root=str(tmp_path / "recv"),
                         max_clip=max_clip, max_files=max_files)
    inbox.bind_now()
    inbox.start()
    time.sleep(0.05)
    return inbox, got


def _stop(inbox):
    inbox.stop()
    time.sleep(0.05)


def test_text_payload_end_to_end(tmp_path):
    inbox, got = _start_inbox(tmp_path)
    try:
        blob = cb.marshall({cb.FMT_TEXT: "跨机文本".encode("utf-8")})
        ok, sent, msg = ct.push_clipboard("127.0.0.1", inbox.port, blob,
                                         timeout=5)
        assert ok, msg
        assert sent >= len(blob)
        assert got and got[0][cb.FMT_TEXT] == "跨机文本".encode("utf-8")
    finally:
        _stop(inbox)


def test_dib_image_end_to_end(tmp_path):
    """裸 DIB(不压缩)是最耗流量的情况: 4K 图约 33MB, 这里用 4MB 级别的假图。"""
    inbox, got = _start_inbox(tmp_path)
    try:
        w, h = 1024, 1024
        pixels = b"\x11\x22\x33\x44" * (w * h)       # 4MB
        dib = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0,
                          len(pixels), 0, 0, 0, 0) + pixels
        blob = cb.marshall({cb.FMT_BITMAP: dib})
        ok, sent, msg = ct.push_clipboard("127.0.0.1", inbox.port, blob,
                                         timeout=30)
        assert ok, msg
        assert got and got[0][cb.FMT_BITMAP] == dib
        assert sent > 4 * 1024 * 1024
    finally:
        _stop(inbox)


def test_files_end_to_end(tmp_path):
    src_dir = tmp_path / "资料"
    (src_dir / "sub").mkdir(parents=True)
    (src_dir / "a.txt").write_bytes(b"hello")
    (src_dir / "sub" / "b.bin").write_bytes(bytes(range(256)) * 100)
    entries, total, _w = ct.collect_sources([str(src_dir)])
    manifest = cb.pack_file_manifest(entries)
    blob = cb.marshall({cb.FMT_FILES: manifest})

    inbox, got = _start_inbox(tmp_path)
    try:
        ok, sent, msg = ct.push_clipboard("127.0.0.1", inbox.port, blob,
                                         entries, timeout=30)
        assert ok, msg
        # 落盘校验: 逐字节一致 + 结构保留
        recv_dir = inbox.last_recv_dir
        assert recv_dir and os.path.isdir(recv_dir)
        a = os.path.join(recv_dir, "资料", "a.txt")
        b = os.path.join(recv_dir, "资料", "sub", "b.bin")
        assert open(a, "rb").read() == b"hello"
        assert open(b, "rb").read() == bytes(range(256)) * 100
        # 回调拿到的是**实际落盘路径**(供设 CF_HDROP)
        files_payload = got[-1]
        paths = cb.unpack_paths(files_payload[cb.FMT_FILES])
        assert sorted(paths) == sorted([a, b])
        assert sent == total + len(blob)
    finally:
        _stop(inbox)


def test_clip_size_cap_rejects(tmp_path):
    inbox, got = _start_inbox(tmp_path, max_clip=1024)
    try:
        blob = cb.marshall({cb.FMT_TEXT: b"x" * 4096})
        ok, _sent, msg = ct.push_clipboard("127.0.0.1", inbox.port, blob,
                                          timeout=5)
        assert not ok, "超过接收方上限必须被拒"
        assert ("过大" in msg) or ("拒收" in msg) or ("确认" in msg), msg
        assert inbox.stats["rejected"] >= 1
        assert got == []
    finally:
        _stop(inbox)


def test_file_total_cap_enforced_on_sender(tmp_path):
    """发送侧就要拦住超量, 不能等连上对端再传一半。"""
    f = tmp_path / "big.bin"
    f.write_bytes(b"\x00" * 4096)
    entries = [{"name": "big.bin", "size": ct.MAX_FILE_BYTES + 1,
                "src": str(f)}]
    ok, _sent, msg = ct.push_clipboard(
        "127.0.0.1", 1, cb.marshall({cb.FMT_TEXT: b"x"}), entries)
    assert not ok and "文件总量过大" in msg


def test_file_count_cap_enforced_on_sender(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"\x00")
    entries = [{"name": f"f{i}.bin", "size": 1, "src": str(f)}
               for i in range(ct.MAX_FILES + 1)]
    ok, _sent, msg = ct.push_clipboard(
        "127.0.0.1", 1, cb.marshall({cb.FMT_TEXT: b"x"}), entries)
    assert not ok and "文件数过多" in msg


def test_malicious_path_is_rejected_on_receive(tmp_path):
    """对端清单里带 ``..`` 时必须整份拒收, 绝不能在用户磁盘上乱写。

    注意这里**不用 push_clipboard**(它只发本机已有文件, 清单名是消毒过的),
    而是直接按协议写帧, 模拟一个敌意对端。
    """
    inbox, got = _start_inbox(tmp_path)
    try:
        sock = socket.create_connection(("127.0.0.1", inbox.port), timeout=5)
        manifest = cb.pack_file_manifest([{"name": "../escape.txt",
                                           "size": 4}])
        blob = cb.marshall({cb.FMT_FILES: manifest})
        ct.send_frame(sock, ct.K_CLIP_START, struct.pack("<Q", len(blob)))
        ct.send_frame(sock, ct.K_CLIP_DATA, blob)
        ct.send_frame(sock, ct.K_CLIP_END)
        ct.send_frame(sock, ct.K_FILE_START, struct.pack("<HQ", 0, 4))
        ct.send_frame(sock, ct.K_FILE_DATA, b"evil")
        ct.send_frame(sock, ct.K_FILE_END)
        ack = ct.read_frame(sock)
        sock.close()
        assert ack is not None and ack[0] == ct.K_ACK
        assert ack[1][:1] == b"\x00", "带 .. 的路径必须被拒"
        assert not os.path.exists(os.path.join(str(tmp_path), "escape.txt")), \
            "绝不能写到接收目录之外"
    finally:
        _stop(inbox)


# --- 发送队列 ---------------------------------------------------------

def test_outbox_sends_in_order(tmp_path):
    inbox, got = _start_inbox(tmp_path)
    out = ct.ClipOutbox("127.0.0.1", inbox.port)
    out.start()
    try:
        for i in range(3):
            out.put(cb.marshall({cb.FMT_TEXT: f"msg{i}".encode()}))
        deadline = time.time() + 5
        while len(got) < 3 and time.time() < deadline:
            time.sleep(0.02)
        assert [g[cb.FMT_TEXT] for g in got] == [b"msg0", b"msg1", b"msg2"]
        assert out.sent == 3 and out.failed == 0
    finally:
        out.stop()
        _stop(inbox)
