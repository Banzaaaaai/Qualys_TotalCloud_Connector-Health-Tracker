"""OCI coverage through the same CLI and provider transaction used by Actions."""

import csv
import io
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import responses

from connector_health import monthly
from connector_health.__main__ import main, process
from connector_health.config import Config
from connector_health.qualys_client import QualysClient
from connector_health.state_store import load
from connector_health.templates import build_monthly_email

NOW = datetime(2026, 1, 1, tzinfo=UTC)
CONFIG = Config("https://qualys.example", "api-user", "api-password", providers=("AWS", "OCI"))
OCI_URL = CONFIG.base_url + "/qps/rest/3.0/search/am/ociassetdataconnector"
AWS_URL = CONFIG.base_url + "/qps/rest/3.0/search/am/awsassetdataconnector"


def fixture(provider="oci"):
    return json.loads((Path(__file__).parent / "fixtures" / f"{provider}.json").read_text())


@responses.activate
@pytest.mark.parametrize("selection", ["", "OCI"])
def test_workflow_cli_defaults_include_oci_without_writing_or_sending(
    tmp_path, monkeypatch, capsys, selection
):
    for key, value in {
        "QUALYS_BASE_URL": CONFIG.base_url,
        "QUALYS_USERNAME": CONFIG.username,
        "QUALYS_PASSWORD": CONFIG.password,
        "PROVIDERS": selection,  # Empty when neither workflow input nor variable is set.
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }.items():
        monkeypatch.setenv(key, value)
    state = tmp_path / "state.json"
    monkeypatch.setattr(
        "sys.argv",
        ["connector_health", "run", "--dry-run", "--force-notify", "--state-file", str(state)],
    )
    providers = ("oci",) if selection else ("aws", "azure", "gcp", "oci")
    for provider in providers:
        url = CONFIG.base_url + f"/qps/rest/3.0/search/am/{provider}assetdataconnector"
        responses.post(url, json=fixture(provider))
    assert main() == 0
    assert len(responses.calls) == len(providers)
    assert "| OCI | 1 | 0 | 0 | 0 | 0 |" in (tmp_path / "summary.md").read_text()
    preview = capsys.readouterr().out
    assert "[OCI] 1 connector(s)" in preview
    assert "ocid1.tenancy.oc1..example" in preview
    assert "synthetic-oci-private-key-secret" not in preview
    assert not state.exists() and not (tmp_path / "monthly").exists()


@responses.activate
@pytest.mark.parametrize(
    "failure",
    ["denied", "missing_pagination", "duplicate_page", "detail_failed", "wrong_detail_id"],
)
def test_incomplete_oci_fetch_preserves_history(tmp_path, failure):
    path = tmp_path / "state.json"
    sender = Mock()
    responses.post(AWS_URL, json=fixture("aws"))
    responses.post(OCI_URL, json=fixture())
    client = QualysClient(CONFIG)
    assert process(CONFIG, client, path, now=NOW, sender=sender) == 0
    before = load(path)
    old_inventory = monthly.load(tmp_path / "monthly" / "2026-01.json")["connectors"]["OCI:101"]
    responses.reset()
    aws = fixture("aws")
    aws["ServiceResponse"]["data"][0]["AwsAssetDataConnector"]["connectorState"] = "SUCCESS"
    responses.post(AWS_URL, json=aws)
    payload = fixture()
    if failure == "denied":
        responses.post(OCI_URL, status=403)
    elif failure == "missing_pagination":
        del payload["ServiceResponse"]["hasMoreRecords"]
        responses.post(OCI_URL, json=payload)
    elif failure == "duplicate_page":
        payload["ServiceResponse"]["hasMoreRecords"] = "true"
        responses.post(OCI_URL, json=payload)
    else:
        raw = payload["ServiceResponse"]["data"][0]["OciAssetDataConnector"]
        raw.pop("connectorAppInfos")
        responses.post(OCI_URL, json=payload)
        detail_url = OCI_URL.replace("/search/", "/get/") + "/101"
        if failure == "detail_failed":
            responses.get(detail_url, status=403)
        else:
            detail = deepcopy(payload)
            detail["ServiceResponse"]["data"][0]["OciAssetDataConnector"]["id"] = 999
            responses.get(detail_url, json=detail)
    assert process(CONFIG, client, path, now=NOW + timedelta(days=1), sender=sender) == 1
    after = load(path)
    assert after["episodes"]["OCI:101"] == before["episodes"]["OCI:101"]
    assert "AWS:101" not in after["episodes"]  # Other providers still make progress.
    stats = monthly.load(tmp_path / "monthly" / "2026-01.json")
    assert stats["connectors"]["OCI:101"] == old_inventory
    assert stats["providers"]["OCI"]["failed_checks"] == 1
    sender.assert_not_called()
    client.close()


@responses.activate
def test_oci_alert_monthly_report_and_inventory_carry_forward(tmp_path):
    config = replace(CONFIG, providers=("OCI",))
    path = tmp_path / "state.json"
    sender = Mock()
    responses.post(OCI_URL, json=fixture())
    client = QualysClient(config)
    assert process(config, client, path, now=NOW, sender=sender) == 0
    sender.assert_not_called()
    assert process(config, client, path, now=NOW + timedelta(hours=23), sender=sender) == 0
    assert sender.call_count == 1
    assert "[OCI] 1 connector(s)" in sender.call_args.args[0]["Subject"]
    assert load(path)["episodes"]["OCI:101"]["notified_at"] is not None
    stats = monthly.load(tmp_path / "monthly" / "2026-01.json")
    entry = stats["connectors"]["OCI:101"]
    assert entry["in_start"] and entry["added_at"] is None
    assert "synthetic-oci-private-key-secret" not in path.read_text()
    assert "synthetic-oci-private-key-secret" not in json.dumps(stats)
    message = build_monthly_email(stats, config)
    text = message.get_body(preferencelist=("plain",)).get_content()
    assert "OCI | 1 | 0 | 0 | 1 | 1" in text
    assert "Total | 1 | 0 | 0 | 1 | 1" in text
    assert "OCI | 0 | 1 | 0 | 0 | 1 | 1 | 1 | 2 ok / 0 failed" in text
    rows = list(csv.DictReader(io.StringIO(next(message.iter_attachments()).get_content())))
    assert len(rows) == 1 and rows[0]["provider"] == "OCI"
    assert rows[0]["cloud_account"] == "ocid1.tenancy.oc1..example"
    february = monthly.new_month("2026-02", stats)
    assert february["connectors"]["OCI:101"]["error_since"] == NOW.isoformat()
    assert february["providers"]["OCI"]["baselined"]
    client.close()
