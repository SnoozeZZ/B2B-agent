from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from .models import FxRate


class FxProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class FxSnapshot:
    provider: str
    base_currency: str
    rates: dict[str, Decimal]
    effective_date: date
    fetched_at: datetime

    def rate(self, quote_currency: str) -> Decimal:
        quote = quote_currency.upper()
        if quote == self.base_currency:
            return Decimal(1)
        try:
            return self.rates[quote]
        except KeyError as exc:
            raise KeyError(f"汇率快照中不存在 {self.base_currency}/{quote}") from exc

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_currency": self.base_currency,
            "rates": {key: str(value) for key, value in self.rates.items()},
            "effective_date": self.effective_date.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
        }


def _read_setting(
    settings: object | None,
    attribute: str,
    env_name: str,
    default: Any = None,
) -> Any:
    if settings is not None and hasattr(settings, attribute):
        value = getattr(settings, attribute)
        if value is not None and value != "":
            return value
    value = os.getenv(env_name)
    return value if value not in (None, "") else default


def configured_symbols(settings: object | None = None) -> tuple[str, ...]:
    raw = _read_setting(settings, "fx_symbols", "FX_SYMBOLS", "CNY,USD,EUR,GBP,JPY,HKD")
    if isinstance(raw, str):
        values = raw.split(",")
    else:
        values = raw
    symbols: list[str] = []
    for item in values:
        symbol = str(item).strip().upper()
        if len(symbol) != 3 or not symbol.isalpha():
            raise FxProviderError(f"无效的汇率币种代码：{item}", retryable=False)
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise FxProviderError("FX_SYMBOLS 至少需要一个币种", retryable=False)
    return tuple(symbols)


def configured_pairs(settings: object | None = None) -> tuple[tuple[str, str], ...]:
    raw = _read_setting(
        settings,
        "fx_sync_pairs",
        "FX_SYNC_PAIRS",
        "USD/CNY,EUR/CNY,GBP/CNY",
    )
    values = raw.split(",") if isinstance(raw, str) else raw
    pairs: list[tuple[str, str]] = []
    for item in values:
        parts = [part.strip().upper() for part in str(item).split("/")]
        if (
            len(parts) != 2
            or parts[0] == parts[1]
            or any(len(part) != 3 or not part.isalpha() for part in parts)
        ):
            raise FxProviderError(f"无效的汇率币种对：{item}", retryable=False)
        pair = (parts[0], parts[1])
        if pair not in pairs:
            pairs.append(pair)
    if not pairs:
        raise FxProviderError("FX_SYNC_PAIRS 至少需要一个币种对", retryable=False)
    return tuple(pairs)


def _normalize_pairs(
    pairs: Iterable[str | tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    normalized: list[tuple[str, str]] = []
    for item in pairs:
        parts = item if isinstance(item, tuple) else tuple(str(item).split("/"))
        if len(parts) != 2:
            raise FxProviderError(f"无效的汇率币种对：{item}", retryable=False)
        base, quote = (str(value).strip().upper() for value in parts)
        if (
            base == quote
            or len(base) != 3
            or len(quote) != 3
            or not base.isalpha()
            or not quote.isalpha()
        ):
            raise FxProviderError(f"无效的汇率币种对：{item}", retryable=False)
        pair = (base, quote)
        if pair not in normalized:
            normalized.append(pair)
    if not normalized:
        raise FxProviderError("至少需要一个汇率币种对", retryable=False)
    return tuple(normalized)


def _positive_decimal(value: Any, currency: str) -> Decimal:
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FxProviderError(f"{currency} 汇率不是有效数字") from exc
    if not rate.is_finite() or rate <= 0:
        raise FxProviderError(f"{currency} 汇率必须是正有限数")
    return rate


def _parse_effective_date(value: Any) -> date:
    if not isinstance(value, str):
        raise FxProviderError("汇率服务未返回有效日期")
    try:
        effective_date = date.fromisoformat(value)
    except ValueError as exc:
        raise FxProviderError("汇率服务日期格式无效") from exc
    if effective_date > datetime.now(UTC).date() + timedelta(days=1):
        raise FxProviderError("汇率服务返回了未来日期")
    return effective_date


def _provider_name(url: str) -> str:
    try:
        hostname = httpx.URL(url).host or "custom"
    except (TypeError, ValueError):
        hostname = "custom"
    return hostname.lower()


def _request_url_and_params(
    configured_url: str,
    *,
    base: str,
    symbols: tuple[str, ...],
) -> tuple[str, dict[str, str]]:
    """Support both the legacy .app API and the current official v1 API."""

    parsed = httpx.URL(configured_url)
    path = parsed.path.rstrip("/")
    if parsed.host == "api.frankfurter.dev":
        if path == "":
            configured_url = f"{configured_url.rstrip('/')}/v1/latest"
        elif path == "/v1":
            configured_url = f"{configured_url.rstrip('/')}/latest"
        return configured_url, {"base": base, "symbols": ",".join(symbols)}
    if path == "":
        configured_url = f"{configured_url.rstrip('/')}/latest"
    return configured_url, {"from": base, "to": ",".join(symbols)}


def _normalize_rates(data: Mapping[str, Any]) -> Mapping[str, Any]:
    rates = data.get("rates")
    if isinstance(rates, Mapping):
        return rates
    rates = data.get("conversion_rates")
    if isinstance(rates, Mapping):
        return rates
    nested = data.get("data")
    if isinstance(nested, Mapping) and isinstance(nested.get("rates"), Mapping):
        return nested["rates"]
    raise FxProviderError("汇率服务响应缺少 rates 字段")


def fetch_latest_rates(
    settings: object | None = None,
    *,
    client: httpx.Client | None = None,
    base_currency: str | None = None,
    symbols: tuple[str, ...] | None = None,
) -> FxSnapshot:
    """Fetch and validate a latest-rate snapshot.

    The default endpoint is Frankfurter's public ECB-backed API. A compatible
    endpoint can be supplied with ``FX_API_URL``; it must return a top-level
    ``base``, ISO ``date`` and ``rates`` mapping.
    """

    base = (
        (base_currency or _read_setting(settings, "fx_base_currency", "FX_BASE_CURRENCY", "USD"))
        .strip()
        .upper()
    )
    if len(base) != 3 or not base.isalpha():
        raise FxProviderError(f"无效的基础币种：{base}", retryable=False)
    requested_symbols = symbols or configured_symbols(settings)
    requested_symbols = tuple(symbol for symbol in requested_symbols if symbol != base)
    if not requested_symbols:
        raise FxProviderError("至少需要一个不同于基础币种的目标币种", retryable=False)

    url = str(
        _read_setting(
            settings,
            "fx_api_url",
            "FX_API_URL",
            "https://api.frankfurter.dev/v1/latest",
        )
    )
    url, params = _request_url_and_params(url, base=base, symbols=requested_symbols)
    timeout_value = _read_setting(settings, "fx_timeout_seconds", "FX_TIMEOUT_SECONDS", 15)
    try:
        timeout = float(timeout_value)
    except (TypeError, ValueError):
        timeout = 15.0
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = 15.0

    api_key = _read_setting(settings, "fx_api_key", "FX_API_KEY")
    api_key_header = str(
        _read_setting(settings, "fx_api_key_header", "FX_API_KEY_HEADER", "apikey")
    )
    headers = {api_key_header: str(api_key)} if api_key else {}
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = client.get(url, params=params, headers=headers)
        if not response.is_success:
            retryable = response.status_code in {408, 409, 425, 429} or response.status_code >= 500
            raise FxProviderError(f"汇率服务返回 HTTP {response.status_code}", retryable=retryable)
        try:
            data = response.json()
        except ValueError as exc:
            raise FxProviderError("汇率服务返回的不是有效 JSON") from exc
        if not isinstance(data, Mapping):
            raise FxProviderError("汇率服务响应格式无效")
        response_base = str(data.get("base") or data.get("base_code") or base).upper()
        if response_base != base:
            raise FxProviderError(f"汇率服务基础币种不一致：期望 {base}，得到 {response_base}")
        raw_rates = _normalize_rates(data)
        rates: dict[str, Decimal] = {}
        missing: list[str] = []
        for symbol in requested_symbols:
            if symbol not in raw_rates:
                missing.append(symbol)
                continue
            rates[symbol] = _positive_decimal(raw_rates[symbol], symbol)
        if missing:
            raise FxProviderError(f"汇率服务缺少币种：{', '.join(missing)}")
        effective_value = data.get("date") or data.get("time_last_update_utc")
        if isinstance(effective_value, str) and len(effective_value) > 10:
            try:
                effective_value = datetime.fromisoformat(effective_value).date().isoformat()
            except ValueError:
                pass
        return FxSnapshot(
            provider=_provider_name(url),
            base_currency=base,
            rates=rates,
            effective_date=_parse_effective_date(effective_value),
            fetched_at=datetime.now(UTC),
        )
    except httpx.HTTPError as exc:
        raise FxProviderError(f"汇率服务请求失败：{type(exc).__name__}") from exc
    finally:
        if owns_client:
            client.close()


def fetch_configured_rates(
    settings: object | None = None,
    *,
    client: httpx.Client | None = None,
    pairs: Iterable[str | tuple[str, str]] | None = None,
) -> list[FxSnapshot]:
    """Fetch every configured ``BASE/QUOTE`` pair, grouped by base currency."""

    groups: dict[str, list[str]] = {}
    selected_pairs = _normalize_pairs(pairs) if pairs is not None else configured_pairs(settings)
    for base, quote in selected_pairs:
        groups.setdefault(base, []).append(quote)
    owns_client = client is None
    if client is None:
        timeout_value = _read_setting(settings, "fx_timeout_seconds", "FX_TIMEOUT_SECONDS", 15)
        try:
            timeout = float(timeout_value)
        except (TypeError, ValueError):
            timeout = 15.0
        client = httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        return [
            fetch_latest_rates(
                settings,
                client=client,
                base_currency=base,
                symbols=tuple(symbols),
            )
            for base, symbols in groups.items()
        ]
    finally:
        if owns_client:
            client.close()


def sync_rates(
    db: Session,
    pairs: Iterable[str | tuple[str, str]] | None = None,
    *,
    settings: object | None = None,
    client: httpx.Client | None = None,
) -> list[FxRate]:
    """Fetch configured pairs and upsert daily ``FxRate`` rows.

    The caller owns the transaction. Rows are flushed so their IDs are
    available, but this function intentionally does not commit.
    """

    from .config import get_settings
    from .models import FxRate

    settings = settings or get_settings()
    snapshots = fetch_configured_rates(settings, client=client, pairs=pairs)
    rows: list[FxRate] = []
    for snapshot in snapshots:
        for quote_currency, rate in snapshot.rates.items():
            row = db.scalar(
                select(FxRate)
                .where(
                    FxRate.base_currency == snapshot.base_currency,
                    FxRate.quote_currency == quote_currency,
                    FxRate.rate_date == snapshot.effective_date,
                )
                .with_for_update()
            )
            if row is None:
                row = FxRate(
                    base_currency=snapshot.base_currency,
                    quote_currency=quote_currency,
                    rate=rate,
                    rate_date=snapshot.effective_date,
                    provider=snapshot.provider,
                    fetched_at=snapshot.fetched_at,
                )
                db.add(row)
            else:
                row.rate = rate
                row.provider = snapshot.provider
                row.fetched_at = snapshot.fetched_at
            rows.append(row)
    db.flush()
    return rows
