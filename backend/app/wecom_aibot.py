from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .schemas import InboundMessageCreate

WECOM_AIBOT_CALLBACK_COMMAND = "aibot_msg_callback"
SUPPORTED_MESSAGE_TYPES = {"text", "mixed", "voice"}


class WecomAIBotMessageIgnored(ValueError):
    """A valid callback that this inquiry listener intentionally does not consume."""


class WecomAIBotMessageInvalid(ValueError):
    """A callback that is missing fields required for safe, idempotent ingestion."""


class WecomAIBotReplyFailed(RuntimeError):
    """The inquiry was queued, but the immediate WeCom acknowledgement failed."""

    def __init__(self, result: WecomAIBotEnqueueResult):
        super().__init__(f"acknowledgement failed for msgid={result.msgid}")
        self.result = result


@dataclass(frozen=True)
class WecomAIBotEnqueueResult:
    msgid: str
    task_id: str | None


def _string(value: Any, *, limit: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:limit] if limit is not None else text


def _content_from_body(body: dict[str, Any]) -> str:
    msgtype = _string(body.get("msgtype")).lower()
    if msgtype in {"text", "voice"}:
        section = body.get(msgtype)
        if isinstance(section, dict):
            return _string(section.get("content"))
        return ""

    if msgtype == "mixed":
        mixed = body.get("mixed")
        if not isinstance(mixed, dict):
            return ""
        items = mixed.get("msg_item")
        if not isinstance(items, list):
            return ""
        text_parts: list[str] = []
        for item in items:
            if not isinstance(item, dict) or item.get("msgtype") != "text":
                continue
            text = item.get("text")
            if isinstance(text, dict):
                content = _string(text.get("content"))
                if content:
                    text_parts.append(content)
        return "\n".join(text_parts)

    return ""


def inbound_message_from_frame(frame: dict[str, Any], settings: Settings) -> InboundMessageCreate:
    """Validate one official AIBot WebSocket frame and map it to the inquiry contract."""

    command = _string(frame.get("cmd"))
    if command and command != WECOM_AIBOT_CALLBACK_COMMAND:
        raise WecomAIBotMessageIgnored(f"unsupported command: {command}")

    body = frame.get("body")
    if not isinstance(body, dict):
        raise WecomAIBotMessageInvalid("callback body must be an object")

    msgid = _string(body.get("msgid"), limit=128)
    if not msgid:
        raise WecomAIBotMessageInvalid("callback is missing msgid")

    callback_bot_id = _string(body.get("aibotid"))
    if not callback_bot_id:
        raise WecomAIBotMessageInvalid("callback is missing aibotid")
    if settings.wecom_aibot_id and callback_bot_id != settings.wecom_aibot_id:
        raise WecomAIBotMessageIgnored("callback belongs to another AIBot")

    chattype = _string(body.get("chattype")).lower()
    chatid = _string(body.get("chatid"), limit=256)
    if chattype == "single" and not settings.wecom_aibot_accept_single_chat:
        raise WecomAIBotMessageIgnored("single-chat ingestion is disabled")
    if chattype == "group" and not chatid:
        raise WecomAIBotMessageInvalid("group callback is missing chatid")
    if chattype not in {"group", "single"}:
        raise WecomAIBotMessageInvalid("callback has an invalid chattype")
    if settings.wecom_aibot_allowed_chat_ids and (
        not chatid or chatid not in settings.wecom_aibot_allowed_chat_ids
    ):
        raise WecomAIBotMessageIgnored("chat is not in WECOM_AIBOT_ALLOWED_CHAT_IDS")

    msgtype = _string(body.get("msgtype")).lower()
    if msgtype not in SUPPORTED_MESSAGE_TYPES:
        raise WecomAIBotMessageIgnored(f"unsupported message type: {msgtype or 'unknown'}")
    content = _content_from_body(body)
    if not content:
        raise WecomAIBotMessageIgnored("message does not contain inquiry text")

    sender = body.get("from")
    sender = sender if isinstance(sender, dict) else {}
    userid = _string(sender.get("userid"), limit=128)
    corpid = _string(sender.get("corpid"), limit=128)
    sender_name = _string(sender.get("name"), limit=128) or userid or "企业微信用户"
    contact_parts = [part for part in (corpid, userid) if part]
    sender_contact = f"wecom:{':'.join(contact_parts)}" if contact_parts else None

    context = {
        "type": "wecom_aibot_context",
        "aibotid": callback_bot_id,
        "chatid": chatid or None,
        "chattype": chattype,
        "msgtype": msgtype,
    }
    return InboundMessageCreate(
        channel="wecom_aibot",
        external_id=msgid,
        sender_name=sender_name,
        sender_contact=sender_contact,
        content=content,
        attachments=[context],
    )


async def handle_wecom_aibot_frame(
    frame: dict[str, Any],
    *,
    client: Any,
    settings: Settings,
    enqueue: Callable[[dict[str, Any]], Any],
) -> WecomAIBotEnqueueResult | None:
    """Validate, enqueue, and acknowledge one AIBot message callback."""

    try:
        message = inbound_message_from_frame(frame, settings)
    except WecomAIBotMessageIgnored:
        return None

    # Celery's broker connection is synchronous. Keep it off the WebSocket
    # event loop so heartbeats and subsequent message callbacks stay responsive.
    async_result = await asyncio.to_thread(enqueue, message.model_dump(mode="json"))
    task_id = str(async_result.id) if getattr(async_result, "id", None) else None
    result = WecomAIBotEnqueueResult(
        msgid=message.external_id or "",
        task_id=task_id,
    )
    if settings.wecom_aibot_auto_reply:
        try:
            await client.reply_stream(
                frame,
                f"inquiry-{uuid.uuid4().hex}",
                f"询价已接收，消息编号：{message.external_id}",
                True,
            )
        except Exception as exc:
            raise WecomAIBotReplyFailed(result) from exc
    return result
