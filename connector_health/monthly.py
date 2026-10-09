"""Per-month connector inventory and status statistics.

One JSON file per UTC month lives in monthly/ next to state.json. A new month starts from the
previous month's surviving inventory, so additions and removals are counted against what was
known when the month began. A provider's first ever successful check is a baseline, not additions.
"""

import json
import re
from pathlib import Path

from .health import classification

CLASSES = ("healthy", "error", "neutral", "disabled")
MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def month_of(now):
    return now.strftime("%Y-%m")


def path_for(directory, month):
    return Path(directory) / f"{month}.json"


def new_provider(baselined=False):
    return {
        "baselined": baselined,
        "baseline_at": None,
        "checks": 0,
        "failed_checks": 0,
        "alerts_sent": 0,
    }


def new_month(month, previous=None):
    stats = {
        "version": 1,
        "month": month,
        "first_observed_at": None,
        "last_observed_at": None,
        "report_sent_at": None,
        "runs": 0,
        "providers": {},
        "connectors": {},
    }
    if previous:
        for provider, info in previous["providers"].items():
            stats["providers"][provider] = new_provider(info["baselined"])
        for key, entry in previous["connectors"].items():
            if entry["removed_at"] is None:
                stats["connectors"][key] = {
                    **entry,
                    "start_class": entry["last_class"],
                    "in_start": True,
                    "added_at": None,
                    "counts": dict.fromkeys(CLASSES, 0),
                }
    return stats


def load(path):
    path = Path(path)
    stats = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(stats, dict)
        or stats.get("version") != 1
        or stats.get("month") != path.stem
        or not isinstance(stats.get("providers"), dict)
        or not isinstance(stats.get("connectors"), dict)
    ):
        raise ValueError("Invalid monthly statistics")
    for entry in stats["connectors"].values():
        if not isinstance(entry, dict) or set(entry.get("counts", {})) != set(CLASSES):
            raise ValueError("Invalid monthly connector entry")
    return stats


def load_month(directory, now):
    """Current month's statistics, started from the latest earlier month when absent."""
    month = month_of(now)
    path = path_for(directory, month)
    if path.exists():
        return load(path)
    earlier = [p for p in months(directory) if p.stem < month]
    return new_month(month, load(earlier[-1]) if earlier else None)


def months(directory):
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.json") if MONTH.fullmatch(p.stem))


def pending(directory, current_month):
    """Completed months whose report has not been sent yet, oldest first."""
    for path in months(directory):
        if path.stem < current_month:
            stats = load(path)
            if stats["report_sent_at"] is None:
                yield path, stats


def start_run(stats, now):
    stamp = now.isoformat()
    stats["runs"] += 1
    stats["first_observed_at"] = stats["first_observed_at"] or stamp
    stats["last_observed_at"] = stamp


def record_failure(stats, provider):
    stats["providers"].setdefault(provider, new_provider())["failed_checks"] += 1


def record(stats, provider, connectors, episodes, now, alerted=0):
    """Apply one fully successful provider fetch; absent connectors count as removed."""
    stamp = now.isoformat()
    info = stats["providers"].setdefault(provider, new_provider())
    baseline = not info["baselined"]
    seen = set()
    for c in connectors:
        kind = classification(c)
        seen.add(c.key)
        entry = stats["connectors"].get(c.key)
        if entry is None or entry["removed_at"]:
            entry = entry or {
                "start_class": kind,
                "in_start": baseline,
                "added_at": None,
                "counts": dict.fromkeys(CLASSES, 0),
            }
            if not baseline:
                entry["added_at"] = stamp
            entry["removed_at"] = None
            stats["connectors"][c.key] = entry
        episode = episodes.get(c.key) or {}
        entry.update(
            name=c.name,
            account=c.account,
            last_state=c.state,
            last_class=kind,
            last_seen_at=stamp,
            error_since=episode.get("first_error_seen_at"),
            last_error=episode.get("last_error", ""),
        )
        entry["counts"][kind] += 1
    prefix = provider + ":"
    for key, entry in stats["connectors"].items():
        if key.startswith(prefix) and key not in seen and entry["removed_at"] is None:
            entry.update(removed_at=stamp, error_since=None)
    if baseline:
        info.update(baselined=True, baseline_at=stamp)
    info["checks"] += 1
    info["alerts_sent"] += alerted
