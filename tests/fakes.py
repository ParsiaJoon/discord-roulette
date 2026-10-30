from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from roulette_bot.engine import RouletteConfig, RouletteEngine
from roulette_bot.state import RouletteState


@dataclass
class Sent:
    guild_id: int
    text_channel_id: int
    content: str
    mention_ids: tuple[int, ...]
    kind: str


class FakeBackend:
    """In-memory stand-in for Discord. voice[(guild, channel)] holds human member ids."""

    def __init__(self) -> None:
        self.voice: dict[tuple[int, int], set[int]] = {}
        self.can_move: dict[tuple[int, int], bool] = {}
        self.disconnect_fails = False
        self.sent: list[Sent] = []
        self.disconnected: list[tuple[int, int, int]] = []
        self.on_send: Callable[[Sent], object] | None = None
        self.engine: RouletteEngine | None = None

    # helpers -------------------------------------------------------------
    def put(self, guild_id: int, channel_id: int, *user_ids: int) -> None:
        self.voice.setdefault((guild_id, channel_id), set()).update(user_ids)

    def move(self, guild_id: int, user_id: int, before: int | None, after: int | None) -> None:
        """Simulate a voice move, including the gateway event the bot would receive."""
        if before is not None:
            self.voice[(guild_id, before)].discard(user_id)
        if after is not None:
            self.put(guild_id, after, user_id)
        if self.engine is not None:
            self.engine.on_voice_move(guild_id, user_id, before, after)

    def kinds(self) -> list[str]:
        return [m.kind for m in self.sent]

    # Backend protocol ----------------------------------------------------
    async def voice_member_ids(self, guild_id: int, channel_id: int) -> set[int] | None:
        await asyncio.sleep(0)  # yield like real I/O would, to expose races
        members = self.voice.get((guild_id, channel_id))
        return None if members is None else set(members)

    async def can_disconnect(self, guild_id: int, channel_id: int) -> bool:
        return self.can_move.get((guild_id, channel_id), True)

    async def disconnect(self, guild_id: int, channel_id: int, user_id: int) -> bool:
        members = self.voice.get((guild_id, channel_id), set())
        if self.disconnect_fails or user_id not in members:
            return False
        self.disconnected.append((guild_id, channel_id, user_id))
        self.move(guild_id, user_id, channel_id, None)
        return True

    async def send(self, guild_id: int, text_channel_id: int, content: str, *,
                   mention_ids: Iterable[int] = (), kind: str = "info") -> bool:
        msg = Sent(guild_id, text_channel_id, content, tuple(mention_ids), kind)
        self.sent.append(msg)
        if self.on_send is not None:
            result = self.on_send(msg)
            if inspect.isawaitable(result):
                await result
        return True

    def display_name(self, guild_id: int, user_id: int) -> str:
        return f"user{user_id}"


async def instant_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_engine(*, seed: int = 0, fakeout_chance: float = 0.0, cooldown: float = 30.0,
                flavor_seed: int = 1) -> tuple[RouletteEngine, FakeBackend, FakeClock]:
    backend = FakeBackend()
    clock = FakeClock()
    engine = RouletteEngine(
        RouletteState(),
        backend,
        RouletteConfig(fakeout_chance=fakeout_chance, spin_cooldown_seconds=cooldown),
        selection_rng=random.Random(seed),
        flavor_rng=random.Random(flavor_seed),
        sleep=instant_sleep,
        clock=clock,
    )
    backend.engine = engine
    return engine, backend, clock
