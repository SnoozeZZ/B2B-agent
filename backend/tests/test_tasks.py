from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import tasks
from app.channels import ProviderResult
from app.database import Base
from app.models import (
    Customer,
    DeliveryReceipt,
    Distribution,
    Inquiry,
    Quote,
    RawMessage,
)


def _database(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'worker.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _distribution(factory: sessionmaker[Session], *, max_attempts: int = 5) -> int:
    with factory() as db:
        raw = RawMessage(
            channel="manual",
            external_id="worker-test",
            sender_name="供应商",
            content="test",
            dedup_key="worker-test-dedup",
        )
        db.add(raw)
        db.flush()
        inquiry = Inquiry(
            raw_message_id=raw.id,
            status="quoted",
            brand="Nike",
            sku="DD123",
            purchase_price=Decimal(100),
            currency="CNY",
            moq=10,
        )
        db.add(inquiry)
        db.flush()
        payload = {
            "brand": "Nike",
            "sku": "DD123",
            "currency": "CNY",
            "unit_price": "105.00",
            "moq": 10,
        }
        quote = Quote(
            inquiry_id=inquiry.id,
            version=1,
            scenario="A",
            output_currency="CNY",
            fx_rate=Decimal(1),
            freight=Decimal(0),
            margin_rate=Decimal("0.05"),
            unit_price=Decimal(105),
            pricing_snapshot={},
            downstream_snapshot=payload,
            sensitive_scan_passed=True,
        )
        customer = Customer(
            name="Worker Customer",
            channel="wecom",
            channel_target="customer-user-id",
        )
        db.add_all([quote, customer])
        db.flush()
        distribution = Distribution(
            quote_id=quote.id,
            customer_id=customer.id,
            channel="wecom",
            status="queued",
            payload_snapshot=payload,
            confirmed_by="tester",
            max_attempts=max_attempts,
        )
        db.add(distribution)
        db.commit()
        return distribution.id


def test_process_distribution_persists_provider_acceptance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = _database(tmp_path)
    distribution_id = _distribution(factory)
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(
        tasks,
        "_send",
        lambda _: ProviderResult(
            channel="wecom",
            provider="wecom_app",
            success=True,
            status="accepted",
            message_id="wx-message-1",
            response={"errcode": 0},
        ),
    )

    result = tasks.process_distribution.run(distribution_id)

    assert result["status"] == "sent"
    with factory() as db:
        row = db.get(Distribution, distribution_id)
        assert row is not None
        assert row.status == "sent"
        assert row.attempt_count == 1
        assert row.provider_message_id == "wx-message-1"
        assert row.provider_response["provider"] == "wecom_app"
        assert row.sent_at is not None


def test_process_distribution_schedules_retry(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _database(tmp_path)
    distribution_id = _distribution(factory)
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(tasks, "_retry_delay", lambda _: 10)
    monkeypatch.setattr(
        tasks,
        "_send",
        lambda _: ProviderResult(
            channel="wecom",
            provider="wecom_app",
            success=False,
            status="failed",
            retryable=True,
            error="rate limited",
        ),
    )

    # Direct task invocation re-raises the retry cause; a Celery worker turns
    # the same call into its internal Retry signal.
    with pytest.raises(RuntimeError, match="rate limited"):
        tasks.process_distribution.run(distribution_id)

    with factory() as db:
        row = db.get(Distribution, distribution_id)
        assert row is not None
        assert row.status == "retry_scheduled"
        assert row.attempt_count == 1
        assert row.next_attempt_at is not None
        assert row.error_message == "rate limited"


def test_delivery_receipt_is_idempotent(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _database(tmp_path)
    distribution_id = _distribution(factory)
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    now = datetime.now(UTC).isoformat()
    receipt = {
        "status": "delivered",
        "event_id": "event-1",
        "message_id": "wx-message-1",
        "occurred_at": now,
    }

    first = tasks.record_delivery_receipt.run(distribution_id, receipt)
    second = tasks.record_delivery_receipt.run(distribution_id, receipt)

    assert first["status"] == "delivered"
    assert second["duplicate"] is True
    with factory() as db:
        row = db.get(Distribution, distribution_id)
        receipts = db.scalars(
            select(DeliveryReceipt).where(DeliveryReceipt.distribution_id == distribution_id)
        ).all()
        assert row is not None
        assert row.status == "delivered"
        assert row.delivered_at is not None
        assert row.provider_message_id == "wx-message-1"
        assert len(receipts) == 1
