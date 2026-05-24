#!/usr/bin/env bash
# Pull production usage CSV from Railway; append only new rows with a date marker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$ROOT/logs/tanya_usage.csv}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

BASE="${TANYA_PUBLIC_URL:-https://worker-production-32fb.up.railway.app}"
URL="${BASE%/}/admin/usage-csv"

if [[ -z "${ADMIN_KEY:-}" ]]; then
  echo "ADMIN_KEY is not set. Add it to $ROOT/.env or run:" >&2
  echo "  export ADMIN_KEY='your-token'" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"
TMP="$(mktemp "${TMPDIR:-/tmp}/tanya-usage.XXXXXX.csv")"
trap 'rm -f "$TMP"' EXIT

curl -fsSL \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  "$URL" \
  -o "$TMP"

python3 - "$TMP" "$OUT" <<'PY'
import csv
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

src = Path(sys.argv[1])
dest = Path(sys.argv[2])
_PACIFIC = ZoneInfo("America/Los_Angeles")
_UTC = ZoneInfo("UTC")
today = datetime.now(_PACIFIC).date().isoformat()


def to_pacific_timestamp(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw or raw == "timestamp":
        return raw
    # DOWNLOAD marker rows use date-only timestamps.
    if "T" not in raw:
        return raw
    try:
        if raw.endswith("Z"):
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        elif len(raw) >= 25 and raw[-6] in "+-" and ":" in raw[-5:]:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.fromisoformat(raw).replace(tzinfo=_UTC)
        return dt.astimezone(_PACIFIC).isoformat(timespec="seconds")
    except ValueError:
        return raw

with src.open(newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    if not reader.fieldnames:
        print("Production log is empty — nothing to append.")
        sys.exit(0)
    fieldnames = list(reader.fieldnames)
    prod_rows = list(reader)

if not prod_rows:
    print("Production log has no data rows — nothing to append.")
    sys.exit(0)

existing_ids: set[str] = set()
dest_exists = dest.exists() and dest.stat().st_size > 0

if dest_exists:
    with dest.open(newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row:
                continue
            key = row[0].strip()
            if key in ("log_id", "DOWNLOAD"):
                continue
            if key.isdigit():
                existing_ids.add(key)

    with dest.open(newline="", encoding="utf-8") as f:
        first_line = f.readline().strip()
    local_header = [c.strip() for c in first_line.split(",")]
    if local_header != fieldnames:
        backup = dest.with_suffix(dest.suffix + ".bak")
        dest.rename(backup)
        print(f"Header mismatch — archived old file to {backup.name}")
        dest_exists = False
        existing_ids.clear()

for row in prod_rows:
    row["timestamp"] = to_pacific_timestamp(row.get("timestamp", ""))

new_rows = [r for r in prod_rows if r.get("log_id", "").strip() not in existing_ids]

if not new_rows:
    print(f"No new rows — local file is up to date ({len(existing_ids)} log_id(s) already).")
    sys.exit(0)

write_header = not dest_exists
mode = "w" if write_header else "a"

with dest.open(mode, newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
    if write_header:
        writer.writeheader()
    marker = {fn: "" for fn in fieldnames}
    marker["log_id"] = "DOWNLOAD"
    marker["timestamp"] = today
    marker["user"] = "snapshot from Railway"
    writer.writerow(marker)
    writer.writerows(new_rows)

total = len(existing_ids) + len(new_rows)
print(f"Appended {len(new_rows)} new row(s) under DOWNLOAD marker {today}")
print(f"Local file now has {total} data row(s) → {dest}")
PY
