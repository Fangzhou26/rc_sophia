import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    allowed_hosts: frozenset[str] = frozenset()
    busy_timeout_ms: int = 5000

    def __post_init__(self):
        object.__setattr__(self, "db_path", Path(self.db_path).resolve())
        object.__setattr__(self, "allowed_hosts", frozenset(
            host.strip().lower() for host in self.allowed_hosts if host.strip()
        ))
        if self.busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be nonnegative")

    @classmethod
    def from_env(cls):
        return cls(
            db_path=Path(os.getenv("NOTIFICATION_DB_PATH", "./data/notifications.sqlite3")),
            allowed_hosts=frozenset(os.getenv("NOTIFICATION_ALLOWED_HOSTS", "").split(",")),
            busy_timeout_ms=int(os.getenv("NOTIFICATION_BUSY_TIMEOUT_MS", "5000")),
        )
