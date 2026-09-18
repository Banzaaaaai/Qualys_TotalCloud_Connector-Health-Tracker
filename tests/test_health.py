from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from connector_health.config import Config
from connector_health.health import evaluate
from connector_health.models import Connector

NOW = datetime(2026, 1, 1, 6, tzinfo=UTC)
CONFIG = Config("https://qualys.example", "user", "password")
ERROR = Connector("AWS", "1", "test", "ERROR", error="Denied")


def run(connectors=(ERROR,), episodes=None, hours=0, config=CONFIG, force=False, resolved=None):
    return evaluate(
        connectors, episodes or {}, resolved or {}, NOW + timedelta(hours=hours), config, force
    )


@pytest.mark.parametrize(
    "hours,eligible", [(0, False), (20, False), (21.99, False), (22, True), (23, True), (24, True)]
)
def test_grace(hours, eligible):
    first = run()
    assert first.eligible == []
    result = run(episodes=first.episodes, hours=hours)
    assert bool(result.eligible) is eligible
    assert first.episodes[ERROR.key]["first_error_seen_at"] == NOW.isoformat()


def test_recovery_flapping_and_deletion():
    first = run()
    healthy = run((replace(ERROR, state="SUCCESS"),), first.episodes, 24)
    assert healthy.episodes == {}
    again = run(episodes=healthy.episodes, hours=25)
    assert not again.eligible
    assert (
        again.episodes[ERROR.key]["first_error_seen_at"] == (NOW + timedelta(hours=25)).isoformat()
    )
    assert run((), first.episodes).episodes == {}


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "PROCESSING", "PENDING"])
def test_transient_preserves_clock_without_alerting(state):
    first = run()
    neutral = run((replace(ERROR, state=state),), first.episodes, 23)
    assert not neutral.eligible
    assert neutral.episodes[ERROR.key]["first_error_seen_at"] == NOW.isoformat()
    assert run(episodes=neutral.episodes, hours=24).eligible
    assert run((replace(ERROR, state=state),)).episodes == {}


def test_dedup_reminders_and_changed_error():
    first = run()
    first.episodes[ERROR.key]["notified_at"] = (NOW + timedelta(hours=23)).isoformat()
    assert not run(episodes=first.episodes, hours=48).eligible
    config = replace(CONFIG, remind_hours=48)
    assert not run(episodes=first.episodes, hours=70, config=config).eligible
    result = run((replace(ERROR, error="New error"),), first.episodes, 71, config)
    assert result.eligible
    assert result.episodes[ERROR.key]["last_error"] == "New error"
    assert result.episodes[ERROR.key]["first_error_seen_at"] == NOW.isoformat()
    assert first.episodes[ERROR.key]["last_error"] == "Denied"


def test_disabled_unknown_and_force():
    assert not run((replace(ERROR, disabled=True),), run().episodes).episodes
    assert run((replace(ERROR, state="UNRECOGNIZED"),), force=True).eligible
    assert run(force=True).eligible
    first = run()
    first.episodes[ERROR.key]["notified_at"] = NOW.isoformat()
    assert not run(episodes=first.episodes, force=True).eligible


def test_pending_resolved_survives_until_next_report():
    config = replace(CONFIG, include_resolved=True)
    result = run((replace(ERROR, state="SUCCESS"),), run().episodes, 24, config)
    assert result.resolved[ERROR.key]["name"] == ERROR.name
    assert not result.eligible
    later = run((), config=config, resolved=result.resolved)
    assert later.resolved == result.resolved
