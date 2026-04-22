from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import aiosqlite

from bot.models import GameConfig, GuildConfig, NationStatus

logger = logging.getLogger(__name__)

_ISO = "%Y-%m-%dT%H:%M:%S"


def _dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except Exception:
        return None


def _ts(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime(_ISO)


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info("Database initialized at %s", self.db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def _create_tables(self) -> None:
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS guilds (
                guild_id        INTEGER PRIMARY KEY,
                report_channel_id INTEGER,
                report_interval_hours INTEGER NOT NULL DEFAULT 4
            );

            CREATE TABLE IF NOT EXISTS games (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id            INTEGER NOT NULL,
                alias               TEXT NOT NULL,
                status_url          TEXT NOT NULL,
                server_ip           TEXT,
                server_port         INTEGER,
                last_turn_number    INTEGER,
                last_check_time     TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                failure_notified    INTEGER NOT NULL DEFAULT 0,
                warnings_sent_turn  INTEGER NOT NULL DEFAULT 0,
                warnings_sent_flags INTEGER NOT NULL DEFAULT 0,
                status_message_id   INTEGER,
                is_active           INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (guild_id) REFERENCES guilds(guild_id),
                UNIQUE (guild_id, alias)
            );

            CREATE TABLE IF NOT EXISTS nation_status (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id         INTEGER NOT NULL,
                nation_name     TEXT NOT NULL,
                position        INTEGER NOT NULL DEFAULT 0,
                submitted       INTEGER NOT NULL DEFAULT 0,
                is_ai           INTEGER NOT NULL DEFAULT 0,
                claimed_by_id   TEXT,
                claimed_by_name TEXT,
                last_updated    TEXT NOT NULL,
                FOREIGN KEY (game_id) REFERENCES games(id),
                UNIQUE (game_id, nation_name)
            );

            CREATE TABLE IF NOT EXISTS warning_messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                message   TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );
        """)
        await self._db.commit()
        await self._migrate()
        await self._seed_warning_messages()

    async def _migrate(self) -> None:
        """Add columns introduced after the initial schema without dropping existing data."""
        new_columns = [
            "ALTER TABLE nation_status ADD COLUMN position INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE nation_status ADD COLUMN is_ai INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE nation_status ADD COLUMN claimed_by_id TEXT",
            "ALTER TABLE nation_status ADD COLUMN claimed_by_name TEXT",
            "ALTER TABLE games ADD COLUMN warnings_sent_turn INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE games ADD COLUMN warnings_sent_flags INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE games ADD COLUMN status_message_id INTEGER",
        ]
        for sql in new_columns:
            try:
                await self._db.execute(sql)
            except Exception:
                pass  # column already exists
        await self._db.commit()

    async def _seed_warning_messages(self) -> None:
        async with self._db.execute("SELECT COUNT(*) FROM warning_messages") as cur:
            row = await cur.fetchone()
        if row[0] > 0:
            return  # already seeded

        messages = [
            "The candles of {game} burn low, {pending}. While you deliberate, {submitted} have already dispatched their armies and retired for the evening.",
            "{pending} — your Pretender stirs restlessly in its magical prison. It whispers: *please submit the turn*. {submitted} heard their Pretender much earlier.",
            "Heralds ride through the realm crying news of the laggards! {pending}, your inaction has been noted. The scribes of {submitted} are already writing about it.",
            "The blood mages have already made their sacrifices and {submitted} sleeps soundly. Meanwhile {pending} still holds the fate of {game} in their idle hands.",
            "Ancient prophecy foretells that {pending} shall submit their turn — eventually. Until then, {submitted} grows stronger by the hour.",
            "A lone prophet wanders the battlefield of {game}, bearing ill tidings for {pending}. {submitted} paid the prophet handsomely to deliver this message.",
            "Even the mindless undead shambling across the fields of {game} move with more urgency than {pending}. {submitted} finds this deeply amusing.",
            "The communion masters of {submitted} are watching through their crystal balls, {pending}. They see everything. They are very judgemental.",
            "Deep in the astral plane, where the threads of fate are woven, the absence of orders from {pending} has been recorded with great disappointment.",
            "The Pantokrator, watching from on high, has specifically noted the tardiness of {pending}. {submitted} has already ascended one rung closer to godhood.",
            "By the forge-fires of Ulm and the blood-pools of Mictlan! {pending}, must the gods themselves beg you? {submitted} needed no such convincing.",
            "The scales of Order tip toward Turmoil with every passing moment that {pending} delays. {submitted} has already stabilised their provinces.",
            "Your Dominion flickers like a candle in a storm, {pending}. {submitted} has already lit their sacrificial pyres for the new turn.",
            "Word reaches the war-council of {game}: {pending} has still not moved. The generals of {submitted} are sharpening their blades with renewed enthusiasm.",
            "In the great libraries of the land, tomes are written on the subject of slow turn submitters. {pending}, your chapter grows long. {submitted} merits only a footnote.",
            "The astrologers have consulted the stars and calculated the exact moment {pending}'s dominion collapses from sheer inaction. It approaches swiftly.",
            "Ravens bearing black feathers and urgent messages have been dispatched to {pending}. {submitted} is the one who sent them.",
            "From the frozen peaks of Niefelheim to the steaming jungles of Lanka, tales spread of the legendary tardiness of {pending}. {submitted} tells these tales.",
            "Your sacred scales weep, {pending}. Your Pretender paces its magical cage. Your armies shuffle their feet. Meanwhile {submitted} has already moved.",
            "It is written in the Book of Turns that {pending} shall face consequences for their dallying. {submitted} sponsored the writing of this book.",
            "The great summoning rituals go uncasted. The strategic gems go unspent. The troops go unordered — all because of {pending}. {submitted} weeps for them.",
            "The court astrologers of {submitted} have completed their divinations. The results concern {pending} greatly, but will not be shared until after turns are submitted.",
            "Rumour spreads through the courts of {game} that {pending} has become enamoured with the Sloth scale. {submitted} diplomatically declines to comment.",
            "Even the imprisoned Pretender of {pending} has managed to send a strongly-worded letter of complaint about the delay. {submitted} received a copy.",
            "A vision from the Beyond: {pending}'s pretender, sitting alone in the dark, staring at the turn submission screen. {submitted} had no such visions. They were too busy winning.",
        ]
        await self._db.executemany(
            "INSERT INTO warning_messages (message) VALUES (?)",
            [(m,) for m in messages],
        )
        await self._db.commit()
        logger.info("Seeded %d warning messages", len(messages))

    async def get_random_warning_message(self) -> Optional[str]:
        async with self._db.execute(
            "SELECT message FROM warning_messages WHERE is_active = 1 ORDER BY RANDOM() LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Guild methods
    # ------------------------------------------------------------------

    async def upsert_guild(
        self,
        guild_id: int,
        report_channel_id: Optional[int] = None,
        report_interval_hours: Optional[int] = None,
    ) -> None:
        existing = await self.get_guild(guild_id)
        if existing is None:
            await self._db.execute(
                "INSERT INTO guilds (guild_id, report_channel_id, report_interval_hours) VALUES (?, ?, ?)",
                (guild_id, report_channel_id, report_interval_hours or 4),
            )
        else:
            if report_channel_id is not None:
                await self._db.execute(
                    "UPDATE guilds SET report_channel_id = ? WHERE guild_id = ?",
                    (report_channel_id, guild_id),
                )
            if report_interval_hours is not None:
                await self._db.execute(
                    "UPDATE guilds SET report_interval_hours = ? WHERE guild_id = ?",
                    (report_interval_hours, guild_id),
                )
        await self._db.commit()

    async def get_guild(self, guild_id: int) -> Optional[GuildConfig]:
        async with self._db.execute(
            "SELECT guild_id, report_channel_id, report_interval_hours FROM guilds WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return GuildConfig(
            guild_id=row["guild_id"],
            report_channel_id=row["report_channel_id"],
            report_interval_hours=row["report_interval_hours"],
        )

    # ------------------------------------------------------------------
    # Game methods
    # ------------------------------------------------------------------

    async def add_game(
        self,
        guild_id: int,
        alias: str,
        status_url: str,
        server_ip: Optional[str] = None,
        server_port: Optional[int] = None,
    ) -> int:
        await self.upsert_guild(guild_id)
        cursor = await self._db.execute(
            """INSERT INTO games (guild_id, alias, status_url, server_ip, server_port)
               VALUES (?, ?, ?, ?, ?)""",
            (guild_id, alias, status_url, server_ip, server_port),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def remove_game(self, guild_id: int, alias: str) -> bool:
        cursor = await self._db.execute(
            "UPDATE games SET is_active = 0 WHERE guild_id = ? AND alias = ? AND is_active = 1",
            (guild_id, alias),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def get_game(self, guild_id: int, alias: str) -> Optional[GameConfig]:
        async with self._db.execute(
            "SELECT * FROM games WHERE guild_id = ? AND alias = ? AND is_active = 1",
            (guild_id, alias),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_game(row) if row else None

    async def get_all_active_games(self) -> list[GameConfig]:
        async with self._db.execute(
            "SELECT * FROM games WHERE is_active = 1"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_game(r) for r in rows]

    async def get_games_for_guild(self, guild_id: int) -> list[GameConfig]:
        async with self._db.execute(
            "SELECT * FROM games WHERE guild_id = ? AND is_active = 1",
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_game(r) for r in rows]

    async def update_game_state(
        self,
        game_id: int,
        turn_number: int,
        check_time: datetime,
    ) -> None:
        await self._db.execute(
            "UPDATE games SET last_turn_number = ?, last_check_time = ? WHERE id = ?",
            (turn_number, _ts(check_time), game_id),
        )
        await self._db.commit()

    async def update_warnings(self, game_id: int, turn: int, flags: int) -> None:
        await self._db.execute(
            "UPDATE games SET warnings_sent_turn = ?, warnings_sent_flags = ? WHERE id = ?",
            (turn, flags, game_id),
        )
        await self._db.commit()

    async def set_status_message_id(self, game_id: int, message_id: Optional[int]) -> None:
        await self._db.execute(
            "UPDATE games SET status_message_id = ? WHERE id = ?",
            (message_id, game_id),
        )
        await self._db.commit()

    async def increment_failure(self, game_id: int) -> int:
        await self._db.execute(
            "UPDATE games SET consecutive_failures = consecutive_failures + 1 WHERE id = ?",
            (game_id,),
        )
        await self._db.commit()
        async with self._db.execute(
            "SELECT consecutive_failures FROM games WHERE id = ?", (game_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["consecutive_failures"] if row else 0

    async def reset_failure(self, game_id: int) -> None:
        await self._db.execute(
            "UPDATE games SET consecutive_failures = 0, failure_notified = 0 WHERE id = ?",
            (game_id,),
        )
        await self._db.commit()

    async def set_failure_notified(self, game_id: int) -> None:
        await self._db.execute(
            "UPDATE games SET failure_notified = 1 WHERE id = ?",
            (game_id,),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Nation status methods
    # ------------------------------------------------------------------

    async def get_nations_for_game(self, game_id: int) -> list[NationStatus]:
        async with self._db.execute(
            """SELECT nation_name, position, submitted, is_ai, claimed_by_id, claimed_by_name
               FROM nation_status WHERE game_id = ? ORDER BY position ASC, id ASC""",
            (game_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_nation(r) for r in rows]

    async def get_nation_by_position(self, game_id: int, position: int) -> Optional[NationStatus]:
        async with self._db.execute(
            """SELECT nation_name, position, submitted, is_ai, claimed_by_id, claimed_by_name
               FROM nation_status WHERE game_id = ? AND position = ?""",
            (game_id, position),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_nation(row) if row else None

    async def replace_nations_for_game(
        self, game_id: int, nations: list[NationStatus]
    ) -> None:
        """
        Upsert nation rows from a freshly scraped status page.
        Updates position/submitted/last_updated but preserves is_ai and claim data
        so player flags survive repeated polls.
        """
        now = _ts(datetime.utcnow())
        await self._db.executemany(
            """INSERT INTO nation_status
                   (game_id, nation_name, position, submitted, last_updated)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (game_id, nation_name) DO UPDATE SET
                   position     = excluded.position,
                   submitted    = excluded.submitted,
                   last_updated = excluded.last_updated""",
            [(game_id, n.name, i + 1, int(n.submitted), now) for i, n in enumerate(nations)],
        )
        # Drop rows for nations no longer on the status page (e.g. defeated)
        if nations:
            placeholders = ",".join("?" * len(nations))
            await self._db.execute(
                f"DELETE FROM nation_status WHERE game_id = ? AND nation_name NOT IN ({placeholders})",
                [game_id, *[n.name for n in nations]],
            )
        await self._db.commit()

    async def set_nation_claim(
        self,
        game_id: int,
        position: int,
        user_id: Optional[str],
        user_name: Optional[str],
    ) -> bool:
        cursor = await self._db.execute(
            """UPDATE nation_status SET claimed_by_id = ?, claimed_by_name = ?
               WHERE game_id = ? AND position = ?""",
            (user_id, user_name, game_id, position),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def set_nation_ai(self, game_id: int, position: int, is_ai: bool) -> bool:
        cursor = await self._db.execute(
            "UPDATE nation_status SET is_ai = ? WHERE game_id = ? AND position = ?",
            (int(is_ai), game_id, position),
        )
        await self._db.commit()
        return cursor.rowcount > 0


def _row_to_nation(row: aiosqlite.Row) -> NationStatus:
    return NationStatus(
        name=row["nation_name"],
        submitted=bool(row["submitted"]),
        position=row["position"],
        is_ai=bool(row["is_ai"]),
        claimed_by_id=row["claimed_by_id"],
        claimed_by_name=row["claimed_by_name"],
    )


def _row_to_game(row: aiosqlite.Row) -> GameConfig:
    return GameConfig(
        id=row["id"],
        guild_id=row["guild_id"],
        alias=row["alias"],
        status_url=row["status_url"],
        server_ip=row["server_ip"],
        server_port=row["server_port"],
        last_turn_number=row["last_turn_number"],
        last_check_time=_dt(row["last_check_time"]),
        consecutive_failures=row["consecutive_failures"],
        failure_notified=bool(row["failure_notified"]),
        warnings_sent_turn=row["warnings_sent_turn"],
        warnings_sent_flags=row["warnings_sent_flags"],
        is_active=bool(row["is_active"]),
        status_message_id=row["status_message_id"],
    )
