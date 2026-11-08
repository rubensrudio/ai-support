"""Model pricing and cost estimation in USD (CT-7, DA-15).

Prices live in the JSON file named by `ATLAS_PRICING_FILE` (default `config/pricing.json`,
relative to the current directory), as USD per **million** tokens:

    {"<model>": {"input": n, "output": n, "cache_read": n, "cache_write": n}}

Keys starting with `_` are comments and are ignored. The daily spend cap depends on this
math (AS-2), so every failure raises: an unknown model, an unreadable file or a bad price
never turns into a silent zero cost. All arithmetic is `Decimal`; floats never touch it.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal, Inexact, localcontext
from functools import lru_cache
from types import MappingProxyType
from typing import Any

from api.config import get_settings
from api.llm.client import Usage

TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)
# Enough digits for any realistic token count times any price, so no step ever rounds.
_COST_PRECISION = 60


class UnknownModelPricing(LookupError):
    """The model has no entry in the pricing table; cost is never silently zero."""

    def __init__(self, model: str, path: str) -> None:
        super().__init__(f"No pricing for model {model!r} in {path}")
        self.model = model
        self.path = path


class PricingConfigError(ValueError):
    """The pricing file is missing, is not valid JSON, or holds an invalid entry."""


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens for each token kind."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal


_PRICE_FIELDS = tuple(f.name for f in fields(ModelPrice))


def _parse_price(path: str, model: str, field: str, value: Any) -> Decimal:
    # bool is an int subclass: `true` must not become a price of 1.
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise PricingConfigError(
            f"Pricing file {path}: model {model!r} field {field!r} must be a number, got {value!r}"
        )
    price = Decimal(str(value))
    if not price.is_finite():
        raise PricingConfigError(
            f"Pricing file {path}: model {model!r} field {field!r} must be finite"
        )
    if price < 0:
        raise PricingConfigError(
            f"Pricing file {path}: model {model!r} field {field!r} must not be negative, "
            f"got {price}"
        )
    return price


def _parse_entry(path: str, model: str, entry: Any) -> ModelPrice:
    if not isinstance(entry, dict):
        raise PricingConfigError(
            f"Pricing file {path}: model {model!r} must be an object with fields "
            f"{', '.join(_PRICE_FIELDS)}"
        )
    prices: dict[str, Decimal] = {}
    for field in _PRICE_FIELDS:
        if field not in entry:
            raise PricingConfigError(
                f"Pricing file {path}: model {model!r} is missing field {field!r}"
            )
        prices[field] = _parse_price(path, model, field, entry[field])
    return ModelPrice(**prices)


@lru_cache
def load_pricing_table(path: str) -> Mapping[str, ModelPrice]:
    """Read and validate the pricing file. Cached per path; failures are not cached."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise PricingConfigError(f"Cannot read pricing file {path}: {exc.strerror}") from exc
    try:
        # Decimal straight from the JSON text: no float round trip, no binary error.
        # NaN/Infinity parse to Decimal too, so the field check below rejects them by name.
        raw = json.loads(text, parse_float=Decimal, parse_constant=Decimal)
    except ValueError as exc:
        raise PricingConfigError(f"Pricing file {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PricingConfigError(f"Pricing file {path} must hold a JSON object of models")

    table = {
        model: _parse_entry(path, model, entry)
        for model, entry in raw.items()
        if not model.startswith("_")
    }
    return MappingProxyType(table)


def _check_usage(usage: Usage) -> None:
    for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        if getattr(usage, name) < 0:
            raise ValueError(f"Usage.{name} must not be negative, got {getattr(usage, name)}")


def estimate_cost(model: str, usage: Usage) -> Decimal:
    """Exact USD cost of one call. Cache tokens are priced only by `cache_read`/`cache_write`;
    `usage.input_tokens` already excludes them. The result is not rounded."""
    _check_usage(usage)
    path = get_settings().pricing_file
    table = load_pricing_table(path)
    price = table.get(model)
    if price is None:
        raise UnknownModelPricing(model, path)

    with localcontext() as ctx:
        ctx.prec = _COST_PRECISION
        ctx.traps[Inexact] = True
        total = (
            Decimal(usage.input_tokens) * price.input
            + Decimal(usage.output_tokens) * price.output
            + Decimal(usage.cache_read_tokens) * price.cache_read
            + Decimal(usage.cache_write_tokens) * price.cache_write
        )
        return total / TOKENS_PER_PRICE_UNIT
