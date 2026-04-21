# Prophet of Procrastination

A Discord bot that monitors Dominions 6 multiplayer games and posts status updates to a channel. It tracks game statuses and warns players if their turn is about to expire.

## What it does

- Scrapes the HTML status page exposed by a Dominions 6 server
- Detects when a new turn starts and posts an immediate notification
- Stores state in SQLite so monitoring resumes after a restart

## Discord bot setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a new application.
2. Under **Bot**, create a bot and copy the **Token** → `DISCORD_TOKEN`.
3. Copy the **Application ID** from the General Information page → `DISCORD_APP_ID`.
4. Under **Bot → Privileged Gateway Intents**, no extra intents are required (the bot only uses slash commands).
5. Under **OAuth2 → URL Generator**, select scopes: `bot`, `applications.commands`.
6. Select bot permissions: `Send Messages`, `Embed Links`, `Read Message History`.
7. Use the generated URL to invite the bot to your server.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISCORD_TOKEN` | Yes | — | Bot token from the Developer Portal |
| `DISCORD_APP_ID` | Yes | — | Application ID (numeric) |
| `DEFAULT_POLL_INTERVAL_SECONDS` | No | `60` | How often (seconds) to poll game servers |
| `LOG_LEVEL` | No | `INFO` | Python logging level |
| `DB_PATH` | No | `/data/dom6bot.db` | Path to the SQLite database |

## Build and run with Docker

```bash
# Clone / copy the project
cp .env.example .env
# Fill in DISCORD_TOKEN and DISCORD_APP_ID in .env

docker compose up -d
docker compose logs -f
```

To rebuild after code changes:

```bash
docker compose up -d --build
```

## Run locally (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Fill in the .env values

DB_PATH=./dom6bot.db python -m bot.main
```

## Slash commands

| Command | Permission | Description |
|---------|-----------|-------------|
| `/addgame <game_name> [status_url]` | Manage Server | Register a game to monitor (defaults to Illwinter hosting) |
| `/removegame <game_name>` | Manage Server | Stop monitoring a game |
| `/setchannel` | Manage Server | Set the current channel for status reports |
| `/status [game_name]` | Everyone | Fetch current status immediately |
| `/listgames` | Everyone | List all monitored games |
| `/claimnation <game_name> <nation_number> [use_lethal_force]` | Everyone | Claim a nation as your player slot |
| `/unclaim <game_name> <nation_number>` | Everyone / Manage Server | Release a nation claim |
| `/flagai <game_name> <nation_number>` | Everyone | Flag a nation as AI (excluded from pending count) |
| `/unflagai <game_name> <nation_number>` | Everyone | Remove AI flag from a nation |

### Automatic notifications

The bot posts to the configured channel at three time thresholds each turn:

| Threshold | Pings claimed players? |
|-----------|----------------------|
| < 12 hours remaining | No |
| < 3 hours remaining | Yes — pings players with unclaimed nations pending |
| < 1 hour remaining | Yes — pings players with unclaimed nations pending |

Unclaimed nations that haven't submitted are listed by name. Nations flagged as AI are shown with 🤖 but never counted as pending.

### Adding a game

#### Illwinter-hosted games (default)

Just provide the game name exactly as it appears in the Dominions lobby — the bot builds the status URL automatically:

```
/addgame game_name:KevSunday
```

#### Self-hosted games

Pass a `status_url` to override the Illwinter default:

```
/addgame game_name:MyGame status_url:http://yourserver.example.com/status.html
```

### Quick start sequence

```
/setchannel                    (in the channel you want reports)
/addgame game_name:KevSunday   (Illwinter URL resolved automatically)
/status game_name:KevSunday    (verify it works)
```
