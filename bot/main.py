"""
Entry point for the Dominions 6 Discord monitor bot.

Run with:  python -m bot.main
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands

from bot.config import load_config
from bot.services.database import Database


def _configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    # Quiet noisy libraries
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


async def main() -> None:
    config = load_config()
    _configure_logging(config.log_level)
    logger = logging.getLogger(__name__)

    # Ensure the data directory exists
    db_dir = os.path.dirname(config.db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    db = Database(config.db_path)
    await db.initialize()

    intents = discord.Intents.default()
    # No message content intent needed — bot uses slash commands only

    bot = commands.Bot(
        command_prefix=commands.when_mentioned,
        intents=intents,
        application_id=int(config.discord_app_id),
        help_command=None,
    )

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (id %d)", bot.user, bot.user.id)
        logger.info("Connected to %d guild(s)", len(bot.guilds))

        # Guild-scoped sync is instant and takes priority over global commands.
        # Sync to every guild the bot is currently in.
        for guild in bot.guilds:
            try:
                synced = await bot.tree.sync(guild=guild)
                logger.info("Guild sync OK: %s (%d) — %d command(s)", guild.name, guild.id, len(synced))
            except discord.HTTPException as exc:
                logger.error("Guild sync FAILED for %s (%d): %s", guild.name, guild.id, exc)

        # Global sync for any future guilds (takes up to 1 hour to propagate).
        try:
            synced = await bot.tree.sync()
            logger.info("Global sync OK — %d command(s)", len(synced))
        except discord.HTTPException as exc:
            logger.error("Global sync FAILED: %s", exc)

    @bot.event
    async def on_guild_join(guild: discord.Guild) -> None:
        logger.info("Joined guild: %s (id %d)", guild.name, guild.id)
        # Sync commands to the new guild for instant availability
        try:
            await bot.tree.sync(guild=guild)
        except discord.HTTPException:
            pass

    @bot.event
    async def on_disconnect() -> None:
        logger.warning("Disconnected from Discord gateway — discord.py will attempt reconnect")

    @bot.event
    async def on_resumed() -> None:
        logger.info("Gateway session resumed")

    # Load cogs
    from bot.cogs.game_monitor import GameMonitor
    await bot.add_cog(GameMonitor(bot, config, db))
    logger.info("Cogs loaded")

    try:
        await bot.start(config.discord_token)
    except discord.LoginFailure:
        logger.critical("Invalid DISCORD_TOKEN — check your .env file")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        await db.close()
        if not bot.is_closed():
            await bot.close()
        logger.info("Bot shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
