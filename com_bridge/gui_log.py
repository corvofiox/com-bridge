# -*- coding: utf-8 -*-
"""GUI 的文件日志 —— 让 --noconsole 打包下 [server]/[agent] 的诊断
打印不再输出到虚空。

★★ 为什么需要它(实测踩坑):
  com-bridge-gui.exe 是 PyInstaller `--noconsole` 打包的, `print(...)`
  的 stdout 是 **无效句柄** —— 所有 `[server] 光标越界`、
  `[server] 键盘钩子自检失败` 等诊断行**全部丢失**。
  用户真机排障时(主机上"钩子吞输入 鼠标有数字 键盘0 回调0"), 我们
  在用户机器上**一个字的证据都拿不到**, 只能靠 GUI 状态行几个数字猜。

设计:
  * `install()` 用 `os.dup2` 把 fd 1/2 重定向到日志文件 —— 这是唯一
    对**所有**已存在的 print/异常处理都生效的办法(改 sys.stdout 只对
    之后新建的对象生效, 且不覆盖 C 层缓冲)。
  * 日志写到 `%LOCALAPPDATA%\\ComBridge\\logs\\com-bridge-gui.log`,
    每次启动追加一段带时间戳的分隔行, 超过 2MB 自动轮转(改名 .old)。
  * 线程安全: os.dup2 是原子操作; print 自带行缓冲即可, 不额外加锁
    (诊断日志允许偶发交错, 不允许丢)。
"""
from __future__ import annotations

import os
import sys
import threading
import time

# ★ 审查项 #11(2026-10-23): 日志**运行时**也要轮转。
#   旧的实现只在启动时 `os.replace(path, path+".old")` 一次:
#     * 只保留一份 .old, 上一次的现场会被覆盖;
#     * 长时间不重启则完全不轮转 —— 一个跑了三天的进程能把日志写到几百 MB
#       (用户实测单文件已经 1.5MB+), 出事时反而拖不开。
#   现在: 超过上限就"快照 + 截断"(copytruncate)。
#     为什么不直接 rename: 日志文件此刻正被 fd 1/2 打开, Windows 上
#     重命名打开中的文件会失败(共享冲突); 而"复制一份 + 把 fd 截断"
#     不需要改文件名, 也就不会被占用卡住。
MAX_LOG_BYTES = int(os.environ.get("COM_BRIDGE_LOG_MAX") or 8 * 1024 * 1024)
KEEP_SNAPSHOTS = 3          # 保留 .1/.2/.3 三份
SNAPSHOT_TAIL = 512 * 1024  # 每份快照只留末尾 512KB
ROTATE_INTERVAL_S = 30.0

_rotate_thread: "threading.Thread | None" = None
_live_path: "str | None" = None


def _log_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "ComBridge", "logs")
    os.makedirs(d, exist_ok=True)
    return d


def snapshot_and_truncate(path: str, keep: int = KEEP_SNAPSHOTS,
                          tail: int = SNAPSHOT_TAIL) -> int:
    """把当前日志的末尾一段存成 .1, 旧快照顺移; 返回快照字节数(0=没做)。

    只做文件操作, 不碰 fd 1 —— 便于单测。
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= 0:
                return 0
            f.seek(max(0, size - tail))
            data = f.read()
    except OSError:
        return 0
    # 旧快照顺移: .(keep-1) -> .keep, ..., .1 -> .2 (最老的丢掉)
    for i in range(keep - 1, 0, -1):
        src, dst = f"{path}.{i}", f"{path}.{i + 1}"
        try:
            if os.path.exists(src):
                os.replace(src, dst)
        except OSError:
            pass
    try:
        with open(path + ".1", "wb") as f:
            f.write(data)
    except OSError:
        return 0
    return len(data)


def _truncate_live(path: str) -> bool:
    """把正在写的日志清空。优先截断 fd 1(stdout 就指向它), 退回复写。"""
    if _live_path == path:
        try:
            os.ftruncate(1, 0)          # 只在 install() 之后才这么做
            return True
        except OSError:
            pass
    try:
        with open(path, "w", encoding="utf-8"):
            pass
        return True
    except OSError:
        return False


def rotate_if_big(path: str, max_bytes: int | None = None) -> bool:
    """超过上限就轮转一次(快照 + 截断)。返回是否真的轮转了。"""
    limit = MAX_LOG_BYTES if max_bytes is None else max_bytes
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= limit:
            return False
    except OSError:
        return False
    if snapshot_and_truncate(path) <= 0:
        return False
    return _truncate_live(path)


def _start_rotator(path: str) -> None:
    """后台每 30 秒检查一次大小(长跑进程也能被轮转)。"""
    global _rotate_thread
    if _rotate_thread is not None:
        return

    def loop() -> None:
        while True:
            time.sleep(ROTATE_INTERVAL_S)
            try:
                rotate_if_big(path)
            except Exception:            # noqa: BLE001
                pass

    _rotate_thread = threading.Thread(target=loop, daemon=True,
                                      name="LogRotate")
    _rotate_thread.start()


def write_line(text: str) -> None:
    """直接把一行写进日志文件(不经过 stdout 重定向)。

    ★ 为什么需要它: `_log_q` 只喂**界面**, 进程结束就没了。而"托盘是否
      就绪""启动为何失败"这类信息, 我们需要在用户机器上**事后**查看。
      `print` 理论上已重定向到同一文件, 但在 `--noconsole` 下 stdout
      可能仍不可用, 所以关键诊断直接落盘更可靠。

    永不抛异常(日志失败不该影响程序)。
    """
    try:
        path = os.path.join(_log_dir(), "com-bridge-gui.log")
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text.rstrip("\n") + "\n")
    except OSError:
        pass


class _TimestampedStream:
    """把每一次 write 的内容按行加时间戳前缀(毫秒精度)。

    ★ 为什么需要(2026-10-21, 排查音频爆音时被卡住):
      日志原来**没有任何时间戳**, 于是"用户听到爆音的瞬间"与"日志里
      水位/欠载/见底等计数"**无法对齐** —— 只能靠猜, 白白多花了几轮。
      加前缀后, 任何一次现象都能用钟点时间直接定位到日志行。

    只做最小实现: 缓冲到换行为止, 然后整行加前缀写出去。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            ts = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
            self._inner.write(f"{ts}  {line}\n")
        return len(s)

    def flush(self) -> None:
        if self._buf:
            ts = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
            self._inner.write(f"{ts}  {self._buf}\n")
            self._buf = ""
        try:
            self._inner.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def fileno(self):
        return self._inner.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._inner, "encoding", "utf-8")

    def __getattr__(self, item):
        return getattr(self._inner, item)


def install() -> str | None:
    """把 stdout/stderr 重定向到日志文件; 返回路径, 失败返回 None。

    在 GUI 进程**最早期**调用一次(比如 cli.main 的 gui 分支开头)。
    console 版 exe 不需要也不受影响(只有 stdout 无效时才重定向)。
    """
    # 已有有效的控制台就不动它(console 版要保留实时输出)
    if sys.stdout is not None and sys.stdout.isatty():
        return None
    try:
        global _live_path
        path = os.path.join(_log_dir(), "com-bridge-gui.log")
        _live_path = path
        rotate_if_big(path)
        # 追加一段启动分隔, 方便按会话检索
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n===== session {time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"pid={os.getpid()} =====\n")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        os.dup2(fd, 1)          # stdout (fd 层)
        os.dup2(fd, 2)          # stderr
        if fd > 2:
            os.close(fd)
        # ★★ 只 dup2 **不够**: --noconsole 下 Python 启动时把
        #   sys.stdout/stderr 初始化为 **None**(无控制台), print
        #   根本不走 fd 1 —— 实测 10 个 GUI 会话只有分隔行、角色
        #   输出一个都没落下。必须**重建**这俩对象指向新 fd。
        import io
        import os as _os
        if _os.isatty(1) is False:
            # fd1 现在是日志文件: 用行缓冲的文本流包它
            sys.stdout = _TimestampedStream(io.open(
                1, "w", encoding="utf-8", errors="replace", buffering=1,
                closefd=False))
            sys.stderr = _TimestampedStream(io.open(
                2, "w", encoding="utf-8", errors="replace", buffering=1,
                closefd=False))
        _start_rotator(path)
        return path
    except OSError:
        return None
