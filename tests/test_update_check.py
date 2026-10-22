"""自动更新检查的测试(审查项 #7)。

设计取舍见 update_check.py 的模块说明: 只提示、不自动替换 exe;
查不到(离线/限流/解析失败)一律静默, 绝不在界面上留红字。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from com_bridge import update_check as uc  # noqa: E402


# ---------------------------------------------------------------- 版本比较
@pytest.mark.parametrize("candidate,current,expected", [
    ("0.4.7", "0.4.6", True),
    ("v0.4.7", "0.4.6", True),
    ("0.10.0", "0.9.9", True),          # 字符串比较会错的那种
    ("0.4.6", "0.4.6", False),
    ("0.4.5", "0.4.6", False),
    ("0.4", "0.4.0", False),            # 短的一方补 0 -> 相等
    ("1.0.0", "0.99.99", True),
    ("", "0.4.6", False),
    ("nightly", "0.4.6", False),        # 解析不出数字 -> 不猜
])
def test_is_newer(candidate, current, expected):
    assert uc.is_newer(candidate, current) is expected


def test_parse_version_handles_prefixes_and_suffixes():
    assert uc.parse_version("v0.4.6") == (0, 4, 6)
    assert uc.parse_version("0.4.6-beta1") == (0, 4, 6, 1)
    assert uc.parse_version("garbage") == ()


# ---------------------------------------------------------------- 网络层
def _fake_resp(payload, status=200):
    class R:
        def __init__(self):
            self.status = status
            self._b = json.dumps(payload).encode()

        def read(self, n=None):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return R()


def test_fetch_latest_parses_release(monkeypatch):
    payload = {"tag_name": "v0.4.7", "html_url": "https://x/rel",
               "name": "Com Bridge v0.4.7"}
    monkeypatch.setattr(uc.urllib.request, "urlopen",
                        lambda req, timeout=0: _fake_resp(payload))
    info = uc.fetch_latest()
    assert info == {"version": "0.4.7", "url": "https://x/rel",
                    "name": "Com Bridge v0.4.7"}


@pytest.mark.parametrize("payload", [
    {"tag_name": "", "html_url": "u"},
    {"tag_name": "nightly"},
    ["not", "a", "dict"],
])
def test_fetch_latest_rejects_useless_payloads(monkeypatch, payload):
    monkeypatch.setattr(uc.urllib.request, "urlopen",
                        lambda req, timeout=0: _fake_resp(payload))
    assert uc.fetch_latest() is None


def test_fetch_latest_is_silent_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("no route to host")
    monkeypatch.setattr(uc.urllib.request, "urlopen", boom)
    assert uc.fetch_latest() is None


def test_fetch_latest_is_silent_on_bad_json(monkeypatch):
    class R:
        status = 200

        def read(self, n=None):
            return b"<html>proxy error</html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(uc.urllib.request, "urlopen",
                        lambda req, timeout=0: R())
    assert uc.fetch_latest() is None


# ---------------------------------------------------------------- 缓存
@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """把 config 指到临时目录, 不污染真实 %LOCALAPPDATA%。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


def test_check_caches_result_and_skips_second_request(isolated_config,
                                                      monkeypatch):
    calls = []

    def fake_fetch(timeout=0):
        calls.append(1)
        return {"version": "0.4.9", "url": "https://x", "name": "n"}

    monkeypatch.setattr(uc, "fetch_latest", fake_fetch)
    first = uc.check(current="0.4.6", now=1000.0)
    assert first["newer"] is True and first["cached"] is False
    second = uc.check(current="0.4.6", now=1001.0)       # 1 秒后
    assert second["cached"] is True and second["version"] == "0.4.9"
    assert len(calls) == 1, "6 小时内不该重复请求"


def test_check_refetches_after_interval(isolated_config, monkeypatch):
    calls = []

    def fake_fetch(timeout=0):
        calls.append(1)
        return {"version": "0.4.9", "url": "https://x", "name": "n"}

    monkeypatch.setattr(uc, "fetch_latest", fake_fetch)
    uc.check(current="0.4.6", now=1000.0)
    uc.check(current="0.4.6", now=1000.0 + uc.CHECK_INTERVAL_S + 1)
    assert len(calls) == 2


def test_check_force_bypasses_cache(isolated_config, monkeypatch):
    calls = []
    monkeypatch.setattr(uc, "fetch_latest",
                        lambda timeout=0: calls.append(1) or {
                            "version": "0.4.9", "url": "u", "name": "n"})
    uc.check(now=1000.0)
    uc.check(now=1000.0, force=True)
    assert len(calls) == 2


def test_check_returns_none_and_remembers_failure(isolated_config,
                                                  monkeypatch):
    monkeypatch.setattr(uc, "fetch_latest", lambda timeout=0: None)
    assert uc.check(now=2000.0) is None
    # 失败也写缓存: 否则每次开界面都会重试, 离线时白等 4 秒
    from com_bridge import config
    cfg = config.load()
    assert float(cfg["update_checked_at"]) == 2000.0
    assert cfg["update_latest"] == ""


def test_check_marks_older_release_as_not_newer(isolated_config, monkeypatch):
    monkeypatch.setattr(uc, "fetch_latest", lambda timeout=0: {
        "version": "0.4.5", "url": "u", "name": "n"})
    info = uc.check(current="0.4.6", now=3000.0)
    assert info is not None and info["newer"] is False


def test_config_round_trips_update_fields(isolated_config):
    from com_bridge import config
    assert config.update(update_checked_at=123.0, update_latest="0.5.0",
                         update_url="https://x") is True
    cfg = config.load()
    assert cfg["update_latest"] == "0.5.0"
    assert float(cfg["update_checked_at"]) == 123.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
