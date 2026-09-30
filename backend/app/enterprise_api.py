from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .auth import (
    Principal,
    authenticate_user,
    create_access_token,
    current_principal,
    hash_password,
    require_roles,
    verify_password,
)
from .commerce import (
    add_payment,
    add_shipment,
    add_shipment_event,
    adjust_inventory,
    create_order,
    inventory_to_read,
    load_order,
    order_to_read,
    patch_order,
)
from .config import get_settings
from .database import get_db
from .models import (
    AuditLog,
    DeliveryReceipt,
    Distribution,
    FxRate,
    InventoryItem,
    Order,
    Shipment,
    User,
)
from .schemas import (
    AuditVerifyRead,
    DeliveryReceiptCreate,
    DistributionRead,
    FxRateRead,
    InventoryAdjust,
    InventoryRead,
    LoginRequest,
    LoginResponse,
    OrderCreate,
    OrderPatch,
    OrderRead,
    PasswordChange,
    PaymentCreate,
    ShipmentCreate,
    ShipmentEventCreate,
    UserCreate,
    UserPatch,
    UserRead,
)
from .services import audit, audit_digest

router = APIRouter(prefix="/api")


def _distribution_to_read(item: Distribution) -> dict:
    return {
        "id": item.id,
        "quote_id": item.quote_id,
        "customer_id": item.customer_id,
        "customer_name": item.customer.name,
        "channel": item.channel,
        "status": item.status,
        "confirmed_by": item.confirmed_by,
        "attempt_count": item.attempt_count,
        "max_attempts": item.max_attempts,
        "provider_message_id": item.provider_message_id,
        "error_message": item.error_message,
        "sent_at": item.sent_at,
        "delivered_at": item.delivered_at,
        "created_at": item.created_at,
    }


@router.get("/channels/status")
def channel_status(_: Principal = Depends(require_roles("admin", "operator"))) -> dict:
    settings = get_settings()
    archive_enabled = os.getenv("WECOM_ARCHIVE_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    archive_key_configured = bool(
        os.getenv("WECOM_ARCHIVE_PRIVATE_KEY")
        or os.getenv("WECOM_ARCHIVE_PRIVATE_KEY_FILE")
        or os.getenv("WECOM_ARCHIVE_PRIVATE_KEYS_JSON")
    )
    return {
        "email": {
            "configured": bool(settings.smtp_host and settings.smtp_from_email),
            "provider": "smtp",
        },
        "wecom": {
            "configured": bool(
                (settings.wecom_corp_id and settings.wecom_agent_id and settings.wecom_secret)
                or os.getenv("WECOM_WEBHOOK_URL")
            ),
            "provider": "app_or_webhook",
        },
        "wecom_aibot": {
            "configured": bool(settings.wecom_aibot_id and settings.wecom_aibot_secret),
            "provider": "websocket",
        },
        "wecom_archive": {
            "enabled": archive_enabled,
            "configured": bool(
                (os.getenv("WECOM_ARCHIVE_CORP_ID") or settings.wecom_corp_id)
                and os.getenv("WECOM_ARCHIVE_SECRET")
                and archive_key_configured
            ),
            "provider": "finance_sdk",
        },
        "feishu": {
            "configured": bool(
                (settings.feishu_app_id and settings.feishu_app_secret)
                or os.getenv("FEISHU_WEBHOOK_URL")
            ),
            "provider": "app_or_webhook",
        },
        "delivery_receipts": {"configured": bool(settings.delivery_webhook_secret)},
    }


@router.post("/auth/login", response_model=LoginResponse)
def login(data: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user = authenticate_user(db, data.username, data.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token, expires_in = create_access_token(user)
    audit(
        db,
        entity_type="user",
        entity_id=user.id,
        action="login",
        actor=user.username,
        payload={},
    )
    db.commit()
    return LoginResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserRead.model_validate(user),
    )


@router.get("/auth/me", response_model=UserRead)
def me(
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db)
) -> User | dict:
    if principal.user_id is None:
        if get_settings().auth_disabled:
            return {
                "id": 0,
                "username": "local-admin",
                "display_name": "本地管理员",
                "role": "admin",
                "active": True,
                "last_login_at": None,
                "created_at": datetime.now(UTC),
            }
        raise HTTPException(status_code=404, detail="服务令牌不对应用户账号")
    user = db.get(User, principal.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


@router.post("/auth/change-password", status_code=204)
def change_password(
    data: PasswordChange,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> None:
    if principal.user_id is None:
        raise HTTPException(status_code=400, detail="服务令牌不能修改密码")
    user = db.get(User, principal.user_id)
    if user is None or not verify_password(data.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码错误")
    user.password_hash = hash_password(data.new_password)
    user.password_changed_at = datetime.now(UTC)
    audit(
        db,
        entity_type="user",
        entity_id=user.id,
        action="password_changed",
        actor=principal.name,
        payload={},
    )
    db.commit()


@router.post("/users", response_model=UserRead, status_code=201)
def create_user(
    data: UserCreate,
    principal: Principal = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> User:
    if db.scalar(select(User).where(User.username == data.username)):
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = User(
        username=data.username,
        display_name=data.display_name,
        password_hash=hash_password(data.password),
        role=data.role,
    )
    db.add(user)
    db.flush()
    audit(
        db,
        entity_type="user",
        entity_id=user.id,
        action="created",
        actor=principal.name,
        payload={"username": user.username, "role": user.role},
    )
    db.commit()
    db.refresh(user)
    return user


@router.get("/users", response_model=list[UserRead])
def list_users(
    _: Principal = Depends(require_roles("admin")), db: Session = Depends(get_db)
) -> list[User]:
    return list(db.scalars(select(User).order_by(User.username)).all())


@router.patch("/users/{user_id}", response_model=UserRead)
def update_user(
    user_id: int,
    data: UserPatch,
    principal: Principal = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    changes = data.model_dump(exclude_unset=True)
    if user.id == principal.user_id and changes.get("active") is False:
        raise HTTPException(status_code=409, detail="不能停用当前账号")
    for field, value in changes.items():
        setattr(user, field, value)
    audit(
        db,
        entity_type="user",
        entity_id=user.id,
        action="updated",
        actor=principal.name,
        payload=changes,
    )
    db.commit()
    db.refresh(user)
    return user


@router.get("/fx-rates", response_model=list[FxRateRead])
def list_fx_rates(
    base: str | None = None,
    quote: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> list[FxRate]:
    query = select(FxRate).order_by(FxRate.rate_date.desc(), FxRate.fetched_at.desc()).limit(limit)
    if base:
        query = query.where(FxRate.base_currency == base.upper())
    if quote:
        query = query.where(FxRate.quote_currency == quote.upper())
    return list(db.scalars(query).all())


@router.post("/fx-rates/sync", response_model=list[FxRateRead])
def sync_fx_rates(
    principal: Principal = Depends(require_roles("admin", "operator")),
    db: Session = Depends(get_db),
) -> list[FxRate]:
    from .fx import sync_rates

    try:
        rates = sync_rates(db)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"汇率同步失败：{exc}") from exc
    audit(
        db,
        entity_type="fx_rate",
        entity_id="sync",
        action="synchronized",
        actor=principal.name,
        payload={"count": len(rates)},
    )
    db.commit()
    return rates


@router.get("/distributions", response_model=list[DistributionRead])
def list_distributions(
    distribution_status: str | None = Query(default=None, alias="status"),
    _: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(Distribution).order_by(Distribution.created_at.desc())
    if distribution_status:
        query = query.where(Distribution.status == distribution_status)
    return [_distribution_to_read(item) for item in db.scalars(query).all()]


@router.post("/distributions/{distribution_id}/retry", response_model=DistributionRead)
def retry_distribution(
    distribution_id: int,
    principal: Principal = Depends(require_roles("admin", "operator")),
    db: Session = Depends(get_db),
) -> dict:
    distribution = db.get(Distribution, distribution_id)
    if distribution is None:
        raise HTTPException(status_code=404, detail="分发记录不存在")
    if distribution.status in {"delivered", "sent"}:
        raise HTTPException(status_code=409, detail="已发送的记录不能重试")
    distribution.status = "queued"
    distribution.next_attempt_at = datetime.now(UTC)
    distribution.error_message = None
    if distribution.attempt_count >= distribution.max_attempts:
        distribution.max_attempts = distribution.attempt_count + 1
    audit(
        db,
        entity_type="distribution",
        entity_id=distribution.id,
        action="manual_retry",
        actor=principal.name,
        payload={"attempt_count": distribution.attempt_count},
    )
    db.commit()
    from .tasks import enqueue_distribution

    enqueue_distribution(distribution.id)
    db.refresh(distribution)
    return _distribution_to_read(distribution)


@router.post("/delivery-receipts/{channel}", status_code=202)
async def delivery_receipt(
    channel: str,
    request: Request,
    x_delivery_signature: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    if not settings.delivery_webhook_secret:
        raise HTTPException(status_code=503, detail="未配置回执签名密钥")
    body = await request.body()
    expected = hmac.new(
        settings.delivery_webhook_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    supplied = (x_delivery_signature or "").removeprefix("sha256=")
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="回执签名无效")
    try:
        data = DeliveryReceiptCreate.model_validate_json(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="回执内容无效") from exc
    distribution = db.scalar(
        select(Distribution).where(
            Distribution.channel == channel,
            Distribution.provider_message_id == data.provider_message_id,
        )
    )
    if distribution is None:
        raise HTTPException(status_code=404, detail="找不到对应分发记录")
    mapped_status = {
        "accepted": "sent",
        "sent": "sent",
        "delivered": "delivered",
        "read": "delivered",
        "failed": "failed",
        "bounced": "failed",
    }[data.status]
    distribution.status = mapped_status
    if mapped_status == "delivered":
        distribution.delivered_at = datetime.now(UTC)
    if mapped_status == "failed":
        distribution.error_message = str(data.payload.get("error") or data.status)
    receipt = DeliveryReceipt(
        distribution_id=distribution.id,
        channel=channel,
        provider_event_id=data.provider_event_id,
        event_type=data.status,
        payload=data.payload,
    )
    db.add(receipt)
    audit(
        db,
        entity_type="distribution",
        entity_id=distribution.id,
        action="receipt_received",
        actor=f"webhook:{channel}",
        payload={"status": data.status, "provider_event_id": data.provider_event_id},
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"accepted": True, "duplicate": True}
    return {"accepted": True, "distribution_id": distribution.id, "status": mapped_status}


@router.get("/inventory", response_model=list[InventoryRead])
def list_inventory(
    search: str | None = None,
    _: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(InventoryItem).order_by(InventoryItem.sku)
    if search:
        query = query.where(InventoryItem.sku.contains(search))
    return [inventory_to_read(item) for item in db.scalars(query).all()]


@router.post("/inventory/{sku}/adjust", response_model=InventoryRead)
def adjust_inventory_endpoint(
    sku: str,
    data: InventoryAdjust,
    principal: Principal = Depends(require_roles("admin", "operator")),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return inventory_to_read(adjust_inventory(db, sku, data, principal.name))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/orders", response_model=OrderRead, status_code=201)
def create_order_endpoint(
    data: OrderCreate,
    principal: Principal = Depends(require_roles("admin", "operator")),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return order_to_read(create_order(db, data, principal.name))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/orders", response_model=list[OrderRead])
def list_orders(
    order_status: str | None = Query(default=None, alias="status"),
    _: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(Order).options(
        selectinload(Order.items),
        selectinload(Order.payments),
        selectinload(Order.shipments).selectinload(Shipment.events),
    ).order_by(Order.created_at.desc())
    if order_status:
        query = query.where(Order.status == order_status)
    return [order_to_read(order) for order in db.scalars(query).unique().all()]


@router.get("/orders/{order_id}", response_model=OrderRead)
def get_order(
    order_id: int,
    _: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> dict:
    order = load_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order_to_read(order)


@router.patch("/orders/{order_id}", response_model=OrderRead)
def update_order(
    order_id: int,
    data: OrderPatch,
    principal: Principal = Depends(require_roles("admin", "operator")),
    db: Session = Depends(get_db),
) -> dict:
    order = load_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    try:
        return order_to_read(patch_order(db, order, data, principal.name))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/orders/{order_id}/payments", response_model=OrderRead)
def create_payment(
    order_id: int,
    data: PaymentCreate,
    principal: Principal = Depends(require_roles("admin", "operator")),
    db: Session = Depends(get_db),
) -> dict:
    order = load_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    try:
        return order_to_read(add_payment(db, order, data, principal.name))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/orders/{order_id}/shipments", response_model=OrderRead)
def create_shipment(
    order_id: int,
    data: ShipmentCreate,
    principal: Principal = Depends(require_roles("admin", "operator")),
    db: Session = Depends(get_db),
) -> dict:
    order = load_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    try:
        return order_to_read(add_shipment(db, order, data, principal.name))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/shipments/{shipment_id}/events", response_model=OrderRead)
def create_shipment_event(
    shipment_id: int,
    data: ShipmentEventCreate,
    principal: Principal = Depends(require_roles("admin", "operator")),
    db: Session = Depends(get_db),
) -> dict:
    shipment = db.get(Shipment, shipment_id)
    if shipment is None:
        raise HTTPException(status_code=404, detail="物流记录不存在")
    try:
        return order_to_read(add_shipment_event(db, shipment, data, principal.name))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/audit-logs/verify", response_model=AuditVerifyRead)
def verify_audit_chain(
    _: Principal = Depends(require_roles("admin")), db: Session = Depends(get_db)
) -> AuditVerifyRead:
    previous_hash: str | None = None
    checked = 0
    for item in db.scalars(select(AuditLog).order_by(AuditLog.id)).all():
        expected = audit_digest(
            previous_hash=previous_hash,
            entity_type=item.entity_type,
            entity_id=item.entity_id,
            action=item.action,
            actor=item.actor,
            payload=item.payload,
            created_at=item.created_at,
        )
        checked += 1
        if item.previous_hash != previous_hash or not hmac.compare_digest(item.record_hash, expected):
            return AuditVerifyRead(valid=False, checked=checked, broken_at_id=item.id)
        previous_hash = item.record_hash
    return AuditVerifyRead(valid=True, checked=checked)
