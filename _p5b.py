"""审查项 #5(续): 剪贴板统计进状态窗口 + busy 标志修正。"""
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


# 1) busy 用真实标志
patch("com_bridge/clip_transfer.py", [
    (
        """        self._cancel = threading.Event()""",
        """        self._cancel = threading.Event()
        self._busy = False""",
    ),
    (
        """            "busy": self._t0 > 0 and not self._cancel.is_set()
            and self.sent + self.failed + self.cancelled == 0,""",
        """            "busy": self._busy,""",
    ),
    (
        """                self._cancel.clear()
                self._t0 = time.monotonic()""",
        """                self._cancel.clear()
                self._busy = True
                self._t0 = time.monotonic()""",
    ),
    (
        """                self.bytes_sent += sent
                self._log_done(ok, sent, msg)""",
        """                self._busy = False
                self.bytes_sent += sent
                self._log_done(ok, sent, msg)""",
    ),
])

# 2) 状态窗口: 新增"剪贴板"一行
patch("com_bridge/gui_windows.py", [
    (
        """    title = "Com Bridge · 实时状态"
    geometry = "720x560\"""",
        """    title = "Com Bridge · 实时状态"
    # 高度含"剪贴板"分组(0.4.7 起), 多一行 + 一个分组标题
    geometry = "720x620\"""",
    ),
    (
        """        ("hook", "输入链路", "键盘钩子"),
    )""",
        """        ("hook", "输入链路", "键盘钩子"),
        ("clipboard", "跨机功能", "剪贴板"),
    )""",
    ),
])

# 3) collect_status: 汇总剪贴板收发
patch("com_bridge/gui.py", [
    (
        """            rr = role.stats.get("render_ratio")
            if rr is not None:
                out["clock"] = (
                    f"渲染比例 {rr:.6f} / 到达率 "
                    f"{role.stats.get('arrival_rate', 0):.0f} Hz", 1)""",
        """            rr = role.stats.get("render_ratio")
            if rr is not None:
                out["clock"] = (
                    f"渲染比例 {rr:.6f} / 到达率 "
                    f"{role.stats.get('arrival_rate', 0):.0f} Hz", 1)
            out["clipboard"] = self._clip_status(role)""",
    ),
    (
        """                out["clock"] = (
                    f"rtt {clock.rtt_us / 1000:.2f}ms / 样本 {clock.samples}"
                    f" / 偏移跳变 {jumps} 次(最大 {maxjump:.2f}ms)"
                    f" ｜ 单调钟偏移 {clock.offset_us / 1000:+.0f}ms"
                    f"（两机开机时刻之差，换算用，不是时钟误差）",
                    1 if synced else 2)
        return out""",
        """                out["clock"] = (
                    f"rtt {clock.rtt_us / 1000:.2f}ms / 样本 {clock.samples}"
                    f" / 偏移跳变 {jumps} 次(最大 {maxjump:.2f}ms)"
                    f" ｜ 单调钟偏移 {clock.offset_us / 1000:+.0f}ms"
                    f"（两机开机时刻之差，换算用，不是时钟误差）",
                    1 if synced else 2)
            out["clipboard"] = self._clip_status(role)
        return out

    @staticmethod
    def _clip_status(role) -> tuple[str, int]:
        \"\"\"状态窗口的"剪贴板"一行: 发/收计数 + 文件数 + 流量。

        ★ 为什么需要(审查项 #5): 剪贴板是本程序的主力功能之一, 但它的
          统计(发了几次、收了几个文件、多少 MB)以前**只存在于日志里**,
          用户想知道"刚才那个 200MB 的文件夹到底传过去没有"只能翻日志。
        \"\"\"
        tx = getattr(role, "_clip_tx", None)
        rx = getattr(role, "_clip_rx", None)
        sync = getattr(role, "_clip_sync", None)
        if tx is None and rx is None:
            return ("未启用", 0)
        parts: list[str] = []
        level = 1
        if tx is not None:
            st = tx.stats() if hasattr(tx, "stats") else {}
            seg = f"发 {st.get('sent', 0)} 项"
            if st.get("bytes_sent"):
                seg += f" ({st['bytes_sent'] / 1048576:.1f} MB)"
            if st.get("busy"):
                seg += " · 传输中…"
            if st.get("queued"):
                seg += f" · 排队 {st['queued']}"
            if st.get("cancelled"):
                seg += f" · 取消 {st['cancelled']}"
            if st.get("failed"):
                seg += f" · !! 失败 {st['failed']}"
                level = 3
            parts.append(seg)
        if rx is not None:
            d = getattr(rx, "stats", {}) or {}
            seg = f"收 {d.get('payloads', 0)} 项"
            if d.get("files"):
                seg += f"（文件 {d['files']} 个"
                seg += f", {d.get('bytes', 0) / 1048576:.1f} MB）"
            if d.get("rejected"):
                seg += f" · !! 拒收 {d['rejected']}"
                level = 3
            parts.append(seg)
        if sync is not None:
            sd = getattr(sync, "stats", {}) or {}
            if sd.get("skipped_big"):
                parts.append(f"过大跳过 {sd['skipped_big']}")
            if sd.get("errors"):
                parts.append(f"!! 采集错误 {sd['errors']}")
                level = max(level, 2)
        return (" ｜ ".join(parts), level)""",
    ),
])
print("done")
