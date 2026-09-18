import pytest

from connector_health.config import Config, providers_from


def test_config_defaults_without_smtp_in_dry_run(monkeypatch):
    for key in (
        "PROVIDERS",
        "GRACE_HOURS",
        "GRACE_TOLERANCE_HOURS",
        "REMIND_EVERY_HOURS",
        "INCLUDE_RESOLVED",
        "GMAIL_USER",
        "GMAIL_APP_PASSWORD",
        "ALERT_RECIPIENTS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("QUALYS_BASE_URL", "https://qualys.example")
    monkeypatch.setenv("QUALYS_USERNAME", "sample")
    monkeypatch.setenv("QUALYS_PASSWORD", "sample")
    config = Config.from_env(dry_run=True)
    assert config.grace_hours == 24 and config.tolerance_hours == 2
    assert config.providers == ("AWS", "AZURE", "GCP")
    with pytest.raises(ValueError, match="Missing GMAIL_USER"):
        Config.from_env()


@pytest.mark.parametrize("value", ["", "OCI", "AWS,", "AWS,GCP,NOPE"])
def test_invalid_provider(value):
    with pytest.raises(ValueError):
        providers_from(value)


@pytest.mark.parametrize(
    "base",
    [
        "http://qualys.example",
        "https://user:pass@qualys.example",
        "https://qualys.example/path",
        "https://qualys.example?token=x",
    ],
)
def test_invalid_origin(monkeypatch, base):
    monkeypatch.setenv("QUALYS_BASE_URL", base)
    with pytest.raises(ValueError, match="HTTPS origin"):
        Config.from_env(True)
