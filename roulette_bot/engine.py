"""Round and chaos-mode logic, independent of Discord.

The engine talks to Discord only through the small ``Backend`` protocol, which
keeps it testable with a fake backend and fake sleep/clock.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from roulette_bot import content
from roulette_bot.state import ChannelState, RouletteState

log = logging.getLogger(__name__)


class Backend(Protocol):
    async def voice_member_ids(self, guild_id: int, channel_id: int) -> set[int] | None:
        """IDs of human (non-bot) members in the voice channel, or None if unavailable."""

    async def can_disconnect(self, guild_id: int, channel_id: int) -> bool:
        """Whether the bot may disconnect members from this voice channel."""

    async def disconnect(self, guild_id: int, channel_id: int, user_id: int) -> bool:
        """Disconnect a member who is in the channel. Returns True on success."""

    async def send(
        self,
        guild_id: int,
        text_channel_id: int,
        content: str,
        *,
        mention_ids: Iterable[int] = (),
        kind: str = "info",
    ) -> bool:
        """Post a message, pinging only ``mention_ids``. Returns True on success."""

    def display_name(self, guild_id: int, user_id: int) -> str:
        """A mention-safe, markdown-escaped display name."""


@dataclass(frozen=True)
class RouletteConfig:
    min_players: int = 2
    countdown_steps: int = 3
    countdown_step_seconds: float = 1.5
    fakeout_chance: float = 0.25
    fakeout_pause_seconds: float = 2.5
    last_words_seconds: float = 5.0
    spin_cooldown_seconds: float = 30.0
    chaos_default_min_seconds: int = 120
    chaos_default_max_seconds: int = 600
    chaos_floor_seconds: int = 30
    chaos_ceiling_seconds: int = 3600


class SpinStatus(enum.Enum):
    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    COOLDOWN = "cooldown"
    NOT_ENOUGH_PLAYERS = "not_enough_players"
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    MISSING_PERMISSIONS = "missing_permissions"


@dataclass(frozen=True)
class SpinResult:
    status: SpinStatus
    eligible_count: int = 0
    retry_after: float = 0.0


class ChaosStatus(enum.Enum):
    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    INVALID_DELAYS = "invalid_delays"
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    MISSING_PERMISSIONS = "missing_permissions"


@dataclass(frozen=True)
class StopResult:
    stopped_chaos: bool
    cancelled_round: bool

    @property
    def stopped_anything(self) -> bool:
        return self.stopped_chaos or self.cancelled_round


class RoundAborted(Exception):
    """Internal: the round cannot continue (e.g. an announcement failed)."""


class RouletteEngine:
    def __init__(
        self,
        state: RouletteState,
        backend: Backend,
        config: RouletteConfig | None = None,
        *,
        selection_rng: random.Random | None = None,
        flavor_rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.state = state
        self.backend = backend
        self.config = config or RouletteConfig()
        # Selection uses its own RNG (OS entropy by default) so that flavor
        # choices such as fake-outs can never influence who is picked.
        self._selection_rng = selection_rng or random.SystemRandom()
        self._flavor_rng = flavor_rng or random.Random()
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic

    # ----------------------------------------------------------- eligibility

    def opt_in(self, guild_id: int, channel_id: int, user_id: int) -> bool:
        return self.state.opt_in(guild_id, channel_id, user_id)

    def opt_out(self, guild_id: int, user_id: int) -> list[int]:
        return self.state.opt_out(guild_id, user_id)

    def on_voice_move(
        self, guild_id: int, user_id: int, before_channel_id: int | None, after_channel_id: int | None
    ) -> list[int]:
        """Leaving or switching voice channels clears the user's opt-in."""
        if before_channel_id is None or before_channel_id == after_channel_id:
            return []
        return self.state.opt_out(guild_id, user_id, channel_id=before_channel_id)

    async def eligible_players(self, guild_id: int, channel_id: int) -> list[int] | None:
        """Opted-in humans currently in the channel (sorted), or None if the channel is gone.

        Stale opt-ins (users no longer in the channel) are cleared as a safety
        net in case a voice event was missed.
        """
        in_voice = await self.backend.voice_member_ids(guild_id, channel_id)
        if in_voice is None:
            return None
        st = self.state.peek(guild_id, channel_id)
        if st is None:
            return []
        st.players.intersection_update(in_voice)
        return sorted(st.players)

    async def _candidates(self, guild_id: int, channel_id: int, everyone: bool) -> list[int] | None:
        """Who can be picked: opted-in players, or every human in the channel for spin-all."""
        if not everyone:
            return await self.eligible_players(guild_id, channel_id)
        in_voice = await self.backend.voice_member_ids(guild_id, channel_id)
        return None if in_voice is None else sorted(in_voice)

    def pick_target(self, eligible: list[int]) -> int:
        """Uniformly random choice among eligible players."""
        return self._selection_rng.choice(sorted(eligible))

    # ----------------------------------------------------------------- spins

    async def start_spin(
        self,
        guild_id: int,
        channel_id: int,
        text_channel_id: int,
        *,
        respect_cooldown: bool = True,
        everyone: bool = False,
    ) -> SpinResult:
        """Start a round. With ``everyone=True`` every human in the channel is in
        the draw, whether or not they opted in (``/roulette spin-all``)."""
        st = self.state.get(guild_id, channel_id)
        # The lock makes check-then-start atomic, so two simultaneous /spin
        # calls can never both start a round.
        async with st.lock:
            if st.round_active:
                return SpinResult(SpinStatus.ALREADY_RUNNING)
            remaining = st.cooldown_until - self._clock()
            if respect_cooldown and remaining > 0:
                return SpinResult(SpinStatus.COOLDOWN, retry_after=remaining)
            eligible = await self._candidates(guild_id, channel_id, everyone)
            if eligible is None:
                return SpinResult(SpinStatus.CHANNEL_UNAVAILABLE)
            if len(eligible) < self.config.min_players:
                return SpinResult(SpinStatus.NOT_ENOUGH_PLAYERS, eligible_count=len(eligible))
            if not await self.backend.can_disconnect(guild_id, channel_id):
                return SpinResult(SpinStatus.MISSING_PERMISSIONS, eligible_count=len(eligible))
            st.round_text_channel_id = text_channel_id
            st.round_task = asyncio.create_task(
                self._run_round(st, text_channel_id, everyone), name=f"roulette-round-{guild_id}-{channel_id}"
            )
            return SpinResult(SpinStatus.STARTED, eligible_count=len(eligible))

    async def _say(self, st: ChannelState, text_channel_id: int, text: str, *,
                   mention_ids: Iterable[int] = (), kind: str = "info", required: bool = True) -> None:
        ok = await self.backend.send(st.guild_id, text_channel_id, text, mention_ids=tuple(mention_ids), kind=kind)
        if not ok and required:
            raise RoundAborted(f"could not send {kind} message")

    async def _run_round(self, st: ChannelState, text_channel_id: int, everyone: bool = False) -> None:
        g, c, cfg, fx = st.guild_id, st.channel_id, self.config, self._flavor_rng
        try:
            await self._say(st, text_channel_id, fx.choice(content.COUNTDOWN_INTROS), kind="intro")
            for n in range(cfg.countdown_steps, 0, -1):
                await self._sleep(cfg.countdown_step_seconds)
                await self._say(st, text_channel_id, fx.choice(content.COUNTDOWN_STEPS).format(n=n), kind="countdown")
            await self._sleep(cfg.countdown_step_seconds)

            # Selection happens after the countdown, so anyone who opted out or
            # left during it is excluded.
            eligible = await self._candidates(g, c, everyone)
            if eligible is None:
                await self._say(st, text_channel_id, "🚫 The voice channel vanished. Spin cancelled.",
                                kind="cancelled", required=False)
                return
            if len(eligible) < cfg.min_players:
                await self._say(st, text_channel_id,
                                f"🚫 Not enough brave souls left (need {cfg.min_players}). Spin cancelled.",
                                kind="cancelled", required=False)
                return

            target = self.pick_target(eligible)

            # Fake-outs are pure theater: the target is already decided above.
            decoys = [u for u in eligible if u != target]
            if decoys and fx.random() < cfg.fakeout_chance:
                decoy_name = self.backend.display_name(g, fx.choice(decoys))
                await self._say(st, text_channel_id, fx.choice(content.FAKEOUT_ANNOUNCEMENTS).format(target=decoy_name),
                                kind="fakeout")
                await self._sleep(cfg.fakeout_pause_seconds)
                await self._say(st, text_channel_id, fx.choice(content.FAKEOUT_REVEALS).format(target=decoy_name),
                                kind="fakeout_reveal")
                await self._sleep(cfg.countdown_step_seconds)

            mention = f"<@{target}>"
            await self._say(st, text_channel_id,
                            fx.choice(content.REAL_ANNOUNCEMENTS).format(target=mention, reason=fx.choice(content.REASONS)),
                            mention_ids=(target,), kind="reveal")
            await self._say(st, text_channel_id,
                            fx.choice(content.LAST_WORDS).format(target=mention, n=f"{cfg.last_words_seconds:g}"),
                            mention_ids=(), kind="last_words")
            await self._sleep(cfg.last_words_seconds)

            # Final recheck right before disconnecting.
            name = self.backend.display_name(g, target)
            in_voice = await self.backend.voice_member_ids(g, c)
            still_playing = everyone or self.state.is_opted_in(g, c, target)
            if not still_playing or in_voice is None or target not in in_voice:
                await self._say(st, text_channel_id, fx.choice(content.CANCELLED_TARGET_GONE).format(target=f"**{name}**"),
                                kind="cancelled", required=False)
                return
            if not await self.backend.can_disconnect(g, c):
                await self._say(st, text_channel_id,
                                "🔒 I no longer have **Move Members** permission in that voice channel. Spin cancelled.",
                                kind="cancelled", required=False)
                return
            if await self.backend.disconnect(g, c, target):
                # Disconnecting also clears their opt-in via the voice event;
                # do it here too so state is consistent immediately.
                self.state.opt_out(g, target, channel_id=c)
                await self._say(st, text_channel_id, fx.choice(content.KICKED).format(target=f"**{name}**"),
                                kind="kicked", required=False)
            else:
                await self._say(st, text_channel_id,
                                f"⚠️ I tried to disconnect **{name}** but Discord said no. They survive... this time.",
                                kind="error", required=False)
        except asyncio.CancelledError:
            log.info("Round in %s/%s cancelled", g, c)
            raise
        except RoundAborted as exc:
            log.warning("Round in %s/%s aborted: %s", g, c, exc)
        except Exception:
            log.exception("Round in %s/%s crashed", g, c)
            try:
                await self._say(st, text_channel_id, "⚠️ The wheel jammed (internal error). Spin cancelled.",
                                kind="error", required=False)
            except Exception:
                pass
        finally:
            st.cooldown_until = self._clock() + cfg.spin_cooldown_seconds
            if st.round_task is asyncio.current_task():
                st.round_task = None
                st.round_text_channel_id = None

    # ----------------------------------------------------------------- chaos

    def validate_chaos_delays(self, min_delay: float, max_delay: float) -> bool:
        cfg = self.config
        return cfg.chaos_floor_seconds <= min_delay <= max_delay <= cfg.chaos_ceiling_seconds

    async def start_chaos(
        self,
        guild_id: int,
        channel_id: int,
        text_channel_id: int,
        min_delay: float | None = None,
        max_delay: float | None = None,
    ) -> ChaosStatus:
        cfg = self.config
        min_delay = cfg.chaos_default_min_seconds if min_delay is None else min_delay
        max_delay = cfg.chaos_default_max_seconds if max_delay is None else max_delay
        if not self.validate_chaos_delays(min_delay, max_delay):
            return ChaosStatus.INVALID_DELAYS
        st = self.state.get(guild_id, channel_id)
        async with st.lock:
            if st.chaos_active:
                return ChaosStatus.ALREADY_RUNNING
            if await self.backend.voice_member_ids(guild_id, channel_id) is None:
                return ChaosStatus.CHANNEL_UNAVAILABLE
            if not await self.backend.can_disconnect(guild_id, channel_id):
                return ChaosStatus.MISSING_PERMISSIONS
            st.chaos_text_channel_id = text_channel_id
            st.chaos_min_delay, st.chaos_max_delay = min_delay, max_delay
            st.chaos_task = asyncio.create_task(self._chaos_loop(st), name=f"roulette-chaos-{guild_id}-{channel_id}")
            return ChaosStatus.STARTED

    async def _chaos_loop(self, st: ChannelState) -> None:
        try:
            while True:
                await self._sleep(self._flavor_rng.uniform(st.chaos_min_delay, st.chaos_max_delay))
                text_channel_id = st.chaos_text_channel_id
                if text_channel_id is None:
                    return
                try:
                    result = await self.start_spin(st.guild_id, st.channel_id, text_channel_id, respect_cooldown=False)
                except Exception:
                    log.exception("Chaos spin failed to start in %s/%s", st.guild_id, st.channel_id)
                    continue
                if result.status is SpinStatus.STARTED and st.round_task is not None:
                    # Wait for the round so chaos never overlaps itself.
                    # stop_chaos() cancels the round and this loop separately.
                    await asyncio.wait([st.round_task])
                elif result.status is SpinStatus.CHANNEL_UNAVAILABLE:
                    log.info("Chaos in %s/%s stopping: channel unavailable", st.guild_id, st.channel_id)
                    return
                else:
                    log.debug("Chaos skipped a spin in %s/%s: %s", st.guild_id, st.channel_id, result.status)
        except asyncio.CancelledError:
            log.info("Chaos in %s/%s cancelled", st.guild_id, st.channel_id)
            raise
        finally:
            if st.chaos_task is asyncio.current_task():
                st.chaos_task = None
                st.chaos_text_channel_id = None

    def stop_chaos(self, guild_id: int, channel_id: int) -> StopResult:
        """Stop chaos mode and cancel any pending countdown or disconnect in the channel."""
        st = self.state.peek(guild_id, channel_id)
        if st is None:
            return StopResult(False, False)
        stopped_chaos = st.chaos_active
        cancelled_round = st.round_active
        if st.chaos_task is not None:
            st.chaos_task.cancel()
        if st.round_task is not None:
            st.round_task.cancel()
        st.chaos_task = st.round_task = None
        st.chaos_text_channel_id = st.round_text_channel_id = None
        return StopResult(stopped_chaos, cancelled_round)

    # --------------------------------------------------------------- cleanup

    def forget_channel(self, guild_id: int, channel_id: int) -> None:
        self.stop_chaos(guild_id, channel_id)
        self.state.drop_channel(guild_id, channel_id)

    def forget_guild(self, guild_id: int) -> None:
        for st in self.state.drop_guild(guild_id):
            for task in (st.chaos_task, st.round_task):
                if task is not None:
                    task.cancel()

    def shutdown(self) -> None:
        for guild_id in self.state.guild_ids():
            self.forget_guild(guild_id)
