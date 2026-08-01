"""飞书通知属性测试（新模式：写知识库子页面 + 回传群链接）。"""

import json
import logging as _logging
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.notify import feishu as feishu_module
from sequoia_x.notify.feishu import FeishuNotifier


def make_settings() -> Settings:
    return Settings(
        db_path="data/test.db",
        start_date="2024-01-01",
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        wiki_parent_node="parent123",
        group_chat_id="oc_test",
    )


SPACE_ID = "space123"
DOC_ID = "doxctest"
PAGE_URL = "https://example.feishu.cn/wiki/node123"


def _mock_post_side(url, *args, **kwargs):
    if "tenant_access_token" in url:
        return MagicMock(status_code=200, json=lambda: {"code": 0, "tenant_access_token": "t-xxx", "expire": 7200})
    if "wiki/v2/spaces" in url:
        return MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": {"node": {"obj_token": DOC_ID, "url": PAGE_URL}}},
        )
    return MagicMock(status_code=200, json=lambda: {"code": 0})


def _mock_get_side(*args, **kwargs):
    return MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"space": {"space_id": SPACE_ID}}})


@given(
    symbols=st.lists(
        st.text(min_size=6, max_size=6, alphabet="0123456789"),
        min_size=1, max_size=10, unique=True,
    )
)
@h_settings(max_examples=30)
def test_publish_writes_doc_and_sends_link(symbols: list[str]) -> None:
    """属性 10/11：publish 写入的文档应包含所有 symbol，且群消息含页面链接。"""
    settings = make_settings()
    notifier = FeishuNotifier(settings)
    notifier.send(strategy_name="TestStrategy", symbols=symbols)

    captured: list[tuple[str, dict]] = []

    def _post(url, *a, **k):
        captured.append((url, k.get("json")))
        return _mock_post_side(url, *a, **k)

    with patch("requests.get", side_effect=_mock_get_side), patch(
        "requests.post", side_effect=_post
    ), patch.object(FeishuNotifier, "_get_stock_names", staticmethod(lambda *a, **k: {})):
        notifier.publish()

    docx_calls = [body for url, body in captured if "docx/v1/documents" in url]
    assert docx_calls, "应调用 docx 写入接口"
    doc_blob = json.dumps(docx_calls)
    for s in symbols:
        assert s in doc_blob

    im_calls = [body for url, body in captured if "im/v1/messages" in url]
    assert im_calls, "应向群发送消息"
    assert PAGE_URL in im_calls[0]["content"]


@given(status_code=st.integers(min_value=400, max_value=599))
@h_settings(max_examples=20)
def test_publish_failure_logs_error(status_code: int) -> None:
    """属性 12：任一接口失败时 publish 应记录 ERROR 且不抛异常。"""
    settings = make_settings()
    notifier = FeishuNotifier(settings)
    notifier.send(strategy_name="Test", symbols=["000001"])

    feishu_logger = _logging.getLogger(feishu_module.__name__)
    records: list[_logging.LogRecord] = []

    class _ListHandler(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            records.append(record)

    handler = _ListHandler(_logging.ERROR)
    feishu_logger.addHandler(handler)
    try:

        def _post_fail(url, *a, **k):
            if "wiki/v2/spaces" in url:
                return MagicMock(status_code=status_code, json=lambda: {"code": 99999})
            return _mock_post_side(url, *a, **k)

        with patch("requests.get", side_effect=_mock_get_side), patch(
            "requests.post", side_effect=_post_fail
        ), patch.object(FeishuNotifier, "_get_stock_names", staticmethod(lambda *a, **k: {})):
            notifier.publish()
    finally:
        feishu_logger.removeHandler(handler)

    assert any(r.levelno == _logging.ERROR for r in records)
