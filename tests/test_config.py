import pytest

from connector_health.config import Config, providers_from


def test_config_defaults_without_smtp_in_dry_run(monkeypatch):
    for key in (
        "PROVIDERS",
        "GRACE_HOURS",
        "GRACE_TOLERANCE_HOURS",
        "REMIND_EVERY_HOURS",
        "INCLUDE_RESOLVED",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "EMAIL_TO",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("QUALYS_BASE_URL", "https://qualys.example")
    monkeypatch.setenv("QUALYS_USERNAME", "sample")
    monkeypatch.setenv("QUALYS_PASSWORD", "sample")
    config = Config.from_env(dry_run=True)
    assert config.grace_hours == 24 and config.tolerance_hours == 2
    assert config.smtp_host == "smtp.gmail.com" and config.smtp_port == 587
    assert config.providers == ("AWS", "AZURE", "GCP")
    with pytest.raises(ValueError, match="Missing SMTP_USER"):
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


@pytest.mark.parametrize(
    "host,port,expected",
    [("smtp.example.com", "2525", ("smtp.example.com", 2525)), ("", "", ("smtp.gmail.com", 587))],
)
def test_smtp_environment(monkeypatch, host, port, expected):
    for key, value in {
        "QUALYS_BASE_URL": "https://qualys.example",
        "QUALYS_USERNAME": "sample",
        "QUALYS_PASSWORD": "sample",
        "SMTP_HOST": host,
        "SMTP_PORT": port,
        "SMTP_USER": "sender@example.com",
        "SMTP_PASSWORD": "test-app-password",
        "EMAIL_TO": "first@example.com, second@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    config = Config.from_env()
    assert (config.smtp_host, config.smtp_port) == expected
    assert config.smtp_user == "sender@example.com"
    assert config.smtp_password == "test-app-password"
    assert config.recipients == ("first@example.com", "second@example.com")


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_invalid_smtp_port(monkeypatch, port):
    monkeypatch.setenv("QUALYS_BASE_URL", "https://qualys.example")
    monkeypatch.setenv("SMTP_PORT", port)
    with pytest.raises(ValueError):
        Config.from_env(True)
