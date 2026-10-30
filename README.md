# Discord Roulette 🎰

A Discord bot for a private server with friends. People who opt in are put in a "roulette" for their voice channel. When someone spins, the bot runs a countdown, posts an announcement, and disconnects one random opted-in player from voice.

**"Kick" here only means disconnecting someone from voice.** The bot never kicks or bans anyone from the server and doesn't have permission to.

- Python 3.10+, [discord.py](https://github.com/Rapptz/discord.py) 2.x with slash commands
- No privileged intents, no Administrator permission, no audio, no paid services
- All state is kept in memory. Restarting the bot resets every opt-in and stops chaos mode.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/roulette join` | Anyone in a voice channel | Opts you in for your current voice channel |
| `/roulette leave` | Anyone | Opts you out immediately, including during a countdown |
| `/roulette players` | Anyone in a voice channel | Lists the opted-in players in your channel |
| `/roulette spin` | Anyone in the voice channel | Runs a countdown, announces a target, gives them 5 seconds for last words, then disconnects them |
| `/roulette spin-all` | Anyone in the voice channel | Same as spin, but **everyone** in the voice channel is in the draw, opted in or not. Bots are excluded |
| `/roulette chaos start [min_delay] [max_delay] [channel]` | Members with **Manage Channels** | Runs spins at random intervals (30–3600 s; default 120–600 s) |
| `/roulette chaos stop [channel]` | Anyone in that voice channel, or a moderator | Stops chaos mode and cancels any pending countdown or disconnect |
| `/roulette help` | Anyone | Shows the commands and rules |

## Rules

- For `/roulette spin` and chaos mode, only humans who opted in **and** are currently in that voice channel can be picked. Bots are never picked.
- `/roulette spin-all` skips the opt-in: every human in the channel is in the draw, and opting out doesn't protect you. It still needs at least 2 people, shares the one-spin-per-channel limit and cooldown, and is cancelled if the target leaves the channel before the disconnect.
- A spin needs at least **2** eligible players.
- Leaving or switching voice channels clears your opt-in. If you come back, you have to opt in again. Getting disconnected by the bot also clears it.
- After the 5 seconds of last words, the bot checks again that the target is still opted in, still in the channel, and that the bot still has Move Members. If any of those fail, the spin is cancelled.
- Only one countdown and one chaos timer can run per voice channel. Manual spins have a 30 s cooldown.
- State is kept separately for each server and voice channel.
- Announcements go to the text channel where the spin or chaos mode was started.
- The target is picked with `random.SystemRandom` from the eligible players. Fake-outs run after that pick and use a separate RNG, so they can't change who is picked. Fake-out messages show a plain, escaped display name and never ping anyone.

## 1. Create the Discord application

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it (e.g. "Discord Roulette").
2. Open the **Bot** tab:
   - Click **Reset Token**, copy the token, and keep it private. You'll need it for `.env`.
   - Under **Privileged Gateway Intents**, leave all three switches **off**. The bot doesn't need them.
   - Optional: turn off **Public Bot** so only you can invite it.
3. Open **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`
   - Bot permissions: **View Channels**, **Send Messages**, and **Move Members**
   - Open the generated URL and add the bot to your server.

   Or use this URL with your Application ID (found under **General Information**) in place of `YOUR_APP_ID`:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=16780288
   ```

   (`16780288` = View Channels 1024 + Send Messages 2048 + Move Members 16777216.)

### Permission notes

- **Move Members** is what lets the bot disconnect someone from voice. If a voice channel has permission overrides, make sure the bot's role still has **View Channel** and **Move Members** in that channel.
- **Send Messages** is needed in each text channel where you run `/roulette spin` or `/roulette chaos start`. That includes the built-in text chat of a voice channel.
- `/roulette chaos start` checks that the person running it has **Manage Channels** in the target voice channel.

## 2. Install

```bash
cd roullette-bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # or: pip install -r requirements.txt (no tests)
cp .env.example .env
```

Edit `.env`:

```
DISCORD_TOKEN=your-bot-token
DISCORD_GUILD_ID=your-server-id    # recommended
```

To get your server ID, turn on **User Settings → Advanced → Developer Mode** in Discord, then right-click your server icon and choose **Copy Server ID**.

## 3. Run

```bash
python -m roulette_bot
```

You should see `Synced 1 command(s) to guild …` and `Logged in as …`. Leave the process running. When it stops, the bot goes offline and all opt-ins are cleared.

## Slash-command registration

Commands are registered automatically every time the bot starts:

- **With `DISCORD_GUILD_ID` set:** commands are registered to that one server and show up right away. This is the best option for a private server.
- **Without it:** commands are registered globally. They can take a while to show up, and they work in every server the bot is in.

If commands don't appear, restart Discord (Ctrl/Cmd+R). Also check that the invite included the `applications.commands` scope. If you switch from global to guild registration, you may briefly see each command twice. The duplicates go away when the global commands are removed, or you can leave them, since both versions work.

## Run the tests

```bash
python -m pytest
```

The tests use a fake Discord backend. They cover eligibility, opt-outs during countdowns and last words, channel changes, simultaneous spins, cooldowns, chaos cancellation, fairness and fake-outs, server isolation, and the Discord adapter (bots excluded, mentions restricted, `move_to(None)` used).

## Project layout

```
roulette_bot/
  state.py            In-memory opt-ins and timers, keyed by (server, voice channel)
  engine.py           Round + chaos logic (no Discord imports; fully testable)
  discord_backend.py  Talks to Discord: voice members, permissions, disconnects, messages
  cog.py              Slash commands and voice-state listeners
  content.py          Announcement templates, fake reasons, fake-outs
  bot.py              Entry point, intents, command sync
tests/                pytest/unittest test suite with a fake backend
```

Timings such as countdown length, fake-out chance, cooldown, and chaos limits are set in `RouletteConfig` in `roulette_bot/engine.py`.
