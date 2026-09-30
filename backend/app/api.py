from __future__ import annotations

from datetime import UTC
from io import BytesIO

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .auth import Principal, current_principal, require_roles
from .config import get_settings
from .database import get_db
from .documents import (
    build_downstream_quote_pdf,
    build_downstream_quote_xlsx,
    downstream_quote_filename,
)
from .models import AuditLog, Customer, Distribution, Inquiry, OpsTask, Quote, TaskStatus
from .schemas import (
    AuditRead,
    CustomerCreate,
    CustomerMatchRead,
    CustomerRead,
    DashboardRead,
    DistributionCreate,
    DistributionRead,
    InboundMessageCreate,
    InquiryPatch,
    InquiryRead,
    OpsTaskPatch,
    OpsTaskRead,
    PricingPreview,
    PricingPreviewRequest,
    QuoteCreate,
    QuoteRead,
)
from .services import (
    audit,
    create_customer,
    create_distributions,
    create_inquiry,
    create_quote,
    customer_to_read,
    inquiry_to_read,
    match_customers,
    patch_inquiry,
    price_inquiry,
)

router = APIRouter(prefix="/api")


def _inquiry_or_404(db: Session, inquiry_id: int) -> Inquiry:
    inquiry = db.get(Inquiry, inquiry_id)
    if not inquiry:
        raise HTTPException(status_code=404, detail="询价不存在")
    return inquiry


def _quote_or_404(db: Session, quote_id: int) -> Quote:
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail="报价不存在")
    return quote


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/dashboard", response_model=DashboardRead)
def dashboard(
    db: Session = Depends(get_db), _: Principal = Depends(current_principal)
) -> DashboardRead:
    return DashboardRead(
        inquiries_total=db.scalar(select(func.count(Inquiry.id))) or 0,
        inquiries_need_attention=db.scalar(
            select(func.count(Inquiry.id)).where(Inquiry.status.in_(["need_info", "need_ops"]))
        )
        or 0,
        quotes_total=db.scalar(select(func.count(Quote.id))) or 0,
        open_tasks=db.scalar(
            select(func.count(OpsTask.id)).where(OpsTask.status != TaskStatus.RESOLVED.value)
        )
        or 0,
        pending_distributions=db.scalar(
            select(func.count(Distribution.id)).where(
                Distribution.status.in_(["pending_confirmation", "queued"])
            )
        )
        or 0,
    )


@router.post("/inquiries", response_model=InquiryRead, status_code=status.HTTP_201_CREATED)
def create_inquiry_endpoint(
    message: InboundMessageCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("admin", "operator")),
) -> dict:
    return inquiry_to_read(create_inquiry(db, message, principal.name))


@router.post("/inquiries/import", response_model=list[InquiryRead])
async def import_inquiries(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("admin", "operator")),
) -> list[dict]:
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx 文件")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件不能超过 10MB")
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Excel 文件无法解析") from exc
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(value).strip() if value is not None else "" for value in rows[0]]
    aliases = {
        "品牌": "品牌",
        "brand": "品牌",
        "sku": "SKU",
        "货号": "SKU",
        "采购价": "采购价",
        "供货价": "采购价",
        "币种": "币种",
        "moq": "MOQ",
        "交货地": "交货地",
        "发货周期": "发货周期",
        "行业": "行业",
        "industry": "行业",
        "目标区域": "目标区域",
        "区域": "目标区域",
    }
    created: list[dict] = []
    for row_number, values in enumerate(rows[1:], start=2):
        parts = []
        for header, value in zip(headers, values):
            if value in (None, ""):
                continue
            label = aliases.get(header.lower(), aliases.get(header, header))
            parts.append(f"{label}: {value}")
        if not parts:
            continue
        message = InboundMessageCreate(
            channel="excel",
            external_id=f"{file.filename}:{row_number}",
            sender_name=file.filename,
            content="; ".join(parts),
            attachments=[{"filename": file.filename, "row": row_number}],
        )
        created.append(inquiry_to_read(create_inquiry(db, message, principal.name)))
    return created


@router.get("/inquiries", response_model=list[InquiryRead])
def list_inquiries(
    inquiry_status: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> list[dict]:
    query = select(Inquiry).order_by(Inquiry.created_at.desc())
    if inquiry_status:
        query = query.where(Inquiry.status == inquiry_status)
    return [inquiry_to_read(item) for item in db.scalars(query).all()]


@router.get("/inquiries/{inquiry_id}", response_model=InquiryRead)
def get_inquiry(
    inquiry_id: int,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> dict:
    return inquiry_to_read(_inquiry_or_404(db, inquiry_id))


@router.patch("/inquiries/{inquiry_id}", response_model=InquiryRead)
def update_inquiry(
    inquiry_id: int,
    patch: InquiryPatch,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("admin", "operator")),
) -> dict:
    inquiry = patch_inquiry(db, _inquiry_or_404(db, inquiry_id), patch, principal.name)
    return inquiry_to_read(inquiry)


@router.post("/inquiries/{inquiry_id}/pricing-preview", response_model=PricingPreview)
def pricing_preview(
    inquiry_id: int,
    request: PricingPreviewRequest,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> PricingPreview:
    try:
        return PricingPreview.model_validate(
            price_inquiry(_inquiry_or_404(db, inquiry_id), request, db).__dict__
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/inquiries/{inquiry_id}/quotes", response_model=QuoteRead)
def create_quote_endpoint(
    inquiry_id: int,
    request: QuoteCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("admin", "operator")),
) -> Quote:
    try:
        return create_quote(db, _inquiry_or_404(db, inquiry_id), request, principal.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/quotes", response_model=list[QuoteRead])
def list_quotes(
    db: Session = Depends(get_db), _: Principal = Depends(current_principal)
) -> list[Quote]:
    return list(db.scalars(select(Quote).order_by(Quote.created_at.desc())).all())


@router.get("/quotes/{quote_id}/preview")
def quote_preview(
    quote_id: int,
    view: str = Query(default="downstream", pattern="^(downstream|internal)$"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(current_principal),
) -> dict:
    quote = _quote_or_404(db, quote_id)
    if view == "downstream":
        return quote.downstream_snapshot
    if principal.role not in {"admin", "operator"}:
        raise HTTPException(status_code=403, detail="内部报价仅运营可见")
    return {"pricing": quote.pricing_snapshot, "downstream": quote.downstream_snapshot}


@router.get("/quotes/{quote_id}/export.xlsx")
def export_quote_xlsx(
    quote_id: int,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> StreamingResponse:
    quote = _quote_or_404(db, quote_id)
    try:
        content = build_downstream_quote_xlsx(quote)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    filename = downstream_quote_filename(quote, "xlsx")
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/quotes/{quote_id}/export.pdf")
def export_quote_pdf(
    quote_id: int,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> StreamingResponse:
    quote = _quote_or_404(db, quote_id)
    try:
        content = build_downstream_quote_pdf(quote)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StreamingResponse(
        BytesIO(content),
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{downstream_quote_filename(quote, "pdf")}"'
            )
        },
    )


@router.post("/customers", response_model=CustomerRead, status_code=201)
def create_customer_endpoint(
    data: CustomerCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("admin", "operator")),
) -> dict:
    return customer_to_read(create_customer(db, data, principal.name))


@router.get("/customers", response_model=list[CustomerRead])
def list_customers(
    db: Session = Depends(get_db), _: Principal = Depends(current_principal)
) -> list[dict]:
    customers = db.scalars(
        select(Customer).options(selectinload(Customer.brand_needs)).order_by(Customer.name)
    ).all()
    return [customer_to_read(customer) for customer in customers]


@router.get("/inquiries/{inquiry_id}/matches", response_model=list[CustomerMatchRead])
def get_customer_matches(
    inquiry_id: int,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> list[dict]:
    return match_customers(db, _inquiry_or_404(db, inquiry_id))


@router.post("/quotes/{quote_id}/distributions", response_model=list[DistributionRead])
def distribute_quote(
    quote_id: int,
    request: DistributionCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("admin", "operator")),
) -> list[dict]:
    try:
        distributions = create_distributions(
            db, _quote_or_404(db, quote_id), request.customer_ids, request.confirm, principal.name
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if request.confirm and get_settings().enqueue_distributions:
        from .tasks import enqueue_distribution

        for item in distributions:
            if item.status == "queued":
                enqueue_distribution(item.id)
    return [
        {
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
        for item in distributions
    ]


@router.get("/ops-tasks", response_model=list[OpsTaskRead])
def list_ops_tasks(
    task_status: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> list[OpsTask]:
    query = select(OpsTask).order_by(OpsTask.created_at.desc())
    if task_status:
        query = query.where(OpsTask.status == task_status)
    return list(db.scalars(query).all())


@router.patch("/ops-tasks/{task_id}", response_model=OpsTaskRead)
def update_ops_task(
    task_id: int,
    patch: OpsTaskPatch,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("admin", "operator")),
) -> OpsTask:
    task = db.get(OpsTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="运营任务不存在")
    task.status = patch.status
    task.assignee = patch.assignee
    if patch.status == TaskStatus.RESOLVED.value:
        from datetime import datetime

        task.resolved_by = principal.name
        task.resolved_at = datetime.now(UTC)
    audit(
        db,
        entity_type="ops_task",
        entity_id=task.id,
        action="status_changed",
        actor=principal.name,
        payload=patch.model_dump(),
    )
    db.commit()
    db.refresh(task)
    return task


@router.get("/audit-logs", response_model=list[AuditRead])
def list_audit_logs(
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: Principal = Depends(require_roles("admin", "operator")),
) -> list[AuditLog]:
    query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)
    return list(db.scalars(query).all())
