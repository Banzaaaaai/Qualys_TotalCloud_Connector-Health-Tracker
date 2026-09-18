"""Pure episode transitions. Notification acknowledgement is a separate operation."""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime

from .config import ERROR_STATES, HEALTHY_STATES, NEUTRAL_STATES, Config
from .models import Connector


def classification(connector: Connector) -> str:
    if connector.disabled:
        return "disabled"
    state = connector.state.upper().strip()
    if state in HEALTHY_STATES:
        return "healthy"
    if state in NEUTRAL_STATES:
        return "neutral"
    if state in ERROR_STATES or "ERROR" in state:
        return "error"
    return "error"


@dataclass
class Evaluation:
    episodes: dict
    resolved: dict
    eligible: list[Connector] = field(default_factory=list)
    counts: dict = field(
        default_factory=lambda: dict.fromkeys(
            ("total", "healthy", "in_grace", "alerted", "disabled", "neutral", "deduplicated"), 0
        )
    )


def evaluate(connectors, episodes, resolved, now: datetime, config: Config, force=False):
    result = Evaluation({}, deepcopy(resolved))
    stamp = now.isoformat()
    for c in connectors:
        result.counts["total"] += 1
        kind = classification(c)
        old = deepcopy(episodes.get(c.key))
        if kind in {"disabled", "healthy"}:
            result.counts[kind] += 1
            if kind == "healthy" and old and config.include_resolved:
                result.resolved[c.key] = {"name": c.name, "resolved_at": stamp}
            continue
        if kind == "neutral":
            result.counts[kind] += 1
            if old:
                old.update(last_seen_at=stamp, last_state=c.state, name=c.name)
                result.episodes[c.key] = old
            continue
        result.resolved.pop(c.key, None)
        episode = old or {"first_error_seen_at": stamp, "notified_at": None, "last_error": ""}
        episode.update(last_seen_at=stamp, last_state=c.state, name=c.name)
        if c.error:
            episode["last_error"] = c.error
        result.episodes[c.key] = episode
        age = (now - datetime.fromisoformat(episode["first_error_seen_at"])).total_seconds() / 3600
        notified = episode["notified_at"]
        reminder = bool(
            notified
            and config.remind_hours > 0
            and (now - datetime.fromisoformat(notified)).total_seconds() / 3600
            >= config.remind_hours
        )
        # force bypasses grace only; dedup still protects existing episodes.
        if not force and age < config.grace_hours - config.tolerance_hours:
            result.counts["in_grace"] += 1
        elif not notified or reminder:
            result.eligible.append(c)
        else:
            result.counts["deduplicated"] += 1
    return result
