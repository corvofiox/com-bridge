"""回归: 文件传输期间不得锁住源文件(审查/报障 2026-10-25)。

用户实测: 在主机上"剪切"文件、粘贴到主机另一个目录, **移动失败**(文件被占用)
—— 纯本机操作, 却是 Com Bridge 造成的。

根因: `push_clipboard()` 用 `open(src, "rb")` 读源文件。Python 在 Windows 上以
`_SH_DENYNO` 打开, 共享 READ|WRITE 但**不共享 DELETE**; 同卷移动 = rename,
需要 DELETE 权限, 于是传输期间资源管理器被拒(WinError 32)。

修法: `clip_transfer.open_shared_read()` 用
`CreateFileW(FILE_SHARE_READ|WRITE|DELETE)` + `open_osfhandle` 打开。
句柄跟着文件走, 移动/改名之后读取仍完整。
"""
import os
import struct
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from com_bridge import clipboard as cb  # noqa: E402
from com_bridge import clip_transfer as ct  # noqa: E402

WIN = os.name == "nt"


# ---------------------------------------------------------------- 根因与修法
@pytest.mark.skipif(not WIN, reason="共享模式是 Windows 语义")
def test_plain_open_blocks_rename_documents_the_bug(tmp_path):
    """说明为什么不能用 open(): 它会让资源管理器无法移动文件。"""
    p = tmp_path / "a.bin"
    p.write_bytes(b"x" * 1000)
    f = open(str(p), "rb")
    try:
        with pytest.raises(PermissionError):
            os.replace(str(p), str(tmp_path / "a.moved"))
    finally:
        f.close()
    # 关掉之后就能移动了(证明拦住它的正是这个句柄)
    os.replace(str(p), str(tmp_path / "a.moved"))
    assert (tmp_path / "a.moved").exists()


def test_shared_open_allows_rename_and_keeps_reading(tmp_path):
    p = tmp_path / "b.bin"
    data = b"payload-" * 900            # 7200 字节
    p.write_bytes(data)
    with ct.open_shared_read(str(p)) as fh:
        os.replace(str(p), str(tmp_path / "b.moved"))   # 传输期间被移动
        assert (tmp_path / "b.moved").read_bytes() == data
        assert fh.read() == data, "移动后仍必须能读完(句柄跟着文件走)"


def test_shared_open_allows_delete_during_read(tmp_path):
    p = tmp_path / "c.bin"
    data = b"q" * 4096
    p.write_bytes(data)
    with ct.open_shared_read(str(p)) as fh:
        os.unlink(str(p))               # 剪切到别的卷时是"复制+删除"
        assert fh.read() == data


def test_cross_volume_like_copy_delete_sequence(tmp_path):
    """模拟跨卷移动: 先复制, 再删除源(都发生在读取期间)。"""
    p = tmp_path / "d.bin"
    data = os.urandom(64 * 1024)
    p.write_bytes(data)
    dest = tmp_path / "d.copied"
    with ct.open_shared_read(str(p)) as fh:
        dest.write_bytes(p.read_bytes())
        os.unlink(str(p))
        assert fh.read() == data


def test_fallback_when_createfile_fails(tmp_path, monkeypatch):
    """CreateFileW 失败时必须退回普通 open(宁可占用也不能传不了)。"""
    p = tmp_path / "e.bin"
    p.write_bytes(b"fallback")

    class Boom:
        argtypes = None
        restype = None

        def __call__(self, *a, **k):
            raise OSError("CreateFileW 不可用")

    import ctypes
    monkeypatch.setattr(ctypes.windll.kernel32, "CreateFileW", Boom())
    with ct.open_shared_read(str(p)) as fh:
        assert fh.read() == b"fallback"


def test_missing_file_still_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        ct.open_shared_read(str(tmp_path / "nope.bin"))


# ---------------------------------------------------------------- 发送路径接线
def test_push_clipboard_uses_shared_open():
    """源码守卫: 发送源文件必须走 open_shared_read, 不能退回 open()。"""
    import inspect
    src = inspect.getsource(ct.push_clipboard)
    assert "open_shared_read(src)" in src
    assert 'open(src, "rb")' not in src, "不要退回普通 open: 它会锁住文件"


def test_missing_source_reports_clear_error_and_sends_nothing(tmp_path,
                                                             capsys):
    inbox, got = _inbox(tmp_path)
    try:
        entries = [{"src": str(tmp_path / "gone.bin"), "name": "gone.bin",
                    "size": 10}]
        blob = cb.marshall({cb.FMT_TEXT: b"hi",
                            cb.FMT_FILES: cb.pack_file_manifest(
                                [{"name": "gone.bin", "size": 10}])})
        ok, _sent, msg = ct.push_clipboard("127.0.0.1", inbox.port, blob,
                                           entries, timeout=3)
        assert ok is False
        assert "已不存在" in msg, msg
        time.sleep(0.2)
        # 剪贴板内容(文本)在文件之前就已完整传输, 被正常交付是**对的**;
        # 不该发生的是"文件落地"(那才会出现半截文件)。
        assert not any(cb.FMT_FILES in p for p in got), "半截传输不该落地文件"
        out = capsys.readouterr().out
        assert "!! 接收失败" not in out, f"中止不是错误, 不该刷红: {out}"
        assert "中止" in out
    finally:
        _stop(inbox)


def test_transfer_survives_rename_midflight(tmp_path, monkeypatch):
    """端到端: 发送端**已经打开源文件**后把文件改名, 传输仍应完成且内容一致。

    时序用事件握手固定下来(不靠 sleep 抢时间): 打开文件 -> 暂停 -> 测试改名
    -> 放行继续读。这正是用户"传输期间剪切/移动"的真实情形。
    """
    data = os.urandom(ct.CHUNK * 2 + 777)
    src = tmp_path / "big.bin"
    src.write_bytes(data)
    moved = tmp_path / "big.moved.bin"

    opened = threading.Event()
    proceed = threading.Event()
    real_open = ct.open_shared_read

    def hook(path):
        fh = real_open(path)
        opened.set()
        proceed.wait(5)                 # 等测试把文件改名
        return fh

    monkeypatch.setattr(ct, "open_shared_read", hook)

    inbox, got = _inbox(tmp_path)
    try:
        entries = [{"src": str(src), "name": "big.bin", "size": len(data)}]
        blob = cb.marshall({
            cb.FMT_TEXT: b"files",
            cb.FMT_FILES: cb.pack_file_manifest(
                [{"name": "big.bin", "size": len(data)}]),
        })
        result = {}

        def send():
            result["r"] = ct.push_clipboard("127.0.0.1", inbox.port, blob,
                                            entries, timeout=10)

        t = threading.Thread(target=send)
        t.start()
        assert opened.wait(5), "发送端应已打开源文件"
        os.replace(str(src), str(moved))        # ★ 用户报的动作
        proceed.set()
        t.join(15)

        ok, _sent, msg = result.get("r", (False, 0, "no result"))
        assert ok is True, msg
        # 等到"带文件清单"的那一次交付(文本那次先到, 不能只等 got 非空)
        deadline = time.time() + 5
        while (time.time() < deadline
               and not any(cb.FMT_FILES in p for p in got)):
            time.sleep(0.02)
        # 剪贴板内容与文件是**两次**交付: 找到带文件清单的那一次
        file_payload = next((p for p in got if cb.FMT_FILES in p), None)
        assert file_payload is not None, f"没收到文件交付: {[list(p) for p in got]}"
        paths = cb.unpack_paths(file_payload[cb.FMT_FILES])
        assert paths and open(paths[0], "rb").read() == data
    finally:
        proceed.set()
        _stop(inbox)


def test_rename_is_never_blocked_by_an_inflight_transfer(tmp_path, monkeypatch):
    """回归核心: 传输期间对源文件做 rename, 不允许出现"文件被占用"。"""
    data = os.urandom(ct.CHUNK + 5)
    src = tmp_path / "locked.bin"
    src.write_bytes(data)
    moved = tmp_path / "locked.moved.bin"

    opened = threading.Event()
    proceed = threading.Event()
    real_open = ct.open_shared_read

    def hook(path):
        fh = real_open(path)
        opened.set()
        proceed.wait(5)
        return fh

    monkeypatch.setattr(ct, "open_shared_read", hook)
    inbox, _got = _inbox(tmp_path)
    try:
        t = threading.Thread(
            target=lambda: ct.push_clipboard(
                "127.0.0.1", inbox.port,
                cb.marshall({
                    cb.FMT_TEXT: b"x",
                    cb.FMT_FILES: cb.pack_file_manifest(
                        [{"name": "locked.bin", "size": len(data)}]),
                }),
                [{"src": str(src), "name": "locked.bin",
                  "size": len(data)}], timeout=10))
        t.start()
        assert opened.wait(5)
        # 这里**不容错**: 用户报的就是这一步抛了占用错误
        os.replace(str(src), str(moved))
        assert moved.exists()
        proceed.set()
        t.join(15)
    finally:
        proceed.set()
        _stop(inbox)


# ---------------------------------------------------------------- 测试脚手架
def _inbox(tmp_path):
    got = []

    def on_payload(payload):
        got.append(payload)

    inbox = ct.ClipInbox(port=0, on_payload=on_payload,
                         recv_root=str(tmp_path / "recv"))
    inbox.bind_now()
    inbox.start()
    time.sleep(0.05)
    return inbox, got


def _stop(inbox):
    inbox.stop()
    time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
