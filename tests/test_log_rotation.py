"""日志运行时轮转的测试(审查项 #11)。

旧实现只在**启动时** `rename -> .old` 一次: 长跑进程不再轮转(用户实测单文件
已 1.5MB+), 而且只留一份快照, 上一次的现场会被覆盖。

新实现是"快照 + 截断"(copytruncate): 因为日志文件此刻正被 fd 1/2 打开,
Windows 上重命名打开中的文件会失败(共享冲突), 而复制一份再把 fd 截断
不需要改文件名。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from com_bridge import gui_log  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_fd(monkeypatch):
    """★ 关键隔离: 让 _live_path 不等于测试文件, 保证绝不 os.ftruncate(1, 0)。

    否则一旦实现有 bug, 测试会把 pytest 自己的 stdout 截断。
    """
    monkeypatch.setattr(gui_log, "_live_path", None)
    yield


def _write(path, text: str):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def test_rotate_if_big_is_noop_under_limit(tmp_path):
    p = str(tmp_path / "a.log")
    _write(p, "x" * 100)
    assert gui_log.rotate_if_big(p, max_bytes=1024) is False
    assert os.path.getsize(p) == 100
    assert not os.path.exists(p + ".1")


def test_rotate_if_big_snapshots_and_truncates(tmp_path):
    p = str(tmp_path / "a.log")
    _write(p, "HEAD" + "x" * 5000 + "TAIL")
    assert gui_log.rotate_if_big(p, max_bytes=1024) is True
    assert os.path.getsize(p) == 0, "轮转后当前日志必须清空"
    snap = open(p + ".1", encoding="utf-8").read()
    assert "TAIL" in snap, "快照必须保留末尾(最近的现场)"
    assert len(snap) <= gui_log.SNAPSHOT_TAIL


def test_snapshot_keeps_only_tail(tmp_path):
    p = str(tmp_path / "b.log")
    _write(p, "A" * 2000 + "RECENT")
    n = gui_log.snapshot_and_truncate(p, keep=3, tail=100)
    assert n == 100
    snap = open(p + ".1", encoding="utf-8").read()
    assert snap.endswith("RECENT") and len(snap) == 100


def test_snapshots_shift_and_oldest_is_dropped(tmp_path):
    p = str(tmp_path / "c.log")
    for name in ("c.log.1", "c.log.2", "c.log.3"):
        (tmp_path / name).write_text("old:" + name, encoding="utf-8")
    _write(p, "fresh" * 50)
    gui_log.snapshot_and_truncate(p, keep=3, tail=50)
    assert (tmp_path / "c.log.1").read_text(encoding="utf-8") != "old:c.log.1"
    assert (tmp_path / "c.log.2").read_text(encoding="utf-8") == "old:c.log.1"
    assert (tmp_path / "c.log.3").read_text(encoding="utf-8") == "old:c.log.2"
    # 最老的那份(.3)被丢掉, 不该再出现第 4 份
    assert not (tmp_path / "c.log.4").exists()


def test_rotate_missing_file_is_safe(tmp_path):
    p = str(tmp_path / "missing.log")
    assert gui_log.rotate_if_big(p, max_bytes=1) is False


def test_empty_file_does_not_create_snapshot(tmp_path):
    p = str(tmp_path / "empty.log")
    open(p, "w").close()
    assert gui_log.snapshot_and_truncate(p) == 0
    assert not os.path.exists(p + ".1")


def test_truncate_never_touches_fd1_when_path_differs(monkeypatch, tmp_path):
    """_live_path 指向别处时, 绝不能截断 fd 1(那可能是控制台/管道)。"""
    called = []
    monkeypatch.setattr(gui_log.os, "ftruncate",
                        lambda fd, n: called.append(fd))
    p = str(tmp_path / "d.log")
    _write(p, "zzz")
    assert gui_log._truncate_live(p) is True
    assert called == [], "路径不是当前日志时不该碰 fd"

    monkeypatch.setattr(gui_log, "_live_path", p)
    gui_log._truncate_live(p)
    assert called == [1], "路径匹配时才通过 fd 1 截断"


def test_default_limit_is_at_least_2mb(monkeypatch):
    assert gui_log.MAX_LOG_BYTES >= 2 * 1024 * 1024
    assert gui_log.KEEP_SNAPSHOTS >= 2


def test_rotator_thread_is_singleton(tmp_path, monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            started.append(name)

        def start(self):
            pass

    monkeypatch.setattr(gui_log, "_rotate_thread", None)
    monkeypatch.setattr(gui_log.threading, "Thread", FakeThread)
    gui_log._start_rotator(str(tmp_path / "e.log"))
    gui_log._start_rotator(str(tmp_path / "e.log"))
    assert started == ["LogRotate"], "重复 install 不能起多个轮转线程"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
