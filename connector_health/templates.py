"""Multipart reports with complete plain text and escaped HTML."""

import csv
import io
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from html import escape


def build_email(provider, result, now, config, forced=False):
    message = EmailMessage()
    suffix = " (forced test)" if forced else ""
    message["Subject"] = (
        f"[Qualys Connector Health][{provider}] {len(result.eligible)} "
        f"connector(s) failing > {config.grace_hours:g}h{suffix}"
    )
    message["From"] = config.smtp_user or "dry-run@example.invalid"
    message["To"] = ", ".join(config.recipients) or "dry-run@example.invalid"
    message["Date"] = format_datetime(now)
    message["Message-ID"] = make_msgid()
    note = (
        f"Cloud provider: {provider}\nGrace: {config.grace_hours:g}h; "
        f"cron tolerance: {config.tolerance_hours:g}h. "
        "Hours below reflect elapsed time since first observed error."
    )
    plain, html = [note], [f"<p>{escape(note)}</p>"]
    for c in result.eligible:
        episode = result.episodes[c.key]
        hours = (
            now - datetime.fromisoformat(episode["first_error_seen_at"])
        ).total_seconds() / 3600
        rows = [
            ("Name", c.name),
            ("Connector id", c.id),
            ("Cloud account", c.account or "Unavailable"),
            ("State", c.state),
            ("Error details", episode["last_error"] or "No error text returned by Qualys"),
            ("First error seen (UTC)", episode["first_error_seen_at"]),
            ("Hours in error", f"{hours:.2f}"),
            ("Last sync", c.last_sync or "Unavailable"),
            ("Qualys UI", "Open Connectors and search by connector id; no verified deep link"),
        ]
        plain.append("\n".join(f"{label}: {value}" for label, value in rows))
        html.append(
            '<table border="1" cellpadding="6">'
            + "".join(
                f"<tr><th>{escape(label)}</th>"
                f'<td style="white-space:pre-wrap">{escape(value)}</td></tr>'
                for label, value in rows
            )
            + "</table><br>"
        )
    if config.include_resolved and result.resolved:
        text = "Resolved since last report:\n" + "\n".join(
            f"{entry['name']} ({key}) at {entry['resolved_at']}"
            for key, entry in sorted(result.resolved.items())
        )
        plain.append(text)
        html.append(f"<pre>{escape(text)}</pre>")
    footer = f"Run id: {config.run_id}\n{config.run_url}"
    plain.append(footer)
    html.append(f"<pre>{escape(footer)}</pre>")
    message.set_content("\n\n".join(plain))
    message.add_alternative(
        "<!doctype html><html><body>" + "".join(html) + "</body></html>", subtype="html"
    )
    return message


LABELS = {"healthy": "Healthy", "error": "Error", "neutral": "In progress", "disabled": "Disabled"}
PROVIDER_ORDER = ("AWS", "AZURE", "GCP")


def table(headers, rows):
    plain = "\n".join(" | ".join(map(str, row)) for row in (headers, *rows))
    html = (
        '<table border="1" cellpadding="4" style="border-collapse:collapse"><tr>'
        + "".join(f"<th>{escape(str(h))}</th>" for h in headers)
        + "</tr>"
        + "".join(
            "<tr>"
            + "".join(f'<td style="white-space:pre-wrap">{escape(str(v))}</td>' for v in row)
            + "</tr>"
            for row in rows
        )
        + "</table>"
    )
    return plain, html


def spreadsheet_safe(value):
    """Stop spreadsheet apps from evaluating connector names or errors as formulas."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def connectors_csv(stats):
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        (
            "provider",
            "connector_id",
            "name",
            "cloud_account",
            "status_at_start",
            "status_at_end",
            "last_state",
            "last_seen_at",
            "added_at",
            "removed_at",
            *(f"{kind}_checks" for kind in LABELS),
            "error_since",
            "last_error",
        )
    )
    for key, e in sorted(stats["connectors"].items()):
        provider, _, connector_id = key.partition(":")
        writer.writerow(
            spreadsheet_safe(v)
            for v in (
                provider,
                connector_id,
                e["name"],
                e["account"],
                LABELS[e["start_class"]],
                "Removed" if e["removed_at"] else LABELS[e["last_class"]],
                e["last_state"],
                e["last_seen_at"],
                e["added_at"] or "",
                e["removed_at"] or "",
                *(e["counts"][kind] for kind in LABELS),
                e["error_since"] or "",
                e["last_error"],
            )
        )
    return stream.getvalue()


def build_monthly_email(stats, config, complete=True):
    """Monthly inventory and status statistics with a CSV of every connector."""
    month = datetime.strptime(stats["month"], "%Y-%m").strftime("%B %Y")
    end = datetime.fromisoformat(stats["last_observed_at"])
    entries = sorted(stats["connectors"].items())
    providers = [p for p in PROVIDER_ORDER if p in stats["providers"]]

    def split(key):
        provider, _, connector_id = key.partition(":")
        return provider, connector_id

    def status(e):
        return "Removed" if e["removed_at"] else LABELS[e["last_class"]]

    def open_issue(e):
        return e["removed_at"] is None and e["error_since"] is not None

    message = EmailMessage()
    suffix = "" if complete else " (month to date)"
    message["Subject"] = f"[Qualys Connector Health] Monthly report {month}{suffix}"
    message["From"] = config.smtp_user or "dry-run@example.invalid"
    message["To"] = ", ".join(config.recipients) or "dry-run@example.invalid"
    message["Date"] = format_datetime(end)
    message["Message-ID"] = make_msgid()

    notes = [
        f"Reporting period (UTC): {stats['first_observed_at']} to {stats['last_observed_at']}; "
        f"{stats['runs']} monitoring run(s).",
        "Start of month carries over the previous month's inventory. Status columns show the "
        "last status reported in the month. Open issues are connectors whose error episode "
        "was still active at the last check.",
    ]
    for provider in providers:
        info = stats["providers"][provider]
        if info["baseline_at"]:
            notes.append(
                f"{provider}: inventory baseline taken at first check {info['baseline_at']}; "
                "connectors present then are counted in Start of month, not Added."
            )
        if info["failed_checks"]:
            notes.append(
                f"{provider}: {info['failed_checks']} check(s) failed with an API error and "
                "are not reflected in the statistics."
            )
    sections = [("Notes", "\n".join(notes), "<p>" + "<br>".join(map(escape, notes)) + "</p>")]

    inventory_rows, status_rows = [], []
    for provider in [*providers, "Total"]:
        rows = [e for k, e in entries if provider == "Total" or split(k)[0] == provider]
        info = stats["providers"].get(provider) or {
            k: sum(stats["providers"][p][k] for p in providers)
            for k in ("checks", "failed_checks", "alerts_sent")
        }
        inventory_rows.append(
            (
                provider,
                sum(e["in_start"] for e in rows),
                sum(e["added_at"] is not None for e in rows),
                sum(e["removed_at"] is not None for e in rows),
                sum(e["removed_at"] is None for e in rows),
                sum(any(e["counts"].values()) for e in rows),
            )
        )
        status_rows.append(
            (
                provider,
                *(sum(status(e) == label for e in rows) for label in LABELS.values()),
                sum(e["counts"]["error"] > 0 for e in rows),
                sum(open_issue(e) for e in rows),
                info["alerts_sent"],
                f"{info['checks']} ok / {info['failed_checks']} failed",
            )
        )
    sections.append(
        (
            "Monitored connectors",
            *table(
                ("Provider", "Start of month", "Added", "Removed", "End of month", "Observed"),
                inventory_rows,
            ),
        )
    )
    sections.append(
        (
            "Reported status at month end",
            *table(
                (
                    "Provider",
                    *LABELS.values(),
                    "Had errors this month",
                    "Open issues",
                    "Alerts sent",
                    "Daily checks",
                ),
                status_rows,
            ),
        )
    )

    def listing(title, headers, rows):
        if rows:
            sections.append((f"{title} ({len(rows)})", *table(headers, rows)))
        else:
            sections.append((title, "None", "<p>None</p>"))

    failing = []
    for key, e in entries:
        if open_issue(e):
            days = (end - datetime.fromisoformat(e["error_since"])).total_seconds() / 86400
            error = e["last_error"] or "No error text returned by Qualys"
            failing.append(
                (
                    *split(key),
                    e["name"],
                    e["account"] or "Unavailable",
                    e["last_state"],
                    e["error_since"],
                    f"{days:.1f}",
                    error if len(error) <= 500 else error[:500] + "... (full text in CSV)",
                )
            )
    listing(
        "Open issues at month end",
        (
            "Provider",
            "Connector id",
            "Name",
            "Cloud account",
            "State",
            "In error since (UTC)",
            "Days",
            "Last error",
        ),
        failing,
    )
    listing(
        "Reported errors this month, no longer failing",
        ("Provider", "Connector id", "Name", "Error checks", "Status at month end"),
        [
            (*split(key), e["name"], e["counts"]["error"], status(e))
            for key, e in entries
            if e["counts"]["error"] and not open_issue(e)
        ],
    )
    listing(
        "Added connectors",
        ("Provider", "Connector id", "Name", "Cloud account", "Added (UTC)", "Status at month end"),
        [
            (*split(key), e["name"], e["account"] or "Unavailable", e["added_at"], status(e))
            for key, e in entries
            if e["added_at"]
        ],
    )
    listing(
        "Removed connectors",
        ("Provider", "Connector id", "Name", "Cloud account", "Missing since (UTC)"),
        [
            (*split(key), e["name"], e["account"] or "Unavailable", e["removed_at"])
            for key, e in entries
            if e["removed_at"]
        ],
    )
    filename = f"qualys-connectors-{stats['month']}.csv"
    footer = f"Per-connector details: {filename}\nRun id: {config.run_id}\n{config.run_url}"
    plain = [f"{title}\n{text}" for title, text, _ in sections] + [footer.strip()]
    html = [f"<h3>{escape(title)}</h3>{body}" for title, _, body in sections]
    html.append(f"<pre>{escape(footer.strip())}</pre>")
    message.set_content("\n\n".join(plain))
    message.add_alternative(
        "<!doctype html><html><body>" + "".join(html) + "</body></html>", subtype="html"
    )
    message.add_attachment(
        connectors_csv(stats).encode("utf-8"), maintype="text", subtype="csv", filename=filename
    )
    return message
