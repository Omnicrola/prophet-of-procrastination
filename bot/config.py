from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    discord_token: str
    discord_app_id: str
    poll_interval_seconds: int
    log_level: str
    db_path: str


def load_config() -> Config:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ValueError("DISCORD_TOKEN environment variable is required")

    app_id = os.environ.get("DISCORD_APP_ID", "").strip()
    if not app_id:
        raise ValueError("DISCORD_APP_ID environment variable is required")

    return Config(
        discord_token=token,
        discord_app_id=app_id,
        poll_interval_seconds=int(os.environ.get("DEFAULT_POLL_INTERVAL_SECONDS", "60")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        db_path=os.environ.get("DB_PATH", "/data/dom6bot.db"),
    )
