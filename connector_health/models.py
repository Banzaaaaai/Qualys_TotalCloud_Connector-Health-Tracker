from dataclasses import dataclass


@dataclass(frozen=True)
class Connector:
    provider: str
    id: str
    name: str = "(unnamed)"
    state: str = "UNKNOWN"
    disabled: bool = False
    error: str = ""
    account: str = ""
    last_sync: str = ""
    next_sync: str = ""
    cloudview_uuid: str = ""

    @property
    def key(self):
        return f"{self.provider}:{self.id}"
