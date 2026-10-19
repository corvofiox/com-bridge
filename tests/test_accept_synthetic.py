# -*- coding: utf-8 -*-
"""回归: accept_synthetic 测试后门 + 生产过滤不可破坏。"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.input as input_mod  # noqa: E402
from com_bridge.server import Server  # noqa: E402


def test_accept_synthetic_default_off():
    """生产默认必须过滤 hDevice==NULL 的合成事件(防回声/防别的程序注入)。"""
    src = inspect.getsource(input_mod.RawInputListener._handle_raw)
    assert "accept_synthetic" in src
    # 生产入口不传 accept_synthetic -> 默认 False
    sig = inspect.signature(input_mod.RawInputListener.__init__)
    assert sig.parameters["accept_synthetic"].default is False


def test_accept_synthetic_still_filters_tag():
    """accept_synthetic=True 也**不得**放过带 TAG 的注入(回声防护不动)。"""
    src = inspect.getsource(input_mod.RawInputListener._handle_raw)
    # TAG 判定必须在 accept_synthetic 判定之前无条件 return
    assert src.index("InputInjector.TAG") < src.index("accept_synthetic")


def test_server_test_backdoor_exists():
    """Server 的测试后门存在且默认关(生产双机部署绝不能开)。"""
    sig = inspect.signature(Server.__init__)
    assert "accept_synthetic_input" in sig.parameters
    assert sig.parameters["accept_synthetic_input"].default is False
    src = inspect.getsource(Server._start_input)
    assert "accept_synthetic" in src
