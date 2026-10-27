"""金额工具：对外用 Decimal，落库用整数最小单位。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: ISO 4217 小数位；demo 只覆盖常见币种，生产应查表
EXPONENTS: dict[str, int] = {
    "USD": 2,
    "EUR": 2,
    "CNY": 2,
    "HKD": 2,
    "SGD": 2,
    "JPY": 0,
    "KRW": 0,
}


def exponent(currency: str) -> int:
    try:
        return EXPONENTS[currency.upper()]
    except KeyError as exc:  # pragma: no cover - 输入校验兜底
        raise ValueError(f"不支持的币种: {currency}") from exc


def to_minor(amount: Decimal, currency: str) -> int:
    """1.23 USD -> 123。超出币种精度直接拒绝，避免静默四舍五入。"""
    exp = exponent(currency)
    quant = Decimal(1).scaleb(-exp)
    try:
        normalized = amount.quantize(quant)
    except InvalidOperation as exc:
        raise ValueError(f"非法金额: {amount!r}") from exc
    if normalized != amount:
        raise ValueError(f"金额精度超过 {currency} 支持的小数位 {exp}")
    return int(normalized.scaleb(exp))


def from_minor(minor: int, currency: str) -> Decimal:
    return Decimal(minor).scaleb(-exponent(currency))
