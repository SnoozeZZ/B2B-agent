from __future__ import annotations

import asyncio
import logging
import re
import signal
import uuid
from typing import Any

from .config import get_settings
from .tasks import process_wecom_aibot_message
from .wecom_aibot import (
    WecomAIBotMessageInvalid,
    WecomAIBotReplyFailed,
    handle_wecom_aibot_frame,
)

logger = logging.getLogger("app.wecom_aibot_listener")


class _SafeSdkLogger:
    """Bridge SDK logs into application logging without retaining callback bodies."""

    _url_pattern = re.compile(r"(?:https?|wss?)://[^\s]+", re.IGNORECASE)

    @classmethod
    def _sanitize(cls, message: object, args: tuple[object, ...]) -> str:
        text = " ".join(str(part) for part in (message, *args)).strip()
        lowered = text.lower()
        if "frame:" in lowered or "push message:" in lowered or "event callback:" in lowered:
            return f"{text.split(':', 1)[0]}: [已脱敏]"
        return cls._url_pattern.sub("[已脱敏 URL]", text)

    def debug(self, message: object, *args: object) -> None:
        # The SDK emits complete callback bodies at DEBUG, including response_url.
        return None

    def info(self, message: object, *args: object) -> None:
        logger.info("企微 SDK: %s", self._sanitize(message, args))

    def warning(self, message: object, *args: object) -> None:
        logger.warning("企微 SDK: %s", self._sanitize(message, args))

    warn = warning

    def error(self, message: object, *args: object) -> None:
        logger.error("企微 SDK: %s", self._sanitize(message, args))


def _build_client(settings: Any) -> Any:
    try:
        from aibot import WSClient, WSClientOptions
    except ImportError as exc:  # pragma: no cover - deployment packaging guard.
        raise RuntimeError("缺少依赖 wecom-aibot-python-sdk") from exc

    options: dict[str, Any] = {
        "bot_id": settings.wecom_aibot_id,
        "secret": settings.wecom_aibot_secret,
        # SDK 1.0.2 keeps its client marked as started after a disconnect, so its
        # built-in retry cannot create a fresh connection. The outer loop below
        # supervises reconnects with a newly constructed client instead.
        "max_reconnect_attempts": 0,
        "request_timeout": max(1000, int(settings.channel_http_timeout_seconds * 1000)),
        "logger": _SafeSdkLogger(),
    }
    if settings.wecom_aibot_ws_url:
        options["ws_url"] = settings.wecom_aibot_ws_url
    return WSClient(WSClientOptions(**options))


async def run_listener() -> None:
    settings = get_settings()
    if not settings.wecom_aibot_id or not settings.wecom_aibot_secret:
        raise RuntimeError("请配置 WECOM_AIBOT_ID 和 WECOM_AIBOT_SECRET")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows fallback.
            pass

    reconnect_attempt = 0
    while not stop.is_set():
        reconnect = asyncio.Event()
        authenticated = asyncio.Event()
        client = _build_client(settings)

        @client.on("authenticated")
        def _authenticated(
            authenticated_event: asyncio.Event = authenticated,
        ) -> None:
            nonlocal reconnect_attempt
            authenticated_event.set()
            reconnect_attempt = 0
            logger.info("企业微信智能机器人长连接认证成功")

        @client.on("disconnected")
        def _disconnected(
            reason: str,
            reconnect_event: asyncio.Event = reconnect,
        ) -> None:
            logger.warning("企业微信智能机器人长连接断开: %s", reason)
            reconnect_event.set()

        @client.on("error")
        def _error(
            error: Exception,
            reconnect_event: asyncio.Event = reconnect,
        ) -> None:
            logger.error("企业微信智能机器人长连接错误: %s", error)
            reconnect_event.set()

        @client.on("message")
        async def _message(
            frame: dict[str, Any],
            current_client: Any = client,
        ) -> None:
            try:
                result = await handle_wecom_aibot_frame(
                    frame,
                    client=current_client,
                    settings=settings,
                    enqueue=process_wecom_aibot_message.delay,
                )
                if result:
                    logger.info(
                        "企业微信询价已入队 msgid=%s task_id=%s",
                        result.msgid,
                        result.task_id,
                    )
            except WecomAIBotMessageInvalid as exc:
                logger.warning("忽略格式不正确的企业微信消息: %s", exc)
            except WecomAIBotReplyFailed as exc:
                logger.exception(
                    "企业微信询价已入队，但接收回执发送失败 msgid=%s task_id=%s",
                    exc.result.msgid,
                    exc.result.task_id,
                )
            except Exception:
                logger.exception("企业微信询价入队失败")
                if settings.wecom_aibot_auto_reply:
                    try:
                        await current_client.reply_stream(
                            frame,
                            f"inquiry-error-{uuid.uuid4().hex}",
                            "询价接收失败，请稍后重试。",
                            True,
                        )
                    except Exception:
                        logger.exception("企业微信失败提示发送失败")

        try:
            await client.connect()
            stop_waiter = asyncio.create_task(stop.wait())
            reconnect_waiter = asyncio.create_task(reconnect.wait())
            authenticated_waiter = asyncio.create_task(authenticated.wait())
            waiters = {stop_waiter, reconnect_waiter, authenticated_waiter}
            done, _ = await asyncio.wait(
                waiters,
                timeout=max(5.0, settings.channel_http_timeout_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                logger.error("企业微信智能机器人认证超时，准备重新连接")
                reconnect.set()
            elif not stop.is_set() and not reconnect.is_set() and authenticated.is_set():
                await asyncio.wait(
                    {stop_waiter, reconnect_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        except Exception:
            logger.exception("企业微信智能机器人监听异常")
        finally:
            client.disconnect()

        if stop.is_set():
            break

        reconnect_attempt += 1
        delay = min(2 ** (reconnect_attempt - 1), 30)
        logger.warning("企业微信智能机器人将在 %s 秒后重新连接", delay)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_listener())


if __name__ == "__main__":
    main()
