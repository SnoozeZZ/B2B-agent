from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.tasks as task_module
from app.config import get_settings
from app.database import Base
from app.models import Inquiry, RawMessage
from app.wecom_aibot import (
    WecomAIBotMessageIgnored,
    WecomAIBotReplyFailed,
    handle_wecom_aibot_frame,
    inbound_message_from_frame,
)
from app.wecom_aibot_listener import _SafeSdkLogger


def _settings(**overrides):
    defaults = {
        "wecom_aibot_id": "bot-001",
        "wecom_aibot_secret": "test-secret",
        "wecom_aibot_allowed_chat_ids": (),
        "wecom_aibot_accept_single_chat": False,
        "wecom_aibot_auto_reply": True,
    }
    defaults.update(overrides)
    return replace(get_settings(), **defaults)


def _text_frame(msgid: str = "aibot-msg-001") -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "req-001"},
        "body": {
            "msgid": msgid,
            "aibotid": "bot-001",
            "chatid": "internal-group-001",
            "chattype": "group",
            "from": {"userid": "zhangsan"},
            "response_url": "https://example.invalid/ephemeral-secret",
            "msgtype": "text",
            "text": {
                "content": (
                    "@询价机器人 品牌：Nike\nSKU：DD123\n采购价：100 CNY\n"
                    "MOQ：20\n行业：运动零售\n区域：上海\n交货地：上海"
                )
            },
        },
    }


def test_maps_official_text_frame_without_persisting_response_url():
    message = inbound_message_from_frame(_text_frame(), _settings())

    assert message.channel == "wecom_aibot"
    assert message.external_id == "aibot-msg-001"
    assert message.sender_name == "zhangsan"
    assert message.sender_contact == "wecom:zhangsan"
    assert "SKU：DD123" in message.content
    assert message.attachments == [
        {
            "type": "wecom_aibot_context",
            "aibotid": "bot-001",
            "chatid": "internal-group-001",
            "chattype": "group",
            "msgtype": "text",
        }
    ]
    assert "response_url" not in str(message.model_dump())


def test_mixed_message_uses_text_parts_and_enforces_chat_policy():
    frame = _text_frame("aibot-mixed-001")
    frame["body"].pop("text")
    frame["body"]["msgtype"] = "mixed"
    frame["body"]["mixed"] = {
        "msg_item": [
            {"msgtype": "text", "text": {"content": "品牌：Adidas"}},
            {"msgtype": "image", "image": {"url": "https://example.invalid/image"}},
            {"msgtype": "text", "text": {"content": "SKU：AD-01"}},
        ]
    }

    message = inbound_message_from_frame(
        frame,
        _settings(wecom_aibot_allowed_chat_ids=("internal-group-001",)),
    )
    assert message.content == "品牌：Adidas\nSKU：AD-01"

    with pytest.raises(WecomAIBotMessageIgnored, match="ALLOWED_CHAT_IDS"):
        inbound_message_from_frame(
            frame,
            _settings(wecom_aibot_allowed_chat_ids=("another-group",)),
        )

    frame["body"]["aibotid"] = "another-bot"
    with pytest.raises(WecomAIBotMessageIgnored, match="another AIBot"):
        inbound_message_from_frame(frame, _settings())


def test_listener_enqueues_then_replies_with_original_frame():
    frame = _text_frame("aibot-enqueue-001")
    enqueued: list[dict] = []

    class FakeClient:
        def __init__(self):
            self.replies: list[tuple[dict, str, str, bool]] = []

        async def reply_stream(self, original: dict, stream_id: str, content: str, finish: bool):
            self.replies.append((original, stream_id, content, finish))

    def enqueue(payload: dict):
        enqueued.append(payload)
        return SimpleNamespace(id="celery-task-001")

    client = FakeClient()
    result = asyncio.run(
        handle_wecom_aibot_frame(
            frame,
            client=client,
            settings=_settings(),
            enqueue=enqueue,
        )
    )

    assert result is not None
    assert result.msgid == "aibot-enqueue-001"
    assert result.task_id == "celery-task-001"
    assert enqueued[0]["external_id"] == "aibot-enqueue-001"
    assert client.replies[0][0] is frame
    assert client.replies[0][1].startswith("inquiry-")
    assert "aibot-enqueue-001" in client.replies[0][2]
    assert client.replies[0][3] is True


def test_sdk_logger_drops_callback_body_and_redacts_urls(caplog):
    sdk_logger = _SafeSdkLogger()
    callback = (
        'Received push message: {"text":"采购价 100",'
        '"response_url":"https://example.invalid/secret?token=abc"}'
    )

    with caplog.at_level("INFO", logger="app.wecom_aibot_listener"):
        sdk_logger.debug(callback)
        sdk_logger.warning(callback)
        sdk_logger.info("Connecting to https://example.invalid/path?token=abc")

    assert "采购价" not in caplog.text
    assert "token=abc" not in caplog.text
    assert "[已脱敏]" in caplog.text
    assert "[已脱敏 URL]" in caplog.text


def test_reply_failure_preserves_successful_enqueue_result():
    class FailingClient:
        async def reply_stream(self, *_args):
            raise RuntimeError("reply rejected")

    with pytest.raises(WecomAIBotReplyFailed) as exc_info:
        asyncio.run(
            handle_wecom_aibot_frame(
                _text_frame("aibot-reply-failed-001"),
                client=FailingClient(),
                settings=_settings(),
                enqueue=lambda _payload: SimpleNamespace(id="celery-task-002"),
            )
        )

    assert exc_info.value.result.msgid == "aibot-reply-failed-001"
    assert exc_info.value.result.task_id == "celery-task-002"


def test_worker_creates_one_inquiry_for_repeated_msgid(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'aibot.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(task_module, "SessionLocal", session_factory)
    payload = inbound_message_from_frame(
        _text_frame("aibot-idempotency-001"), _settings()
    ).model_dump(mode="json")

    first = task_module.process_wecom_aibot_message.run(payload)
    second = task_module.process_wecom_aibot_message.run(payload)

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["inquiry_id"] == first["inquiry_id"]

    with session_factory() as db:
        raw_messages = db.scalars(select(RawMessage)).all()
        inquiries = db.scalars(select(Inquiry)).all()
        assert len(raw_messages) == len(inquiries) == 1
        assert raw_messages[0].channel == "wecom_aibot"
        assert inquiries[0].sku == "DD123"
        assert inquiries[0].status == "ready_to_quote"
