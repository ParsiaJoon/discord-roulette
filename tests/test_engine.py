import asyncio
import collections
import random
import unittest

from roulette_bot.engine import ChaosStatus, SpinStatus
from tests.fakes import Sent, make_engine

G, G2 = 1, 2          # guilds
VC, VC2 = 10, 11      # voice channels
TXT = 100             # text channel


async def finish_round(engine, guild_id=G, channel_id=VC):
    st = engine.state.peek(guild_id, channel_id)
    if st is not None and st.round_task is not None:
        await asyncio.wait([st.round_task])


async def wait_until(predicate, steps=10_000):
    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


class EligibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_opted_in_members_in_channel_are_eligible(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2, 3)          # 3 is in voice but never opts in
        backend.put(G, VC2, 4)
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 2)
        engine.opt_in(G, VC, 4)              # stale opt-in: 4 is not in VC
        self.assertEqual(await engine.eligible_players(G, VC), [1, 2])
        self.assertFalse(engine.state.is_opted_in(G, VC, 4), "stale opt-ins should be pruned")

    async def test_bots_are_never_eligible(self):
        # The backend only reports humans; the engine must not add anyone else.
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1)
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 999)            # a "bot" id that the backend filters out
        self.assertEqual(await engine.eligible_players(G, VC), [1])

    async def test_requires_two_eligible_players(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2)
        engine.opt_in(G, VC, 1)
        result = await engine.start_spin(G, VC, TXT)
        self.assertEqual(result.status, SpinStatus.NOT_ENOUGH_PLAYERS)
        self.assertEqual(result.eligible_count, 1)

    async def test_unavailable_channel_and_missing_permissions(self):
        engine, backend, _ = make_engine()
        self.assertEqual((await engine.start_spin(G, VC, TXT)).status, SpinStatus.CHANNEL_UNAVAILABLE)
        backend.put(G, VC, 1, 2)
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 2)
        backend.can_move[(G, VC)] = False
        self.assertEqual((await engine.start_spin(G, VC, TXT)).status, SpinStatus.MISSING_PERMISSIONS)

    async def test_full_round_disconnects_one_eligible_player(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2, 3)
        for u in (1, 2):
            engine.opt_in(G, VC, u)
        self.assertEqual((await engine.start_spin(G, VC, TXT)).status, SpinStatus.STARTED)
        await finish_round(engine)
        self.assertEqual(len(backend.disconnected), 1)
        _, _, target = backend.disconnected[0]
        self.assertIn(target, (1, 2))
        self.assertFalse(engine.state.is_opted_in(G, VC, target), "kicked player must opt in again")
        self.assertTrue(all(m.text_channel_id == TXT for m in backend.sent))
        self.assertEqual(backend.kinds()[-1], "kicked")
        reveal = next(m for m in backend.sent if m.kind == "reveal")
        self.assertEqual(reveal.mention_ids, (target,))


class ChannelChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_leaving_or_switching_clears_opt_in(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2)
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 2)
        backend.move(G, 1, VC, VC2)          # switch
        backend.move(G, 2, VC, None)         # leave
        self.assertEqual(engine.state.players(G, VC), frozenset())
        self.assertEqual(engine.state.players(G, VC2), frozenset())
        backend.move(G, 1, VC2, VC)          # come back: still not opted in
        self.assertFalse(engine.state.is_opted_in(G, VC, 1))

    async def test_non_channel_voice_updates_keep_opt_in(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1)
        engine.opt_in(G, VC, 1)
        engine.on_voice_move(G, 1, VC, VC)   # e.g. mute/deafen toggled
        self.assertTrue(engine.state.is_opted_in(G, VC, 1))

    async def test_target_switching_channel_during_last_words_cancels(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2)
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 2)

        def on_send(msg: Sent):
            if msg.kind == "reveal":
                backend.move(G, msg.mention_ids[0], VC, VC2)

        backend.on_send = on_send
        await engine.start_spin(G, VC, TXT)
        await finish_round(engine)
        self.assertEqual(backend.disconnected, [])
        self.assertEqual(backend.kinds()[-1], "cancelled")


class OptOutDuringCountdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_opt_out_during_countdown_excludes_player(self):
        for seed in range(25):
            engine, backend, _ = make_engine(seed=seed)
            backend.put(G, VC, 1, 2, 3)
            for u in (1, 2, 3):
                engine.opt_in(G, VC, u)

            def on_send(msg: Sent):
                if msg.kind == "countdown":
                    engine.opt_out(G, 1)

            backend.on_send = on_send
            await engine.start_spin(G, VC, TXT)
            await finish_round(engine)
            self.assertEqual(len(backend.disconnected), 1)
            self.assertNotEqual(backend.disconnected[0][2], 1, f"seed {seed} picked an opted-out player")

    async def test_opt_out_below_minimum_cancels_round(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2)
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 2)
        backend.on_send = lambda msg: engine.opt_out(G, 2) if msg.kind == "intro" else None
        await engine.start_spin(G, VC, TXT)
        await finish_round(engine)
        self.assertEqual(backend.disconnected, [])
        self.assertNotIn("reveal", backend.kinds())
        self.assertEqual(backend.kinds()[-1], "cancelled")

    async def test_target_opting_out_during_last_words_cancels(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2)
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 2)
        backend.on_send = lambda msg: engine.opt_out(G, msg.mention_ids[0]) if msg.kind == "reveal" else None
        await engine.start_spin(G, VC, TXT)
        await finish_round(engine)
        self.assertEqual(backend.disconnected, [])
        self.assertEqual(backend.kinds()[-1], "cancelled")

    async def test_permission_lost_before_disconnect_cancels(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2)
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 2)
        backend.on_send = lambda msg: backend.can_move.__setitem__((G, VC), False) if msg.kind == "last_words" else None
        await engine.start_spin(G, VC, TXT)
        await finish_round(engine)
        self.assertEqual(backend.disconnected, [])
        self.assertIn("Move Members", backend.sent[-1].content)

    async def test_disconnect_failure_is_reported_not_raised(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2)
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 2)
        backend.disconnect_fails = True
        await engine.start_spin(G, VC, TXT)
        await finish_round(engine)
        self.assertEqual(backend.kinds()[-1], "error")
        self.assertIsNone(engine.state.peek(G, VC).round_task)


class SimultaneousSpinTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_of_simultaneous_spins_starts(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2, 3)
        for u in (1, 2, 3):
            engine.opt_in(G, VC, u)
        results = await asyncio.gather(*(engine.start_spin(G, VC, TXT) for _ in range(5)))
        statuses = collections.Counter(r.status for r in results)
        self.assertEqual(statuses[SpinStatus.STARTED], 1)
        self.assertEqual(statuses[SpinStatus.ALREADY_RUNNING], 4)
        await finish_round(engine)
        self.assertEqual(len(backend.disconnected), 1)

    async def test_cooldown_after_round(self):
        engine, backend, clock = make_engine(cooldown=30)
        backend.put(G, VC, 1, 2, 3)
        for u in (1, 2, 3):
            engine.opt_in(G, VC, u)
        await engine.start_spin(G, VC, TXT)
        await finish_round(engine)
        survivors = [u for u in (1, 2, 3) if (G, VC, u) not in backend.disconnected]
        result = await engine.start_spin(G, VC, TXT)
        self.assertEqual(result.status, SpinStatus.COOLDOWN)
        self.assertAlmostEqual(result.retry_after, 30)
        clock.now += 31
        self.assertEqual(len(survivors), 2)
        self.assertEqual((await engine.start_spin(G, VC, TXT)).status, SpinStatus.STARTED)
        await finish_round(engine)


class FairnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_fakeouts_do_not_change_selection(self):
        async def targets(fakeout_chance):
            picked = []
            engine, backend, _ = make_engine(seed=42, fakeout_chance=fakeout_chance, cooldown=0)
            for _ in range(20):
                backend.put(G, VC, 1, 2, 3, 4)
                for u in (1, 2, 3, 4):
                    engine.opt_in(G, VC, u)
                await engine.start_spin(G, VC, TXT)
                await finish_round(engine)
                picked.append(backend.disconnected[-1][2])
            return picked, backend

        plain, _ = await targets(0.0)
        faked, backend = await targets(1.0)
        self.assertEqual(plain, faked)
        self.assertIn("fakeout", backend.kinds())

    async def test_fakeout_messages_never_mention_anyone(self):
        engine, backend, _ = make_engine(fakeout_chance=1.0)
        backend.put(G, VC, 1, 2)
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 2)
        await engine.start_spin(G, VC, TXT)
        await finish_round(engine)
        fakes = [m for m in backend.sent if m.kind.startswith("fakeout")]
        self.assertEqual(len(fakes), 2)
        for m in fakes:
            self.assertEqual(m.mention_ids, ())
            self.assertNotIn("<@", m.content)
        decoy_msg = fakes[0].content
        target = backend.disconnected[0][2]
        self.assertNotIn(f"user{target}", decoy_msg, "the decoy must not be the real target")

    async def test_selection_is_roughly_uniform(self):
        engine, _, _ = make_engine()
        engine._selection_rng = random.Random(7)
        counts = collections.Counter(engine.pick_target([5, 1, 3, 2]) for _ in range(8000))
        self.assertEqual(set(counts), {1, 2, 3, 5})
        for c in counts.values():
            self.assertAlmostEqual(c / 8000, 0.25, delta=0.03)


class ChaosTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine, self.backend, _ = make_engine()
        self.backend.put(G, VC, 1, 2, 3)
        for u in (1, 2, 3):
            self.engine.opt_in(G, VC, u)

    async def asyncTearDown(self):
        self.engine.shutdown()
        await asyncio.sleep(0)

    async def test_chaos_runs_spins_and_only_one_timer_per_channel(self):
        self.assertEqual(await self.engine.start_chaos(G, VC, TXT, 30, 60), ChaosStatus.STARTED)
        self.assertEqual(await self.engine.start_chaos(G, VC, TXT, 30, 60), ChaosStatus.ALREADY_RUNNING)
        await wait_until(lambda: self.backend.disconnected)
        self.assertTrue(all(m.text_channel_id == TXT for m in self.backend.sent))
        result = self.engine.stop_chaos(G, VC)
        self.assertTrue(result.stopped_chaos)

    async def test_invalid_delays_rejected(self):
        self.assertEqual(await self.engine.start_chaos(G, VC, TXT, 10, 60), ChaosStatus.INVALID_DELAYS)
        self.assertEqual(await self.engine.start_chaos(G, VC, TXT, 120, 60), ChaosStatus.INVALID_DELAYS)
        self.assertEqual(await self.engine.start_chaos(G, VC, TXT, 30, 99999), ChaosStatus.INVALID_DELAYS)

    async def test_stop_during_countdown_cancels_everything(self):
        stopped = []

        def on_send(msg: Sent):
            if msg.kind == "countdown" and not stopped:
                stopped.append(self.engine.stop_chaos(G, VC))

        self.backend.on_send = on_send
        await self.engine.start_chaos(G, VC, TXT, 30, 30)
        await wait_until(lambda: stopped)
        for _ in range(200):
            await asyncio.sleep(0)
        st = self.engine.state.peek(G, VC)
        self.assertTrue(stopped[0].stopped_chaos)
        self.assertTrue(stopped[0].cancelled_round)
        self.assertFalse(st.chaos_active)
        self.assertFalse(st.round_active)
        self.assertEqual(self.backend.disconnected, [])
        self.assertNotIn("reveal", self.backend.kinds())

    async def test_stop_during_last_words_prevents_disconnect(self):
        stopped = []

        def on_send(msg: Sent):
            if msg.kind == "last_words" and not stopped:
                stopped.append(self.engine.stop_chaos(G, VC))

        self.backend.on_send = on_send
        await self.engine.start_chaos(G, VC, TXT, 30, 30)
        await wait_until(lambda: stopped)
        for _ in range(200):
            await asyncio.sleep(0)
        self.assertEqual(self.backend.disconnected, [])
        self.assertNotIn("kicked", self.backend.kinds())

    async def test_stop_also_cancels_a_manual_spin(self):
        self.backend.on_send = lambda msg: self.engine.stop_chaos(G, VC) if msg.kind == "intro" else None
        await self.engine.start_spin(G, VC, TXT)
        for _ in range(200):
            await asyncio.sleep(0)
        self.assertEqual(self.backend.disconnected, [])
        self.assertFalse(self.engine.state.peek(G, VC).round_active)

    async def test_stop_with_nothing_running(self):
        self.assertFalse(self.engine.stop_chaos(G, VC).stopped_anything)
        self.assertFalse(self.engine.stop_chaos(G2, 12345).stopped_anything)


class SpinAllTests(unittest.IsolatedAsyncioTestCase):
    async def test_everyone_in_channel_is_eligible_without_opt_in(self):
        picked = set()
        for seed in range(40):
            engine, backend, _ = make_engine(seed=seed)
            backend.put(G, VC, 1, 2, 3)      # nobody opted in
            backend.put(G, VC2, 4)           # different channel: never picked
            result = await engine.start_spin(G, VC, TXT, everyone=True)
            self.assertEqual(result.status, SpinStatus.STARTED)
            self.assertEqual(result.eligible_count, 3)
            await finish_round(engine)
            self.assertEqual(len(backend.disconnected), 1)
            picked.add(backend.disconnected[0][2])
        self.assertEqual(picked, {1, 2, 3})

    async def test_regular_spin_still_requires_opt_in(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2, 3)
        self.assertEqual((await engine.start_spin(G, VC, TXT)).status, SpinStatus.NOT_ENOUGH_PLAYERS)

    async def test_needs_two_people(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1)
        result = await engine.start_spin(G, VC, TXT, everyone=True)
        self.assertEqual(result.status, SpinStatus.NOT_ENOUGH_PLAYERS)

    async def test_target_leaving_during_last_words_cancels(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2)
        backend.on_send = lambda msg: backend.move(G, msg.mention_ids[0], VC, None) if msg.kind == "reveal" else None
        await engine.start_spin(G, VC, TXT, everyone=True)
        await finish_round(engine)
        self.assertEqual(backend.disconnected, [])
        self.assertEqual(backend.kinds()[-1], "cancelled")

    async def test_opting_out_does_not_protect_from_spin_all(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2)
        engine.opt_in(G, VC, 1)
        backend.on_send = lambda msg: engine.opt_out(G, msg.mention_ids[0]) if msg.kind == "reveal" else None
        await engine.start_spin(G, VC, TXT, everyone=True)
        await finish_round(engine)
        self.assertEqual(len(backend.disconnected), 1)

    async def test_shares_the_one_round_per_channel_limit(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2)
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 2)
        results = await asyncio.gather(engine.start_spin(G, VC, TXT, everyone=True), engine.start_spin(G, VC, TXT))
        self.assertEqual(sorted(r.status.value for r in results), ["already_running", "started"])
        await finish_round(engine)
        self.assertEqual(len(backend.disconnected), 1)


class ServerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_channel_id_in_different_guilds_is_isolated(self):
        engine, backend, _ = make_engine()
        backend.put(G, VC, 1, 2)
        backend.put(G2, VC, 1, 3)            # same user 1 in another server, same channel id
        engine.opt_in(G, VC, 1)
        engine.opt_in(G, VC, 2)
        self.assertEqual(await engine.eligible_players(G2, VC), [])
        engine.opt_in(G2, VC, 1)
        engine.opt_in(G2, VC, 3)

        engine.opt_out(G, 1)                 # leaving in one server keeps the other
        self.assertTrue(engine.state.is_opted_in(G2, VC, 1))
        backend.move(G, 2, VC, None)
        self.assertTrue(engine.state.is_opted_in(G2, VC, 3))

    async def test_spins_and_chaos_are_isolated_per_guild(self):
        engine, backend, _ = make_engine()
        for g in (G, G2):
            backend.put(g, VC, 1, 2)
            engine.opt_in(g, VC, 1)
            engine.opt_in(g, VC, 2)
        self.assertEqual((await engine.start_spin(G, VC, TXT)).status, SpinStatus.STARTED)
        self.assertEqual((await engine.start_spin(G2, VC, TXT)).status, SpinStatus.STARTED)
        await asyncio.wait([engine.state.peek(G, VC).round_task, engine.state.peek(G2, VC).round_task])
        self.assertEqual(sorted(g for g, _, _ in backend.disconnected), [G, G2])

        await engine.start_chaos(G, VC, TXT, 30, 30)
        await engine.start_chaos(G2, VC, TXT, 30, 30)
        engine.stop_chaos(G, VC)
        self.assertFalse(engine.state.peek(G, VC).chaos_active)
        self.assertTrue(engine.state.peek(G2, VC).chaos_active)
        engine.shutdown()
        await asyncio.sleep(0)

    async def test_forget_guild_cancels_only_that_guild(self):
        engine, backend, _ = make_engine()
        for g in (G, G2):
            backend.put(g, VC, 1, 2)
            engine.opt_in(g, VC, 1)
            engine.opt_in(g, VC, 2)
            await engine.start_chaos(g, VC, TXT, 30, 30)
        engine.forget_guild(G)
        self.assertIsNone(engine.state.peek(G, VC))
        self.assertTrue(engine.state.peek(G2, VC).chaos_active)
        engine.shutdown()
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
