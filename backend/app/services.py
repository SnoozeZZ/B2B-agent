from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from fnmatch import fnmatchcase

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .config import get_settings
from .domain.extraction import REQUIRED_FIELDS, extract_inquiry, suggested_question
from .domain.pricing import PricingResult, calculate_price
from .domain.redaction import downstream_view, find_sensitive_paths
from .models import (
    AuditLog,
    Customer,
    CustomerBrandNeed,
    Distribution,
    FxRate,
    Inquiry,
    InquiryStatus,
    OpsTask,
    Quote,
    RawMessage,
    TaskStatus,
)
from .schemas import CustomerCreate, InboundMessageCreate, InquiryPatch, PricingPreviewRequest


def audit_digest(
    *,
    previous_hash: str | None,
    entity_type: str,
    entity_id: int | str,
    action: str,
    actor: str,
    payload: dict,
    created_at: datetime,
) -> str:
    timestamp = created_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    timestamp = timestamp.astimezone(UTC)
    canonical = json.dumps(
        {
            "previous_hash": previous_hash,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "action": action,
            "actor": actor,
            "payload": jsonable_encoder(payload),
            "created_at": timestamp.isoformat(timespec="microseconds"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def audit(
    db: Session,
    *,
    entity_type: str,
    entity_id: int | str,
    action: str,
    actor: str,
    payload: dict | None = None,
) -> None:
    encoded_payload = jsonable_encoder(payload or {})
    previous = db.scalar(
        select(AuditLog).order_by(AuditLog.id.desc()).limit(1).with_for_update()
    )
    previous_hash = previous.record_hash if previous else None
    created_at = datetime.now(UTC)
    db.add(
        AuditLog(
            entity_type=entity_type,
            entity_id=str(entity_id),
            action=action,
            actor=actor,
            payload=encoded_payload,
            previous_hash=previous_hash,
            record_hash=audit_digest(
                previous_hash=previous_hash,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                actor=actor,
                payload=encoded_payload,
                created_at=created_at,
            ),
            created_at=created_at,
        )
    )


def inquiry_to_read(inquiry: Inquiry) -> dict:
    return {
        "id": inquiry.id,
        "status": inquiry.status,
        "brand": inquiry.brand,
        "sku": inquiry.sku,
        "industry": inquiry.industry,
        "target_region": inquiry.target_region,
        "purchase_price": inquiry.purchase_price,
        "currency": inquiry.currency,
        "moq": inquiry.moq,
        "delivery_location": inquiry.delivery_location,
        "trade_term": inquiry.trade_term,
        "payment_terms": inquiry.payment_terms,
        "lead_time": inquiry.lead_time,
        "stock_type": inquiry.stock_type,
        "confidence": inquiry.confidence,
        "missing_fields": inquiry.missing_fields,
        "extraction_source": inquiry.extraction_source,
        "created_at": inquiry.created_at,
        "updated_at": inquiry.updated_at,
        "source_channel": inquiry.raw_message.channel,
        "sender_name": inquiry.raw_message.sender_name,
        "sender_contact": inquiry.raw_message.sender_contact,
        "source_content": inquiry.raw_message.content,
    }


def _dedup_key(message: InboundMessageCreate) -> str:
    stable = message.external_id or f"{message.sender_name}|{message.content}"
    return hashlib.sha256(f"{message.channel}|{stable}".encode()).hexdigest()


def create_inquiry(db: Session, message: InboundMessageCreate, actor: str) -> Inquiry:
    key = _dedup_key(message)
    existing_raw = db.scalar(select(RawMessage).where(RawMessage.dedup_key == key))
    if existing_raw:
        return db.scalar(select(Inquiry).where(Inquiry.raw_message_id == existing_raw.id))

    raw = RawMessage(
        channel=message.channel,
        external_id=message.external_id,
        sender_name=message.sender_name,
        sender_contact=message.sender_contact,
        content=message.content,
        attachments=message.attachments,
        dedup_key=key,
    )
    db.add(raw)
    db.flush()

    result = extract_inquiry(message.content, get_settings())
    status = (
        InquiryStatus.NEED_INFO.value
        if result.missing_fields
        else InquiryStatus.READY_TO_QUOTE.value
    )
    inquiry = Inquiry(
        raw_message_id=raw.id,
        status=status,
        confidence=result.confidence,
        missing_fields=result.missing_fields,
        extraction_source=result.source,
        **result.values,
    )
    db.add(inquiry)
    db.flush()

    if result.missing_fields:
        db.add(
            OpsTask(
                inquiry_id=inquiry.id,
                task_type="missing_fields",
                priority="high",
                title="补全供货信息",
                detail={"missing_fields": result.missing_fields},
                suggested_reply=suggested_question(result.missing_fields),
            )
        )
    audit(
        db,
        entity_type="inquiry",
        entity_id=inquiry.id,
        action="created_and_extracted",
        actor=actor,
        payload={
            "channel": message.channel,
            "extraction_source": result.source,
            "confidence": result.confidence,
            "missing_fields": result.missing_fields,
        },
    )
    db.commit()
    db.refresh(inquiry)
    return inquiry


def patch_inquiry(db: Session, inquiry: Inquiry, patch: InquiryPatch, actor: str) -> Inquiry:
    changes = patch.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(inquiry, field, value)
    missing = [field for field in REQUIRED_FIELDS if getattr(inquiry, field) in (None, "")]
    inquiry.missing_fields = missing
    inquiry.status = (
        InquiryStatus.NEED_INFO.value if missing else InquiryStatus.READY_TO_QUOTE.value
    )
    if changes:
        inquiry.confidence = Decimal(1)
        inquiry.extraction_source = "operator"
    if not missing:
        tasks = db.scalars(
            select(OpsTask).where(
                OpsTask.inquiry_id == inquiry.id,
                OpsTask.task_type == "missing_fields",
                OpsTask.status != TaskStatus.RESOLVED.value,
            )
        ).all()
        for task in tasks:
            task.status = TaskStatus.RESOLVED.value
            task.resolved_by = actor
            task.resolved_at = datetime.now(UTC)
    audit(
        db,
        entity_type="inquiry",
        entity_id=inquiry.id,
        action="operator_updated",
        actor=actor,
        payload={"changes": changes, "missing_fields": missing},
    )
    db.commit()
    db.refresh(inquiry)
    return inquiry


def price_inquiry(
    inquiry: Inquiry, request: PricingPreviewRequest, db: Session | None = None
) -> PricingResult:
    if inquiry.purchase_price is None or inquiry.currency is None:
        raise ValueError("询价缺少采购价或币种")
    if request.fx_rate is not None:
        fx_rate = request.fx_rate
    elif inquiry.currency.upper() == request.output_currency.upper():
        fx_rate = Decimal(1)
    elif db is not None:
        stored = db.scalar(
            select(FxRate)
            .where(
                FxRate.base_currency == inquiry.currency.upper(),
                FxRate.quote_currency == request.output_currency.upper(),
            )
            .order_by(FxRate.rate_date.desc(), FxRate.fetched_at.desc())
            .limit(1)
        )
        if stored is not None:
            fx_rate = Decimal(stored.rate)
        elif inquiry.currency.upper() == "USD" and request.output_currency.upper() == "CNY":
            fx_rate = get_settings().default_fx_rate
        else:
            raise ValueError("未找到可用汇率，请先同步汇率或手动提供")
    elif inquiry.currency.upper() == "USD" and request.output_currency.upper() == "CNY":
        fx_rate = get_settings().default_fx_rate
    else:
        raise ValueError("跨币种报价必须提供汇率")
    return calculate_price(
        purchase_price=Decimal(inquiry.purchase_price),
        source_currency=inquiry.currency,
        output_currency=request.output_currency,
        fx_rate=fx_rate,
        freight_per_unit=request.freight_per_unit,
        scenario=request.scenario,
    )


def create_quote(
    db: Session,
    inquiry: Inquiry,
    request: PricingPreviewRequest,
    actor: str,
) -> Quote:
    if inquiry.status not in {
        InquiryStatus.READY_TO_QUOTE.value,
        InquiryStatus.QUOTED.value,
        InquiryStatus.DISTRIBUTED.value,
    }:
        raise ValueError("询价信息尚未补全，不能生成报价")
    pricing = price_inquiry(inquiry, request, db)
    snapshot = pricing.json_snapshot()
    external = downstream_view(inquiry, snapshot)
    findings = find_sensitive_paths(external)
    if findings:
        raise ValueError(f"下游报价包含敏感字段：{', '.join(findings)}")
    current_version = db.scalar(
        select(func.max(Quote.version)).where(Quote.inquiry_id == inquiry.id)
    ) or 0
    quote = Quote(
        inquiry_id=inquiry.id,
        version=current_version + 1,
        scenario=request.scenario,
        output_currency=request.output_currency.upper(),
        fx_rate=pricing.fx_rate,
        freight=pricing.freight_per_unit,
        margin_rate=pricing.margin_rate,
        unit_price=pricing.unit_price,
        pricing_snapshot=snapshot,
        downstream_snapshot=external,
        sensitive_scan_passed=True,
        created_by=actor,
    )
    db.add(quote)
    db.flush()
    inquiry.status = InquiryStatus.QUOTED.value
    audit(
        db,
        entity_type="quote",
        entity_id=quote.id,
        action="created",
        actor=actor,
        payload={"inquiry_id": inquiry.id, "version": quote.version, "pricing": snapshot},
    )
    db.commit()
    db.refresh(quote)
    return quote


def create_customer(db: Session, data: CustomerCreate, actor: str) -> Customer:
    customer = Customer(
        name=data.name,
        email=data.email,
        channel=data.channel,
        channel_target=data.channel_target,
        industries=sorted({value.strip() for value in data.industries if value.strip()}),
        regions=sorted({value.strip() for value in data.regions if value.strip()}),
        sku_patterns=sorted({value.strip() for value in data.sku_patterns if value.strip()}),
        match_threshold=data.match_threshold,
    )
    customer.brand_needs = [
        CustomerBrandNeed(brand=brand.strip(), priority=0)
        for brand in sorted(set(data.brands))
        if brand.strip()
    ]
    db.add(customer)
    db.flush()
    audit(
        db,
        entity_type="customer",
        entity_id=customer.id,
        action="created",
        actor=actor,
        payload={"brands": data.brands},
    )
    db.commit()
    return db.scalar(
        select(Customer)
        .options(selectinload(Customer.brand_needs))
        .where(Customer.id == customer.id)
    )


def customer_to_read(customer: Customer) -> dict:
    return {
        "id": customer.id,
        "name": customer.name,
        "email": customer.email,
        "channel": customer.channel,
        "channel_target": customer.channel_target,
        "active": customer.active,
        "brands": [need.brand for need in customer.brand_needs],
        "industries": customer.industries or [],
        "regions": customer.regions or [],
        "sku_patterns": customer.sku_patterns or [],
        "match_threshold": customer.match_threshold,
    }


def match_customers(db: Session, inquiry: Inquiry) -> list[dict]:
    customers = db.scalars(
        select(Customer)
        .options(selectinload(Customer.brand_needs))
        .where(Customer.active.is_(True))
        .order_by(Customer.name)
    ).unique().all()

    def normalize(value: str | None) -> str:
        return "".join((value or "").lower().split())

    def similarity(left: str | None, right: str | None) -> int:
        if not left or not right:
            return 0
        return round(SequenceMatcher(None, normalize(left), normalize(right)).ratio() * 100)

    matches: list[dict] = []
    for customer in customers:
        reasons: dict[str, int] = {}
        matched_brand: str | None = None
        if inquiry.brand and customer.brand_needs:
            best_need = max(customer.brand_needs, key=lambda need: similarity(inquiry.brand, need.brand))
            ratio = similarity(inquiry.brand, best_need.brand)
            if ratio >= 55:
                reasons["brand"] = round(ratio * 0.45)
                matched_brand = best_need.brand
        if inquiry.sku and customer.sku_patterns:
            normalized_sku = normalize(inquiry.sku)
            if any(
                fnmatchcase(normalized_sku, normalize(pattern))
                or normalize(pattern) in normalized_sku
                for pattern in customer.sku_patterns
            ):
                reasons["sku"] = 25
        if inquiry.industry and customer.industries:
            ratio = max(similarity(inquiry.industry, value) for value in customer.industries)
            if ratio >= 55:
                reasons["industry"] = round(ratio * 0.15)
        inquiry_region = inquiry.target_region or inquiry.delivery_location
        if inquiry_region and customer.regions:
            ratio = max(similarity(inquiry_region, value) for value in customer.regions)
            if any(normalize(value) in normalize(inquiry_region) for value in customer.regions):
                ratio = 100
            if ratio >= 50:
                reasons["region"] = round(ratio * 0.15)
        score = min(100, sum(reasons.values()))
        if score >= customer.match_threshold:
            matches.append(
                {
                    "customer": customer_to_read(customer),
                    "matched_brand": matched_brand,
                    "score": score,
                    "matched_fields": list(reasons),
                    "reasons": reasons,
                }
            )
    return sorted(matches, key=lambda item: (-item["score"], item["customer"]["name"]))


def create_distributions(
    db: Session,
    quote: Quote,
    customer_ids: list[int],
    confirm: bool,
    actor: str,
) -> list[Distribution]:
    customers = db.scalars(
        select(Customer)
        .options(selectinload(Customer.brand_needs))
        .where(Customer.id.in_(customer_ids), Customer.active.is_(True))
    ).all()
    if len(customers) != len(set(customer_ids)):
        raise ValueError("客户不存在或已停用")
    created: list[Distribution] = []
    for customer in customers:
        existing = db.scalar(
            select(Distribution).where(
                Distribution.quote_id == quote.id,
                Distribution.customer_id == customer.id,
            )
        )
        if existing:
            created.append(existing)
            continue
        distribution = Distribution(
            quote_id=quote.id,
            customer_id=customer.id,
            channel=customer.channel,
            status="queued" if confirm else "pending_confirmation",
            payload_snapshot=quote.downstream_snapshot,
            confirmed_by=actor if confirm else None,
            next_attempt_at=datetime.now(UTC) if confirm else None,
        )
        db.add(distribution)
        created.append(distribution)
    if confirm and created:
        quote.inquiry.status = InquiryStatus.DISTRIBUTED.value
    audit(
        db,
        entity_type="quote",
        entity_id=quote.id,
        action="distribution_created",
        actor=actor,
        payload={"customer_ids": customer_ids, "confirmed": confirm},
    )
    db.commit()
    for distribution in created:
        db.refresh(distribution)
    return created
