# dom6-bot

A Discord bot that monitors Dominions 6 multiplayer games and posts status updates to a channel. It tracks turn submissions, announces new turns, and sends scheduled status reports.

## What it does

- Scrapes the HTML status page exposed by a Dominions 6 server (`--statuspage` flag)
- Detects when a new turn starts and posts an immediate notification
- Posts a scheduled status report at a configurable interval (default: every 4 hours)
- Falls back to the Dominions TCP query protocol if the HTML page is unreachable and TCP is configured
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
| `/addgame <game_name> [status_url] [server_ip] [server_port]` | Manage Server | Register a game to monitor (defaults to Illwinter hosting) |
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

Optionally add TCP fallback:

```
/addgame game_name:MyGame status_url:http://yourserver.example.com/status.html server_ip:192.0.2.1 server_port:2020
```

### Quick start sequence

```
/setchannel                    (in the channel you want reports)
/setinterval hours:4
/addgame game_name:KevSunday   (Illwinter URL resolved automatically)
/status game_name:KevSunday    (verify it works)
```

## Deploy to Oracle Cloud Always Free

These notes assume an AMD Micro instance (1/8 OCPU, 1 GB RAM, x86_64, Ubuntu 22.04).

```bash
# On the instance:
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER
# Log out and back in

# Copy the project
scp -r . ubuntu@<instance-ip>:~/dom6-bot
ssh ubuntu@<instance-ip>

cd ~/dom6-bot
cp .env.example .env
nano .env   # fill in tokens

docker compose up -d
```

The `docker-compose.yml` sets a 256 MB memory limit and 0.1 CPU limit so the bot coexists with other processes on the shared instance.

The bot's 60-second polling loop generates periodic outbound network activity, which is sufficient to prevent Oracle's idle instance reclamation.

## TCP query protocol

The TCP fallback is based on the [Dom3 server format notes](http://www.cs.helsinki.fi/u/aitakang/dom3_serverformat_notes) and the [dominions-5-status Rust bot](https://github.com/djmcgill/dominions-5-status). It is a best-effort implementation — the HTML status page is the primary and more reliable data source. If TCP queries consistently fail, you can safely ignore them and rely on the HTML scraper alone.

One known limitation of TCP-only data: the protocol returns nation IDs, not names. Nation names are only available from the HTML status page.
