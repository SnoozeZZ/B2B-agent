import re
from datetime import UTC, datetime

import pytest

from app.documents import (
    build_downstream_quote_pdf,
    downstream_quote_filename,
)
from app.models import Quote


def _quote(**snapshot_updates) -> Quote:
    snapshot = {
        "brand": "Nike",
        "sku": "DD123",
        "currency": "CNY",
        "unit_price": "761.0000",
        "moq": 20,
        "trade_term": "EXW",
        "payment_terms": "30%预付，70%发货前付清",
        "lead_time": "7 个工作日",
        "stock_type": "现货",
    }
    snapshot.update(snapshot_updates)
    return Quote(
        id=42,
        inquiry_id=7,
        version=3,
        scenario="A",
        output_currency="CNY",
        fx_rate="7.2",
        freight="5",
        margin_rate="0.05",
        unit_price="761",
        pricing_snapshot={},
        downstream_snapshot=snapshot,
        sensitive_scan_passed=True,
        created_at=datetime(2026, 9, 30, 8, 15, tzinfo=UTC),
    )


def test_pdf_is_valid_deterministic_and_generically_named():
    quote = _quote()

    content = build_downstream_quote_pdf(quote)
    repeated = build_downstream_quote_pdf(quote)

    assert content.startswith(b"%PDF-")
    assert content.rstrip().endswith(b"%%EOF")
    assert content == repeated
    assert b"/Title (B2B Quote)" in content
    assert downstream_quote_filename(quote) == "quote-42-v3.pdf"
    assert downstream_quote_filename(quote, ".xlsx") == "quote-42-v3.xlsx"


def test_pdf_rejects_sensitive_fields_at_any_depth():
    quote = _quote(items=[{"sku": "DD123", "purchase_price": "100"}])

    with pytest.raises(ValueError, match=r"\$\.items\[0\]\.purchase_price"):
        build_downstream_quote_pdf(quote)


def test_pdf_handles_long_content_across_multiple_pages():
    quote = _quote(payment_terms="收到付款后安排出货。" * 500)

    content = build_downstream_quote_pdf(quote)
    page_objects = re.findall(rb"/Type\s*/Page\b", content)

    assert len(page_objects) >= 2
    assert len(content) > 5_000


def test_quote_filename_rejects_unknown_formats():
    with pytest.raises(ValueError, match="不支持的报价文件格式"):
        downstream_quote_filename(_quote(), "html")
