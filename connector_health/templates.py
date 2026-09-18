"""Multipart reports with complete plain text and escaped HTML."""

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
