import csv
import io
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from connector_health import monthly
from connector_health.__main__ import process, report
from connector_health.config import Config
from connector_health.models import Connector
from connector_health.notifier import MailError
from connector_health.qualys_client import APIError
from connector_health.templates import build_monthly_email

OCT = datetime(2026, 10, 9, 13, tzinfo=UTC)
NOV = datetime(2026, 11, 1, 13, tzinfo=UTC)
CONFIG = Config(
    "https://qualys.example",
    "user",
    "password",
    "sender@example.com",
    "app-password",
    ("recipient@example.com",),
    providers=("AWS", "AZURE"),
)
BASE = [
    Connector("AWS", "1", "kept", "SUCCESS", account="111"),
    Connector("AWS", "2", "<broken&>", "FINISHED_ERRORS", error="API error in all regions"),
    Connector("AWS", "3", "=retired", "SUCCESS"),
    Connector("AZURE", "4", "paused", "SUCCESS", disabled=True),
]


def client_for(connectors):
    client = Mock()
    client.fetch.side_effect = lambda provider: [c for c in connectors if c.provider == provider]
    return client


def run(tmp_path, connectors, now, sender=None, client=None, **kwargs):
    return process(
        CONFIG,
        client or client_for(connectors),
        tmp_path / "state.json",
        now=now,
        sender=sender or Mock(),
        **kwargs,
    )


def october(tmp_path):
    """Baseline, then one day later: AWS:3 removed, AWS:5 added and recovered later."""
    run(tmp_path, BASE, OCT)
    day2 = [c for c in BASE if c.id != "3"] + [Connector("AWS", "5", "new", "ERROR")]
    run(tmp_path, day2, OCT + timedelta(days=1))
    day3 = day2[:-1] + [Connector("AWS", "5", "new", "SUCCESS")]
    run(tmp_path, day3, OCT + timedelta(days=22))
    return monthly.load(tmp_path / "monthly" / "2026-10.json")


def test_baseline_additions_removals_and_counts(tmp_path):
    stats = october(tmp_path)
    connectors = stats["connectors"]
    assert stats["runs"] == 3 and stats["report_sent_at"] is None
    assert stats["providers"]["AWS"]["baseline_at"] == OCT.isoformat()
    assert all(connectors[k]["in_start"] for k in ("AWS:1", "AWS:2", "AWS:3", "AZURE:4"))
    assert connectors["AWS:1"]["added_at"] is None
    assert connectors["AWS:3"]["removed_at"] == (OCT + timedelta(days=1)).isoformat()
    assert connectors["AWS:5"]["added_at"] == (OCT + timedelta(days=1)).isoformat()
    assert not connectors["AWS:5"]["in_start"]
    assert connectors["AWS:5"]["counts"] == {"healthy": 1, "error": 1, "neutral": 0, "disabled": 0}
    assert connectors["AWS:5"]["error_since"] is None
    assert connectors["AWS:2"]["error_since"] == OCT.isoformat()
    assert connectors["AWS:2"]["last_error"] == "API error in all regions"
    assert connectors["AZURE:4"]["last_class"] == "disabled"


def test_month_end_report_sent_once_and_new_month_carries_inventory(tmp_path):
    october(tmp_path)
    sender = Mock()
    assert run(tmp_path, BASE[:2] + BASE[3:], NOV, sender=sender) == 0
    message = sender.call_args_list[0].args[0]
    assert message["Subject"] == "[Qualys Connector Health] Monthly report October 2026"
    assert monthly.load(tmp_path / "monthly" / "2026-10.json")["report_sent_at"] == NOV.isoformat()

    november = monthly.load(tmp_path / "monthly" / "2026-11.json")
    assert set(november["connectors"]) == {"AWS:1", "AWS:2", "AWS:5", "AZURE:4"}
    assert not any(e["added_at"] for e in november["connectors"].values())
    assert november["connectors"]["AWS:5"]["start_class"] == "healthy"
    assert november["connectors"]["AWS:2"]["error_since"] == OCT.isoformat()
    # AWS:5 disappeared on the first November check.
    assert november["connectors"]["AWS:5"]["removed_at"] == NOV.isoformat()

    sender.reset_mock()
    run(tmp_path, BASE[:2] + BASE[3:], NOV + timedelta(days=1), sender=sender)
    assert all("Monthly" not in call.args[0]["Subject"] for call in sender.call_args_list)


def test_report_content(tmp_path):
    stats = october(tmp_path)
    message = build_monthly_email(stats, CONFIG)
    text = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "AWS | 3 | 1 | 1 | 3 | 4" in text  # start, added, removed, end, observed
    assert "AWS | 2 | 1 | 0 | 0 | 2 | 1 | 1 | 3 ok / 0 failed" in text
    assert "Total | 4 | 1 | 1 | 4 | 5" in text
    assert "Open issues at month end (1)\n" in text
    assert "AWS | 2 | <broken&> |" in text and "| 22.0 | API error in all regions" in text
    assert "Reported errors this month, no longer failing (1)\nProvider" in text
    assert "Added connectors (1)" in text and "Removed connectors (1)" in text
    assert "<broken&>" not in html and "&lt;broken&amp;&gt;" in html
    assert "inventory baseline taken" in text

    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == "qualys-connectors-2026-10.csv"
    rows = list(csv.DictReader(io.StringIO(attachment.get_content())))
    assert len(rows) == 5
    retired = next(r for r in rows if r["connector_id"] == "3")
    assert retired["name"] == "'=retired" and retired["status_at_end"] == "Removed"


def test_report_smtp_failure_stays_pending_and_fails_job(tmp_path):
    october(tmp_path)

    def sender(message, config):
        if "Monthly" in message["Subject"]:
            raise MailError("smtp_failed")

    assert run(tmp_path, BASE, NOV, sender=sender) == 1
    assert monthly.load(tmp_path / "monthly" / "2026-10.json")["report_sent_at"] is None
    assert monthly.load(tmp_path / "monthly" / "2026-11.json")["runs"] == 1
    retry = Mock()
    assert run(tmp_path, BASE, NOV + timedelta(days=1), sender=retry) == 0
    assert "October 2026" in retry.call_args_list[0].args[0]["Subject"]


def test_api_failure_counts_without_marking_removals(tmp_path):
    run(tmp_path, BASE, OCT)
    client = client_for(BASE)
    client.fetch.side_effect = lambda provider: (_ for _ in ()).throw(APIError("down"))
    assert run(tmp_path, BASE, OCT + timedelta(days=1), client=client) == 1
    stats = monthly.load(tmp_path / "monthly" / "2026-10.json")
    assert stats["providers"]["AWS"]["failed_checks"] == 1
    assert not any(e["removed_at"] for e in stats["connectors"].values())


def test_dry_run_previews_pending_report_without_writing(tmp_path, capsys):
    october(tmp_path)
    before = (tmp_path / "monthly" / "2026-10.json").read_bytes()
    sender = Mock()
    assert run(tmp_path, BASE, NOV, sender=sender, dry_run=True) == 0
    sender.assert_not_called()
    assert "Monthly report October 2026" in capsys.readouterr().out
    assert (tmp_path / "monthly" / "2026-10.json").read_bytes() == before
    assert not (tmp_path / "monthly" / "2026-11.json").exists()


def test_on_demand_month_to_date_report(tmp_path, capsys):
    october(tmp_path)
    state = tmp_path / "state.json"
    sender = Mock()
    now = OCT + timedelta(days=22)
    assert report(CONFIG, state, send=True, now=now, sender=sender) == 0
    assert sender.call_args.args[0]["Subject"].endswith("October 2026 (month to date)")
    assert monthly.load(tmp_path / "monthly" / "2026-10.json")["report_sent_at"] is None
    assert report(CONFIG, state, "2026-10", now=NOV) == 0
    assert "Subject: [Qualys Connector Health] Monthly report October 2026\n" in (
        capsys.readouterr().out
    )
    assert report(CONFIG, state, "2026-09", now=NOV) == 1


def test_mail_only_config_skips_qualys(monkeypatch):
    for key in ("QUALYS_BASE_URL", "QUALYS_USERNAME", "QUALYS_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EMAIL_TO", "a@example.com")
    assert Config.from_env(True, qualys=False).recipients == ("a@example.com",)
    with pytest.raises(ValueError):
        Config.from_env(True)


def test_corrupt_monthly_file_fails(tmp_path):
    directory = tmp_path / "monthly"
    directory.mkdir()
    (directory / "2026-10.json").write_text('{"version":1,"month":"2026-09"}', encoding="utf-8")
    with pytest.raises(ValueError):
        run(tmp_path, BASE, OCT)
