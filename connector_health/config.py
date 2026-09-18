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
    smtp_user: str = ""
    smtp_password: str = ""
    recipients: tuple[str, ...] = ()
    providers: tuple[str, ...] = ("AWS", "AZURE", "GCP")
    grace_hours: float = 24
    tolerance_hours: float = 2
    remind_hours: float = 0
    include_resolved: bool = False
    run_url: str = ""
    run_id: str = "local"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587

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
        smtp_host = (os.environ.get("SMTP_HOST") or "smtp.gmail.com").strip()
        smtp_port = int(os.environ.get("SMTP_PORT") or "587")
        if not smtp_host or any(c.isspace() for c in smtp_host) or not 1 <= smtp_port <= 65535:
            raise ValueError("Invalid SMTP host or port")
        mail = {
            k: os.environ.get(k, "") if dry_run else required(k)
            for k in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")
        }
        recipients = tuple(x.strip() for x in mail["EMAIL_TO"].split(",") if x.strip())
        for address in (mail["SMTP_USER"], *recipients):
            if address and ("@" not in address or any(c in address for c in "\r\n")):
                raise ValueError("Invalid email address")
        if not dry_run and not recipients:
            raise ValueError("Missing EMAIL_TO")
        run_id = os.environ.get("GITHUB_RUN_ID", "local")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return cls(
            base,
            required("QUALYS_USERNAME"),
            required("QUALYS_PASSWORD"),
            mail["SMTP_USER"],
            mail["SMTP_PASSWORD"],
            recipients,
            providers_from(os.environ.get("PROVIDERS") or "AWS,AZURE,GCP"),
            *numbers,
            resolved == "true",
            f"{server}/{repo}/actions/runs/{run_id}" if repo else "",
            run_id,
            smtp_host,
            smtp_port,
        )
