import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from api.config import get_settings
from api.llm.client import Usage
from api.llm.pricing import (
    ModelPrice,
    PricingConfigError,
    UnknownModelPricing,
    estimate_cost,
    load_pricing_table,
)

ROOT_DIR = Path(__file__).resolve().parents[4]
REPO_PRICING_FILE = ROOT_DIR / "config" / "pricing.json"

PRICES: dict[str, Any] = {
    "_comment": "test table",
    "model-a": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "model-b": {"input": 1, "output": 5, "cache_read": 0, "cache_write": 0},
}


@pytest.fixture(autouse=True)
def clear_caches() -> Iterator[None]:
    load_pricing_table.cache_clear()
    get_settings.cache_clear()
    yield
    load_pricing_table.cache_clear()
    get_settings.cache_clear()


def _write(path: Path, content: Any) -> Path:
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def pricing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _write(tmp_path / "pricing.json", PRICES)
    monkeypatch.setenv("ATLAS_PRICING_FILE", str(path))
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    return path


def _use_file(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_PRICING_FILE", str(path))
    get_settings.cache_clear()


# --- cost formula -------------------------------------------------------------------------


def test_one_million_input_tokens_cost_the_input_price(pricing_file: Path) -> None:
    cost = estimate_cost("model-a", Usage(input_tokens=1_000_000, output_tokens=0))
    assert cost == Decimal("3.0")
    assert isinstance(cost, Decimal)


def test_output_tokens_use_the_output_price(pricing_file: Path) -> None:
    cost = estimate_cost("model-a", Usage(input_tokens=0, output_tokens=2_000_000))
    assert cost == Decimal("30")


def test_cache_tokens_use_cache_prices_not_the_input_price(pricing_file: Path) -> None:
    read = estimate_cost(
        "model-a", Usage(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000)
    )
    write = estimate_cost(
        "model-a", Usage(input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000)
    )
    assert read == Decimal("0.3")
    assert write == Decimal("3.75")
    assert read != Decimal("3.0")
    assert write != Decimal("3.0")


def test_cache_tokens_are_free_when_the_cache_price_is_zero(pricing_file: Path) -> None:
    cost = estimate_cost(
        "model-b",
        Usage(input_tokens=0, output_tokens=0, cache_read_tokens=500, cache_write_tokens=700),
    )
    assert cost == Decimal("0")


def test_cost_sums_all_four_token_kinds_exactly(pricing_file: Path) -> None:
    usage = Usage(
        input_tokens=1234, output_tokens=567, cache_read_tokens=8901, cache_write_tokens=23
    )
    expected = (
        Decimal(1234) * Decimal("3.0")
        + Decimal(567) * Decimal("15.0")
        + Decimal(8901) * Decimal("0.3")
        + Decimal(23) * Decimal("3.75")
    ) / Decimal(1_000_000)
    cost = estimate_cost("model-a", usage)
    assert cost == expected
    assert cost == Decimal("0.01496355")


def test_cost_is_not_rounded(pricing_file: Path) -> None:
    cost = estimate_cost("model-a", Usage(input_tokens=1, output_tokens=0))
    assert cost == Decimal("0.000003")


def test_zero_usage_costs_zero(pricing_file: Path) -> None:
    assert estimate_cost("model-a", Usage(input_tokens=0, output_tokens=0)) == Decimal("0")


def test_float_prices_do_not_leak_binary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prices = {"m": {"input": 0.1, "output": 0.2, "cache_read": 0, "cache_write": 0}}
    _use_file(_write(tmp_path / "p.json", prices), monkeypatch)
    cost = estimate_cost("m", Usage(input_tokens=1_000_000, output_tokens=1_000_000))
    assert cost == Decimal("0.3")


# --- unknown model ------------------------------------------------------------------------


def test_unknown_model_raises_and_names_the_model(pricing_file: Path) -> None:
    with pytest.raises(UnknownModelPricing, match="model-zzz"):
        estimate_cost("model-zzz", Usage(input_tokens=10, output_tokens=10))


def test_unknown_model_pricing_is_a_lookup_error() -> None:
    assert issubclass(UnknownModelPricing, LookupError)


def test_underscore_keys_are_ignored_and_not_models(pricing_file: Path) -> None:
    table = load_pricing_table(str(pricing_file))
    assert "_comment" not in table
    assert set(table) == {"model-a", "model-b"}
    with pytest.raises(UnknownModelPricing):
        estimate_cost("_comment", Usage(input_tokens=1, output_tokens=1))


# --- invalid usage ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "usage",
    [
        Usage(input_tokens=-1, output_tokens=0),
        Usage(input_tokens=0, output_tokens=-1),
        Usage(input_tokens=0, output_tokens=0, cache_read_tokens=-1),
        Usage(input_tokens=0, output_tokens=0, cache_write_tokens=-1),
    ],
)
def test_negative_tokens_raise_value_error(pricing_file: Path, usage: Usage) -> None:
    with pytest.raises(ValueError, match="negative"):
        estimate_cost("model-a", usage)


# --- invalid pricing file -----------------------------------------------------------------


def test_missing_file_raises_config_error_naming_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope.json"
    _use_file(missing, monkeypatch)
    with pytest.raises(PricingConfigError) as excinfo:
        estimate_cost("model-a", Usage(input_tokens=1, output_tokens=1))
    assert str(missing) in str(excinfo.value)


def test_invalid_json_raises_config_error(tmp_path: Path) -> None:
    path = _write(tmp_path / "bad.json", "{not json")
    with pytest.raises(PricingConfigError) as excinfo:
        load_pricing_table(str(path))
    assert str(path) in str(excinfo.value)


def test_top_level_must_be_an_object(tmp_path: Path) -> None:
    path = _write(tmp_path / "list.json", [1, 2])
    with pytest.raises(PricingConfigError):
        load_pricing_table(str(path))


def test_model_entry_must_be_an_object(tmp_path: Path) -> None:
    path = _write(tmp_path / "entry.json", {"m": 3.0})
    with pytest.raises(PricingConfigError, match="'m'"):
        load_pricing_table(str(path))


@pytest.mark.parametrize("field", ["input", "output", "cache_read", "cache_write"])
def test_missing_field_raises_config_error_naming_model_and_field(
    tmp_path: Path, field: str
) -> None:
    entry = {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}
    del entry[field]
    path = _write(tmp_path / "missing_field.json", {"m": entry})
    with pytest.raises(PricingConfigError) as excinfo:
        load_pricing_table(str(path))
    message = str(excinfo.value)
    assert str(path) in message
    assert "'m'" in message
    assert field in message


def test_negative_price_raises_config_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "neg.json",
        {"m": {"input": 1, "output": -0.5, "cache_read": 0, "cache_write": 0}},
    )
    with pytest.raises(PricingConfigError) as excinfo:
        load_pricing_table(str(path))
    message = str(excinfo.value)
    assert str(path) in message
    assert "'m'" in message
    assert "output" in message


@pytest.mark.parametrize("value", ['"3.0"', "true", "null", "NaN", "Infinity", "[1]"])
def test_non_numeric_or_non_finite_price_raises_config_error(tmp_path: Path, value: str) -> None:
    text = '{"m": {"input": ' + value + ', "output": 1, "cache_read": 0, "cache_write": 0}}'
    path = _write(tmp_path / "type.json", text)
    with pytest.raises(PricingConfigError, match="input"):
        load_pricing_table(str(path))


def test_config_error_is_a_value_error() -> None:
    assert issubclass(PricingConfigError, ValueError)


def test_invalid_file_never_yields_a_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_file(_write(tmp_path / "bad.json", "{"), monkeypatch)
    with pytest.raises(PricingConfigError):
        estimate_cost("model-a", Usage(input_tokens=0, output_tokens=0))


# --- table loading and cache --------------------------------------------------------------


def test_table_entries_are_frozen_decimal_prices(pricing_file: Path) -> None:
    price = load_pricing_table(str(pricing_file))["model-a"]
    assert price == ModelPrice(
        input=Decimal("3.0"),
        output=Decimal("15.0"),
        cache_read=Decimal("0.3"),
        cache_write=Decimal("3.75"),
    )
    with pytest.raises(AttributeError):
        price.input = Decimal("0")  # type: ignore[misc]


def test_table_is_read_only(pricing_file: Path) -> None:
    table = load_pricing_table(str(pricing_file))
    with pytest.raises(TypeError):
        table["model-x"] = table["model-a"]  # type: ignore[index]


def test_table_is_cached_per_path(pricing_file: Path) -> None:
    first = load_pricing_table(str(pricing_file))
    _write(pricing_file, {})
    assert load_pricing_table(str(pricing_file)) is first


def test_switching_pricing_file_uses_the_new_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    cheap = {"m": {"input": 1, "output": 0, "cache_read": 0, "cache_write": 0}}
    pricey = {"m": {"input": 7, "output": 0, "cache_read": 0, "cache_write": 0}}

    _use_file(_write(tmp_path / "cheap.json", cheap), monkeypatch)
    assert estimate_cost("m", usage) == Decimal("1")

    _use_file(_write(tmp_path / "pricey.json", pricey), monkeypatch)
    assert estimate_cost("m", usage) == Decimal("7")


# --- shipped config/pricing.json ----------------------------------------------------------


def test_repo_pricing_file_has_comment_and_free_fake_model() -> None:
    raw = json.loads(REPO_PRICING_FILE.read_text(encoding="utf-8"))
    assert isinstance(raw.get("_comment"), str) and raw["_comment"]
    table = load_pricing_table(str(REPO_PRICING_FILE))
    assert table["fake-model"] == ModelPrice(
        input=Decimal(0), output=Decimal(0), cache_read=Decimal(0), cache_write=Decimal(0)
    )


def test_default_pricing_file_is_relative_to_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT_DIR)
    monkeypatch.setenv("ATLAS_PRICING_FILE", "config/pricing.json")
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    usage = Usage(input_tokens=10, output_tokens=10, cache_read_tokens=5, cache_write_tokens=5)
    assert estimate_cost("fake-model", usage) == Decimal("0")
