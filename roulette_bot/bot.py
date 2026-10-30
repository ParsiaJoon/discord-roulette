"""Bot entrypoint: `python -m roulette_bot`."""

from __future__ import annotations

import logging
import os
import sys

import discord
from discord.ext import commands

from roulette_bot.cog import RouletteCog

log = logging.getLogger("roulette_bot")


class RouletteBot(commands.Bot):
    def __init__(self, dev_guild_id: int | None = None) -> None:
        # Only non-privileged intents: guilds (channels/roles) and voice states.
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.dev_guild_id = dev_guild_id

    async def setup_hook(self) -> None:
        await self.add_cog(RouletteCog(self))
        if self.dev_guild_id:
            guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to guild %s (instant)", len(synced), self.dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global command(s) (may take a while to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s) in %d server(s)", self.user, self.user and self.user.id, len(self.guilds))


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        sys.exit("DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token.")

    guild_raw = os.environ.get("DISCORD_GUILD_ID", "").strip()
    try:
        guild_id = int(guild_raw) if guild_raw else None
    except ValueError:
        sys.exit("DISCORD_GUILD_ID must be a numeric server ID (or left empty).")

    RouletteBot(dev_guild_id=guild_id).run(token, log_handler=None)


if __name__ == "__main__":
    main()
