# TanyaTalk Admin Reference

## Usage CSV Download

Download the full session cost log (all clients, all sessions, cost in USD).

### Easiest: run the script

From the project folder:

```bash
./download-usage.sh
```

Saves to **`logs/tanya_usage.csv`**. Each run:

- Fetches the full production log from Railway
- **Appends only new rows** (matched by `log_id`) — does not wipe older local history
- Inserts a **`DOWNLOAD` marker row** with the date before each batch (easy to spot in Excel)

Optional custom path:

```bash
./download-usage.sh ~/Desktop/tanya_usage.csv
```

The script reads **`ADMIN_KEY`** from your local `.env` (same value as Railway).

### Manual curl

Manual curl always **replaces** the target file. Prefer `./download-usage.sh` for append behavior.

```bash
curl -H "Authorization: Bearer $ADMIN_KEY" \
  https://worker-production-32fb.up.railway.app/admin/usage-csv \
  -o /tmp/tanya_usage_fresh.csv
```

**Note:** Production logs are not pushed to your Mac automatically when users text Tanya. Run `./download-usage.sh` whenever you want the latest snapshot.

Set `ADMIN_KEY` in Railway **Variables** and in your local `.env` — they must match.

Then open `logs/tanya_usage.csv` in Numbers or Excel. Filter out rows where `log_id` = `DOWNLOAD` if you only want message data.

**Columns:** `log_id, timestamp, phone_hash, user, model, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, approx_usd`

**Timestamps** are US Pacific (`America/Los_Angeles`, PST/PDT). May–Oct rows show `-07:00` (PDT); Nov–Mar show `-08:00` (PST). `./download-usage.sh` converts production UTC rows on pull.

**Marker row example:** `DOWNLOAD,2026-05-22,,snapshot from Railway,,,,,`

---

> Keep `ADMIN_KEY` out of git (`.env` is ignored). If the token is ever exposed, rotate it in Railway and update `.env`.
