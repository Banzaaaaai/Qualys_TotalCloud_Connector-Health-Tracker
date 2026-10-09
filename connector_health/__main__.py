"""CLI and provider transaction orchestration."""

import argparse
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from . import monthly
from .config import Config, providers_from
from .health import evaluate
from .notifier import MailError, send_email
from .qualys_client import APIError, QualysClient
from .state_store import load, save
from .templates import build_email, build_monthly_email

LOG = logging.getLogger("connector_health")


def event(name, **fields):
    LOG.info(json.dumps({"event": name, **fields}, sort_keys=True))


def monthly_dir(state_path):
    return Path(state_path).parent / "monthly"


def preview(message):
    print(f"Subject: {message['Subject']}\n")
    print(message.get_body(preferencelist=("plain",)).get_content())


def send_pending_reports(config, directory, current_month, dry_run, now, sender):
    """Email each completed month once; a failed send stays pending for the next run."""
    sent, failed = [], []
    for path, stats in monthly.pending(directory, current_month):
        message = build_monthly_email(stats, config)
        if dry_run:
            preview(message)
            event("monthly_report_preview", month=stats["month"])
            continue
        try:
            sender(message, config)
        except MailError:
            event("monthly_report_smtp_failed", month=stats["month"])
            failed.append(stats["month"])
        else:
            stats["report_sent_at"] = now.isoformat()
            save(path, stats)
            event("monthly_report_sent", month=stats["month"])
            sent.append(stats["month"])
    return sent, failed


def process(config, client, state_path, dry_run=False, force=False, now=None, sender=send_email):
    now = now or datetime.now(UTC)
    state = load(state_path)
    directory = monthly_dir(state_path)
    stats = monthly.load_month(directory, now)
    stats_path = monthly.path_for(directory, stats["month"])
    reports_sent, reports_failed = send_pending_reports(
        config, directory, stats["month"], dry_run, now, sender
    )
    monthly.start_run(stats, now)
    failures, summaries = [], []
    for provider in config.providers:
        try:
            connectors = client.fetch(provider)
        except APIError as exc:
            event("provider_api_failed", provider=provider, reason=str(exc))
            failures.append(provider)
            summaries.append(f"| {provider} | API failed; state preserved | - | - | - | - |")
            monthly.record_failure(stats, provider)
            if not dry_run:
                save(stats_path, stats)
            continue
        prefix = provider + ":"
        episodes = {k: v for k, v in state["episodes"].items() if k.startswith(prefix)}
        resolved = {k: v for k, v in state["resolved"].items() if k.startswith(prefix)}
        result = evaluate(connectors, episodes, resolved, now, config, force)
        for connector in connectors:
            if connector.disabled:
                event("connector_disabled", provider=provider, connector_id=connector.id)
        if result.eligible:
            message = build_email(provider, result, now, config, force)
            if dry_run:
                print(message.as_string())
                event("email_preview", provider=provider, eligible=len(result.eligible))
            else:
                try:
                    sender(message, config)
                except MailError:
                    event("provider_smtp_failed", provider=provider)
                    failures.append(provider)
                else:
                    for connector in result.eligible:
                        result.episodes[connector.key]["notified_at"] = now.isoformat()
                    result.counts["alerted"] = len(result.eligible)
                    result.resolved = {}
        if not dry_run:
            for section, replacement in (
                ("episodes", result.episodes),
                ("resolved", result.resolved),
            ):
                state[section] = {
                    k: v for k, v in state[section].items() if not k.startswith(prefix)
                }
                state[section].update(replacement)
            save(state_path, state)
            monthly.record(
                stats, provider, connectors, result.episodes, now, result.counts["alerted"]
            )
            save(stats_path, stats)
        counts = result.counts
        event("provider_complete", provider=provider, dry_run=dry_run, **counts)
        summaries.append(
            f"| {provider} | "
            + " | ".join(
                str(counts[k]) for k in ("total", "healthy", "in_grace", "alerted", "disabled")
            )
            + " |"
        )
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(
                "\nQualys connector health" + (" (dry run)" if dry_run else "") + "\n\n"
                "| Provider | Total | Healthy | In grace | Alerted | Disabled |\n"
                "| --- | --- | --- | --- | --- | --- |\n" + "\n".join(summaries) + "\n"
            )
            if failures:
                stream.write("\nFailed providers: " + ", ".join(failures) + "\n")
            if reports_sent:
                stream.write("\nMonthly report sent: " + ", ".join(reports_sent) + "\n")
            if reports_failed:
                stream.write("\nMonthly report not sent: " + ", ".join(reports_failed) + "\n")
    return 1 if failures or reports_failed else 0


def report(config, state_path, month=None, send=False, now=None, sender=send_email):
    """Print or email one month's report on demand without changing saved statistics."""
    now = now or datetime.now(UTC)
    month = month or monthly.month_of(now)
    path = monthly.path_for(monthly_dir(state_path), month)
    if not path.exists():
        event("monthly_report_missing", month=month)
        return 1
    message = build_monthly_email(monthly.load(path), config, month < monthly.month_of(now))
    if not send:
        preview(message)
        return 0
    try:
        sender(message, config)
    except MailError:
        event("monthly_report_smtp_failed", month=month)
        return 1
    event("monthly_report_sent", month=month, on_demand=True)
    return 0


def month_from(value):
    if not monthly.MONTH.fullmatch(value):
        raise argparse.ArgumentTypeError("month must be YYYY-MM")
    return value


def main():
    parser = argparse.ArgumentParser(description="Monitor Qualys cloud connector health")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--force-notify", action="store_true")
    run.add_argument("--state-file", default="state.json")
    run.add_argument("--providers", type=providers_from)
    on_demand = sub.add_parser("report", help="Print or email a monthly report")
    on_demand.add_argument("--state-file", default="state.json")
    on_demand.add_argument("--month", type=month_from, help="YYYY-MM; default current UTC month")
    on_demand.add_argument("--send", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='{"level":"%(levelname)s","data":%(message)s}')
    client = None
    try:
        if args.command == "report":
            config = Config.from_env(not args.send, qualys=False)
            return report(config, args.state_file, args.month, args.send)
        config = Config.from_env(args.dry_run)
        if args.providers:
            from dataclasses import replace

            config = replace(config, providers=args.providers)
        client = QualysClient(config)
        return process(config, client, args.state_file, args.dry_run, args.force_notify)
    except (ValueError, OSError, TypeError, KeyError):
        # Exception strings can contain JSON, local paths or SMTP/API secrets.
        event("run_failed", reason="configuration_or_state_error")
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
