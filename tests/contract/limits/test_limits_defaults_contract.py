"""Default demo limits and retention (DEMO-02, DEMO-03, DEMO-10, CT-66 settings).

Without any environment variable or `.env`, a persona gets 20 messages per hour, the demo spends
at most 2.00 USD per UTC day and data is kept for 7 days.
"""

from decimal import Decimal

import pytest

from api.config import Settings

LIMIT_VARIABLES = (
    "ATLAS_RATE_LIMIT_PER_HOUR",
    "ATLAS_DAILY_SPEND_CAP_USD",
    "ATLAS_RETENTION_DAYS",
)


def test_default_limits_without_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LIMIT_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.rate_limit_per_hour == 20
    assert settings.daily_spend_cap_usd == Decimal("2.00")
    assert isinstance(settings.daily_spend_cap_usd, Decimal)
    assert settings.retention_days == 7


def test_limits_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_RATE_LIMIT_PER_HOUR", "5")
    monkeypatch.setenv("ATLAS_DAILY_SPEND_CAP_USD", "0.50")
    monkeypatch.setenv("ATLAS_RETENTION_DAYS", "3")

    settings = Settings(_env_file=None)

    assert settings.rate_limit_per_hour == 5
    assert settings.daily_spend_cap_usd == Decimal("0.50")
    assert settings.retention_days == 3
