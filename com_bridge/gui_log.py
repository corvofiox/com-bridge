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
import time


def _log_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "ComBridge", "logs")
    os.makedirs(d, exist_ok=True)
    return d


def _rotate_if_big(path: str, max_bytes: int = 2 * 1024 * 1024) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".old")
    except OSError:
        pass


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
        path = os.path.join(_log_dir(), "com-bridge-gui.log")
        _rotate_if_big(path)
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
        return path
    except OSError:
        return None
