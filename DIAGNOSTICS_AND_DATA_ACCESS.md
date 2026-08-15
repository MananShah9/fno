# 🔍 Stage Diagnostics & Data Access Guide

This guide explains how the system tracks stage-wise timestamps, execution metadata, warnings, and errors for every message (and each message edit/revision), and how you can easily access and analyze this data for debugging and performance monitoring.

---

## 🏛️ 1. Architecture & What Is Tracked

For every message ingested from Telegram, the system automatically records a detailed, non-blocking stage trace directly in PostgreSQL (`message_stage_traces` table). 

### Key Data Fields Captured per Stage:
* **`stage`**: The exact step in the pipeline (e.g. `AI_ANALYSIS`, `INSTRUMENT_RESOLUTION_AND_SIZING`, `ORDER_EXECUTION`).
* **`status`**: Status badge: `SUCCESS`, `WARNING`, `ERROR`, `SKIPPED`, `INFO`, `IN_PROGRESS`.
* **`revision`**: `Rev 0` for initial message; `Rev 1, 2, ...` for edits/updates.
* **`timestamp`**: Precise UTC timestamp down to the millisecond.
* **`duration_ms`**: Milliseconds taken to complete that specific stage.
* **`location`**: Exact source code location in format `filename:function:lineno` (e.g. `worker.py:process_single_message:840`).
* **`details`**: Structured JSON payload containing intermediate inputs and outputs (e.g., AI extracted trade fields, resolved Zerodha instrument tokens, lot sizing calculations, deduplication reasons).
* **`error_message` & `stack_trace`**: Full error text and complete Python traceback if an exception or order rejection occurs.

---

## 🚦 2. Pipeline Stages Reference

| Stage Name | Description | Possible Statuses |
| :--- | :--- | :--- |
| `SYNC_RECEIVED` | Raw message ingested from Telegram channel | `SUCCESS` |
| `POKE_FILTER` | Message is identified as a poke ping (`.` or `trade incoming`) and skipped | `SKIPPED` |
| `TIME_WINDOW_FILTER` | Message sent outside configured active hours or weekdays; skipped from processing | `SKIPPED` |
| `MESSAGE_EDIT_DETECTED` | Edit event received for a message | `INFO` |
| `EDIT_REPROCESSING_STARTED`| Reprocessing triggered with incremented revision | `INFO` |
| `EDIT_REPROCESSING_SKIPPED`| Edit skipped because actions were already executed for the original message | `SKIPPED` |
| `CONTEXT_FETCH` | Fetching currently open trades to provide context to AI | `SUCCESS`, `ERROR` |
| `AI_ANALYSIS` | Google Gemini LLM extracting structured trade recommendations | `SUCCESS`, `ERROR` |
| `AI_NON_TRADE_MESSAGE` | AI determined message is informational/commentary, not an actionable trade | `SKIPPED` |
| `TRADE_ROUTING` | Matching message to existing parent Trade or creating a new Trade | `SUCCESS`, `ERROR` |
| `INSTRUMENT_RESOLUTION_AND_SIZING` | Mapping underlying/strike/expiry to NFO tokens & calculating budget lot sizes | `SUCCESS`, `WARNING` |
| `SQUARE_OFF_GENERATION` | Generating reverse exit legs for closed trades or stop-loss hits | `SUCCESS` |
| `ORDER_EXECUTION` | Executing automated orders on Zerodha Kite API | `SUCCESS`, `WARNING`, `ERROR` |
| `ORDER_PLACED` | Individual leg order placed on Zerodha (includes Order ID) | `SUCCESS` |
| `ORDER_DEDUPLICATED` | Order skipped because position/order already exists on Zerodha | `INFO` |
| `ORDER_FAILED` | Zerodha order placement failed (includes API error message) | `ERROR` |
| `TELEGRAM_NOTIFICATION` | Formatting and sending action card HTML to Telegram actions channel | `SUCCESS`, `ERROR` |
| `MESSAGE_COMPLETED` | Entire pipeline finished successfully for the message | `SUCCESS` |
| `TELEGRAM_BUTTON_CLICKED` | Telegram user clicked "🚀 Place Order(s)" inline button | `INFO` |
| `MANUAL_ORDER_EXECUTION` | Manual order placement execution triggered via Telegram button | `SUCCESS`, `ERROR` |

---

## 💻 3. How to Access and Analyze the Data

You have 4 easy ways to access the diagnostic data:

### Method A: Direct CLI Commands (Fastest for Terminal / Scripts)

You can query diagnostics instantly using `main.py`:

```bash
# 1. View recent messages with health status, last stage, and trace count
python main.py trace --recent

# 2. Inspect full chronological stage timeline for a specific DB Message ID
python main.py trace <message_id>
# Example: python main.py trace 633

# 3. Inspect full stage timeline by Telegram Message ID
python main.py trace tg:<telegram_message_id>
# Example: python main.py trace tg:1001

# 4. View all stuck, incomplete, or errored messages
python main.py trace --stuck

# 5. Export diagnostics to a JSON report file
python main.py trace --export
```

#### If Running in Docker:
Use `docker exec fno_app` prefix:
```bash
docker exec fno_app python main.py trace --recent
docker exec fno_app python main.py trace 633
docker exec fno_app python main.py trace --stuck
```

---

### Method B: Interactive Terminal CLI Menu

Run the interactive CLI dashboard:
```bash
python main.py cli
# (or inside Docker: docker exec -it fno_app python main.py cli)
```

1. Select option **`[6] 🔍 Message Diagnostics & Stage Traces`**.
2. Choose from the submenu:
   * `[1] 📋 View Recent Messages & Diagnostic Health Summary`
   * `[2] ⚠️  View Stuck, Errored & Incomplete Messages`
   * `[3] 🔎 Inspect Full Stage Timeline for a Message (by DB ID)`
   * `[4] 🔎 Inspect Full Stage Timeline for a Message (by Telegram Msg ID)`
   * `[5] 💾 Export Trace Diagnostics to JSON / Markdown File`
   * `[6] 🔙 Return to Main Menu`

---

### Method C: Direct SQL Queries (PostgreSQL)

You can query the database directly using any PostgreSQL client (psql, DBeaver, pgAdmin, TablePlus, or VS Code):
* **Host Port:** `localhost:5430` (or `db:5432` inside docker network)
* **Database:** `fno_db`
* **User:** `postgres`
* **Password:** `postgres_pass` (or your configured `DB_PASSWORD`)

#### Useful SQL Queries:

**1. View full timeline of a specific message:**
```sql
SELECT 
    id, 
    revision, 
    stage, 
    status, 
    timestamp, 
    duration_ms, 
    location, 
    error_message, 
    details 
FROM message_stage_traces 
WHERE message_id = 633 
ORDER BY id ASC;
```

**2. Find all failed stages and errors in the last 24 hours:**
```sql
SELECT 
    message_id, 
    telegram_message_id, 
    stage, 
    location, 
    error_message, 
    timestamp 
FROM message_stage_traces 
WHERE status = 'ERROR' 
ORDER BY timestamp DESC;
```

**3. Find slowest stages (performance bottlenecks > 1000ms):**
```sql
SELECT 
    stage, 
    AVG(duration_ms) AS avg_duration_ms, 
    MAX(duration_ms) AS max_duration_ms, 
    COUNT(*) AS total_runs 
FROM message_stage_traces 
WHERE duration_ms IS NOT NULL 
GROUP BY stage 
ORDER BY avg_duration_ms DESC;
```

**4. Find all stuck messages (unprocessed or error status):**
```sql
SELECT 
    id, 
    telegram_message_id, 
    date, 
    last_stage, 
    last_status, 
    last_error, 
    text 
FROM messages 
WHERE processed = FALSE OR last_status IN ('ERROR', 'WARNING')
ORDER BY id DESC;
```

---

### Method D: Programmatic Python Access (`stage_tracker.py`)

You can import `stage_tracker` in any custom Python script:

```python
import db
import stage_tracker

session = db.SessionLocal()

# 1. Fetch all stages for a message
traces = stage_tracker.get_message_history(message_id=633, session=session)
for t in traces:
    print(f"Stage: {t['stage']} | Status: {t['status']} | Duration: {t['duration_ms']}ms | Loc: {t['location']}")

# 2. Fetch stuck messages
stuck = stage_tracker.get_stuck_or_failed_messages(limit=20, session=session)

# 3. Fetch recent diagnostics summary
recent = stage_tracker.get_recent_messages_diagnostics(limit=20, session=session)

session.close()
```

---

## 🛠️ 4. Common Debugging Scenarios

### Scenario 1: "Why was this message not traded?"
Run `python main.py trace <message_id>`:
* If the last stage is `POKE_FILTER` with status `SKIPPED`, the message was a dot or ping notification (`trade incoming`).
* If the last stage is `AI_NON_TRADE_MESSAGE` with status `SKIPPED`, Gemini analyzed the text and identified it as general commentary or market news, not an actionable trade signal.

### Scenario 2: "Why did a Zerodha order fail?"
Run `python main.py trace <message_id>`:
* Look for stage `ORDER_FAILED` with status `❌ ERROR`.
* Inspect the `Error / Warning` field and `Details`. The exact response from Zerodha (e.g. `Price outside circuit limit`, `Markets are closed`, `Insufficient margin`, `Invalid session token`) along with the exact order parameters will be displayed.

### Scenario 3: "How were edits to a message handled?"
Run `python main.py trace <message_id>`:
* The timeline table displays stages partitioned by `Rev 0`, `Rev 1`, `Rev 2`, etc.
* You can see exactly what the system did when the message was initially received vs. what it did when the author edited the message in the Telegram channel.
