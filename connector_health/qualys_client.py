"""Qualys v3 wire mapping. Only explicitly allowed fields leave this module."""

import time
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from .health import classification
from .models import Connector

TYPES = {
    "AWS": ("awsassetdataconnector", "AwsAssetDataConnector"),
    "AZURE": ("azureassetdataconnector", "AzureAssetDataConnector"),
    "GCP": ("gcpassetdataconnector", "GcpAssetDataConnector"),
}
ERROR_FIELDS = {"lastError", "error", "errorMessage", "message", "status", "state"}


class APIError(Exception):
    """Safe error category; never includes response bodies or credentials."""


def scalar(value):
    return (
        str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""
    )


def error_text(value):
    """Read error/status leaves without serializing arbitrary auth/config objects."""
    parts = []
    if isinstance(value, list):
        for item in value:
            parts.extend(error_text(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ERROR_FIELDS and scalar(item):
                parts.append(f"{key}: {scalar(item)}")
            elif key in {
                "list",
                "set",
                "ConnectorAppInfoQList",
                "ConnectorAppInfo",
                "error",
                "errors",
            }:
                parts.extend(error_text(item))
    return parts


def parse_connector(provider, raw):
    connector_id = scalar(raw.get("id"))
    if not connector_id:
        raise APIError("missing_connector_id")
    auth = raw.get("authRecord")
    auth = auth if isinstance(auth, dict) else {}
    accounts = {
        "AWS": scalar(raw.get("arn")) or scalar(raw.get("awsAccountId")),
        "AZURE": " / ".join(
            filter(
                None,
                (
                    scalar(auth.get("subscriptionId")) or scalar(raw.get("subscriptionId")),
                    scalar(auth.get("directoryId")) or scalar(raw.get("tenantId")),
                ),
            )
        ),
        "GCP": scalar(auth.get("projectId")) or scalar(raw.get("projectId")),
    }
    errors = error_text({k: raw[k] for k in ("lastError", "error", "errorMessage") if k in raw})
    errors += error_text(raw.get("connectorAppInfos"))
    return Connector(
        provider,
        connector_id,
        scalar(raw.get("name")) or "(unnamed)",
        scalar(raw.get("connectorState")) or "UNKNOWN",
        str(raw.get("disabled", "false")).lower() == "true",
        "\n".join(dict.fromkeys(errors)),
        accounts[provider],
        scalar(raw.get("lastSync")),
        scalar(raw.get("nextSync")),
        scalar(raw.get("cloudviewUuid")),
    )


def records(response, provider):
    data = response.get("data", [])
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise APIError("invalid_data")
    rows = []
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get(TYPES[provider][1]), dict):
            raise APIError("invalid_connector_wrapper")
        rows.append(entry[TYPES[provider][1]])
    try:
        count = int(response["count"])
    except (KeyError, ValueError, TypeError):
        raise APIError("invalid_count") from None
    if count != len(rows):
        raise APIError("count_mismatch")
    return rows


def retry_delay(headers, now):
    delays = [0.0]
    for name in ("Retry-After", "X-RateLimit-ToWait-Sec"):
        value = headers.get(name)
        if value:
            try:
                delays.append(float(value))
            except ValueError:
                if name == "Retry-After":
                    try:
                        delays.append((parsedate_to_datetime(value) - now).total_seconds())
                    except (ValueError, TypeError):
                        pass
    return max(delays)


SENSITIVE_FIELDS = {
    "password",
    "authenticationkey",
    "privatekey",
    "private_key",
    "secretaccesskey",
    "secret_access_key",
    "accesskey",
    "accesskeyid",
    "clientsecret",
    "client_secret",
    "token",
    "accesstoken",
    "refreshtoken",
    "authorization",
    "credentials",
    "serviceaccountkey",
    "keyfile",
    "jsonkey",
    "secret",
}


def redact_payload(payload, known_secrets):
    secrets = set(filter(None, known_secrets))

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in SENSITIVE_FIELDS and isinstance(item, str) and item:
                    secrets.add(item)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload)

    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k.lower() not in SENSITIVE_FIELDS}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, str):
            for secret in sorted(secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
        return value

    return clean(payload)


class QualysClient:
    def __init__(self, config, session=None, sleep=time.sleep):
        self.base = config.base_url
        self.secrets = (config.username, config.password, config.gmail_password)
        self.session = session or requests.Session()
        self.session.auth = (config.username, config.password)
        self.session.headers.update(
            {"X-Requested-With": "python-requests", "Accept": "application/json"}
        )
        self.sleep = sleep
        self.pending_wait = 0

    def close(self):
        self.session.close()

    def request(self, method, path, body=None):
        for attempt in range(5):
            if self.pending_wait:
                self.sleep(self.pending_wait)
                self.pending_wait = 0
            response = None
            try:
                response = self.session.request(
                    method, self.base + path, json=body, timeout=30, allow_redirects=False
                )
            except (requests.Timeout, requests.ConnectionError):
                pass
            except requests.RequestException:
                raise APIError("request_failed") from None
            if response is None or response.status_code == 429 or response.status_code >= 500:
                if attempt == 4:
                    raise APIError("retries_exhausted")
                delay = (
                    retry_delay(response.headers, datetime.now(UTC)) if response is not None else 0
                )
                self.sleep(max(2**attempt, delay))
                continue
            if not 200 <= response.status_code < 300:
                raise APIError(f"http_{response.status_code}")
            if response.headers.get("X-RateLimit-Remaining") == "0":
                self.pending_wait = retry_delay(response.headers, datetime.now(UTC))
            try:
                payload = response.json()
                service = payload["ServiceResponse"]
                if not isinstance(service, dict) or service.get("responseCode") != "SUCCESS":
                    raise APIError("non_success_response")
                return redact_payload(service, self.secrets)
            except (ValueError, KeyError, TypeError):
                raise APIError("invalid_response") from None
        raise APIError("retries_exhausted")

    def fetch(self, provider):
        resource = TYPES[provider][0]
        offset, cursor = 1, None
        seen, connectors = set(), []
        while True:
            preferences = {"limitResults": 100, "startFromOffset": offset}
            request = {"preferences": preferences}
            if cursor is not None:
                request["filters"] = {
                    "Criteria": [{"field": "id", "operator": "GREATER", "value": cursor}]
                }
            response = self.request(
                "POST", f"/qps/rest/3.0/search/am/{resource}", {"ServiceRequest": request}
            )
            rows = records(response, provider)
            for row in rows:
                connector = parse_connector(provider, row)
                if connector.id in seen:
                    raise APIError("repeated_connector_page")
                seen.add(connector.id)
                connectors.append(connector)
            more = str(response.get("hasMoreRecords", "missing")).lower()
            if more not in {"true", "false"}:
                raise APIError("invalid_pagination_flag")
            if more == "false":
                break
            if not rows:
                raise APIError("empty_continuation_page")
            last_id = scalar(response.get("lastId"))
            if last_id:
                if cursor == last_id:
                    raise APIError("repeated_cursor")
                cursor, offset = last_id, 1
            else:
                offset += len(rows)
        # Enrichment must succeed before any provider state can be changed.
        for index, connector in enumerate(connectors):
            if classification(connector) == "error" and not connector.error:
                response = self.request(
                    "GET", f"/qps/rest/3.0/get/am/{resource}/" + quote(connector.id, safe="")
                )
                rows = records(response, provider)
                if len(rows) != 1 or scalar(rows[0].get("id")) != connector.id:
                    raise APIError("detail_identity_mismatch")
                detail = parse_connector(provider, rows[0])
                connectors[index] = replace(
                    connector,
                    error=detail.error,
                    account=connector.account or detail.account,
                    last_sync=connector.last_sync or detail.last_sync,
                )
        return connectors
