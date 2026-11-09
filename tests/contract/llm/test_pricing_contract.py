"""Contract tests for CT-7 (`api.llm.pricing`), written from the contract only.

Prices are USD per million tokens; `estimate_cost` must use exact `Decimal` arithmetic and
never fall back to a silent zero cost (DA-15).
"""

import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from api.config import get_settings
from api.llm.client import Usage
from api.llm.pricing import (
    PricingConfigError,
    UnknownModelPricing,
    estimate_cost,
    load_pricing_table,
)

ROOT_DIR = Path(__file__).resolve().parents[3]
REPO_PRICING_FILE = ROOT_DIR / "config" / "pricing.json"

MODEL = "contract-model"
PRICES: dict[str, Any] = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

PricingFactory = Callable[[Any], Path]


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _point_to(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("ATLAS_PRICING_FILE", str(path))
    get_settings.cache_clear()


@pytest.fixture
def pricing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PricingFactory:
    """Write a pricing table (dict or raw text) to a fresh file and point the settings to it."""
    counter = iter(range(1_000))

    def write(content: Any) -> Path:
        path = tmp_path / f"pricing-{next(counter)}.json"
        text = content if isinstance(content, str) else json.dumps(content)
        path.write_text(text, encoding="utf-8")
        _point_to(monkeypatch, path)
        return path

    return write


@pytest.fixture
def standard_table(pricing_file: PricingFactory) -> Path:
    return pricing_file({"_comment": "contract test table", MODEL: PRICES})


def test_public_names_and_error_hierarchy() -> None:
    assert callable(load_pricing_table)
    assert issubclass(UnknownModelPricing, LookupError)
    assert issubclass(PricingConfigError, ValueError)


def test_one_million_input_tokens_cost_the_input_price(standard_table: Path) -> None:
    cost = estimate_cost(MODEL, Usage(input_tokens=1_000_000, output_tokens=0))

    assert cost == Decimal("3.0")


def test_returns_a_decimal(standard_table: Path) -> None:
    cost = estimate_cost(MODEL, Usage(input_tokens=10, output_tokens=10))

    assert type(cost) is Decimal


def test_output_tokens_use_the_output_price(standard_table: Path) -> None:
    cost = estimate_cost(MODEL, Usage(input_tokens=0, output_tokens=1_000_000))

    assert cost == Decimal("15.0")


def test_cache_read_tokens_use_only_the_cache_read_price(standard_table: Path) -> None:
    usage = Usage(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000)

    cost = estimate_cost(MODEL, usage)

    assert cost == Decimal("0.3")
    assert cost != Decimal("3.0")


def test_cache_write_tokens_use_only_the_cache_write_price(standard_table: Path) -> None:
    usage = Usage(input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000)

    cost = estimate_cost(MODEL, usage)

    assert cost == Decimal("3.75")
    assert cost != Decimal("3.0")


def test_cache_tokens_are_not_billed_at_the_input_price(pricing_file: PricingFactory) -> None:
    # Input is expensive, cache is free: any cache token billed as input would show up.
    pricing_file({MODEL: {"input": 100, "output": 0, "cache_read": 0, "cache_write": 0}})
    usage = Usage(
        input_tokens=0, output_tokens=0, cache_read_tokens=500_000, cache_write_tokens=500_000
    )

    assert estimate_cost(MODEL, usage) == Decimal("0")


def test_mixed_usage_sums_the_four_parts(standard_table: Path) -> None:
    usage = Usage(
        input_tokens=1_000, output_tokens=500, cache_read_tokens=2_000, cache_write_tokens=400
    )
    # 1000*3.0 + 500*15.0 + 2000*0.3 + 400*3.75 = 3000 + 7500 + 600 + 1500 = 12600 -> /1M
    expected = Decimal("0.0126")

    assert estimate_cost(MODEL, usage) == expected


def test_small_values_keep_exact_precision(pricing_file: PricingFactory) -> None:
    pricing_file({MODEL: {"input": 0.15, "output": 0.6, "cache_read": 0.075, "cache_write": 0.3}})

    assert estimate_cost(MODEL, Usage(input_tokens=1, output_tokens=0)) == Decimal("0.00000015")
    assert estimate_cost(MODEL, Usage(input_tokens=0, output_tokens=1)) == Decimal("0.0000006")
    usage = Usage(input_tokens=0, output_tokens=0, cache_read_tokens=1)
    assert estimate_cost(MODEL, usage) == Decimal("0.000000075")


def test_zero_usage_costs_zero(standard_table: Path) -> None:
    cost = estimate_cost(MODEL, Usage(input_tokens=0, output_tokens=0))

    assert cost == Decimal("0")
    assert type(cost) is Decimal


def test_unknown_model_raises(standard_table: Path) -> None:
    with pytest.raises(UnknownModelPricing):
        estimate_cost("model-not-in-table", Usage(input_tokens=1, output_tokens=1))


def test_underscore_keys_are_ignored(standard_table: Path) -> None:
    with pytest.raises(UnknownModelPricing):
        estimate_cost("_comment", Usage(input_tokens=1, output_tokens=1))


def test_underscore_keys_do_not_need_price_fields(pricing_file: PricingFactory) -> None:
    pricing_file({"_comment": "free text", "_meta": {"source": "operator"}, MODEL: PRICES})

    assert estimate_cost(MODEL, Usage(input_tokens=1_000_000, output_tokens=0)) == Decimal("3.0")


def test_missing_file_raises_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _point_to(monkeypatch, tmp_path / "does-not-exist.json")

    with pytest.raises(PricingConfigError):
        estimate_cost(MODEL, Usage(input_tokens=1, output_tokens=1))


def test_invalid_json_raises_config_error(pricing_file: PricingFactory) -> None:
    pricing_file('{"contract-model": {"input": 3.0,')

    with pytest.raises(PricingConfigError):
        estimate_cost(MODEL, Usage(input_tokens=1, output_tokens=1))


@pytest.mark.parametrize("missing", PRICE_FIELDS)
def test_missing_price_field_raises_config_error(
    pricing_file: PricingFactory, missing: str
) -> None:
    prices = {name: value for name, value in PRICES.items() if name != missing}
    pricing_file({MODEL: prices})

    with pytest.raises(PricingConfigError):
        estimate_cost(MODEL, Usage(input_tokens=1, output_tokens=1))


@pytest.mark.parametrize("negative", PRICE_FIELDS)
def test_negative_price_raises_config_error(pricing_file: PricingFactory, negative: str) -> None:
    pricing_file({MODEL: {**PRICES, negative: -0.01}})

    with pytest.raises(PricingConfigError):
        estimate_cost(MODEL, Usage(input_tokens=1, output_tokens=1))


@pytest.mark.parametrize("negative", USAGE_FIELDS)
def test_negative_tokens_raise_value_error(standard_table: Path, negative: str) -> None:
    counts = {name: 0 for name in USAGE_FIELDS}
    counts[negative] = -1

    with pytest.raises(ValueError):
        estimate_cost(MODEL, Usage(**counts))


def test_switching_the_file_changes_the_result(pricing_file: PricingFactory) -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=0)

    pricing_file({MODEL: PRICES})
    assert estimate_cost(MODEL, usage) == Decimal("3.0")

    pricing_file({MODEL: {**PRICES, "input": 6.0}})
    assert estimate_cost(MODEL, usage) == Decimal("6.0")


def test_previous_test_table_does_not_leak(pricing_file: PricingFactory) -> None:
    # Runs after the test above: a stale cache would still answer 6.0 or 3.0.
    pricing_file({MODEL: {**PRICES, "input": 1.5}})

    assert estimate_cost(MODEL, Usage(input_tokens=1_000_000, output_tokens=0)) == Decimal("1.5")


def test_repository_fake_model_costs_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _point_to(monkeypatch, REPO_PRICING_FILE)
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )

    assert estimate_cost("fake-model", usage) == Decimal("0")


def test_repository_comment_key_is_not_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _point_to(monkeypatch, REPO_PRICING_FILE)

    with pytest.raises(UnknownModelPricing):
        estimate_cost("_comment", Usage(input_tokens=1, output_tokens=1))
