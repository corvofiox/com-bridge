# -*- coding: utf-8 -*-
"""GUI 配置持久化 —— 让程序记住"上次的角色/IP/是否运行中"。

## 为什么需要它
用户要求(2026-10-19):
  "副机模块需要增加一个记忆IP的功能和记忆启动状态的功能。
   如果程序是从启动状态退出的, 而非停止状态退出的,
   下次程序启动时自动重连上次连接的主机IP。"

要实现"开机后静默自动连接", 必须跨进程记住三件事:
  1. 上次选的**角色**(主电脑 / 副电脑) —— 决定了启动后跑什么
  2. 副机的**主电脑 IP** 与延迟目标(主机还要记方位)
  3. 退出时是否处于**运行中** —— 只有"运行状态下退出"才自动重连;
     用户主动点了"停止"再退出, 下次**不该**自作主张连上

## 存放位置
`%LOCALAPPDATA%\\ComBridge\\config.json`。
选 LOCALAPPDATA 而非程序目录: 安装到 Program Files 时程序目录不可写。
"""
from __future__ import annotations

import json
import os
import tempfile
import time

# 配置版本: 将来结构变化时用于迁移/丢弃旧配置
CONFIG_VERSION = 1

_DEFAULTS = {
    "version": CONFIG_VERSION,
    "role": "server",          # server | agent
    "side": "right",           # 副电脑位于主电脑的哪一侧
    "jitter_ms": 60.0,         # 抖动缓冲目标
    "server_host": "",         # 副机记住的主电脑 IP
    # ★ 只有它为 True 时才在下次启动自动连接(见模块说明)
    "was_running": False,
    # 上次退出时间(诊断用, 也让"多久没动过"可读)
    "last_exit": 0.0,
}


def config_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "ComBridge")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config.json")


def load() -> dict:
    """读取配置; 任何异常都退回默认值(配置坏掉不该阻止程序启动)。"""
    cfg = dict(_DEFAULTS)
    try:
        path = config_path()
        if not os.path.exists(path):
            return cfg
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return cfg
        # 版本不符: 保守地只保留能认出的键
        if data.get("version") != CONFIG_VERSION:
            data.pop("version", None)
        for k, v in _DEFAULTS.items():
            if k in data and isinstance(data[k], type(v)):
                cfg[k] = data[k]
            elif k in data and isinstance(v, float) and isinstance(
                    data[k], (int, float)):
                cfg[k] = float(data[k])       # int 也接受, 统一成 float
        return cfg
    except (OSError, ValueError, TypeError):
        return cfg


def save(cfg: dict) -> bool:
    """原子写入(先写临时文件再 replace), 避免断电/崩溃留下半个文件。

    返回是否成功 —— 失败时调用方可以决定是否提示用户,
    但**绝不抛异常**: 配置写不进去不该让程序崩。
    """
    try:
        path = config_path()
        payload = dict(_DEFAULTS)
        payload.update({k: v for k, v in (cfg or {}).items() if k in _DEFAULTS})
        payload["version"] = CONFIG_VERSION
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return True
    except OSError:
        return False


def mark_running(role: str, side: str, jitter_ms: float,
                 server_host: str) -> bool:
    """启动成功时调用: 记住"现在是运行中"以及全部连接参数。

    下次进程启动时若看到 was_running=True, 就自动按这些参数连接。
    """
    cfg = load()
    cfg.update({
        "role": role,
        "side": side,
        "jitter_ms": float(jitter_ms),
        "server_host": server_host or "",
        "was_running": True,
    })
    return save(cfg)


def mark_stopped() -> bool:
    """用户主动**停止**时调用: 清除"运行中"标志。

    ★ 这是"记忆启动状态"的关键语义: 用户点了停止, 说明他不想再连了,
      下次启动就不该自动连。与"直接关窗口/崩溃退出"区分开。
    """
    cfg = load()
    cfg["was_running"] = False
    cfg["last_exit"] = time.time()
    return save(cfg)


def mark_exit_keeping_state() -> bool:
    """进程退出时调用(不改变 was_running): 只记录退出时间。"""
    cfg = load()
    cfg["last_exit"] = time.time()
    return save(cfg)


def should_autostart() -> bool:
    """下次启动是否应自动连接(上次是在运行状态下退出的)。"""
    return bool(load().get("was_running"))


def update(**kwargs) -> bool:
    """更新任意已知字段(角色切换/IP 输入等即时保存)。"""
    cfg = load()
    for k, v in kwargs.items():
        if k in _DEFAULTS:
            cfg[k] = v
    return save(cfg)


def default_host_for_autostart() -> str:
    """自动重连时用的主机地址(空字符串表示还没填过)。"""
    return str(load().get("server_host") or "")
