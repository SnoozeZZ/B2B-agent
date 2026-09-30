from decimal import Decimal

from app.config import get_settings
from app.domain.extraction import extract_inquiry
from app.domain.pricing import calculate_price
from app.domain.redaction import find_sensitive_paths


def test_rule_extraction_finds_required_fields(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    get_settings.cache_clear()
    result = extract_inquiry(
        "品牌: Nike; SKU: DD123; 采购价: 100 USD; MOQ: 20; 交货地: 上海",
        get_settings(),
    )

    assert result.values["brand"] == "Nike"
    assert result.values["sku"] == "DD123"
    assert result.values["purchase_price"] == Decimal(100)
    assert result.values["currency"] == "USD"
    assert result.values["moq"] == 20
    assert result.missing_fields == []


def test_pricing_scenario_a_uses_expected_formula():
    result = calculate_price(
        purchase_price=Decimal(100),
        source_currency="USD",
        output_currency="CNY",
        fx_rate=Decimal("7.2"),
        freight_per_unit=Decimal(5),
        scenario="A",
    )

    assert result.converted_cost == Decimal("720.00")
    assert result.margin_rate == Decimal("0.05")
    assert result.unit_price == Decimal("761.00")


def test_sensitive_scan_walks_nested_payloads():
    findings = find_sensitive_paths(
        {"brand": "Nike", "items": [{"sku": "DD123", "purchase_price": "100"}]}
    )

    assert findings == ["$.items[0].purchase_price"]

