# Qualys Cloud Connector Health Tracker

A Python 3.12 GitHub Actions job that observes AWS, Azure and GCP connectors daily and sends one Gmail message per provider with eligible failures. State lives on the private repository's orphan `health-state` branch. No external database, state cache, or artifacts are used.

## Setup

1. Keep this repository private and place the workflows on its default branch. Enable GitHub Actions and allow the workflow's `contents: write` permission. Repository rules must permit the Actions bot to create and update `health-state`.
2. Create a dedicated Qualys API user. Request connector read access with scope covering **all** monitored connectors; the code only performs searches and detail reads. **Permission caveat:** the official v3 detail documentation specifies **Managers with full scope**, not a documented read-only role. Ask your Qualys administrator/support whether a restricted role is supported on your subscription; do not assume it is. Incomplete scope looks like deleted connectors to this tool.
3. Enable Google 2-Step Verification and create a dedicated [Google App Password](https://support.google.com/accounts/answer/185833). Use the app password, not your ordinary Gmail password. Organization policies or Advanced Protection may prevent app passwords.
4. Add these repository **Actions secrets** under Settings → Secrets and variables → Actions:

| Secret | Value |
| --- | --- |
| `QUALYS_USERNAME` | Dedicated API username |
| `QUALYS_PASSWORD` | API password |
| `SMTP_HOST` | `smtp.gmail.com` (default) |
| `SMTP_PORT` | `587` (default, STARTTLS) |
| `SMTP_USER` | Full sending Gmail address |
| `SMTP_PASSWORD` | Google App Password |
| `EMAIL_TO` | Comma-separated email addresses |

Use the same five email secret names and values as your release tracker. For Gmail, `SMTP_PASSWORD` is the Google App Password. Set these secrets in this repository too; repository secrets are not automatically shared. If you configured the original names, migrate `GMAIL_USER` to `SMTP_USER`, `GMAIL_APP_PASSWORD` to `SMTP_PASSWORD`, and `ALERT_RECIPIENTS` to `EMAIL_TO`. Port 465 implicit SSL has been replaced by STARTTLS on port 587.

5. Add repository **Actions variables**:

| Variable | Default / value |
| --- | --- |
| `QUALYS_BASE_URL` | Required: `https://qualysapi.qg2.apps.qualys.eu` |
| `PROVIDERS` | `AWS,AZURE,GCP` (subset supported) |
| `GRACE_HOURS` | `24` |
| `GRACE_TOLERANCE_HOURS` | `2` |
| `REMIND_EVERY_HOURS` | `0` (no reminders) |
| `INCLUDE_RESOLVED` | `false`; set `true` to queue recoveries for the next failure email |

6. Open Actions → Connector health → Run workflow. First select `dry_run=true` and `force_notify=true` to preview first-observed failures immediately. Check the logs and summary, then run with both inputs false to begin tracking. A live forced run sends real mail and marks the episode notified. Force bypasses grace only; it does not bypass deduplication. Dry runs do not require Gmail secrets.
7. The first non-dry run with a successful provider automatically initializes and pushes `health-state` as an orphan branch containing only `state.json`. No manual branch setup is necessary. If every provider fails, no branch is created. Later runs check out this branch into `health-state/`; a missing state file on an existing branch fails rather than silently losing history.

The schedule is **06:00 UTC daily**. Change `on.schedule.cron` in `.github/workflows/connector-health.yml` to adjust it; cron cannot use a repository variable. Enable GitHub Actions failure notifications for your account. API, SMTP, configuration, corrupt state, and state push failures fail the job.

## Episode semantics

State keys are `provider:connector_id`. Each active episode contains `first_error_seen_at`, `last_seen_at`, `last_state`, `last_error`, `notified_at`, and `name`. Times are UTC. Reports add current account and sync information from the API without persisting full API responses.

- First observed error starts the clock; no normal notification is sent.
- Eligibility is elapsed hours **>= GRACE_HOURS - GRACE_TOLERANCE_HOURS**. Defaults therefore allow a notification at **22 hours**, including a second daily run at 23h50m. Set tolerance to `0` for a strict minimum of 24 hours. The subject retains the requested nominal `> 24h` wording; the body states the tolerance and actual elapsed hours.
- `ERROR`, `FINISHED_ERRORS`, `INCOMPLETE`, any state containing `ERROR`, and unknown/missing states are errors. Unknown raw states appear in reports. Edit policy sets in `config.py` to customize classifications.
- `QUEUED`, `RUNNING`, `PROCESSING`, and `PENDING` preserve existing episodes and clocks, but never trigger an email themselves. Healthy states `SUCCESS` and `FINISHED_SUCCESS` close the episode. A later failure starts a new clock.
- Disabled connectors are logged by id and removed from active tracking; re-enabling starts a new episode. Connectors absent from a **fully successful** provider fetch are removed. Providers omitted from `PROVIDERS` are left untouched.
- Send once per episode, unless reminders are enabled; reminders require both an eligible error and elapsed reminder interval since the last successful send. Changed error text refreshes the report without restarting the clock.
- Recoveries can be queued separately under `resolved` until the next email for that provider. There are no recovery-only emails. Re-failure removes a pending recovery for that connector.
- A provider API failure, including a failed detail lookup or malformed/incomplete pagination, leaves that provider's state unchanged. Other providers continue. SMTP failure preserves new observations but does not mark alerts notified. Successful sends are saved per provider and the workflow persists state even when another provider fails.

This is observation-based monitoring: it cannot prove uninterrupted failure between daily observations. SMTP delivery and Git commits cannot be one atomic transaction. If a runner stops or a push fails after mail acceptance, the next run can resend; partial SMTP recipient acceptance can also result in duplicates for recipients who already received it. State push failures must be investigated. Do not manually run concurrent live writers; GitHub runs are serialized with concurrency group `connector-health`.

## API contract and field mapping

The mapping is centralized in `connector_health/qualys_client.py`. Documentation checked on 2026-09-18:

| Provider | Search (POST) and detail path (GET in this client) | Official docs |
| --- | --- | --- |
| AWS | `/qps/rest/3.0/search/am/awsassetdataconnector`; `/qps/rest/3.0/get/am/awsassetdataconnector/<id>` | [Search](https://docs.qualys.com/en/conn/api/aws_3/search_aws_connector_3.0.htm), [Details](https://docs.qualys.com/en/conn/api/aws_3/get_aws_connector_info_3.0.htm) |
| Azure | `/qps/rest/3.0/search/am/azureassetdataconnector`; `/qps/rest/3.0/get/am/azureassetdataconnector/<id>` | [Search](https://docs.qualys.com/en/conn/api/azure_3/search_azure_connector_3.0.htm), [Details](https://docs.qualys.com/en/conn/api/azure_3/get_azure_connector_info_3.0.htm) |
| GCP | `/qps/rest/3.0/search/am/gcpassetdataconnector`; `/qps/rest/3.0/get/am/gcpassetdataconnector/<id>` | [Search](https://docs.qualys.com/en/conn/api/gcp_3/search_gcp_connector_3.0.htm), [Details](https://docs.qualys.com/en/conn/api/gcp_3/get_gcp_connector_info_3.0.htm) |

The documented `ServiceResponse` envelope uses `responseCode`, `count`, `hasMoreRecords`, and `data` entries wrapped in `AwsAssetDataConnector`, `AzureAssetDataConnector`, or `GcpAssetDataConnector`. Only `SUCCESS` is accepted. Identity is required to track safely; a missing id fails the provider instead of inventing an id. Other connector fields are optional.

| Qualys field | Report/use | Verification |
| --- | --- | --- |
| `id`, `name` | Connector id, name | v3 samples |
| `connectorState`, `disabled` | State, skip disabled | v3 samples; booleans and string booleans accepted |
| `lastSync`, `nextSync` | Last sync; next sync parsed for future use | v3 samples |
| `arn`, fallback `awsAccountId` | AWS cloud account | AWS samples |
| `authRecord.subscriptionId`, `authRecord.directoryId` | Azure subscription / tenant | Azure samples |
| `authRecord.projectId` | GCP project | GCP samples |
| `cloudviewUuid` | Parsed identifier; no enrichment requests | v3 samples |
| `lastError` | Error details | Search docs describe this as an error **date**; older API examples also show text. Preserved literally, not interpreted as a guaranteed message. |
| `error`, `errorMessage`; nested error/status leaves in `connectorAppInfos` | Additional error details | Defensive extensions; these error/status fields are **not guaranteed by v3 samples**. Documented app-info wrappers are traversed, not dumped. |
| Top-level subscription/tenant/project fallbacks | Cloud account when nested field absent | Defensive compatibility aliases, not claimed as verified v3 fields |

When an error connector has no error/status text in the search result, a detail GET is attempted. If both responses omit it, reports say no error text was returned. Full error text is retained except secret redaction.

**Documentation inconsistencies:** AWS's search JSON example is malformed, Azure's detail example invokes search, and GCP's detail example uses POST. Endpoint paths and fields above are confirmed, but Azure/GCP detail **GET behavior still requires a tenant smoke test**. This implementation uses the requested GET contract and fails the provider safely if rejected; it does not automatically try alternate methods. There is no confirmed UI deep-link format, so the email instructs you to search Connectors by id. Optional legacy CloudView enrichment is not implemented/enabled; no unverified endpoint is called.

Search bodies use `ServiceRequest.preferences` with `limitResults=100`. [Qualys QPS pagination](https://docs.qualys.com/en/was/api/get_started/making_api_calls.htm) defines `startFromOffset` as a **1-based** index and documents `id GREATER lastId`. The client starts at 1, uses that id filter and offset 1 when a lastId cursor exists, otherwise advances the current offset by the number of returned rows. It rejects duplicate ids, repeated cursors, empty continuation pages, and count mismatches. This generic QPS pagination contract supplements the connector docs, which only describe page size.

Basic auth and `X-Requested-With: python-requests` / `Accept: application/json` are sent over HTTPS; redirects are rejected. Requests time out after 30 seconds and get at most **five total attempts** on 429, 5xx, timeout, or connection errors, with exponential waits of 1/2/4/8 seconds or a longer server delay. `Retry-After` accepts seconds or HTTP dates. [Qualys rate headers](https://docs.qualys.com/en/am/api/get_started/tracking_api_usage.htm) supply `X-RateLimit-ToWait-Sec`; an exhausted `X-RateLimit-Remaining` also delays the next successful-response follow-up. The workflow's 60-minute limit bounds prolonged waits.

## Security and operations

Only selected fields leave the API client. Auth records are never serialized into state or logs; credential fields are discarded, and configured secrets plus credential values identified in API responses are redacted from strings. Ordinary INFO logs contain JSON event records, counts, and connector ids, never request bodies or auth headers. Dry-run email previews intentionally expose connector names/account metadata/error text in private Actions logs. Qualys must not include unrelated secrets in free-form error text; arbitrary unknown secrets cannot be identified reliably.

Email follows [qualys-release-tracker](https://github.com/Banzaaaaai/qualys-release-tracker): configurable `SMTP_HOST`/`SMTP_PORT` (defaults `smtp.gmail.com:587`), certificate-verified STARTTLS before authentication and stdlib `smtplib`/`email`. HTML escapes every API-provided string. Plain text includes untruncated error details for Jira. Only providers with eligible failures get mail. Secrets are scoped to the monitor step; action versions are pinned to full commit SHAs. The pip cache contains dependencies only, never monitoring state.

The state branch retains historical names and error messages in Git history. Restrict repository access and follow your organization's retention policy. If state becomes corrupt, restore a known-good `state.json` from branch history before rerunning; do not delete it casually because that restarts grace and deduplication.

## CLI and development

Production execution is via GitHub Actions. For local development, inject environment variables through your shell or secret manager; no `.env` loader or credential file is used.

```text
python -m connector_health run [--dry-run] [--force-notify] [--state-file PATH] [--providers AWS,GCP]
```

```sh
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest -q
```

CI runs these checks on Python 3.12 for pull requests and code pushes. Tests use synthetic, sanitized fixtures shaped like the documented envelopes and injected UTC clocks (no real network or email). They cover grace/tolerance boundaries, recovery/flapping, transient states, dedup/reminders, grouping/escaping, retries/pagination, API isolation, SMTP failure, dry-run immutability, and state validation.
