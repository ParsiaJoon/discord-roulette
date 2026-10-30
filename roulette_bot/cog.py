"""Slash commands: /roulette join|leave|players|spin|help and /roulette chaos start|stop."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from roulette_bot.discord_backend import DiscordBackend, VoiceLike
from roulette_bot.engine import ChaosStatus, RouletteEngine, SpinResult, SpinStatus
from roulette_bot.state import RouletteState

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()


def _current_voice(member: discord.abc.User) -> VoiceLike | None:
    voice = getattr(member, "voice", None)
    channel = voice.channel if voice else None
    return channel if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)) else None


def _can_post_here(interaction: discord.Interaction) -> bool:
    perms = interaction.app_permissions
    if isinstance(interaction.channel, discord.Thread):
        return perms.view_channel and perms.send_messages_in_threads
    return perms.view_channel and perms.send_messages


async def _reply(interaction: discord.Interaction, text: str, *, ephemeral: bool = True) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=ephemeral, allowed_mentions=NO_PINGS)
    else:
        await interaction.response.send_message(text, ephemeral=ephemeral, allowed_mentions=NO_PINGS)


HELP_TEXT = """\
🎰 **Discord Roulette** — opt-in voice-channel roulette. "Kicked" means *disconnected from voice*; \
nobody is ever kicked or banned from the server.

**Commands**
• `/roulette join` — opt in for the voice channel you're in.
• `/roulette leave` — opt out immediately (works mid-countdown).
• `/roulette players` — list opted-in players in your voice channel.
• `/roulette spin` — dramatic countdown, then one random opted-in player is disconnected after 5 seconds of last words.
• `/roulette spin-all` — same as spin, but **everyone** in your voice channel is in the draw, opted in or not (bots excluded).
• `/roulette chaos start [min_delay] [max_delay]` — random spins at unpredictable intervals \
(needs **Manage Channels**). Delays are in seconds ({floor}–{ceiling}, default {dmin}–{dmax}).
• `/roulette chaos stop` — stop chaos mode and cancel any pending spin. Anyone in that voice channel, or a moderator, can use it.
• `/roulette help` — this message.

**Rules**
• For `/roulette spin`, only humans who opted in *and* are currently in the voice channel can be picked. At least {min_players} are needed.
• Leaving or switching voice channels clears your opt-in. Rejoining means opting in again.
• You must be in the voice channel to join or spin. One spin at a time per channel, with a {cooldown:g}s cooldown.
• The target is re-checked right before disconnecting: if they opted out or left, the spin is cancelled.
• Fake-outs are just theater: the real target is picked fairly before any fake-out happens.
• Everything is in memory. Restarting the bot resets all opt-ins and stops chaos mode.
"""


@app_commands.guild_only()
class RouletteCog(commands.GroupCog, group_name="roulette", group_description="Opt-in voice channel roulette"):
    chaos = app_commands.Group(name="chaos", description="Random spins at unpredictable intervals")

    def __init__(self, bot: commands.Bot, engine: RouletteEngine | None = None) -> None:
        self.bot = bot
        self.engine = engine or RouletteEngine(RouletteState(), DiscordBackend(bot))
        super().__init__()

    async def cog_unload(self) -> None:
        self.engine.shutdown()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Log every command that reaches the bot, so it's clear whether a
        # member's command arrived at all or was blocked by Discord.
        command = interaction.command.qualified_name if interaction.command else "?"
        voice = _current_voice(interaction.user)
        log.info("/%s used by %s (%s) in #%s; voice: %s", command, interaction.user, interaction.user.id,
                 getattr(interaction.channel, "name", interaction.channel_id), voice.name if voice else "none")
        return True

    # ------------------------------------------------------------- commands

    @app_commands.command(name="join", description="Opt in to roulette for your current voice channel")
    async def join(self, interaction: discord.Interaction) -> None:
        member, guild = interaction.user, interaction.guild
        assert guild is not None
        channel = _current_voice(member)
        if channel is None:
            return await _reply(interaction, "🔇 Join a voice channel first, then opt in.")
        if member.bot:
            return await _reply(interaction, "🤖 Bots can't play roulette.")
        if not self.engine.opt_in(guild.id, channel.id, member.id):
            return await _reply(interaction, f"You're already in the roulette for {channel.mention}.")
        count = len(self.engine.state.players(guild.id, channel.id))
        warning = ""
        if not await self.engine.backend.can_disconnect(guild.id, channel.id):
            warning = "\n⚠️ I don't have **Move Members** in that voice channel, so spins won't work there yet."
        await _reply(
            interaction,
            f"🎰 {member.mention} has entered the roulette for {channel.mention}. "
            f"Brave souls: **{count}**.{warning}",
            ephemeral=False,
        )

    @app_commands.command(name="leave", description="Opt out of roulette immediately")
    async def leave(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        if not self.engine.opt_out(interaction.guild.id, interaction.user.id):
            return await _reply(interaction, "You weren't opted in. You're safe. For now.")
        await _reply(interaction, f"🐔 {interaction.user.mention} has left the roulette. Coward. (Respectfully.)",
                     ephemeral=False)

    @app_commands.command(name="players", description="List opted-in players in your voice channel")
    async def players(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        channel = _current_voice(interaction.user)
        if channel is None:
            return await _reply(interaction, "🔇 Join a voice channel to see its roulette players.")
        eligible = await self.engine.eligible_players(guild.id, channel.id) or []
        if not eligible:
            return await _reply(interaction, f"Nobody has opted in for {channel.mention} yet. Use `/roulette join`.",
                                ephemeral=False)
        names = "\n".join(f"• {self.engine.backend.display_name(guild.id, uid)}" for uid in eligible)
        need = self.engine.config.min_players
        status = "✅ Ready to spin." if len(eligible) >= need else f"⏳ Need at least {need} to spin."
        await _reply(interaction, f"🎰 **Roulette players in {channel.mention}** ({len(eligible)}):\n{names}\n{status}",
                     ephemeral=False)

    @app_commands.command(name="spin", description="Spin the wheel: one random opted-in player gets disconnected")
    async def spin(self, interaction: discord.Interaction) -> None:
        await self._start_spin(interaction, everyone=False)

    @app_commands.command(name="spin-all",
                          description="Spin with EVERYONE in your voice channel (no opt-in needed, bots excluded)")
    async def spin_all(self, interaction: discord.Interaction) -> None:
        await self._start_spin(interaction, everyone=True)

    async def _start_spin(self, interaction: discord.Interaction, *, everyone: bool) -> None:
        guild = interaction.guild
        assert guild is not None and interaction.channel_id is not None
        channel = _current_voice(interaction.user)
        if channel is None:
            return await _reply(interaction, "🔇 You must be in the voice channel to spin.")
        if not _can_post_here(interaction):
            return await _reply(interaction, "I can't send messages in this text channel. Try another one.")
        result = await self.engine.start_spin(guild.id, channel.id, interaction.channel_id, everyone=everyone)
        await _reply(interaction, self._spin_message(result, interaction, channel, everyone),
                     ephemeral=result.status is not SpinStatus.STARTED)

    def _spin_message(self, result: SpinResult, interaction: discord.Interaction, channel: VoiceLike,
                      everyone: bool) -> str:
        match result.status:
            case SpinStatus.STARTED if everyone:
                return (f"☢️ {interaction.user.mention} pulled the **SPIN-ALL** lever for {channel.mention}! "
                        f"Nobody is safe. **{result.eligible_count}** people are in the wheel...")
            case SpinStatus.STARTED:
                return (f"🎰 {interaction.user.mention} pulled the lever for {channel.mention}! "
                        f"**{result.eligible_count}** players are in the wheel...")
            case SpinStatus.ALREADY_RUNNING:
                return "⏳ A spin is already in progress for that channel."
            case SpinStatus.COOLDOWN:
                return f"🧊 The wheel is cooling down. Try again in {result.retry_after:.0f}s."
            case SpinStatus.NOT_ENOUGH_PLAYERS if everyone:
                return (f"Need at least {self.engine.config.min_players} people (not counting bots) in "
                        f"{channel.mention} to spin-all (currently {result.eligible_count}).")
            case SpinStatus.NOT_ENOUGH_PLAYERS:
                return (f"Need at least {self.engine.config.min_players} opted-in players in {channel.mention} "
                        f"(currently {result.eligible_count}). Use `/roulette join`.")
            case SpinStatus.CHANNEL_UNAVAILABLE:
                return "I can't see that voice channel."
            case SpinStatus.MISSING_PERMISSIONS:
                return f"🔒 I need **Move Members** (and View Channel) in {channel.mention} to run roulette there."
        return "Something odd happened."

    @app_commands.command(name="help", description="Explain roulette commands and rules")
    async def help(self, interaction: discord.Interaction) -> None:
        cfg = self.engine.config
        await _reply(interaction, HELP_TEXT.format(
            floor=cfg.chaos_floor_seconds, ceiling=cfg.chaos_ceiling_seconds,
            dmin=cfg.chaos_default_min_seconds, dmax=cfg.chaos_default_max_seconds,
            min_players=cfg.min_players, cooldown=cfg.spin_cooldown_seconds,
        ))

    @chaos.command(name="start", description="Start random spins at unpredictable intervals (Manage Channels)")
    @app_commands.describe(
        min_delay="Minimum seconds between spins",
        max_delay="Maximum seconds between spins",
        channel="Voice channel (defaults to the one you're in)",
    )
    async def chaos_start(
        self,
        interaction: discord.Interaction,
        min_delay: app_commands.Range[int, 30, 3600] | None = None,
        max_delay: app_commands.Range[int, 30, 3600] | None = None,
        channel: discord.VoiceChannel | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None and interaction.channel_id is not None
        target = channel or _current_voice(interaction.user)
        if target is None:
            return await _reply(interaction, "Join a voice channel or pass `channel:` to choose one.")
        if not target.permissions_for(interaction.user).manage_channels:
            return await _reply(interaction, "🔒 Only members with **Manage Channels** can start chaos mode.")
        if not _can_post_here(interaction):
            return await _reply(interaction, "I can't send messages in this text channel. Try another one.")
        cfg = self.engine.config
        lo = cfg.chaos_default_min_seconds if min_delay is None else min_delay
        hi = cfg.chaos_default_max_seconds if max_delay is None else max_delay
        if min_delay is not None and max_delay is None and lo > hi:
            hi = lo
        if max_delay is not None and min_delay is None and lo > hi:
            lo = hi
        status = await self.engine.start_chaos(guild.id, target.id, interaction.channel_id, lo, hi)
        match status:
            case ChaosStatus.STARTED:
                await _reply(interaction,
                             f"🌪️ **CHAOS MODE ENGAGED** for {target.mention}. A spin will happen randomly every "
                             f"{lo}–{hi} seconds. Opted-in players, stay nervous. `/roulette chaos stop` ends it.",
                             ephemeral=False)
            case ChaosStatus.ALREADY_RUNNING:
                await _reply(interaction, f"Chaos mode is already running for {target.mention}.")
            case ChaosStatus.INVALID_DELAYS:
                await _reply(interaction, f"Delays must satisfy {cfg.chaos_floor_seconds} ≤ min ≤ max ≤ "
                                          f"{cfg.chaos_ceiling_seconds} seconds.")
            case ChaosStatus.CHANNEL_UNAVAILABLE:
                await _reply(interaction, "I can't see that voice channel.")
            case ChaosStatus.MISSING_PERMISSIONS:
                await _reply(interaction, f"🔒 I need **Move Members** (and View Channel) in {target.mention}.")

    @chaos.command(name="stop", description="Stop chaos mode and cancel any pending spin")
    @app_commands.describe(channel="Voice channel (defaults to the one you're in)")
    async def chaos_stop(self, interaction: discord.Interaction, channel: discord.VoiceChannel | None = None) -> None:
        guild = interaction.guild
        assert guild is not None
        member = interaction.user
        current = _current_voice(member)
        target = channel or current
        if target is None:
            return await _reply(interaction, "Join a voice channel or pass `channel:` to choose one.")
        is_participant = (current is not None and current.id == target.id) or \
            self.engine.state.is_opted_in(guild.id, target.id, member.id)
        is_moderator = target.permissions_for(member).manage_channels
        if not (is_participant or is_moderator):
            return await _reply(interaction, "🔒 Only people in that voice channel, or moderators, can stop chaos there.")
        result = self.engine.stop_chaos(guild.id, target.id)
        if not result.stopped_anything:
            return await _reply(interaction, f"Nothing to stop in {target.mention}.")
        parts = []
        if result.stopped_chaos:
            parts.append("chaos mode is off")
        if result.cancelled_round:
            parts.append("the pending spin was cancelled")
        await _reply(interaction, f"🛑 {member.mention} pulled the emergency brake: {' and '.join(parts)} "
                                  f"for {target.mention}.", ephemeral=False)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.exception("Command error", exc_info=error)
        try:
            await _reply(interaction, "⚠️ Something went wrong running that command.")
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------- listeners

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        before_id = before.channel.id if before.channel else None
        after_id = after.channel.id if after.channel else None
        self.engine.on_voice_move(member.guild.id, member.id, before_id, after_id)
        if before_id is None or before_id == after_id:
            return
        # If the channel is now empty of humans, stop chaos so it doesn't run forever.
        st = self.engine.state.peek(member.guild.id, before_id)
        if st is None or not (st.chaos_active or st.round_active):
            return
        humans = await self.engine.backend.voice_member_ids(member.guild.id, before_id)
        if humans:
            return
        text_channel_id = st.chaos_text_channel_id or st.round_text_channel_id
        result = self.engine.stop_chaos(member.guild.id, before_id)
        if result.stopped_anything and text_channel_id is not None:
            await self.engine.backend.send(member.guild.id, text_channel_id,
                                           "🌙 Everyone left the voice channel, so roulette chaos has stopped.",
                                           kind="info")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        self.engine.forget_channel(channel.guild.id, channel.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.engine.forget_guild(guild.id)
