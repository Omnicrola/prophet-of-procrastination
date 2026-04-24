"""
GameMonitor cog — slash commands and the background polling loop.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.config import Config
from bot.models import GameConfig, GameState, NationStatus
from bot.services import database as db_module
from bot.services import status_scraper, tcp_query
from bot.services.status_scraper import illwinter_status_url

logger = logging.getLogger(__name__)

_MAX_BACKOFF_SECONDS = 3600
_FAILURE_WARN_THRESHOLD = 5

# (seconds_threshold, bitmask_flag, label, ping_players)
_WARN_THRESHOLDS = [
    (12 * 3600, 0x01, "12 hours", False),
    (3  * 3600, 0x02, "3 hours",  True),
    (1  * 3600, 0x04, "1 hour",   True),
]


def _backoff_seconds(consecutive_failures: int) -> int:
    return min(60 * (2 ** min(consecutive_failures - 1, 6)), _MAX_BACKOFF_SECONDS)


def _format_time(seconds: Optional[int]) -> str:
    if seconds is None:
        return "unknown"
    hours, rem = divmod(seconds, 3600)
    mins = rem // 60
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _short_name(name: str) -> str:
    return name.split(",")[0].strip()


def _nation_line(n: NationStatus) -> str:
    """Format a single nation row for an embed field."""
    if n.is_ai:
        icon = "🤖"
    elif n.submitted:
        icon = "✅"
    else:
        icon = "❌"

    line = f"`{n.position:2d}.` {icon} {n.name}"
    if n.is_ai:
        line += "  *(AI)*"
    elif n.claimed_by_name:
        line += f"  — {n.claimed_by_name}"
    return line


def _nations_field(nations: list[NationStatus]) -> tuple[str, str]:
    """Return (field_name, field_value) for the nations embed field."""
    active = [n for n in nations if not n.is_ai]
    ai = [n for n in nations if n.is_ai]
    submitted = sum(1 for n in active if n.submitted)

    ai_note = f", {len(ai)} AI" if ai else ""
    name = f"Nations ({submitted}/{len(active)} submitted{ai_note})"

    ordered = sorted(nations, key=lambda n: n.position)
    value = "\n".join(_nation_line(n) for n in ordered) or "No nation data"
    return name, value[:1024]


def _build_status_embed(
    game: GameConfig,
    state: GameState,
    db_nations: Optional[list[NationStatus]] = None,
    stale: bool = False,
) -> discord.Embed:
    color = discord.Color.orange() if stale else discord.Color.green()
    title = f"{'[STALE] ' if stale else ''}📊 {state.game_name} — Turn {state.turn_number}"
    embed = discord.Embed(title=title, color=color)

    time_str = state.time_remaining or _format_time(state.time_remaining_seconds)
    embed.add_field(name="⏰ Time Remaining", value=time_str or "unknown", inline=True)
    embed.add_field(name="Game", value=game.alias, inline=True)

    nations = db_nations if db_nations else state.nations
    if nations:
        field_name, field_value = _nations_field(nations)
        embed.add_field(name=field_name, value=field_value, inline=False)
    else:
        embed.add_field(name="Nations", value="No nation data yet — check back after the first poll.", inline=False)

    if stale:
        embed.set_footer(text="No change since last check.")
    embed.timestamp = datetime.utcnow()
    return embed


def _build_new_turn_embed(game: GameConfig, state: GameState) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔔 New Turn! {state.game_name} — Turn {state.turn_number}",
        color=discord.Color.blue(),
    )
    time_str = state.time_remaining or _format_time(state.time_remaining_seconds)
    embed.add_field(name="⏰ Time Remaining", value=time_str or "unknown", inline=True)
    embed.add_field(name="Game", value=game.alias, inline=True)
    embed.timestamp = datetime.utcnow()
    return embed


class GameMonitor(commands.Cog):
    def __init__(self, bot: commands.Bot, config: Config, db: db_module.Database) -> None:
        self.bot = bot
        self.config = config
        self.db = db
        self._http_session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        self._http_session = aiohttp.ClientSession(
            headers={"User-Agent": "dom6-discord-bot/1.0"}
        )
        self.poll_task.change_interval(seconds=self.config.poll_interval_seconds)
        self.poll_task.start()
        logger.info("GameMonitor cog loaded; poll interval = %ds", self.config.poll_interval_seconds)

    async def cog_unload(self) -> None:
        self.poll_task.cancel()
        if self._http_session:
            await self._http_session.close()
        logger.info("GameMonitor cog unloaded")

    # ------------------------------------------------------------------
    # Background polling loop
    # This 60-second heartbeat also keeps the Oracle Cloud instance
    # from being reclaimed for idleness.
    # ------------------------------------------------------------------

    @tasks.loop(seconds=60)
    async def poll_task(self) -> None:
        games = await self.db.get_all_active_games()
        for game in games:
            try:
                await self._poll_game(game)
            except Exception:
                logger.exception("Unhandled error polling game %s (guild %d)", game.alias, game.guild_id)

    @poll_task.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Core polling logic
    # ------------------------------------------------------------------

    async def _poll_game(self, game: GameConfig) -> None:
        if game.consecutive_failures > 0:
            backoff = _backoff_seconds(game.consecutive_failures)
            if game.last_check_time:
                elapsed = (datetime.utcnow() - game.last_check_time).total_seconds()
                if elapsed < backoff:
                    return

        state = await self._fetch_state(game)

        if state is None:
            failures = await self.db.increment_failure(game.id)
            await self.db.update_game_state(game.id, game.last_turn_number or 0, datetime.utcnow())
            logger.warning("Failed to fetch %s (guild %d), failures: %d", game.alias, game.guild_id, failures)
            if failures >= _FAILURE_WARN_THRESHOLD and not game.failure_notified:
                await self._send_failure_warning(game, failures)
                await self.db.set_failure_notified(game.id)
            return

        await self.db.reset_failure(game.id)

        # Detect new turn before saving so we can compare against the stored number
        new_turn = game.last_turn_number is not None and state.turn_number > game.last_turn_number

        # Persist fresh state (upsert preserves claims and AI flags)
        await self.db.update_game_state(game.id, state.turn_number, datetime.utcnow())
        if state.nations:
            await self.db.replace_nations_for_game(game.id, state.nations)

        # Fetch enriched nations (with claims/AI) for notifications
        db_nations = await self.db.get_nations_for_game(game.id)

        if new_turn:
            await self._notify_new_turn(game, state)

        await self._check_thresholds(game, state, db_nations)
        await self._update_status_embed(game, state, db_nations)

    async def _fetch_state(self, game: GameConfig) -> Optional[GameState]:
        state = await status_scraper.fetch_status(game.status_url, self._http_session)
        if state is not None:
            return state
        if game.server_ip and game.server_port:
            state = await tcp_query.query_server(game.server_ip, game.server_port, game.alias)
            if state is not None:
                logger.debug("Used TCP fallback for %s", game.alias)
                return state
        return None

    async def _notify_new_turn(self, game: GameConfig, state: GameState) -> None:
        guild_cfg = await self.db.get_guild(game.guild_id)
        if not guild_cfg or not guild_cfg.report_channel_id:
            return
        channel = self.bot.get_channel(guild_cfg.report_channel_id)
        if channel is None:
            return
        embed = _build_new_turn_embed(game, state)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            logger.warning("Failed to send new turn notification: %s", exc)

    async def _update_status_embed(
        self, game: GameConfig, state: GameState, db_nations: list[NationStatus]
    ) -> None:
        guild_cfg = await self.db.get_guild(game.guild_id)
        if not guild_cfg or not guild_cfg.report_channel_id:
            return
        channel = self.bot.get_channel(guild_cfg.report_channel_id)
        if channel is None:
            logger.warning(
                "Status embed update skipped for %s: channel %s not found in cache",
                game.alias, guild_cfg.report_channel_id,
            )
            return

        embed = _build_status_embed(game, state, db_nations)

        if game.status_message_id:
            try:
                # get_partial_message avoids a fetch (no READ_MESSAGE_HISTORY needed).
                # edit() raises NotFound if deleted, Forbidden if uneditable.
                message = channel.get_partial_message(game.status_message_id)
                await message.edit(embed=embed)
                return
            except discord.NotFound:
                pass  # message was deleted; fall through to post a new one
            except discord.HTTPException as exc:
                logger.warning("Failed to edit status embed for %s: %s", game.alias, exc)
                return

        try:
            message = await channel.send(embed=embed)
            await self.db.set_status_message_id(game.id, message.id)
        except discord.HTTPException as exc:
            logger.warning("Failed to post status embed for %s: %s", game.alias, exc)

    async def _check_thresholds(
        self, game: GameConfig, state: GameState, db_nations: list[NationStatus]
    ) -> None:
        if state.time_remaining_seconds is None:
            logger.debug(
                "Threshold check skipped for %s turn %d: time_remaining_seconds unavailable "
                "(raw time string: %r)",
                game.alias, state.turn_number, state.time_remaining,
            )
            return

        guild_cfg = await self.db.get_guild(game.guild_id)
        if not guild_cfg or not guild_cfg.report_channel_id:
            return
        channel = self.bot.get_channel(guild_cfg.report_channel_id)
        if channel is None:
            logger.warning(
                "Threshold check skipped for %s: channel %s not found in cache",
                game.alias, guild_cfg.report_channel_id,
            )
            return

        # Reset flags when the turn has advanced since we last tracked warnings
        sent_turn = game.warnings_sent_turn
        sent_flags = game.warnings_sent_flags
        if sent_turn != state.turn_number:
            sent_turn = state.turn_number
            sent_flags = 0

        new_flags = sent_flags
        for threshold_secs, flag, label, do_ping in _WARN_THRESHOLDS:
            if state.time_remaining_seconds < threshold_secs and not (sent_flags & flag):
                await self._send_threshold_warning(channel, game, state, db_nations, label, do_ping)
                new_flags |= flag

        if new_flags != game.warnings_sent_flags or sent_turn != game.warnings_sent_turn:
            await self.db.update_warnings(game.id, sent_turn, new_flags)

    def _format_warning_content(
        self,
        template: str,
        game_name: str,
        pending_nations: list[NationStatus],
        submitted_nations: list[NationStatus],
        ping_players: bool,
    ) -> str:
        if ping_players:
            pending_parts = []
            for n in pending_nations:
                if n.claimed_by_id:
                    pending_parts.append(f"<@{n.claimed_by_id}>")
                else:
                    pending_parts.append(f"*{_short_name(n.name)}*")
        else:
            pending_parts = [f"*{_short_name(n.name)}*" for n in pending_nations]

        submitted_parts = [f"*{_short_name(n.name)}*" for n in submitted_nations]

        pending_str = ", ".join(pending_parts) if pending_parts else "nobody"
        submitted_str = ", ".join(submitted_parts) if submitted_parts else "nobody"

        return template.format(game=game_name, pending=pending_str, submitted=submitted_str)

    async def _send_threshold_warning(
        self,
        channel: discord.TextChannel,
        game: GameConfig,
        state: GameState,
        db_nations: list[NationStatus],
        label: str,
        ping_players: bool,
    ) -> None:
        is_urgent = label == "1 hour"
        icon = "🚨" if is_urgent else "⏰"
        color = discord.Color.red() if is_urgent else discord.Color.orange()

        embed = discord.Embed(
            title=f"{icon} {state.game_name} — Turn {state.turn_number} — Less than {label} remaining!",
            color=color,
        )
        if db_nations:
            field_name, field_value = _nations_field(db_nations)
            embed.add_field(name=field_name, value=field_value, inline=False)
        embed.timestamp = datetime.utcnow()

        pending = [n for n in db_nations if not n.is_ai and not n.submitted]
        submitted = [n for n in db_nations if not n.is_ai and n.submitted]

        content = None
        template = await self.db.get_random_warning_message()
        if template:
            try:
                content = self._format_warning_content(
                    template, state.game_name, pending, submitted, ping_players
                )
            except KeyError:
                logger.warning("Warning message template has unknown placeholder: %r", template)
                content = template
        elif ping_players and pending:
            ping_parts = [f"<@{n.claimed_by_id}>" if n.claimed_by_id else f"*{_short_name(n.name)}*" for n in pending]
            content = f"{icon} Less than {label} left in **{state.game_name}**! Still waiting on: {', '.join(ping_parts)}"

        try:
            await channel.send(content=content, embed=embed)
        except discord.HTTPException as exc:
            logger.warning("Failed to send threshold warning (%s) for %s: %s", label, game.alias, exc)

    async def _send_failure_warning(self, game: GameConfig, failures: int) -> None:
        guild_cfg = await self.db.get_guild(game.guild_id)
        if not guild_cfg or not guild_cfg.report_channel_id:
            return
        channel = self.bot.get_channel(guild_cfg.report_channel_id)
        if channel is None:
            return
        embed = discord.Embed(
            title=f"⚠️ Cannot reach {game.alias}",
            description=(
                f"The status page for **{game.alias}** has been unreachable for "
                f"**{failures}** consecutive checks.\n\nURL: `{game.status_url}`\n\n"
                "The bot will continue retrying with exponential backoff."
            ),
            color=discord.Color.red(),
        )
        embed.timestamp = datetime.utcnow()
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            logger.warning("Failed to send failure warning: %s", exc)

    # ------------------------------------------------------------------
    # Helper: resolve game from name, reply on error
    # ------------------------------------------------------------------

    async def _resolve_game(
        self, interaction: discord.Interaction, game_name: str
    ) -> Optional[GameConfig]:
        game = await self.db.get_game(interaction.guild_id, game_name.strip().lower())
        if game is None:
            await interaction.response.send_message(
                f"No active game named `{game_name}`. Use `/listgames` to see what's monitored.",
                ephemeral=True,
            )
        return game

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    @app_commands.command(name="addgame", description="Register a Dominions 6 game to monitor.")
    @app_commands.describe(
        game_name="Exact game name as it appears in the Dominions lobby (e.g. 'KevSunday')",
        status_url="Override status page URL — leave blank to use Illwinter hosting",
        server_ip="Optional: game server IP for TCP fallback",
        server_port="Optional: game server port for TCP fallback",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def add_game(
        self,
        interaction: discord.Interaction,
        game_name: str,
        status_url: Optional[str] = None,
        server_ip: Optional[str] = None,
        server_port: Optional[int] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        game_name = game_name.strip()
        if not game_name or len(game_name) > 64:
            await interaction.followup.send("Game name must be 1–64 characters.", ephemeral=True)
            return

        alias = game_name.lower()
        resolved_url = status_url.strip() if status_url else illwinter_status_url(game_name)

        if await self.db.get_game(interaction.guild_id, alias):
            await interaction.followup.send(
                f"A game named `{game_name}` is already monitored. Remove it first with `/removegame`.",
                ephemeral=True,
            )
            return

        game_id = await self.db.add_game(
            guild_id=interaction.guild_id,
            alias=alias,
            status_url=resolved_url,
            server_ip=server_ip,
            server_port=server_port,
        )

        hosting = "Illwinter" if not status_url else "custom URL"
        logger.info("Game added: %s (guild %d, id %d, %s)", game_name, interaction.guild_id, game_id, hosting)
        parts = [
            f"Game **{game_name}** added and monitoring started.",
            f"Status URL ({hosting}): `{resolved_url}`",
        ]
        if server_ip:
            parts.append(f"TCP fallback: `{server_ip}:{server_port or '?'}`")
        parts.append("Use `/setchannel` to set where status reports are posted.")
        await interaction.followup.send("\n".join(parts), ephemeral=True)

    @app_commands.command(name="removegame", description="Stop monitoring a game.")
    @app_commands.describe(game_name="Name of the game to remove")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_game(self, interaction: discord.Interaction, game_name: str) -> None:
        await interaction.response.defer(ephemeral=True)
        removed = await self.db.remove_game(interaction.guild_id, game_name.strip().lower())
        if removed:
            logger.info("Game removed: %s (guild %d)", game_name, interaction.guild_id)
            await interaction.followup.send(f"Game **{game_name}** removed.", ephemeral=True)
        else:
            await interaction.followup.send(f"No active game named `{game_name}`.", ephemeral=True)

    @app_commands.command(name="status", description="Fetch and display current game status.")
    @app_commands.describe(
        game_name="Game name (omit to show all games)",
        broadcast="Post the status publicly in the channel instead of just to you",
    )
    @app_commands.guild_only()
    async def status_cmd(
        self,
        interaction: discord.Interaction,
        game_name: Optional[str] = None,
        broadcast: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=not broadcast)

        games = (
            [await self.db.get_game(interaction.guild_id, game_name.strip().lower())]
            if game_name
            else await self.db.get_games_for_guild(interaction.guild_id)
        )
        games = [g for g in games if g is not None]

        if not games:
            msg = f"No active game named `{game_name}`." if game_name else "No games are being monitored."
            await interaction.followup.send(msg, ephemeral=not broadcast)
            return

        for game in games:
            state = await self._fetch_state(game)
            if state is None:
                await interaction.followup.send(
                    f"Could not fetch status for **{game.alias}**. The server may be offline.",
                    ephemeral=not broadcast,
                )
                continue
            # Save fresh data so DB nations have current submitted status
            await self.db.update_game_state(game.id, state.turn_number, datetime.utcnow())
            if state.nations:
                await self.db.replace_nations_for_game(game.id, state.nations)
            db_nations = await self.db.get_nations_for_game(game.id)
            embed = _build_status_embed(game, state, db_nations)
            message = await interaction.followup.send(embed=embed, ephemeral=not broadcast, wait=True)
            if broadcast:
                await self.db.set_status_message_id(game.id, message.id)

    @app_commands.command(name="setchannel", description="Set this channel as the reporting channel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_channel(self, interaction: discord.Interaction) -> None:
        await self.db.upsert_guild(interaction.guild_id, report_channel_id=interaction.channel_id)
        logger.info("Report channel set: guild %d -> channel %d", interaction.guild_id, interaction.channel_id)
        await interaction.response.send_message(
            f"Status reports will now be posted in <#{interaction.channel_id}>.", ephemeral=True
        )

    @app_commands.command(name="listgames", description="List all monitored games in this server.")
    @app_commands.guild_only()
    async def list_games(self, interaction: discord.Interaction) -> None:
        games = await self.db.get_games_for_guild(interaction.guild_id)
        if not games:
            await interaction.response.send_message("No games are currently being monitored.", ephemeral=True)
            return

        embed = discord.Embed(title="Monitored Games", color=discord.Color.blurple())
        for game in games:
            last_turn = str(game.last_turn_number) if game.last_turn_number else "unknown"
            value_parts = [f"Status URL: `{game.status_url}`", f"Last known turn: {last_turn}"]
            if game.server_ip:
                value_parts.append(f"TCP: `{game.server_ip}:{game.server_port}`")
            if game.consecutive_failures > 0:
                value_parts.append(f"⚠️ {game.consecutive_failures} consecutive failures")
            embed.add_field(name=game.alias, value="\n".join(value_parts), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="claimnation", description="Claim a nation as your player slot.")
    @app_commands.describe(
        game_name="Game name",
        nation_number="Nation number shown in /status",
        use_lethal_force="Override an existing claim",
    )
    @app_commands.guild_only()
    async def claim_nation(
        self,
        interaction: discord.Interaction,
        game_name: str,
        nation_number: int,
        use_lethal_force: bool = False,
    ) -> None:
        game = await self._resolve_game(interaction, game_name)
        if game is None:
            return

        nation = await self.db.get_nation_by_position(game.id, nation_number)
        if nation is None:
            await interaction.response.send_message(
                f"No nation #{nation_number} in **{game_name}**. "
                f"Use `/status game_name:{game_name}` to see the numbered list.",
                ephemeral=True,
            )
            return

        if nation.is_ai:
            await interaction.response.send_message(
                f"**{_short_name(nation.name)}** is flagged as AI-controlled and cannot be claimed.",
                ephemeral=True,
            )
            return

        if nation.claimed_by_id and not use_lethal_force:
            await interaction.response.send_message(
                f"⚠️ **{_short_name(nation.name)}** is already claimed by **{nation.claimed_by_name}**.\n"
                f"Add `use_lethal_force:True` to override their claim.",
                ephemeral=True,
            )
            return

        prev_claimer = nation.claimed_by_name
        await self.db.set_nation_claim(
            game.id, nation_number,
            str(interaction.user.id),
            interaction.user.display_name,
        )

        if prev_claimer:
            msg = f"Claimed **{_short_name(nation.name)}** in **{game_name}**, overriding **{prev_claimer}**'s claim."
        else:
            msg = f"You've claimed **{_short_name(nation.name)}** in **{game_name}**."

        logger.info("Nation claimed: %s #%d by %s (guild %d)", game_name, nation_number, interaction.user, interaction.guild_id)
        await interaction.response.send_message(msg)

    @app_commands.command(name="unclaim", description="Release your claim on a nation.")
    @app_commands.describe(
        game_name="Game name",
        nation_number="Nation number shown in /status",
    )
    @app_commands.guild_only()
    async def unclaim_nation(
        self,
        interaction: discord.Interaction,
        game_name: str,
        nation_number: int,
    ) -> None:
        game = await self._resolve_game(interaction, game_name)
        if game is None:
            return

        nation = await self.db.get_nation_by_position(game.id, nation_number)
        if nation is None:
            await interaction.response.send_message(f"No nation #{nation_number} in **{game_name}**.", ephemeral=True)
            return

        if not nation.claimed_by_id:
            await interaction.response.send_message(f"**{_short_name(nation.name)}** has no claim to release.", ephemeral=True)
            return

        is_own = nation.claimed_by_id == str(interaction.user.id)
        is_admin = interaction.permissions.manage_guild

        if not is_own and not is_admin:
            await interaction.response.send_message(
                f"You can only release your own claims. "
                f"**{_short_name(nation.name)}** is claimed by **{nation.claimed_by_name}**.",
                ephemeral=True,
            )
            return

        await self.db.set_nation_claim(game.id, nation_number, None, None)
        await interaction.response.send_message(
            f"Claim on **{_short_name(nation.name)}** in **{game_name}** released.", ephemeral=True
        )

    @app_commands.command(name="flagai", description="Flag one or more nations as AI-controlled (excluded from 'waiting on' count).")
    @app_commands.describe(
        game_name="Game name",
        nation_numbers="Nation number(s) shown in /status — separate multiple with commas (e.g. 1,3,5)",
    )
    @app_commands.guild_only()
    async def flag_ai(
        self,
        interaction: discord.Interaction,
        game_name: str,
        nation_numbers: str,
    ) -> None:
        game = await self._resolve_game(interaction, game_name)
        if game is None:
            return

        try:
            positions = [int(p.strip()) for p in nation_numbers.split(",") if p.strip()]
        except ValueError:
            await interaction.response.send_message(
                "Nation numbers must be integers separated by commas (e.g. `1,3,5`).", ephemeral=True
            )
            return

        if not positions:
            await interaction.response.send_message("Please provide at least one nation number.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        flagged, already_ai, not_found = [], [], []
        for pos in positions:
            nation = await self.db.get_nation_by_position(game.id, pos)
            if nation is None:
                not_found.append(str(pos))
                continue
            if nation.is_ai:
                already_ai.append(_short_name(nation.name))
                continue
            await self.db.set_nation_ai(game.id, pos, True)
            if nation.claimed_by_id:
                await self.db.set_nation_claim(game.id, pos, None, None)
            flagged.append(_short_name(nation.name))

        logger.info("Nations flagged AI: %s %s (guild %d)", game_name, positions, interaction.guild_id)

        lines = []
        if flagged:
            names = ", ".join(f"**{n}**" for n in flagged)
            lines.append(f"Flagged as AI in **{game_name}**: {names}. They will appear with 🤖 and won't count toward pending turns.")
        if already_ai:
            names = ", ".join(f"**{n}**" for n in already_ai)
            lines.append(f"Already flagged as AI: {names}.")
        if not_found:
            nums = ", ".join(f"`#{n}`" for n in not_found)
            lines.append(f"Not found: {nums} — use `/status game_name:{game_name}` to see the numbered list.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="unflagai", description="Remove the AI flag from a nation.")
    @app_commands.describe(
        game_name="Game name",
        nation_number="Nation number shown in /status",
    )
    @app_commands.guild_only()
    async def unflag_ai(
        self,
        interaction: discord.Interaction,
        game_name: str,
        nation_number: int,
    ) -> None:
        game = await self._resolve_game(interaction, game_name)
        if game is None:
            return

        nation = await self.db.get_nation_by_position(game.id, nation_number)
        if nation is None:
            await interaction.response.send_message(f"No nation #{nation_number} in **{game_name}**.", ephemeral=True)
            return

        if not nation.is_ai:
            await interaction.response.send_message(
                f"**{_short_name(nation.name)}** is not flagged as AI.", ephemeral=True
            )
            return

        await self.db.set_nation_ai(game.id, nation_number, False)
        await interaction.response.send_message(
            f"AI flag removed from **{_short_name(nation.name)}** in **{game_name}**.", ephemeral=True
        )
