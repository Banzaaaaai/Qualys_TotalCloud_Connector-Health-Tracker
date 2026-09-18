"""Configuration and editable health policy."""

import math
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

ERROR_STATES = frozenset({"ERROR", "FINISHED_ERRORS", "INCOMPLETE"})
NEUTRAL_STATES = frozenset({"QUEUED", "RUNNING", "PROCESSING", "PENDING"})
HEALTHY_STATES = frozenset({"SUCCESS", "FINISHED_SUCCESS"})


def providers_from(value: str) -> tuple[str, ...]:
    providers = tuple(dict.fromkeys(p.strip().upper() for p in value.split(",")))
    if not providers or any(p not in {"AWS", "AZURE", "GCP"} for p in providers):
        raise ValueError("PROVIDERS must contain AWS, AZURE or GCP")
    return providers


@dataclass(frozen=True)
class Config:
    base_url: str
    username: str
    password: str
    gmail_user: str = ""
    gmail_password: str = ""
    recipients: tuple[str, ...] = ()
    providers: tuple[str, ...] = ("AWS", "AZURE", "GCP")
    grace_hours: float = 24
    tolerance_hours: float = 2
    remind_hours: float = 0
    include_resolved: bool = False
    run_url: str = ""
    run_id: str = "local"

    @classmethod
    def from_env(cls, dry_run=False):
        def required(key):
            value = os.environ.get(key, "").strip()
            if not value:
                raise ValueError(f"Missing {key}")
            return value

        base = required("QUALYS_BASE_URL").rstrip("/")
        url = urlsplit(base)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path
        ):
            raise ValueError("QUALYS_BASE_URL must be an HTTPS origin")
        numbers = [
            float(os.environ.get(k) or d)
            for k, d in (
                ("GRACE_HOURS", "24"),
                ("GRACE_TOLERANCE_HOURS", "2"),
                ("REMIND_EVERY_HOURS", "0"),
            )
        ]
        if any(not math.isfinite(n) or n < 0 for n in numbers) or numbers[1] > numbers[0]:
            raise ValueError("Invalid grace, tolerance or reminder hours")
        resolved = (os.environ.get("INCLUDE_RESOLVED") or "false").lower()
        if resolved not in {"true", "false"}:
            raise ValueError("INCLUDE_RESOLVED must be true or false")
        mail = {
            k: os.environ.get(k, "") if dry_run else required(k)
            for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "ALERT_RECIPIENTS")
        }
        recipients = tuple(x.strip() for x in mail["ALERT_RECIPIENTS"].split(",") if x.strip())
        for address in (mail["GMAIL_USER"], *recipients):
            if address and ("@" not in address or any(c in address for c in "\r\n")):
                raise ValueError("Invalid email address")
        if not dry_run and not recipients:
            raise ValueError("Missing ALERT_RECIPIENTS")
        run_id = os.environ.get("GITHUB_RUN_ID", "local")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return cls(
            base,
            required("QUALYS_USERNAME"),
            required("QUALYS_PASSWORD"),
            mail["GMAIL_USER"],
            mail["GMAIL_APP_PASSWORD"],
            recipients,
            providers_from(os.environ.get("PROVIDERS") or "AWS,AZURE,GCP"),
            *numbers,
            resolved == "true",
            f"{server}/{repo}/actions/runs/{run_id}" if repo else "",
            run_id,
        )
