import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
import responses

from connector_health.config import Config
from connector_health.qualys_client import APIError, QualysClient, parse_connector

CONFIG = Config("https://qualys.example", "user", "password")
URL = CONFIG.base_url + "/qps/rest/3.0/search/am/awsassetdataconnector"


def page(ids, **fields):
    return {
        "ServiceResponse": {
            "responseCode": "SUCCESS",
            "count": len(ids),
            "hasMoreRecords": "false",
            "data": [
                {"AwsAssetDataConnector": {"id": i, "connectorState": "SUCCESS"}} for i in ids
            ],
            **fields,
        }
    }


@responses.activate
def test_offset_pagination_and_headers():
    responses.post(URL, json=page([1, 2], hasMoreRecords="true"))
    responses.post(URL, json=page([3]))
    result = QualysClient(CONFIG).fetch("AWS")
    assert [c.id for c in result] == ["1", "2", "3"]
    bodies = [json.loads(call.request.body) for call in responses.calls]
    assert [b["ServiceRequest"]["preferences"]["startFromOffset"] for b in bodies] == [1, 3]
    assert responses.calls[0].request.headers["X-Requested-With"] == "python-requests"
    assert responses.calls[0].request.headers["Accept"] == "application/json"
    assert responses.calls[0].request.headers["Authorization"].startswith("Basic ")


@responses.activate
def test_last_id_pagination():
    responses.post(URL, json=page([100], hasMoreRecords=True, lastId=100))
    responses.post(URL, json=page([200]))
    QualysClient(CONFIG).fetch("AWS")
    body = json.loads(responses.calls[1].request.body)["ServiceRequest"]
    assert body["filters"]["Criteria"] == [{"field": "id", "operator": "GREATER", "value": "100"}]
    assert body["preferences"]["startFromOffset"] == 1


@responses.activate
@pytest.mark.parametrize(
    "payload",
    [
        {"ServiceResponse": {"responseCode": "FAILURE"}},
        {"ServiceResponse": {"responseCode": "SUCCESS", "count": 1}},
        {"ServiceResponse": {"responseCode": "SUCCESS", "count": 1, "data": [{"Wrong": {}}]}},
    ],
)
def test_invalid_response(payload):
    responses.post(URL, json=payload)
    with pytest.raises(APIError):
        QualysClient(CONFIG).fetch("AWS")


@responses.activate
@pytest.mark.parametrize("failure", [429, 500, requests.Timeout()])
def test_retry(failure):
    if isinstance(failure, int):
        responses.post(
            URL, status=failure, headers={"Retry-After": "3", "X-RateLimit-ToWait-Sec": "5"}
        )
    else:
        responses.post(URL, body=failure)
    responses.post(URL, json=page([1]))
    sleep = Mock()
    assert QualysClient(CONFIG, sleep=sleep).fetch("AWS")
    sleep.assert_called_once_with(5 if isinstance(failure, int) else 1)


@responses.activate
def test_exhausted_retries():
    responses.post(URL, status=503)
    sleep = Mock()
    with pytest.raises(APIError, match="retries_exhausted"):
        QualysClient(CONFIG, sleep=sleep).fetch("AWS")
    assert len(responses.calls) == 5
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4, 8]


@responses.activate
def test_repeated_page_fails():
    responses.post(URL, json=page([1], hasMoreRecords="true"))
    with pytest.raises(APIError, match="repeated_connector_page"):
        QualysClient(CONFIG).fetch("AWS")


@responses.activate
def test_detail_fallback():
    payload = page([1])
    payload["ServiceResponse"]["data"][0]["AwsAssetDataConnector"]["connectorState"] = "ERROR"
    responses.post(URL, json=payload)
    detail = page([1])
    detail["ServiceResponse"]["data"][0]["AwsAssetDataConnector"]["lastError"] = "Denied"
    responses.get(CONFIG.base_url + "/qps/rest/3.0/get/am/awsassetdataconnector/1", json=detail)
    assert QualysClient(CONFIG).fetch("AWS")[0].error == "lastError: Denied"


def test_missing_fields():
    c = parse_connector("AWS", {"id": 1, "unexpected": "ignored"})
    assert c.state == "UNKNOWN" and c.name == "(unnamed)" and not c.disabled
    with pytest.raises(APIError):
        parse_connector("AWS", {})
    assert parse_connector("AWS", {"id": 1, "disabled": "false"}).disabled is False


@responses.activate
@pytest.mark.parametrize("provider", ["AWS", "AZURE", "GCP"])
def test_fixtures(provider):
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / f"{provider.lower()}.json").read_text()
    )
    url = CONFIG.base_url + f"/qps/rest/3.0/search/am/{provider.lower()}assetdataconnector"
    responses.post(url, json=payload)
    connector = QualysClient(CONFIG).fetch(provider)[0]
    assert connector.account and connector.error and connector.last_sync
    assert "secret" not in repr(connector)


@responses.activate
def test_secret_redaction_in_error_text():
    payload = page([1])
    raw = payload["ServiceResponse"]["data"][0]["AwsAssetDataConnector"]
    raw.update(
        connectorState="ERROR",
        lastError="Denied password and cloud-secret-value",
        authRecord={"authenticationKey": "cloud-secret-value"},
    )
    responses.post(URL, json=payload)
    result = QualysClient(CONFIG).fetch("AWS")[0]
    assert "password" not in result.error
    assert "cloud-secret-value" not in result.error
    assert result.error.count("[REDACTED]") == 2


@responses.activate
def test_success_rate_limit_delays_next_request():
    responses.post(
        URL,
        json=page([1], hasMoreRecords="true"),
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-ToWait-Sec": "7"},
    )
    responses.post(URL, json=page([2]))
    sleep = Mock()
    QualysClient(CONFIG, sleep=sleep).fetch("AWS")
    sleep.assert_called_once_with(7)


@responses.activate
def test_redirect_rejected():
    responses.post(URL, status=302, headers={"Location": "https://other.example/"})
    with pytest.raises(APIError, match="http_302"):
        QualysClient(CONFIG).fetch("AWS")
    assert len(responses.calls) == 1


def test_retry_after_http_date():
    from datetime import UTC, datetime

    from connector_health.qualys_client import retry_delay

    assert (
        retry_delay(
            {"Retry-After": "Thu, 01 Jan 2026 00:01:00 GMT"}, datetime(2026, 1, 1, tzinfo=UTC)
        )
        == 60
    )
