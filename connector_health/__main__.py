"""CLI and provider transaction orchestration."""

import argparse
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from .config import Config, providers_from
from .health import evaluate
from .notifier import MailError, send_email
from .qualys_client import APIError, QualysClient
from .state_store import load, save
from .templates import build_email

LOG = logging.getLogger("connector_health")


def event(name, **fields):
    LOG.info(json.dumps({"event": name, **fields}, sort_keys=True))


def process(config, client, state_path, dry_run=False, force=False, now=None, sender=send_email):
    now = now or datetime.now(UTC)
    state = load(state_path)
    failures, summaries = [], []
    for provider in config.providers:
        try:
            connectors = client.fetch(provider)
        except APIError as exc:
            event("provider_api_failed", provider=provider, reason=str(exc))
            failures.append(provider)
            summaries.append(f"| {provider} | API failed; state preserved | - | - | - | - |")
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
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description="Monitor Qualys cloud connector health")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--force-notify", action="store_true")
    run.add_argument("--state-file", default="state.json")
    run.add_argument("--providers", type=providers_from)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='{"level":"%(levelname)s","data":%(message)s}')
    client = None
    try:
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
