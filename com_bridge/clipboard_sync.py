# -*- coding: utf-8 -*-
"""剪贴板变化检测 —— 只负责"本机复制了什么", 传输交给 clip_transfer.py。

## 分工(与 Deskflow 一致)
* **检测**: 轮询 `GetClipboardSequenceNumber()`, 变了才去读内容 ——
  比轮询内容便宜得多(`IClipboard::Time` / `MSWindowsClipboard::getTime`)。
* **防回环**: Deskflow 用**所有权标记**(`"Deskflow Ownership"` 格式)判断
  "这份内容是不是我自己写进去的"。照搬: 我们写剪贴板时带
  `"Com Bridge Ownership"`, 采样前先查它(见 `clipboard.is_owned_by_us`)。
  这比"写完后 1.2 秒内不采样"的时间窗可靠得多 —— 尤其对**文件**:
  落盘 + 设剪贴板可能几百毫秒, 时间窗早就过期了。
  (时间窗仍然保留, 用来吸收"标记被别的程序清掉"的极端情况。)
* **去重**: 与上次发出的内容哈希相同就跳过(用户可能重复复制同一份)。

## 传输
* 文本/HTML/图片 -> `cb.marshall()` 打包 -> `ClipOutbox.put(blob)` (TCP);
* 文件 -> 展开成条目 -> 清单放进剪贴板内容的 `files` 格式 ->
  同一连接上继续传文件内容。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time

from . import clipboard as cb

# 轮询间隔: 剪贴板变化检测很便宜(只读序列号), 但也别太频繁
POLL_INTERVAL_S = 0.25
# 自己写入后忽略变化的兜底时间窗(主要防环手段是所有权标记, 见文件头)
SELF_WRITE_GRACE_S = 1.2


class ClipboardSync:
    """采集侧: 监听本机剪贴板变化, 交给回调发送。

    on_copy(blob: bytes)      文本/HTML/图片(已 marshall)
    on_files(blob, entries)   文件: 剪贴板内容(带 files 清单) + 待传条目
    """

    def __init__(self, on_copy, enabled: bool = True, on_files=None):
        self._on_copy = on_copy
        self._on_files = on_files
        self._enabled = enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_seq = 0
        self._last_hash = b""
        self._last_files_hash = b""
        self._self_write_until = 0.0
        self.stats = {"sent": 0, "files_sent": 0, "skipped_self": 0,
                      "skipped_same": 0, "skipped_big": 0, "errors": 0}

    # -- 生命周期 ----------------------------------------------------

    def start(self) -> None:
        self._last_seq = cb.sequence_number()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ClipboardSync")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None

    def note_self_write(self) -> None:
        """自己刚写过剪贴板: 不要把它当成"用户复制"。"""
        self._self_write_until = time.monotonic() + SELF_WRITE_GRACE_S
        self._last_seq = cb.sequence_number()

    # -- 主循环 ------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001
                self.stats["errors"] = self.stats.get("errors", 0) + 1
            self._stop.wait(POLL_INTERVAL_S)

    def _poll_once(self) -> None:
        if not self._enabled:
            return
        seq = cb.sequence_number()
        if seq == self._last_seq:
            return
        self._last_seq = seq
        # ★ 防回环(第一道, Deskflow 的所有权标记): 是我们写的就整轮跳过
        if cb.is_owned_by_us():
            self.stats["skipped_self"] = \
                self.stats.get("skipped_self", 0) + 1
            return
        # 第二道兜底: 时间窗(标记可能被别的程序清掉)
        if time.monotonic() < self._self_write_until:
            self.stats["skipped_self"] = \
                self.stats.get("skipped_self", 0) + 1
            return

        # -- 文件优先: 复制文件时剪贴板里往往还带着路径文本, 混发没意义 --
        if self._on_files is not None:
            paths = cb.sample_files()
            if paths:
                self._send_files(paths)
                return

        payload = cb.sample()
        if not payload:
            return
        if sum(len(v) for v in payload.values()) > cb.MAX_PAYLOAD:
            self.stats["skipped_big"] = self.stats.get("skipped_big", 0) + 1
            return
        blob = cb.marshall(payload)
        if not blob:
            return
        h = hashlib.sha1(blob).digest()
        if h == self._last_hash:
            self.stats["skipped_same"] = \
                self.stats.get("skipped_same", 0) + 1
            return
        self._last_hash = h
        try:
            print(f"[clip] 发送 {cb.describe(payload)} / "
                  f"{len(blob)} 字节", flush=True)
            self._on_copy(blob)
            self.stats["sent"] = self.stats.get("sent", 0) + 1
        except Exception:  # noqa: BLE001
            self.stats["errors"] = self.stats.get("errors", 0) + 1

    def _send_files(self, paths: list[str]) -> None:
        from .clip_transfer import collect_sources
        entries, total, warnings = collect_sources(paths)
        if warnings:
            # 只打前几条: 整目录只有一个不可读文件时不要刷屏
            print(f"[clip] 文件清单警告 {len(warnings)} 条: "
                  f"{'; '.join(warnings[:3])}", flush=True)
        if not entries:
            return
        h = hashlib.sha1()
        for e in entries:
            h.update(e["name"].encode("utf-8", "replace"))
            h.update(b"\x00")
            h.update(str(e["size"]).encode())
            h.update(b"\x00")
            try:                       # 大小相同但内容变过(改过文件)也要重发
                h.update(str(int(os.path.getmtime(e["src"]))).encode())
            except OSError:
                pass
        digest = h.digest()
        if digest == self._last_files_hash:
            self.stats["skipped_same"] = \
                self.stats.get("skipped_same", 0) + 1
            return
        self._last_files_hash = digest
        manifest = cb.pack_file_manifest(entries)
        blob = cb.marshall({cb.FMT_FILES: manifest})
        if not blob:
            return
        try:
            self._on_files(blob, entries)
            self.stats["files_sent"] = self.stats.get("files_sent", 0) + 1
            print(f"[clip] 复制了 {len(entries)} 个文件({total} 字节) -> 开始同步",
                  flush=True)
        except Exception:  # noqa: BLE001
            self.stats["errors"] = self.stats.get("errors", 0) + 1
