"""Validated, atomic JSON persistence; corrupt state is never silently replaced."""

import json
import os
from datetime import datetime
from pathlib import Path


def empty_state():
    return {"version": 1, "episodes": {}, "resolved": {}}


def load(path):
    path = Path(path)
    if not path.exists():
        return empty_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("Unsupported state format")
    for section in ("episodes", "resolved"):
        if not isinstance(state.get(section), dict):
            raise ValueError("Invalid state section")
        for key, entry in state[section].items():
            if (
                not isinstance(key, str)
                or key.partition(":")[0] not in {"AWS", "AZURE", "GCP"}
                or not key.partition(":")[2]
                or not isinstance(entry, dict)
            ):
                raise ValueError("Invalid state entry")
            fields = (
                ("first_error_seen_at", "last_seen_at", "notified_at")
                if section == "episodes"
                else ("resolved_at",)
            )
            for name in fields:
                value = entry.get(name)
                if name == "notified_at" and value is None:
                    continue
                if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
                    raise ValueError("Invalid state timestamp")
            for name in (
                ("name", "last_state", "last_error") if section == "episodes" else ("name",)
            ):
                if not isinstance(entry.get(name), str):
                    raise ValueError("Invalid state text")
    return state


def save(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
