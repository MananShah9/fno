# 📈 F&O Telegram Trader System

An intelligent, AI-powered futures and options trading system that monitors Telegram channels, extracts trade signals using Gemini AI, manages trade lifecycle/state, and broadcasts structured order actions to execution/mirroring channels.

---

## 🚀 Key Features

*   **Telegram Message Syncing:** Real-time progressive syncing of messages from designated F&O signal channels using Telethon.
*   **AI-Powered Signal Extraction:** Integrates Google's Gemini LLM (`gemini-2.5-flash` / `gemini-flash-latest`) to parse raw, messy option signal messages into highly structured JSON formats (underlying ticker, entry/exit type, strike, premium, stop-loss, and target).
*   **Trade Lifecycle Management:** Dynamically links updates (e.g. stop-loss updates, leg closures, full exits) back to original open parent trades.
*   **Structured Broadcasts:** Generates beautifully formatted HTML messages with click-to-copy (monospace) trade actions.
*   **Interactive CLI:** A terminal dashboard to manage configurations, run initial auth setup, and monitor messages, trades, and order execution logs.
*   **Postgres Storage:** Robust schema modeling for raw messages, trades, and actions using SQLAlchemy.
*   **Docker Containerization:** Seamless deployment of PostgreSQL and the background worker.

---

## 📋 Prerequisites

*   Python 3.10+ (if running locally)
*   PostgreSQL Database (or Docker & Docker Compose)
*   Telegram API credentials (API ID & API Hash)
*   Gemini API Key

---

## 🔑 Environment Configuration

Create a `.env` file in the root directory by copying the template file:

```bash
cp .env.template .env
```

Open `.env` and fill in the configuration options:

```ini
# Telegram API credentials (get from https://my.telegram.org)
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE=+1234567890             # Format: +CountryCodePhone

# Telegram Channels (channel username e.g. @my_channel or integer ID e.g. -1001234567)
TELEGRAM_SOURCE_CHANNEL=@source_chan_username
TELEGRAM_MIRROR_CHANNEL=@mirror_chan_username
TELEGRAM_ACTIONS_CHANNEL=@actions_chan_username

# Telegram polling / refresh interval in seconds
TELEGRAM_REFRESH_INTERVAL=10

# Telegram Message Schedule & Active Time Filter
TELEGRAM_TIME_FILTER_ENABLED=true
TELEGRAM_START_TIME=08:30
TELEGRAM_END_TIME=16:30
TELEGRAM_WEEKDAYS_ONLY=true
TELEGRAM_TIMEZONE=Asia/Kolkata

# Gemini API Settings
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.5-flash

# Database Settings
DB_HOST=db
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=postgres_pass
DB_NAME=fno_db
```

---

## 🛡️ First-Time Setup & Telegram Authorization

Telethon requires user-level authentication to access channel messages. Because of Telegram's security design, this involves a one-time interactive login using your phone number and an OTP code.

Follow these steps to complete the initial setup:

### 1. Run the Database
If you are running the database via Docker:
```bash
docker-compose up -d db
```
*(If running Postgres locally, make sure the service is started and the database credentials in `.env` match your local environment).*

### 2. Install Python Dependencies (for Local CLI Setup)
```bash
pip install -r requirements.txt
```

### 3. Authenticate with Telegram (Interactive CLI)
Run the interactive CLI utility:
```bash
python main.py cli
```

Once the CLI loads:
1. Select **`[1] Setup & Telegram Authorization`** from the main menu.
2. The system will initialize the database schema and check your current auth status.
3. If not already authenticated, it will start the Telegram login process:
   * It will ask for your **Telegram Phone Number** (e.g. `+1234567890`) if not pre-configured in `.env`.
   * You will receive a secure OTP code from Telegram in your Telegram app.
   * Enter the **OTP code** into the terminal.
   * If you have Two-Factor Authentication (2FA) enabled, enter your account password.
4. Once successfully verified, Telethon creates an in-memory session string saved automatically to **`TELEGRAM_STRING_SESSION`** in `.env`. This avoids SQLite database locking issues across concurrent processes. Do **NOT** share or commit your session string.
5. In the same menu, you can interactively configure your F&O Source, Mirror, and Actions channels or input your Gemini API Key.

---

## ⚙️ Running the System

You can run the system elements together using Docker Compose or separately using Python.

### Option A: Using Docker (Recommended)
Docker-Compose automatically spins up PostgreSQL and boots the Python background worker which syncs messages and performs AI parsing.

1. Bring up the containers:
   ```bash
   docker-compose up -d --build
   ```
2. Verify worker logs:
   ```bash
   docker-compose logs -f app
   ```

**First-time Docker setup (interactive Telegram auth)**

If this is the first time you're running the system in Docker, complete the Telegram authorization interactively so the `TELEGRAM_STRING_SESSION` environment variable is generated and saved into `.env`.

1. Start the containers (database + app):
```bash
docker-compose up -d --build
```
2. Run the interactive CLI inside the app container and run the setup flow:
```bash
docker exec -it fno_app python main.py cli
```
3. In the CLI select **`[1] Setup & Telegram Authorization`** and follow the prompts to authenticate with Telegram. This automatically generates and stores `TELEGRAM_STRING_SESSION` in `.env`.
4. After completing the authorization, restart the app container so the worker picks up the new session:
```bash
docker restart fno_app
```

*Note: With in-memory `StringSession`, multiple tools (worker daemon, CLI dashboards, simulator, diagnostics) can run concurrently without SQLite database file lock conflicts.*

---

### Option B: Running Manually

#### Run Background Worker
To start syncing messages from Telegram and running the Gemini AI analysis engine every 10 seconds:
```bash
python main.py worker
```

#### Run Interactive CLI
To view messages, monitor open/closed trades, check order logs, and manage configuration variables:
```bash
python main.py cli
```

#### Run Message Stage Diagnostics & Tracing
To inspect stage-wise execution timelines, latencies, code locations, and errors for any message:
```bash
python main.py trace --recent      # View recent message health summary
python main.py trace <message_id>  # Full stage timeline for message
python main.py trace --stuck       # View stuck/failed messages
```
*(For comprehensive details, see [`DIAGNOSTICS_AND_DATA_ACCESS.md`](./DIAGNOSTICS_AND_DATA_ACCESS.md)).*

---

## 📂 Project Structure

```
.
├── sessions/                # Stores your persistent Telegram user sessions (Git ignored)
├── cli.py                   # Command-line interface dashboard & diagnostics viewer
├── config.py                # Environment configuration loader and updater
├── db.py                    # SQLAlchemy connection and table initialization & migrations
├── docker-compose.yml       # Docker definition for DB & Worker App services
├── Dockerfile               # Production multi-stage Docker build for the app
├── gemini_client.py         # Google Gemini integration & structured extraction prompt
├── instruments_manager.py   # NFO contract resolver & budget lot sizing calculator
├── main.py                  # Entrypoint router (Worker, CLI, Trace, Simulator)
├── models.py                # Database models (Message, Trade, Action, MessageStageTrace)
├── requirements.txt         # Project requirements
├── stage_tracker.py         # Stage diagnostic tracer & timing context managers
├── telegram_client.py       # Telethon client wrapper & channel resolver
├── test_simulator.py        # Local simulation script with stage tracing verification
├── zerodha_client.py        # Zerodha Kite Connect order execution & proxy handler
├── DIAGNOSTICS_AND_DATA_ACCESS.md # Full guide on debugging and analyzing message stage data
└── worker.py                # Sync scheduler, context builder, and HTML notification compiler
```
