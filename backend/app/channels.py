from __future__ import annotations

import json
import os
import smtplib
import ssl
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True)
class DeliveryAttachment:
    """An attachment that can be sent by channels which support it."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class ProviderResult:
    """Normalized acknowledgement returned by an outbound provider.

    ``delivered`` is deliberately false for ordinary SMTP/API success: those
    providers only acknowledge acceptance. A later provider callback may mark a
    distribution as delivered.
    """

    channel: str
    provider: str
    success: bool
    status: str
    message_id: str | None = None
    retryable: bool = False
    delivered: bool = False
    status_code: int | None = None
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    acknowledged_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["acknowledged_at"] = self.acknowledged_at.isoformat()
        return data


class ChannelConfigurationError(ValueError):
    pass


_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_token_lock = threading.Lock()


def _read_setting(
    settings: object | None,
    attribute: str,
    env_name: str,
    default: Any = None,
) -> Any:
    if settings is not None and hasattr(settings, attribute):
        value = getattr(settings, attribute)
        if value is not None and value != "":
            return value
    value = os.getenv(env_name)
    return value if value not in (None, "") else default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _required(value: Any, name: str) -> str:
    if value is None or not str(value).strip():
        raise ChannelConfigurationError(f"缺少渠道配置 {name}")
    return str(value).strip()


def _clean_subject(subject: str) -> str:
    return " ".join(subject.replace("\r", " ").replace("\n", " ").split())[:200]


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        return {"text": response.text[:1000]}
    return data if isinstance(data, dict) else {"data": data}


def _http_retryable(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _http_error_message(exc: BaseException) -> str:
    # httpx exception strings can contain webhook keys or token query parameters.
    return f"{type(exc).__name__}: 渠道 HTTP 请求失败"


def _validate_webhook_url(url: str, channel: str) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    allowed = {
        "wecom": ("qyapi.weixin.qq.com",),
        "feishu": ("open.feishu.cn", "open.larksuite.com"),
    }[channel]
    if parsed.scheme != "https" or not any(
        hostname == host or hostname.endswith(f".{host}") for host in allowed
    ):
        raise ChannelConfigurationError(f"{channel} webhook 必须使用官方 HTTPS 地址")


def _resolve_channel_target(target: str, settings: object | None, channel: str) -> str:
    if target.strip().lower() not in {"default", "env", "webhook"}:
        return target.strip()
    if channel == "wecom":
        return _required(
            _read_setting(settings, "wecom_webhook_url", "WECOM_WEBHOOK_URL"),
            "WECOM_WEBHOOK_URL",
        )
    return _required(
        _read_setting(settings, "feishu_webhook_url", "FEISHU_WEBHOOK_URL"),
        "FEISHU_WEBHOOK_URL",
    )


def _provider_failure(
    *,
    channel: str,
    provider: str,
    error: str,
    retryable: bool,
    status_code: int | None = None,
    response: dict[str, Any] | None = None,
) -> ProviderResult:
    return ProviderResult(
        channel=channel,
        provider=provider,
        success=False,
        status="failed",
        retryable=retryable,
        status_code=status_code,
        response=response or {},
        error=error[:2000],
    )


def send_email(
    *,
    target: str,
    subject: str,
    text: str,
    settings: object | None = None,
    html: str | None = None,
    attachments: Iterable[DeliveryAttachment] = (),
) -> ProviderResult:
    """Send a message through a configured SMTP server."""

    host = _required(_read_setting(settings, "smtp_host", "SMTP_HOST"), "SMTP_HOST")
    port = int(_read_setting(settings, "smtp_port", "SMTP_PORT", 465))
    username = _read_setting(settings, "smtp_username", "SMTP_USERNAME")
    password = _read_setting(settings, "smtp_password", "SMTP_PASSWORD")
    from_email = _required(
        _read_setting(settings, "smtp_from_email", "SMTP_FROM_EMAIL", username),
        "SMTP_FROM_EMAIL",
    )
    from_name = str(_read_setting(settings, "smtp_from_name", "SMTP_FROM_NAME", "B2B 报价中心"))
    use_ssl = _as_bool(_read_setting(settings, "smtp_use_ssl", "SMTP_USE_SSL", True), True)
    use_tls = _as_bool(_read_setting(settings, "smtp_use_tls", "SMTP_USE_TLS", False))
    timeout = _as_float(
        _read_setting(settings, "smtp_timeout_seconds", "SMTP_TIMEOUT_SECONDS", 20), 20
    )
    if use_ssl and use_tls:
        raise ChannelConfigurationError("SMTP_USE_SSL 与 SMTP_USE_TLS 不能同时启用")
    if username and not password:
        raise ChannelConfigurationError("配置 SMTP_USERNAME 时必须提供 SMTP_PASSWORD")

    recipients = [item.strip() for item in target.replace(";", ",").split(",") if item.strip()]
    if not recipients or any("@" not in parseaddr(item)[1] for item in recipients):
        return _provider_failure(
            channel="email",
            provider="smtp",
            error="收件邮箱地址无效",
            retryable=False,
        )

    message = EmailMessage()
    message_id = make_msgid(domain=from_email.partition("@")[2] or None)
    message["Message-ID"] = message_id
    message["From"] = formataddr((from_name, from_email))
    message["To"] = ", ".join(recipients)
    message["Subject"] = _clean_subject(subject)
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    for attachment in attachments:
        maintype, separator, subtype = attachment.content_type.partition("/")
        if not separator:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=Path(attachment.filename).name,
        )

    context = ssl.create_default_context()
    try:
        connection = (
            smtplib.SMTP_SSL(host=host, port=port, timeout=timeout, context=context)
            if use_ssl
            else smtplib.SMTP(host=host, port=port, timeout=timeout)
        )
        with connection as server:
            if use_tls:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            if username:
                server.login(str(username), str(password))
            refused = server.send_message(message)
        if refused:
            return _provider_failure(
                channel="email",
                provider="smtp",
                error="SMTP 拒绝部分或全部收件人",
                retryable=False,
                response={"refused_recipients": sorted(str(key) for key in refused)},
            )
        return ProviderResult(
            channel="email",
            provider="smtp",
            success=True,
            status="accepted",
            message_id=message_id.strip("<>"),
            response={"recipient_count": len(recipients)},
        )
    except smtplib.SMTPAuthenticationError as exc:
        return _provider_failure(
            channel="email",
            provider="smtp",
            error=f"SMTP 认证失败：{exc}",
            retryable=False,
        )
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
        return _provider_failure(
            channel="email",
            provider="smtp",
            error=f"SMTP 拒绝地址：{exc}",
            retryable=False,
        )
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        return _provider_failure(
            channel="email",
            provider="smtp",
            error=f"SMTP 发送失败：{exc}",
            retryable=True,
        )


def _cached_token(cache_key: tuple[str, str]) -> str | None:
    with _token_lock:
        cached = _token_cache.get(cache_key)
        if cached and cached[1] > time.monotonic() + 30:
            return cached[0]
    return None


def _store_token(cache_key: tuple[str, str], token: str, expires_in: Any) -> None:
    try:
        ttl = max(60, int(expires_in))
    except (TypeError, ValueError):
        ttl = 7200
    with _token_lock:
        _token_cache[cache_key] = (token, time.monotonic() + ttl)


def _wecom_access_token(
    client: httpx.Client,
    *,
    base_url: str,
    corp_id: str,
    corp_secret: str,
) -> tuple[str | None, ProviderResult | None]:
    cache_key = ("wecom", corp_id)
    if cached := _cached_token(cache_key):
        return cached, None
    try:
        response = client.get(
            f"{base_url}/cgi-bin/gettoken",
            params={"corpid": corp_id, "corpsecret": corp_secret},
        )
    except httpx.HTTPError as exc:
        return None, _provider_failure(
            channel="wecom",
            provider="wecom_app",
            error=_http_error_message(exc),
            retryable=True,
        )
    data = _safe_json(response)
    if response.is_success and data.get("errcode") == 0 and data.get("access_token"):
        token = str(data["access_token"])
        _store_token(cache_key, token, data.get("expires_in"))
        return token, None
    retryable = _http_retryable(response.status_code) or data.get("errcode") in {
        -1,
        40014,
        42001,
        45009,
    }
    return None, _provider_failure(
        channel="wecom",
        provider="wecom_app",
        error=str(data.get("errmsg") or "企业微信 access token 获取失败"),
        retryable=retryable,
        status_code=response.status_code,
        response={"errcode": data.get("errcode"), "errmsg": data.get("errmsg")},
    )


def send_wecom(
    *,
    target: str,
    subject: str,
    text: str,
    settings: object | None = None,
    client: httpx.Client | None = None,
) -> ProviderResult:
    """Send through an Enterprise WeChat app or an official group webhook."""

    target = _resolve_channel_target(target, settings, "wecom")
    shared_timeout = _read_setting(
        settings, "channel_http_timeout_seconds", "CHANNEL_HTTP_TIMEOUT_SECONDS", 20
    )
    timeout = _as_float(
        _read_setting(settings, "wecom_timeout_seconds", "WECOM_TIMEOUT_SECONDS", shared_timeout),
        20,
    )
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    provider = "wecom_webhook" if target.startswith("https://") else "wecom_app"
    try:
        if provider == "wecom_webhook":
            _validate_webhook_url(target, "wecom")
            response = client.post(
                target,
                json={"msgtype": "text", "text": {"content": f"{subject}\n\n{text}"}},
            )
            data = _safe_json(response)
            success = response.is_success and data.get("errcode") == 0
            if success:
                return ProviderResult(
                    channel="wecom",
                    provider=provider,
                    success=True,
                    status="accepted",
                    status_code=response.status_code,
                    response={"errcode": 0, "errmsg": data.get("errmsg")},
                )
            return _provider_failure(
                channel="wecom",
                provider=provider,
                error=str(data.get("errmsg") or "企业微信群机器人发送失败"),
                retryable=_http_retryable(response.status_code)
                or data.get("errcode") in {-1, 45009},
                status_code=response.status_code,
                response={"errcode": data.get("errcode"), "errmsg": data.get("errmsg")},
            )

        base_url = str(
            _read_setting(
                settings,
                "wecom_api_base_url",
                "WECOM_API_BASE_URL",
                "https://qyapi.weixin.qq.com",
            )
        ).rstrip("/")
        corp_id = _required(
            _read_setting(settings, "wecom_corp_id", "WECOM_CORP_ID"), "WECOM_CORP_ID"
        )
        configured_secret = _read_setting(settings, "wecom_secret", "WECOM_SECRET")
        corp_secret = _required(
            _read_setting(
                settings,
                "wecom_corp_secret",
                "WECOM_CORP_SECRET",
                configured_secret,
            ),
            "WECOM_SECRET",
        )
        agent_id = _required(
            _read_setting(settings, "wecom_agent_id", "WECOM_AGENT_ID"), "WECOM_AGENT_ID"
        )
        token, failure = _wecom_access_token(
            client, base_url=base_url, corp_id=corp_id, corp_secret=corp_secret
        )
        if failure:
            return failure
        response = client.post(
            f"{base_url}/cgi-bin/message/send",
            params={"access_token": token},
            json={
                "touser": target,
                "msgtype": "text",
                "agentid": int(agent_id),
                "text": {"content": f"{subject}\n\n{text}"},
                "safe": 0,
            },
        )
        data = _safe_json(response)
        success = response.is_success and data.get("errcode") == 0
        if success:
            return ProviderResult(
                channel="wecom",
                provider=provider,
                success=True,
                status="accepted",
                message_id=str(data.get("msgid")) if data.get("msgid") else None,
                status_code=response.status_code,
                response={
                    key: data.get(key)
                    for key in ("errcode", "errmsg", "invaliduser")
                    if data.get(key) not in (None, "")
                },
            )
        return _provider_failure(
            channel="wecom",
            provider=provider,
            error=str(data.get("errmsg") or "企业微信应用消息发送失败"),
            retryable=_http_retryable(response.status_code)
            or data.get("errcode") in {-1, 40014, 42001, 45009},
            status_code=response.status_code,
            response={"errcode": data.get("errcode"), "errmsg": data.get("errmsg")},
        )
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        error = _http_error_message(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
        return _provider_failure(channel="wecom", provider=provider, error=error, retryable=True)
    except (ChannelConfigurationError, ValueError) as exc:
        return _provider_failure(
            channel="wecom", provider=provider, error=str(exc), retryable=False
        )
    finally:
        if owns_client:
            client.close()


def _feishu_access_token(
    client: httpx.Client,
    *,
    base_url: str,
    app_id: str,
    app_secret: str,
) -> tuple[str | None, ProviderResult | None]:
    cache_key = ("feishu", app_id)
    if cached := _cached_token(cache_key):
        return cached, None
    try:
        response = client.post(
            f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
    except httpx.HTTPError as exc:
        return None, _provider_failure(
            channel="feishu",
            provider="feishu_app",
            error=_http_error_message(exc),
            retryable=True,
        )
    data = _safe_json(response)
    code = data.get("code", data.get("StatusCode"))
    token = data.get("tenant_access_token")
    if response.is_success and code == 0 and token:
        _store_token(cache_key, str(token), data.get("expire", 7200))
        return str(token), None
    return None, _provider_failure(
        channel="feishu",
        provider="feishu_app",
        error=str(data.get("msg") or data.get("StatusMessage") or "飞书 access token 获取失败"),
        retryable=_http_retryable(response.status_code) or code in {99991663, 99991668},
        status_code=response.status_code,
        response={"code": code, "msg": data.get("msg") or data.get("StatusMessage")},
    )


def _feishu_target(target: str, default_type: str) -> tuple[str, str]:
    if ":" in target:
        prefix, value = target.split(":", 1)
        if prefix in {"open_id", "user_id", "union_id", "email", "chat_id"} and value:
            return prefix, value
    return default_type, target


def send_feishu(
    *,
    target: str,
    subject: str,
    text: str,
    settings: object | None = None,
    client: httpx.Client | None = None,
) -> ProviderResult:
    """Send through a Feishu app or an official custom-bot webhook."""

    target = _resolve_channel_target(target, settings, "feishu")
    shared_timeout = _read_setting(
        settings, "channel_http_timeout_seconds", "CHANNEL_HTTP_TIMEOUT_SECONDS", 20
    )
    timeout = _as_float(
        _read_setting(settings, "feishu_timeout_seconds", "FEISHU_TIMEOUT_SECONDS", shared_timeout),
        20,
    )
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    provider = "feishu_webhook" if target.startswith("https://") else "feishu_app"
    try:
        if provider == "feishu_webhook":
            _validate_webhook_url(target, "feishu")
            response = client.post(
                target,
                json={
                    "msg_type": "text",
                    "content": {"text": f"{subject}\n\n{text}"},
                },
            )
            data = _safe_json(response)
            code = data.get("code", data.get("StatusCode"))
            if response.is_success and code == 0:
                return ProviderResult(
                    channel="feishu",
                    provider=provider,
                    success=True,
                    status="accepted",
                    status_code=response.status_code,
                    response={"code": code, "msg": data.get("msg") or data.get("StatusMessage")},
                )
            return _provider_failure(
                channel="feishu",
                provider=provider,
                error=str(data.get("msg") or data.get("StatusMessage") or "飞书机器人发送失败"),
                retryable=_http_retryable(response.status_code) or code in {99991663, 99991668},
                status_code=response.status_code,
                response={"code": code, "msg": data.get("msg") or data.get("StatusMessage")},
            )

        base_url = str(
            _read_setting(
                settings,
                "feishu_api_base_url",
                "FEISHU_API_BASE_URL",
                "https://open.feishu.cn",
            )
        ).rstrip("/")
        app_id = _required(
            _read_setting(settings, "feishu_app_id", "FEISHU_APP_ID"), "FEISHU_APP_ID"
        )
        app_secret = _required(
            _read_setting(settings, "feishu_app_secret", "FEISHU_APP_SECRET"),
            "FEISHU_APP_SECRET",
        )
        token, failure = _feishu_access_token(
            client, base_url=base_url, app_id=app_id, app_secret=app_secret
        )
        if failure:
            return failure
        default_type = str(
            _read_setting(settings, "feishu_receive_id_type", "FEISHU_RECEIVE_ID_TYPE", "open_id")
        )
        receive_id_type, receive_id = _feishu_target(target, default_type)
        response = client.post(
            f"{base_url}/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": f"{subject}\n\n{text}"}, ensure_ascii=False),
            },
        )
        data = _safe_json(response)
        code = data.get("code")
        if response.is_success and code == 0:
            message_id = (data.get("data") or {}).get("message_id")
            return ProviderResult(
                channel="feishu",
                provider=provider,
                success=True,
                status="accepted",
                message_id=str(message_id) if message_id else None,
                status_code=response.status_code,
                response={"code": code, "msg": data.get("msg")},
            )
        return _provider_failure(
            channel="feishu",
            provider=provider,
            error=str(data.get("msg") or "飞书应用消息发送失败"),
            retryable=_http_retryable(response.status_code) or code in {99991663, 99991668},
            status_code=response.status_code,
            response={"code": code, "msg": data.get("msg")},
        )
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        error = _http_error_message(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
        return _provider_failure(channel="feishu", provider=provider, error=error, retryable=True)
    except (ChannelConfigurationError, ValueError) as exc:
        return _provider_failure(
            channel="feishu", provider=provider, error=str(exc), retryable=False
        )
    finally:
        if owns_client:
            client.close()


def send_distribution(
    *,
    channel: str,
    target: str,
    subject: str,
    text: str,
    settings: object | None = None,
    html: str | None = None,
    attachments: Iterable[DeliveryAttachment] = (),
    client: httpx.Client | None = None,
) -> ProviderResult:
    normalized = channel.strip().lower()
    try:
        if normalized == "email":
            return send_email(
                target=target,
                subject=subject,
                text=text,
                settings=settings,
                html=html,
                attachments=attachments,
            )
        if normalized == "wecom":
            return send_wecom(
                target=target, subject=subject, text=text, settings=settings, client=client
            )
        if normalized == "feishu":
            return send_feishu(
                target=target, subject=subject, text=text, settings=settings, client=client
            )
    except (ChannelConfigurationError, TypeError, ValueError) as exc:
        return _provider_failure(
            channel=normalized,
            provider=normalized,
            error=str(exc),
            retryable=False,
        )
    return _provider_failure(
        channel=normalized,
        provider="unknown",
        error=f"不支持的分发渠道：{channel}",
        retryable=False,
    )


def render_quote_message(payload: Mapping[str, Any], *, customer_name: str = "") -> tuple[str, str]:
    """Render the redacted quote snapshot into a plain-text outbound message."""

    brand = str(payload.get("brand") or "商品")
    sku = str(payload.get("sku") or "")
    subject = f"报价单｜{brand}{f' {sku}' if sku else ''}"
    labels = (
        ("brand", "品牌"),
        ("sku", "SKU/货号"),
        ("currency", "币种"),
        ("unit_price", "报价单价"),
        ("moq", "MOQ"),
        ("trade_term", "贸易条款"),
        ("payment_terms", "付款条款"),
        ("lead_time", "发货周期"),
        ("stock_type", "库存类型"),
    )
    lines = [
        f"{label}：{payload[key]}" for key, label in labels if payload.get(key) not in (None, "")
    ]
    salutation = f"{customer_name}，您好：\n\n" if customer_name else "您好：\n\n"
    body = salutation + "以下是本次报价：\n" + "\n".join(lines) + "\n\n此报价以最终确认为准。"
    return subject, body
