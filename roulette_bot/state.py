"""In-memory roulette state, isolated per (guild, voice channel).

Nothing here is persisted: restarting the bot resets every opt-in and
stops every timer, which is intentional (consent never outlives a session).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

ChannelKey = tuple[int, int]  # (guild_id, voice_channel_id)


@dataclass
class ChannelState:
    guild_id: int
    channel_id: int
    players: set[int] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    round_task: asyncio.Task | None = None
    round_text_channel_id: int | None = None
    chaos_task: asyncio.Task | None = None
    chaos_text_channel_id: int | None = None
    chaos_min_delay: float = 0.0
    chaos_max_delay: float = 0.0
    cooldown_until: float = 0.0

    @property
    def key(self) -> ChannelKey:
        return (self.guild_id, self.channel_id)

    @property
    def round_active(self) -> bool:
        return self.round_task is not None and not self.round_task.done()

    @property
    def chaos_active(self) -> bool:
        return self.chaos_task is not None and not self.chaos_task.done()


class RouletteState:
    """Opt-ins and per-channel bookkeeping. Pure data; no Discord calls."""

    def __init__(self) -> None:
        self._channels: dict[ChannelKey, ChannelState] = {}

    def get(self, guild_id: int, channel_id: int) -> ChannelState:
        key = (guild_id, channel_id)
        st = self._channels.get(key)
        if st is None:
            st = self._channels[key] = ChannelState(guild_id, channel_id)
        return st

    def peek(self, guild_id: int, channel_id: int) -> ChannelState | None:
        return self._channels.get((guild_id, channel_id))

    def guild_ids(self) -> set[int]:
        return {g for g, _ in self._channels}

    def channels_in_guild(self, guild_id: int) -> list[ChannelState]:
        return [st for (g, _), st in self._channels.items() if g == guild_id]

    def opt_in(self, guild_id: int, channel_id: int, user_id: int) -> bool:
        """Opt a user in for one channel. Returns False if already opted in.

        A user can only sit in one voice channel per guild, so any opt-in for
        another channel in the same guild is dropped.
        """
        for st in self.channels_in_guild(guild_id):
            if st.channel_id != channel_id:
                st.players.discard(user_id)
        st = self.get(guild_id, channel_id)
        if user_id in st.players:
            return False
        st.players.add(user_id)
        return True

    def opt_out(self, guild_id: int, user_id: int, channel_id: int | None = None) -> list[int]:
        """Remove a user's opt-in. Returns the channel ids they were removed from."""
        removed = []
        for st in self.channels_in_guild(guild_id):
            if channel_id is not None and st.channel_id != channel_id:
                continue
            if user_id in st.players:
                st.players.discard(user_id)
                removed.append(st.channel_id)
        return removed

    def is_opted_in(self, guild_id: int, channel_id: int, user_id: int) -> bool:
        st = self.peek(guild_id, channel_id)
        return st is not None and user_id in st.players

    def players(self, guild_id: int, channel_id: int) -> frozenset[int]:
        st = self.peek(guild_id, channel_id)
        return frozenset(st.players) if st else frozenset()

    def drop_channel(self, guild_id: int, channel_id: int) -> ChannelState | None:
        return self._channels.pop((guild_id, channel_id), None)

    def drop_guild(self, guild_id: int) -> list[ChannelState]:
        dropped = self.channels_in_guild(guild_id)
        for st in dropped:
            del self._channels[st.key]
        return dropped
