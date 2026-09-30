from __future__ import annotations

from typing import Any

SENSITIVE_KEYS = {
    "purchase_price",
    "supplier",
    "supplier_name",
    "sender_contact",
    "delivery_location",
    "source_content",
    "internal_margin",
}


def downstream_view(inquiry, pricing: dict) -> dict:
    return {
        "brand": inquiry.brand,
        "sku": inquiry.sku,
        "currency": pricing["output_currency"],
        "unit_price": pricing["unit_price"],
        "moq": inquiry.moq,
        "trade_term": inquiry.trade_term,
        "payment_terms": inquiry.payment_terms,
        "lead_time": inquiry.lead_time,
        "stock_type": inquiry.stock_type,
    }


def find_sensitive_paths(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            next_path = f"{path}.{key}"
            if key.lower() in SENSITIVE_KEYS:
                findings.append(next_path)
            findings.extend(find_sensitive_paths(child, next_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(find_sensitive_paths(child, f"{path}[{index}]"))
    return findings
