from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class NationStatus:
    name: str
    submitted: bool
    is_human: bool = True
    position: int = 0            # 1-indexed display order from status page
    is_ai: bool = False          # flagged by players; excluded from "waiting on" count
    claimed_by_id: Optional[str] = None    # Discord user snowflake
    claimed_by_name: Optional[str] = None  # Display name at time of claim
    notify: bool = False                   # ping on new turn


@dataclass
class GameState:
    game_name: str
    turn_number: int
    time_remaining: Optional[str]
    time_remaining_seconds: Optional[int]
    nations: list[NationStatus] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.utcnow)

    def human_nations(self) -> list[NationStatus]:
        return [n for n in self.nations if n.is_human]

    def active_nations(self) -> list[NationStatus]:
        """Human-controlled, non-AI nations — the ones we wait on."""
        return [n for n in self.human_nations() if not n.is_ai]

    def submitted_count(self) -> int:
        return sum(1 for n in self.active_nations() if n.submitted)

    def pending_count(self) -> int:
        return sum(1 for n in self.active_nations() if not n.submitted)


@dataclass
class GuildConfig:
    guild_id: int
    report_channel_id: Optional[int]
    report_interval_hours: int = 4


@dataclass
class GameConfig:
    id: Optional[int]
    guild_id: int
    alias: str
    status_url: str
    server_ip: Optional[str]
    server_port: Optional[int]
    last_turn_number: Optional[int]
    last_check_time: Optional[datetime]
    consecutive_failures: int = 0
    failure_notified: bool = False
    warnings_sent_turn: int = 0
    warnings_sent_flags: int = 0
    is_active: bool = True
    status_message_id: Optional[int] = None
