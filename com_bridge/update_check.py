# -*- coding: utf-8 -*-
"""自动更新检查(审查项 #7 之一)。

## 为什么需要
用户在两台机器上各跑一份, 每次发版都得手工 scp + 替换 exe + 重启计划任务。
程序自己知道"有新版本"是最省事的一步 —— 至少能提示"去下新版", 而不是让
用户永远停在旧版本上(本项目的版本迭代很频繁: 一晚上能发几版)。

## 设计取舍
* **只提示, 不自动替换**。自替换需要在运行中覆盖自己的 exe(Windows 会锁
  文件), 常规做法是"下载到临时目录 + 起一个替换进程 + 退出", 这条路一旦
  出错就是"程序打不开", 代价远大于收益。所以这里只做:
  查 -> 比较版本 -> 界面提示 + 日志 + 可点击打开 Release 页。
* **不打扰**: 6 小时最多查一次(结果记进 config.json), 失败静默
  (没网、GitHub 不通、企业代理拦截都不该在界面上留红字)。
* **不引入依赖**: 只用 `urllib.request` + `json`。
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

from . import __version__

API_URL = ("https://api.github.com/repos/corvofiox/com-bridge"
           "/releases/latest")
RELEASES_PAGE = "https://github.com/corvofiox/com-bridge/releases/latest"
# 查得太勤没意义, 而且 GitHub 匿名 API 有速率限制(60 次/小时/IP)
CHECK_INTERVAL_S = 6 * 3600.0
_TIMEOUT_S = 4.0


def parse_version(text: str) -> tuple[int, ...]:
    """把 "v0.4.6" / "0.4.6" / "0.4.6-beta" 变成可比较的元组。

    ★ 只取数字段, 后缀(-beta/rc1)一律忽略: 本项目的版本号是
      `0.4.x` 单调递增的数字段, 预发布标记不参与"谁更新"的判断。
      解析不出数字时返回 () —— 调用方按"不知道"处理, 绝不猜。
    """
    if not text:
        return ()
    nums = re.findall(r"\d+", str(text))
    if not nums:
        return ()
    return tuple(int(n) for n in nums[:4])


def is_newer(candidate: str, current: str = __version__) -> bool:
    """candidate 是否比 current 新(元组比较, 短的一方补 0)。"""
    a = parse_version(candidate)
    b = parse_version(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b


def fetch_latest(timeout: float = _TIMEOUT_S) -> dict | None:
    """查 GitHub 最新 release。返回 {version, url, name} 或 None。

    任何异常都返回 None(离线/限流/解析失败都当作"不知道")。
    """
    req = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"ComBridge/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            raw = resp.read(256 * 1024)
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "")
    if not parse_version(tag):
        return None
    return {
        "version": tag.lstrip("vV"),
        "url": str(data.get("html_url") or RELEASES_PAGE),
        "name": str(data.get("name") or tag),
    }


def check(current: str = __version__, force: bool = False,
          now: float | None = None) -> dict | None:
    """查一次更新(带 6 小时缓存)。返回 {version, url, name, newer} 或 None。

    ★ 缓存写在 config.json 里, 所以**进程重启不会重新查**, 也不会因为
      反复开窗口而触发限流。
    """
    from . import config

    now = time.time() if now is None else float(now)
    cfg = config.load()
    cached_at = float(cfg.get("update_checked_at") or 0.0)
    cached_ver = str(cfg.get("update_latest") or "")
    cached_url = str(cfg.get("update_url") or "")
    if not force and cached_at > 0 and (now - cached_at) < CHECK_INTERVAL_S:
        if not cached_ver:
            return None                     # 上次查过, 没有可用结果
        return {
            "version": cached_ver,
            "url": cached_url or RELEASES_PAGE,
            "name": f"v{cached_ver}" if not cached_ver.startswith("v")
            else cached_ver,
            "newer": is_newer(cached_ver, current),
            "cached": True,
        }
    info = fetch_latest()
    config.update(update_checked_at=now,
                  update_latest=(info or {}).get("version", ""),
                  update_url=(info or {}).get("url", ""))
    if not info:
        return None
    return {
        "version": info["version"],
        "url": info["url"],
        "name": info["name"],
        "newer": is_newer(info["version"], current),
        "cached": False,
    }
