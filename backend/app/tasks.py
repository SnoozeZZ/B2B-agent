from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from celery import Celery
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import joinedload

from .channels import DeliveryAttachment, ProviderResult, render_quote_message, send_distribution
from .config import get_settings
from .database import SessionLocal
from .documents import build_downstream_quote_xlsx
from .fx import FxProviderError, sync_rates
from .models import DeliveryReceipt, Distribution, Inquiry, RawMessage
from .schemas import InboundMessageCreate
from .services import audit, create_inquiry


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


celery_app = Celery(
    "b2b_distribution_agent",
    broker=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1"),
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=3,
    task_publish_retry=False,
    task_always_eager=_env_bool("CELERY_TASK_ALWAYS_EAGER"),
    beat_schedule={
        "scan-pending-distributions": {
            "task": "app.tasks.enqueue_pending_distributions",
            "schedule": float(_env_int("DISTRIBUTION_SCAN_INTERVAL_SECONDS", 30)),
        },
        "sync-latest-fx-rates": {
            "task": "app.tasks.sync_exchange_rates",
            "schedule": float(_env_int("FX_SYNC_INTERVAL_SECONDS", 3600, minimum=60)),
        },
        **(
            {
                "poll-wecom-conversation-archive": {
                    "task": "app.tasks.poll_wecom_conversation_archive",
                    "schedule": float(
                        _env_int("WECOM_ARCHIVE_POLL_INTERVAL_SECONDS", 30, minimum=10)
                    ),
                }
            }
            if _env_bool("WECOM_ARCHIVE_ENABLED")
            else {}
        ),
    },
)


TERMINAL_DISTRIBUTION_STATUSES = {"sent", "delivered", "failed", "cancelled"}
PROCESSABLE_DISTRIBUTION_STATUSES = {"queued", "retry_scheduled"}


def _set_if_present(instance: Any, name: str, value: Any) -> None:
    if hasattr(instance, name):
        setattr(instance, name, value)


def _attempt_count(distribution: Distribution) -> int:
    value = getattr(distribution, "attempt_count", None)
    if value is None:
        value = getattr(distribution, "retry_count", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _record_attempt(distribution: Distribution, now: datetime) -> int:
    attempt = _attempt_count(distribution) + 1
    _set_if_present(distribution, "attempt_count", attempt)
    _set_if_present(distribution, "retry_count", max(0, attempt - 1))
    _set_if_present(distribution, "last_attempt_at", now)
    _set_if_present(distribution, "next_attempt_at", None)
    return attempt


def _retry_delay(attempt: int) -> int:
    base = _env_int("DISTRIBUTION_RETRY_BASE_SECONDS", 30)
    maximum = _env_int("DISTRIBUTION_RETRY_MAX_SECONDS", 3600)
    raw = min(maximum, base * (2 ** max(0, attempt - 1)))
    return max(1, int(raw * random.uniform(0.9, 1.1)))


def _outbound_target(distribution: Distribution) -> str:
    customer = distribution.customer
    if distribution.channel.lower() == "email":
        target = customer.email or customer.channel_target
    else:
        target = customer.channel_target or "default"
    if not target:
        raise ValueError(f"客户 {customer.id} 未配置 {distribution.channel} 接收目标")
    return str(target).strip()


def _email_attachments(distribution: Distribution) -> list[DeliveryAttachment]:
    if distribution.channel.lower() != "email":
        return []
    quote = distribution.quote
    attachments: list[DeliveryAttachment] = []
    try:
        from .documents import build_downstream_quote_pdf

        attachments.append(
            DeliveryAttachment(
                filename=f"quote-{quote.id}-v{quote.version}.pdf",
                content=build_downstream_quote_pdf(quote),
                content_type="application/pdf",
            )
        )
    except (ImportError, AttributeError):
        # PDF support is installed by the document module independently. The
        # existing XLSX export remains a usable attachment during rolling deploys.
        pass
    attachments.append(
        DeliveryAttachment(
            filename=f"quote-{quote.id}-v{quote.version}.xlsx",
            content=build_downstream_quote_xlsx(quote),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    )
    return attachments


def _send(distribution: Distribution) -> ProviderResult:
    payload = distribution.payload_snapshot or distribution.quote.downstream_snapshot
    subject, text = render_quote_message(payload, customer_name=distribution.customer.name)
    return send_distribution(
        channel=distribution.channel,
        target=_outbound_target(distribution),
        subject=subject,
        text=text,
        settings=get_settings(),
        attachments=_email_attachments(distribution),
    )


def _store_provider_result(distribution: Distribution, result: ProviderResult) -> None:
    _set_if_present(distribution, "provider", result.provider)
    _set_if_present(distribution, "provider_message_id", result.message_id)
    _set_if_present(distribution, "provider_response", result.as_dict())
    _set_if_present(distribution, "delivery_receipt", result.as_dict())


@celery_app.task(
    bind=True,
    name="app.tasks.process_distribution",
    autoretry_for=(OperationalError,),
    retry_backoff=30,
    retry_backoff_max=600,
    retry_kwargs={"max_retries": 5},
)
def process_distribution(self: Any, distribution_id: int) -> dict[str, Any]:
    """Send one confirmed distribution and persist its provider acknowledgement."""

    now = datetime.now(UTC)
    with SessionLocal() as db:
        distribution = db.scalar(
            select(Distribution)
            .options(joinedload(Distribution.customer), joinedload(Distribution.quote))
            .where(Distribution.id == distribution_id)
            .with_for_update()
        )
        if distribution is None:
            return {"distribution_id": distribution_id, "status": "missing"}
        if distribution.status in TERMINAL_DISTRIBUTION_STATUSES:
            return {
                "distribution_id": distribution_id,
                "status": distribution.status,
                "skipped": True,
            }
        stuck_before = now - timedelta(
            seconds=_env_int("DISTRIBUTION_STUCK_SECONDS", 900, minimum=60)
        )
        stale_sending = (
            distribution.status == "sending"
            and distribution.last_attempt_at is not None
            and distribution.last_attempt_at < stuck_before
        )
        if distribution.status not in PROCESSABLE_DISTRIBUTION_STATUSES and not stale_sending:
            return {
                "distribution_id": distribution_id,
                "status": distribution.status,
                "skipped": True,
            }
        attempt = _record_attempt(distribution, now)
        distribution.status = "sending"
        distribution.error_message = None
        audit(
            db,
            entity_type="distribution",
            entity_id=distribution.id,
            action="delivery_started",
            actor="worker",
            payload={"attempt": attempt, "channel": distribution.channel},
        )
        db.commit()

    try:
        with SessionLocal() as db:
            distribution = db.scalar(
                select(Distribution)
                .options(joinedload(Distribution.customer), joinedload(Distribution.quote))
                .where(Distribution.id == distribution_id)
            )
            if distribution is None:
                return {"distribution_id": distribution_id, "status": "missing"}
            result = _send(distribution)
    except Exception as exc:  # noqa: BLE001 - worker boundary persists unexpected failures.
        result = ProviderResult(
            channel=getattr(distribution, "channel", "unknown"),
            provider="worker",
            success=False,
            status="failed",
            retryable=True,
            error=f"{type(exc).__name__}: {exc}",
        )

    with SessionLocal() as db:
        distribution = db.scalar(
            select(Distribution).where(Distribution.id == distribution_id).with_for_update()
        )
        if distribution is None:
            return {"distribution_id": distribution_id, "status": "missing"}
        max_attempts = int(
            getattr(distribution, "max_attempts", None) or _env_int("DISTRIBUTION_MAX_ATTEMPTS", 5)
        )
        _store_provider_result(distribution, result)
        if result.success:
            distribution.status = "delivered" if result.delivered else "sent"
            distribution.sent_at = result.acknowledged_at
            if result.delivered:
                _set_if_present(distribution, "delivered_at", result.acknowledged_at)
            distribution.error_message = None
            action = "delivery_confirmed" if result.delivered else "provider_accepted"
            audit(
                db,
                entity_type="distribution",
                entity_id=distribution.id,
                action=action,
                actor="worker",
                payload=result.as_dict(),
            )
            db.commit()
            return {
                "distribution_id": distribution.id,
                "status": distribution.status,
                "provider": result.provider,
                "message_id": result.message_id,
            }

        distribution.error_message = result.error
        retryable = result.retryable and attempt < max_attempts
        if retryable:
            delay = _retry_delay(attempt)
            distribution.status = "retry_scheduled"
            _set_if_present(
                distribution, "next_attempt_at", datetime.now(UTC) + timedelta(seconds=delay)
            )
            audit(
                db,
                entity_type="distribution",
                entity_id=distribution.id,
                action="delivery_retry_scheduled",
                actor="worker",
                payload={**result.as_dict(), "attempt": attempt, "countdown": delay},
            )
            db.commit()
            raise self.retry(
                exc=RuntimeError(result.error or "渠道发送失败"),
                countdown=delay,
                max_retries=max_attempts - 1,
            )

        distribution.status = "failed"
        audit(
            db,
            entity_type="distribution",
            entity_id=distribution.id,
            action="delivery_failed",
            actor="worker",
            payload={**result.as_dict(), "attempt": attempt},
        )
        db.commit()
        return {
            "distribution_id": distribution.id,
            "status": "failed",
            "retryable": result.retryable,
            "error": result.error,
        }


def enqueue_distribution(distribution_id: int) -> str | None:
    """Best-effort enqueue; a broker outage leaves the DB row for the beat scan."""

    try:
        result = process_distribution.delay(distribution_id)
    except Exception:  # noqa: BLE001 - broker failure must not fail the API transaction.
        return None
    return str(result.id) if result.id else None


@celery_app.task(name="app.tasks.enqueue_pending_distributions")
def enqueue_pending_distributions() -> dict[str, Any]:
    """Dispatch queued/retry-due rows and rescue workers stuck in ``sending``."""

    now = datetime.now(UTC)
    limit = _env_int("DISTRIBUTION_SCAN_LIMIT", 200)
    stuck_before = now - timedelta(seconds=_env_int("DISTRIBUTION_STUCK_SECONDS", 900, minimum=60))
    with SessionLocal() as db:
        due_conditions = [
            Distribution.status == "queued",
            Distribution.status == "retry_scheduled",
        ]
        if hasattr(Distribution, "last_attempt_at"):
            due_conditions.append(
                (Distribution.status == "sending") & (Distribution.last_attempt_at < stuck_before)
            )
        query = select(Distribution.id).where(or_(*due_conditions))
        if hasattr(Distribution, "next_attempt_at"):
            query = query.where(
                or_(
                    Distribution.next_attempt_at.is_(None),
                    Distribution.next_attempt_at <= now,
                    Distribution.status == "queued",
                )
            )
        ids = list(db.scalars(query.order_by(Distribution.id).limit(limit)).all())
    for distribution_id in ids:
        enqueue_distribution(distribution_id)
    return {"queued": len(ids), "distribution_ids": ids}


@celery_app.task(
    bind=True,
    name="app.tasks.sync_exchange_rates",
    autoretry_for=(OperationalError,),
    retry_backoff=60,
    retry_backoff_max=900,
    retry_kwargs={"max_retries": 5},
)
def sync_exchange_rates(self: Any) -> dict[str, Any]:
    """Fetch configured public FX rates and idempotently persist the snapshot."""

    now = datetime.now(UTC)
    try:
        with SessionLocal() as db:
            rows = sync_rates(db, settings=get_settings())
            payload = {
                "rates": [
                    {
                        "base_currency": row.base_currency,
                        "quote_currency": row.quote_currency,
                        "rate": str(row.rate),
                        "rate_date": row.rate_date.isoformat(),
                        "provider": row.provider,
                    }
                    for row in rows
                ]
            }
            audit(
                db,
                entity_type="fx_rate",
                entity_id=f"sync:{now.isoformat()}",
                action="rates_synchronized",
                actor="worker",
                payload=payload,
            )
            db.commit()
    except FxProviderError as exc:
        if exc.retryable:
            raise self.retry(exc=exc, countdown=60, max_retries=5)
        raise
    return {
        **payload,
        "persisted": len(rows),
    }


# Backwards-compatible Python imports for deployments that used the initial names.
scan_pending_distributions = enqueue_pending_distributions
sync_latest_fx_rates = sync_exchange_rates


@celery_app.task(name="app.tasks.record_delivery_receipt")
def record_delivery_receipt(distribution_id: int, receipt: dict[str, Any]) -> dict[str, Any]:
    """Persist a provider callback after the API layer has verified its signature."""

    provider_status = str(receipt.get("status") or receipt.get("event_type") or "").strip().lower()
    delivered = provider_status in {"delivered", "delivery_success", "read", "success"}
    failed = provider_status in {
        "failed",
        "delivery_failed",
        "bounced",
        "rejected",
        "undeliverable",
    }
    if not delivered and not failed:
        raise ValueError(f"无法识别的送达回执状态：{provider_status}")
    now = datetime.now(UTC)
    with SessionLocal() as db:
        distribution = db.scalar(
            select(Distribution).where(Distribution.id == distribution_id).with_for_update()
        )
        if distribution is None:
            return {"distribution_id": distribution_id, "status": "missing"}
        provider_event_id = receipt.get("event_id") or receipt.get("provider_event_id")
        if provider_event_id:
            existing = db.scalar(
                select(DeliveryReceipt).where(
                    DeliveryReceipt.channel == distribution.channel,
                    DeliveryReceipt.provider_event_id == str(provider_event_id),
                )
            )
            if existing:
                return {
                    "distribution_id": distribution_id,
                    "status": distribution.status,
                    "duplicate": True,
                }
        db.add(
            DeliveryReceipt(
                distribution_id=distribution.id,
                channel=distribution.channel,
                provider_event_id=str(provider_event_id) if provider_event_id else None,
                event_type=provider_status,
                payload=receipt,
                received_at=now,
            )
        )
        distribution.status = "delivered" if delivered else "failed"
        if delivered:
            _set_if_present(distribution, "delivered_at", now)
        else:
            distribution.error_message = str(
                receipt.get("error") or receipt.get("reason") or "渠道返回投递失败"
            )[:2000]
        _set_if_present(distribution, "delivery_receipt", receipt)
        message_id = receipt.get("message_id") or receipt.get("msgid")
        if message_id:
            _set_if_present(distribution, "provider_message_id", str(message_id))
        audit(
            db,
            entity_type="distribution",
            entity_id=distribution.id,
            action="delivery_receipt_received",
            actor="provider_callback",
            payload=receipt,
        )
        db.commit()
    return {"distribution_id": distribution_id, "status": distribution.status}


@celery_app.task(
    name="app.tasks.process_wecom_aibot_message",
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_kwargs={"max_retries": 5},
)
def process_wecom_aibot_message(message_payload: dict[str, Any]) -> dict[str, Any]:
    """Create one inquiry from a validated AIBot callback payload.

    ``RawMessage.dedup_key`` is database-unique, while the official callback
    ``msgid`` is used as ``external_id``. Together they make redelivery and
    worker retry safe without another persistence table.
    """

    message = InboundMessageCreate.model_validate(message_payload)
    if message.channel != "wecom_aibot" or not message.external_id:
        raise ValueError("企业微信智能机器人消息缺少 channel 或 msgid")

    with SessionLocal() as db:
        existing_raw = db.scalar(
            select(RawMessage).where(
                RawMessage.channel == message.channel,
                RawMessage.external_id == message.external_id,
            )
        )
        duplicate = existing_raw is not None
        try:
            inquiry = create_inquiry(db, message, actor="wecom_aibot")
        except IntegrityError:
            # Concurrent redeliveries may both pass the initial lookup. The
            # unique dedup_key arbitrates; after rollback, return the winner.
            db.rollback()
            existing_raw = db.scalar(
                select(RawMessage).where(
                    RawMessage.channel == message.channel,
                    RawMessage.external_id == message.external_id,
                )
            )
            inquiry = (
                db.scalar(select(Inquiry).where(Inquiry.raw_message_id == existing_raw.id))
                if existing_raw is not None
                else None
            )
            if inquiry is None:
                raise
            duplicate = True

        return {
            "msgid": message.external_id,
            "inquiry_id": inquiry.id,
            "status": inquiry.status,
            "duplicate": duplicate,
        }


@celery_app.task(bind=True, name="app.tasks.poll_wecom_conversation_archive")
def poll_wecom_conversation_archive(self: Any) -> dict[str, Any]:
    """Poll the licensed WeCom conversation archive through the native Finance SDK."""

    from .wecom_archive import (
        WecomArchiveConfigurationError,
        WecomArchiveError,
        poll_wecom_archive_once,
    )

    try:
        return poll_wecom_archive_once()
    except WecomArchiveConfigurationError:
        # Retrying cannot fix a missing licence, secret, private key, SDK, or IP allowlist.
        raise
    except (WecomArchiveError, OperationalError) as exc:
        retries = int(getattr(self.request, "retries", 0) or 0)
        base = _env_int("WECOM_ARCHIVE_RETRY_BASE_SECONDS", 30)
        maximum = _env_int("WECOM_ARCHIVE_RETRY_MAX_SECONDS", 900)
        delay = min(maximum, base * (2**retries))
        raise self.retry(
            exc=exc,
            countdown=delay,
            max_retries=_env_int("WECOM_ARCHIVE_MAX_RETRIES", 5),
        )


@celery_app.task(bind=True, name="app.tasks.replay_wecom_archive_dead_letter")
def replay_wecom_archive_dead_letter(self: Any, failure_id: int) -> dict[str, Any]:
    """Replay one encrypted WeCom archive dead letter after configuration is fixed."""

    from .wecom_archive import (
        WecomArchiveConfigurationError,
        WecomArchiveError,
        WecomArchiveNonReplayableError,
    )
    from .wecom_archive import replay_wecom_archive_dead_letter as replay

    try:
        return replay(failure_id)
    except (WecomArchiveConfigurationError, WecomArchiveNonReplayableError):
        raise
    except (WecomArchiveError, OperationalError) as exc:
        retries = int(getattr(self.request, "retries", 0) or 0)
        base = _env_int("WECOM_ARCHIVE_RETRY_BASE_SECONDS", 30)
        maximum = _env_int("WECOM_ARCHIVE_RETRY_MAX_SECONDS", 900)
        delay = min(maximum, base * (2**retries))
        raise self.retry(
            exc=exc,
            countdown=delay,
            max_retries=_env_int("WECOM_ARCHIVE_MAX_RETRIES", 5),
        )
