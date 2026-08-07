from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MOVIE_STAFF_ROLE_ID = 1535138119664402443
DEFAULT_CUSTOMER_NOTIFICATION_ROLE_ID = 1515866262306033737


class ConfigError(RuntimeError):
    """Raised when required bot configuration is missing or invalid."""


def _load_local_env(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    database_path: Path
    transcript_dir: Path
    dev_guild_id: int | None
    movie_staff_role_id: int | None
    customer_notification_role_id: int | None
    log_level: int

    @classmethod
    def from_env(cls) -> Settings:
        _load_local_env()

        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigError("DISCORD_TOKEN is missing.")

        raw_guild_id = os.getenv("DEV_GUILD_ID", "").strip()
        try:
            dev_guild_id = int(raw_guild_id) if raw_guild_id else None
        except ValueError as exc:
            raise ConfigError("DEV_GUILD_ID must be a plain Discord server ID.") from exc
        if dev_guild_id is not None and dev_guild_id <= 0:
            raise ConfigError("DEV_GUILD_ID must be a positive Discord server ID.")

        role_ids: dict[str, int | None] = {}
        role_defaults = {
            "MOVIE_STAFF_ROLE_ID": DEFAULT_MOVIE_STAFF_ROLE_ID,
            "CUSTOMER_NOTIFICATION_ROLE_ID": DEFAULT_CUSTOMER_NOTIFICATION_ROLE_ID,
        }
        for variable_name, default_value in role_defaults.items():
            raw_value = os.getenv(variable_name, str(default_value)).strip()
            try:
                parsed_value = int(raw_value) if raw_value else None
            except ValueError as exc:
                raise ConfigError(f"{variable_name} must be a plain Discord role ID.") from exc
            if parsed_value is not None and parsed_value <= 0:
                raise ConfigError(f"{variable_name} must be a positive Discord role ID.")
            role_ids[variable_name] = parsed_value

        log_name = os.getenv("LOG_LEVEL", "INFO").upper()
        log_level = getattr(logging, log_name, None)
        if not isinstance(log_level, int):
            raise ConfigError(f"Unknown LOG_LEVEL: {log_name}")

        return cls(
            token=token,
            database_path=Path(os.getenv("DATABASE_PATH", "data/movie_orders.db")),
            transcript_dir=Path(os.getenv("TRANSCRIPT_DIR", "data/transcripts")),
            dev_guild_id=dev_guild_id,
            movie_staff_role_id=role_ids["MOVIE_STAFF_ROLE_ID"],
            customer_notification_role_id=role_ids["CUSTOMER_NOTIFICATION_ROLE_ID"],
            log_level=log_level,
        )
