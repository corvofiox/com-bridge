"""审查项 #5: 文件/图片传输加进度与取消, 剪贴板统计进 GUI。"""
import pathlib

root = pathlib.Path(__file__).parent


def patch(rel, pairs):
    p = root / rel
    s = p.read_text(encoding="utf-8")
    for old, new in pairs:
        assert s.count(old) == 1, f"{rel}: 未命中/重复 {old[:60]!r} ({s.count(old)})"
        s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print(f"patched {rel}")


# ================================================================ clip_transfer
patch("com_bridge/clip_transfer.py", [
    # 上限: 2GB -> 16GB(原来是个随意门槛, 不是技术限制)
    (
        "MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024",
        "MAX_FILE_BYTES = 16 * 1024 * 1024 * 1024",
    ),
    # push_clipboard: 进度 + 取消
    (
        """def push_clipboard(host: str, port: int, blob: bytes,
                   entries: list[dict] | None = None,
                   timeout: float = ACK_TIMEOUT_S) -> tuple[bool, int, str]:
    \"\"\"把一份剪贴板内容(可选带文件)推给对端。返回 (成功, 发送字节, 说明)。\"\"\"""",
        """def push_clipboard(host: str, port: int, blob: bytes,
                   entries: list[dict] | None = None,
                   timeout: float = ACK_TIMEOUT_S,
                   on_progress=None,
                   cancel: "threading.Event | None" = None) -> tuple[bool, int, str]:
    \"\"\"把一份剪贴板内容(可选带文件)推给对端。返回 (成功, 发送字节, 说明)。

    on_progress(sent, total)  每传一段回调一次(由调用方自行节流/打印)
    cancel                    threading.Event, 置位后在下一个分块处中止
                              (返回 (False, 已发字节, "已取消"))

    ★ 为什么需要这两个参数(2026-10-23 审查项 #5): 传 200MB 文件时旧实现
      在整个过程中**一句话都不打印**, 用户完全不知道是在传、卡住了、还是
      根本失败了; 也无法中止 —— 只能杀进程。现在进度会进日志窗口,
      取消挂在"停止"上(停止服务即中止在途传输)。
    \"\"\"""",
    ),
    (
        """    sent = 0
    try:
        sock.settimeout(IO_TIMEOUT_S)
        send_frame(sock, K_CLIP_START, struct.pack("<Q", len(blob)))
        for off in range(0, len(blob), CHUNK):
            piece = blob[off:off + CHUNK]
            send_frame(sock, K_CLIP_DATA, piece)
            sent += len(piece)
        send_frame(sock, K_CLIP_END)""",
        """    total = len(blob) + total_files

    def _tick() -> bool:
        \"\"\"回调进度; 返回 True 表示调用方要求取消。\"\"\"
        if on_progress is not None:
            try:
                on_progress(sent, total)
            except Exception:              # noqa: BLE001
                pass
        return bool(cancel is not None and cancel.is_set())

    sent = 0
    try:
        sock.settimeout(IO_TIMEOUT_S)
        send_frame(sock, K_CLIP_START, struct.pack("<Q", len(blob)))
        for off in range(0, len(blob), CHUNK):
            if _tick():
                return False, sent, "已取消"
            piece = blob[off:off + CHUNK]
            send_frame(sock, K_CLIP_DATA, piece)
            sent += len(piece)
        send_frame(sock, K_CLIP_END)""",
    ),
    (
        """            send_frame(sock, K_FILE_START, struct.pack("<HQ", idx, size))
            with open(src, "rb") as fh:
                left = size
                while left > 0:
                    piece = fh.read(min(CHUNK, left))
                    if not piece:
                        break
                    send_frame(sock, K_FILE_DATA, piece)
                    left -= len(piece)
                    sent += len(piece)""",
        """            send_frame(sock, K_FILE_START, struct.pack("<HQ", idx, size))
            with open(src, "rb") as fh:
                left = size
                while left > 0:
                    if _tick():
                        return False, sent, "已取消"
                    piece = fh.read(min(CHUNK, left))
                    if not piece:
                        break
                    send_frame(sock, K_FILE_DATA, piece)
                    left -= len(piece)
                    sent += len(piece)""",
    ),
    (
        """        send_frame(sock, K_DONE)
        try:""",
        """        send_frame(sock, K_DONE)
        _tick()                            # 100%: 让界面停在"已发完, 等确认"
        try:""",
    ),
    # ClipOutbox: 进度打印 + 取消
    (
        """        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None""",
        """        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None
        self.bytes_sent = 0
        self.cancelled = 0
        # ★ 在途传输的取消开关(见 push_clipboard 的 cancel 参数)
        self._cancel = threading.Event()
        # 进度节流状态
        self._prog_t = 0.0
        self._prog_last = -1.0
        self._t0 = 0.0""",
    ),
    (
        """    def put(self, blob: bytes, entries: list[dict] | None = None) -> None:
        with self._lock:
            self._q.append((blob, entries or []))
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()""",
        """    def put(self, blob: bytes, entries: list[dict] | None = None) -> None:
        with self._lock:
            self._q.append((blob, entries or []))
        self._wake.set()

    def cancel_current(self) -> None:
        \"\"\"中止**在途**那一份传输(排队中的不受影响)。\"\"\"
        self._cancel.set()

    def stop(self) -> None:
        # 停止服务时也中止在途传输 —— 否则一个 2GB 的传输会把线程占住,
        # 进程退出要等它, 用户以为"关不掉"。
        self._cancel.set()
        self._stop.set()
        self._wake.set()

    # -- 进度(节流: 最多 2 行/秒, 且进度至少变化 1%) --------------------
    def _on_progress(self, done: int, total: int) -> None:
        now = time.monotonic()
        frac = (done / total) if total > 0 else 0.0
        if frac >= 1.0:
            return                          # 收尾由 _log_done 统一打印
        if now - self._prog_t < 0.5 and frac - self._prog_last < 0.01:
            return
        self._prog_t = now
        self._prog_last = frac
        speed = 0.0
        if self._t0 > 0 and now > self._t0:
            speed = done / (now - self._t0) / (1024 * 1024)
        print(f"[clip] 发送中 {frac * 100:.0f}% "
              f"({done / 1048576:.1f}/{total / 1048576:.1f} MB"
              + (f", {speed:.1f} MB/s" if speed > 0 else "") + ")",
              flush=True)

    def _log_done(self, ok: bool, sent: int, msg: str) -> None:
        dt = max(1e-6, time.monotonic() - self._t0) if self._t0 else 0.0
        mb = sent / 1048576
        if ok:
            speed = f", {mb / dt:.1f} MB/s" if dt > 0 else ""
            print(f"[clip] 已发送 {mb:.1f} MB 用时 {dt:.1f}s{speed}", flush=True)
        else:
            print(f"[clip] !! 发送未完成({mb:.1f} MB 已发): {msg}", flush=True)""",
    ),
    (
        """                ok, _sent, msg = push_clipboard(self.host, self.port,
                                                blob, entries)
                if ok:
                    self.sent += 1
                else:
                    self.failed += 1
                    self.last_error = msg
                    print(f"[clip] !! 发送失败: {msg}", flush=True)""",
        """                self._cancel.clear()
                self._t0 = time.monotonic()
                self._prog_t = 0.0
                self._prog_last = -1.0
                ok, sent, msg = push_clipboard(
                    self.host, self.port, blob, entries,
                    on_progress=self._on_progress, cancel=self._cancel)
                self.bytes_sent += sent
                self._log_done(ok, sent, msg)
                if ok:
                    self.sent += 1
                elif msg == "已取消":
                    self.cancelled += 1
                else:
                    self.failed += 1
                    self.last_error = msg""",
    ),
    # stats() 汇总
    (
        """    def _run(self) -> None:
        while not self._stop.is_set():""",
        """    def stats(self) -> dict:
        \"\"\"供 GUI 展示(状态窗口的"剪贴板"一行)。\"\"\"
        return {
            "sent": self.sent,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "bytes_sent": self.bytes_sent,
            "queued": len(self._q),
            "busy": self._t0 > 0 and not self._cancel.is_set()
            and self.sent + self.failed + self.cancelled == 0,
            "last_error": self.last_error,
        }

    def _run(self) -> None:
        while not self._stop.is_set():""",
    ),
])
print("done")
