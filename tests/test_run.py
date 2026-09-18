from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from connector_health.__main__ import process
from connector_health.config import Config
from connector_health.health import evaluate
from connector_health.models import Connector
from connector_health.notifier import MailError, send_email
from connector_health.qualys_client import APIError
from connector_health.state_store import empty_state, load, save

NOW = datetime(2026, 1, 1, tzinfo=UTC)
CONFIG = Config(
    "https://qualys.example",
    "user",
    "password",
    "sender@example.com",
    "app-password",
    ("recipient@example.com",),
    include_resolved=True,
)
CONNECTORS = [
    Connector(p, str(i), f"<{p}&{i}>", "ERROR", error="<script>alert('x')</script>\n" + "x" * 2000)
    for i, p in enumerate(("AWS", "AWS", "AZURE", "GCP"))
]


def client_for(connectors):
    client = Mock()
    client.fetch.side_effect = lambda provider: [c for c in connectors if c.provider == provider]
    return client


def initial_state(path):
    state = empty_state()
    state["episodes"] = evaluate(CONNECTORS, {}, {}, NOW, CONFIG).episodes
    save(path, state)
    return state


def test_one_email_per_provider_and_escaping(tmp_path):
    path = tmp_path / "state.json"
    initial_state(path)
    sender = Mock()
    assert (
        process(CONFIG, client_for(CONNECTORS), path, now=NOW + timedelta(hours=23), sender=sender)
        == 0
    )
    assert sender.call_count == 3
    messages = [call.args[0] for call in sender.call_args_list]
    assert "[AWS] 2 connector(s)" in messages[0]["Subject"]
    assert "[AZURE] 1 connector(s)" in messages[1]["Subject"]
    text = messages[0].get_body(preferencelist=("plain",)).get_content()
    html = messages[0].get_body(preferencelist=("html",)).get_content()
    assert "x" * 2000 in text and "<AWS&0>" in text
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert all(entry["notified_at"] for entry in load(path)["episodes"].values())
    sender.reset_mock()
    process(CONFIG, client_for(CONNECTORS), path, now=NOW + timedelta(hours=48), sender=sender)
    sender.assert_not_called()


def test_api_failure_preserves_provider_and_continues(tmp_path):
    path = tmp_path / "state.json"
    before = initial_state(path)
    client = client_for(CONNECTORS)

    def fetch(provider):
        if provider == "AWS":
            raise APIError("retries_exhausted")
        return [c for c in CONNECTORS if c.provider == provider]

    client.fetch.side_effect = fetch
    sender = Mock()
    assert process(CONFIG, client, path, now=NOW + timedelta(hours=24), sender=sender) == 1
    after = load(path)
    for c in CONNECTORS[:2]:
        assert after["episodes"][c.key] == before["episodes"][c.key]
    assert sender.call_count == 2


def test_smtp_failure_persists_observations_without_ack(tmp_path):
    path = tmp_path / "state.json"
    initial_state(path)
    sender = Mock(side_effect=[MailError("failed"), None, None])
    assert (
        process(CONFIG, client_for(CONNECTORS), path, now=NOW + timedelta(hours=24), sender=sender)
        == 1
    )
    assert load(path)["episodes"]["AWS:0"]["notified_at"] is None
    assert load(path)["episodes"]["AZURE:2"]["notified_at"] is not None


def test_dry_run_never_writes_or_sends(tmp_path, capsys):
    path = tmp_path / "state.json"
    initial_state(path)
    before = path.read_bytes()
    sender = Mock()
    assert (
        process(
            CONFIG, client_for(CONNECTORS), path, dry_run=True, force=True, now=NOW, sender=sender
        )
        == 0
    )
    assert path.read_bytes() == before
    sender.assert_not_called()
    assert capsys.readouterr().out.count("Subject:") == 3
    missing = tmp_path / "missing.json"
    process(
        CONFIG, client_for(CONNECTORS), missing, dry_run=True, force=True, now=NOW, sender=sender
    )
    assert not missing.exists()


def test_no_email_for_only_resolved(tmp_path):
    path = tmp_path / "state.json"
    initial_state(path)
    sender = Mock()
    healthy = [replace(c, state="SUCCESS") for c in CONNECTORS]
    process(CONFIG, client_for(healthy), path, now=NOW + timedelta(hours=24), sender=sender)
    sender.assert_not_called()
    state = load(path)
    assert not state["episodes"] and len(state["resolved"]) == 4


def test_state_validation_and_atomic_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = initial_state(path)
    assert load(path) == state
    unchanged = path.stat().st_mtime_ns
    save(path, deepcopy(state))
    assert path.stat().st_mtime_ns == unchanged
    path.write_text('{"version":1,"episodes":[]}', encoding="utf-8")
    with pytest.raises(ValueError):
        load(path)
    assert path.read_text() == '{"version":1,"episodes":[]}'


def test_smtp_tls_and_partial_refusal(monkeypatch):
    smtp = Mock()
    smtp.send_message.return_value = {"recipient@example.com": (550, b"refused")}
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=smtp)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("connector_health.notifier.smtplib.SMTP", factory)
    with pytest.raises(MailError):
        send_email(Mock(), CONFIG)
    assert factory.call_args.args == ("smtp.gmail.com", 587)
    assert factory.call_args.kwargs["timeout"] == 30
    assert smtp.starttls.call_args.kwargs["context"].check_hostname
    assert smtp.ehlo.call_count == 2


def test_summary(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    process(CONFIG, client_for(CONNECTORS), tmp_path / "state.json", now=NOW)
    text = summary.read_text()
    assert "| AWS | 2 | 0 | 2 | 0 | 0 |" in text


@pytest.mark.parametrize("tls_fails", [False, True])
def test_smtp_starttls_before_authentication(monkeypatch, tls_fails):
    import smtplib
    from unittest.mock import ANY, call

    smtp = Mock()
    smtp.send_message.return_value = {}
    if tls_fails:
        smtp.starttls.side_effect = smtplib.SMTPNotSupportedError("STARTTLS unavailable")
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=smtp)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("connector_health.notifier.smtplib.SMTP", factory)
    message = Mock()
    config = replace(CONFIG, smtp_host="smtp.example.com", smtp_port=2525)
    if tls_fails:
        with pytest.raises(MailError):
            send_email(message, config)
        smtp.login.assert_not_called()
        smtp.send_message.assert_not_called()
    else:
        send_email(message, config)
        assert smtp.method_calls == [
            call.ehlo(),
            call.starttls(context=ANY),
            call.ehlo(),
            call.login(config.smtp_user, config.smtp_password),
            call.send_message(
                message, from_addr=config.smtp_user, to_addrs=list(config.recipients)
            ),
        ]
    factory.assert_called_once_with("smtp.example.com", 2525, timeout=30)
