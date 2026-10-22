# -*- coding: utf-8 -*-
"""配置读写的回归测试。"""
from __future__ import annotations

import json
import os
import pathlib

from com_bridge import config as cfg


def test_load_tolerates_utf8_bom(tmp_path, monkeypatch):
    """★ 2026-10-23: 记事本 / PowerShell Set-Content 写出的 JSON 带 UTF-8 BOM,
    json.load 会直接抛 ValueError, 整个配置退回默认值 —— 表现是
    "was_running=true 却不自动连接"。必须容 BOM。"""
    path = tmp_path / "config.json"
    doc = {"version": cfg._DEFAULTS["version"], "role": "agent",
           "was_running": True, "server_host": "192.168.10.12",
           "side": "right", "jitter_ms": 60.0, "last_exit": 0.0,
           "ui_theme": "auto"}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode("utf-8"))
    monkeypatch.setattr(cfg, "config_path", lambda: str(path))
    got = cfg.load()
    assert got["role"] == "agent"
    assert got["was_running"] is True
    assert got["server_host"] == "192.168.10.12"


def test_load_broken_json_falls_back_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cfg, "config_path", lambda: str(path))
    assert cfg.load()["role"] == cfg._DEFAULTS["role"]


def test_save_writes_without_bom(tmp_path, monkeypatch):
    """我们自己写出去的配置不能带 BOM(否则别的工具读不了)。"""
    path = tmp_path / "config.json"
    monkeypatch.setattr(cfg, "config_path", lambda: str(path))
    assert cfg.save({"role": "server", "was_running": True})
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert cfg.load()["was_running"] is True
