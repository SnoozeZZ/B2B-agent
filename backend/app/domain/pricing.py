from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
MARGINS = {"A": Decimal("0.05"), "B": Decimal("0.15")}


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PricingResult:
    purchase_price: Decimal
    source_currency: str
    output_currency: str
    fx_rate: Decimal
    converted_cost: Decimal
    margin_rate: Decimal
    freight_per_unit: Decimal
    unit_price: Decimal
    formula: str

    def json_snapshot(self) -> dict:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}


def calculate_price(
    *,
    purchase_price: Decimal,
    source_currency: str,
    output_currency: str,
    fx_rate: Decimal,
    freight_per_unit: Decimal,
    scenario: str,
) -> PricingResult:
    if scenario not in MARGINS:
        raise ValueError("场景必须是 A 或 B")
    if min(purchase_price, fx_rate) <= 0 or freight_per_unit < 0:
        raise ValueError("价格、汇率和运费参数无效")
    converted = money(purchase_price * fx_rate)
    margin = MARGINS[scenario]
    unit_price = money(converted * (Decimal(1) + margin) + freight_per_unit)
    return PricingResult(
        purchase_price=money(purchase_price),
        source_currency=source_currency.upper(),
        output_currency=output_currency.upper(),
        fx_rate=fx_rate,
        converted_cost=converted,
        margin_rate=margin,
        freight_per_unit=money(freight_per_unit),
        unit_price=unit_price,
        formula="采购价 × 汇率 × (1 + 加价率) + 单件运费",
    )

