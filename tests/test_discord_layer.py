import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from roulette_bot.bot import RouletteBot
from roulette_bot.cog import RouletteCog
from roulette_bot.discord_backend import DiscordBackend


def fake_member(user_id, *, bot=False):
    m = MagicMock(spec=discord.Member)
    m.id, m.bot, m.display_name = user_id, bot, f"@everyone *user{user_id}*"
    m.move_to = AsyncMock()
    return m


class DiscordBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.members = {1: fake_member(1), 2: fake_member(2), 3: fake_member(3, bot=True)}
        self.channel = MagicMock(spec=discord.VoiceChannel)
        self.channel.voice_states = {1: object(), 2: object(), 3: object()}
        self.guild = MagicMock(spec=discord.Guild)
        self.guild.get_channel.return_value = self.channel
        self.guild.get_member.side_effect = self.members.get
        client = MagicMock(spec=discord.Client)
        client.get_guild.return_value = self.guild
        self.backend = DiscordBackend(client)

    async def test_voice_member_ids_excludes_bots(self):
        self.assertEqual(await self.backend.voice_member_ids(1, 10), {1, 2})

    async def test_non_voice_channel_is_unavailable(self):
        self.guild.get_channel.return_value = MagicMock(spec=discord.TextChannel)
        self.assertIsNone(await self.backend.voice_member_ids(1, 10))

    async def test_disconnect_uses_move_to_none_and_handles_forbidden(self):
        self.assertTrue(await self.backend.disconnect(1, 10, 1))
        self.members[1].move_to.assert_awaited_once()
        self.assertIsNone(self.members[1].move_to.await_args.args[0])

        self.members[2].move_to.side_effect = discord.Forbidden(MagicMock(status=403), "nope")
        self.assertFalse(await self.backend.disconnect(1, 10, 2))

    async def test_disconnect_skips_member_not_in_channel(self):
        self.channel.voice_states = {2: object()}
        self.assertFalse(await self.backend.disconnect(1, 10, 1))
        self.members[1].move_to.assert_not_awaited()

    async def test_send_restricts_mentions(self):
        text = MagicMock(spec=discord.TextChannel)
        text.send = AsyncMock()
        self.guild.get_channel_or_thread.return_value = text
        self.assertTrue(await self.backend.send(1, 100, "hi", mention_ids=(5,)))
        allowed = text.send.await_args.kwargs["allowed_mentions"]
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)
        self.assertEqual([u.id for u in allowed.users], [5])

    def test_display_name_is_sanitized(self):
        name = self.backend.display_name(1, 1)
        self.assertNotIn("@everyone", name)
        self.assertNotIn("*user1*", name)


class CommandTreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_commands_are_registered(self):
        bot = RouletteBot()
        await bot.add_cog(RouletteCog(bot))
        group = bot.tree.get_command("roulette")
        self.assertIsNotNone(group)
        self.assertTrue(group.guild_only)
        names = {c.qualified_name for c in group.walk_commands()}
        self.assertTrue({
            "roulette join", "roulette leave", "roulette players", "roulette spin", "roulette spin-all", "roulette help",
            "roulette chaos start", "roulette chaos stop",
        } <= names)
        self.assertFalse(bot.intents.members, "no privileged intents required")
        self.assertFalse(bot.intents.message_content)
        await bot.close()


if __name__ == "__main__":
    unittest.main()
