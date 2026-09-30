from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Self

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.channels import send_distribution, send_email, send_feishu, send_wecom
from app.fx import FxProviderError, configured_pairs, fetch_latest_rates, sync_rates
from app.models import FxRate


def test_wecom_app_sends_with_access_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "token-for-test", "expires_in": 7200},
            )
        assert request.url.params["access_token"] == "token-for-test"
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok", "msgid": "wx-1"})

    settings = SimpleNamespace(
        wecom_corp_id="corp-channel-test",
        wecom_secret="secret",
        wecom_agent_id=100001,
        wecom_api_base_url="https://qyapi.weixin.qq.com",
        channel_http_timeout_seconds=2,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = send_wecom(
            target="zhangsan",
            subject="报价",
            text="Nike DD123",
            settings=settings,
            client=client,
        )

    assert result.success is True
    assert result.status == "accepted"
    assert result.delivered is False
    assert result.message_id == "wx-1"
    assert len(requests) == 2


def test_feishu_webhook_normalizes_rate_limit_as_retryable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"code": 99991663, "msg": "rate limited"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = send_feishu(
            target="https://open.feishu.cn/open-apis/bot/v2/hook/test",
            subject="报价",
            text="content",
            client=client,
        )

    assert result.success is False
    assert result.retryable is True
    assert result.status_code == 429


def test_webhook_secret_can_stay_in_environment_settings() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/hook/secret-from-env")
        return httpx.Response(200, json={"code": 0, "msg": "ok"})

    settings = SimpleNamespace(
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/secret-from-env"
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = send_feishu(
            target="default",
            subject="报价",
            text="content",
            settings=settings,
            client=client,
        )
    assert result.success is True


def test_dispatcher_returns_permanent_failure_for_missing_email_config() -> None:
    result = send_distribution(
        channel="email",
        target="buyer@example.com",
        subject="报价",
        text="content",
        settings=SimpleNamespace(),
    )
    assert result.success is False
    assert result.retryable is False
    assert "SMTP_HOST" in (result.error or "")


def test_smtp_success_is_provider_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_messages = []

    class FakeSmtp:
        def __init__(self, **_: object) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def ehlo(self) -> None:
            pass

        def starttls(self, **_: object) -> None:
            pass

        def login(self, *_: object) -> None:
            pass

        def send_message(self, message: object) -> dict:
            sent_messages.append(message)
            return {}

    monkeypatch.setattr("app.channels.smtplib.SMTP", FakeSmtp)
    result = send_email(
        target="buyer@example.com",
        subject="报价\n单",
        text="content",
        settings=SimpleNamespace(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="sender@example.com",
            smtp_password="password",
            smtp_from_email="sender@example.com",
            smtp_use_ssl=False,
            smtp_use_tls=True,
        ),
    )
    assert result.success is True
    assert result.delivered is False
    assert len(sent_messages) == 1
    assert sent_messages[0]["Subject"] == "报价 单"


def test_fetch_latest_rates_validates_and_converts_decimal() -> None:
    today = datetime.now(UTC).date().isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/latest"
        assert request.url.params["from"] == "USD"
        assert request.url.params["to"] == "CNY,EUR"
        return httpx.Response(
            200,
            json={"base": "USD", "date": today, "rates": {"CNY": 7.12, "EUR": "0.91"}},
        )

    settings = SimpleNamespace(fx_api_url="https://api.frankfurter.app")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot = fetch_latest_rates(
            settings, client=client, base_currency="USD", symbols=("CNY", "EUR")
        )
    assert str(snapshot.rate("CNY")) == "7.12"
    assert str(snapshot.rate("USD")) == "1"


def test_fetch_latest_rates_supports_current_official_v1_url() -> None:
    today = datetime.now(UTC).date().isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/latest"
        assert request.url.params["base"] == "USD"
        assert request.url.params["symbols"] == "CNY"
        return httpx.Response(
            200,
            json={"base": "USD", "date": today, "rates": {"CNY": "7.15"}},
        )

    settings = SimpleNamespace(fx_api_url="https://api.frankfurter.dev")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot = fetch_latest_rates(
            settings, client=client, base_currency="USD", symbols=("CNY",)
        )
    assert str(snapshot.rate("CNY")) == "7.15"


def test_fetch_latest_rates_rejects_missing_currency() -> None:
    today = datetime.now(UTC).date().isoformat()
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"base": "USD", "date": today, "rates": {"EUR": 0.91}})
    )
    with httpx.Client(transport=transport) as client, pytest.raises(FxProviderError, match="CNY"):
        fetch_latest_rates(
            SimpleNamespace(fx_api_url="https://api.frankfurter.app/latest"),
            client=client,
            base_currency="USD",
            symbols=("CNY",),
        )


def test_configured_pairs_are_normalized() -> None:
    settings = SimpleNamespace(fx_sync_pairs=("usd/cny", "EUR/CNY", "USD/CNY"))
    assert configured_pairs(settings) == (("USD", "CNY"), ("EUR", "CNY"))


def test_sync_rates_upserts_same_pair_and_date() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FxRate.__table__.create(engine)
    today = datetime.now(UTC).date().isoformat()
    values = iter(("7.10", "7.20"))

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"base": "USD", "date": today, "rates": {"CNY": next(values)}},
        )

    settings = SimpleNamespace(
        fx_api_url="https://api.frankfurter.app",
        fx_sync_pairs=("USD/CNY",),
    )
    with Session(engine) as db, httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = sync_rates(db, settings=settings, client=client)
        first_id = first[0].id
        db.commit()
        second = sync_rates(db, settings=settings, client=client)
        db.commit()
        assert second[0].id == first_id
        assert str(second[0].rate) == "7.20000000"
        assert db.scalar(select(func.count(FxRate.id))) == 1
