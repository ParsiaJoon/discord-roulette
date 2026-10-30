"""Implements the engine's Backend protocol on top of discord.py."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import discord

log = logging.getLogger(__name__)

VoiceLike = discord.VoiceChannel | discord.StageChannel


class DiscordBackend:
    def __init__(self, client: discord.Client) -> None:
        self.client = client

    def _voice_channel(self, guild_id: int, channel_id: int) -> tuple[discord.Guild, VoiceLike] | None:
        guild = self.client.get_guild(guild_id)
        if guild is None:
            return None
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return None
        return guild, channel

    async def _member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None

    async def voice_member_ids(self, guild_id: int, channel_id: int) -> set[int] | None:
        found = self._voice_channel(guild_id, channel_id)
        if found is None:
            return None
        guild, channel = found
        humans: set[int] = set()
        # voice_states is maintained from the voice-states intent, so this works
        # without the privileged Server Members intent.
        for user_id in list(channel.voice_states):
            member = await self._member(guild, user_id)
            if member is not None and not member.bot:
                humans.add(user_id)
        return humans

    async def can_disconnect(self, guild_id: int, channel_id: int) -> bool:
        found = self._voice_channel(guild_id, channel_id)
        if found is None:
            return False
        guild, channel = found
        perms = channel.permissions_for(guild.me)
        return perms.view_channel and perms.move_members

    async def disconnect(self, guild_id: int, channel_id: int, user_id: int) -> bool:
        found = self._voice_channel(guild_id, channel_id)
        if found is None:
            return False
        guild, channel = found
        if user_id not in channel.voice_states:
            return False
        member = await self._member(guild, user_id)
        if member is None:
            return False
        try:
            # move_to(None) disconnects from voice. It never kicks from the server.
            await member.move_to(None, reason="Discord Roulette: opted-in player lost the spin")
            return True
        except discord.Forbidden:
            log.warning("Missing permission to disconnect %s in %s/%s", user_id, guild_id, channel_id)
        except discord.HTTPException:
            log.exception("Failed to disconnect %s in %s/%s", user_id, guild_id, channel_id)
        return False

    async def send(
        self,
        guild_id: int,
        text_channel_id: int,
        content: str,
        *,
        mention_ids: Iterable[int] = (),
        kind: str = "info",
    ) -> bool:
        guild = self.client.get_guild(guild_id)
        channel = guild.get_channel_or_thread(text_channel_id) if guild else None
        if channel is None or not hasattr(channel, "send"):
            log.warning("Text channel %s in guild %s unavailable (%s)", text_channel_id, guild_id, kind)
            return False
        allowed = discord.AllowedMentions(
            everyone=False,
            roles=False,
            replied_user=False,
            users=[discord.Object(id=i) for i in mention_ids],
        )
        try:
            await channel.send(content, allowed_mentions=allowed)
            return True
        except discord.Forbidden:
            log.warning("Missing permission to send in %s/%s (%s)", guild_id, text_channel_id, kind)
        except discord.HTTPException:
            log.exception("Failed to send %s message in %s/%s", kind, guild_id, text_channel_id)
        return False

    def display_name(self, guild_id: int, user_id: int) -> str:
        guild = self.client.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        name = member.display_name if member else f"Player {user_id}"
        return discord.utils.escape_markdown(discord.utils.escape_mentions(name))
