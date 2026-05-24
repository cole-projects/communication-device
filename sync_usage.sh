#!/bin/bash
LOCAL="/Users/colewannemacher/Desktop/moms auto/Communication Device/logs/tanya_usage.csv"
TMP=$(mktemp)
TODAY=$(date '+%Y-%m-%d')

curl -s \
  -H "Authorization: Bearer cjDEPh3u5QrHuGlo4KPK7cd9_ZClA20t" \
  "https://worker-production-32fb.up.railway.app/admin/usage-csv" \
  -o "$TMP"

if [ ! -s "$TMP" ]; then
  rm "$TMP"
  exit 1
fi

python3 << PYEOF
import csv, sys, os

local_path = "$LOCAL"
tmp_path = "$TMP"
today = "$TODAY"

# Get the highest log_id already saved locally
last_id = 0
if os.path.exists(local_path):
    with open(local_path, 'r') as f:
        for line in f:
            if line.startswith('---'):
                continue
            parts = line.strip().split(',')
            try:
                lid = int(parts[0])
                if lid > last_id:
                    last_id = lid
            except:
                pass
else:
    with open(tmp_path, 'r') as f:
        header = f.readline()
    with open(local_path, 'w') as f:
        f.write(header)

# Collect new rows from complete days only (date < today)
new_rows = {}
with open(tmp_path, 'r') as f:
    reader = csv.reader(f)
    next(reader)  # skip header
    for row in reader:
        try:
            lid = int(row[0])
        except:
            continue
        if lid <= last_id:
            continue
        row_date = row[1][:10]  # YYYY-MM-DD
        if row_date >= today:
            continue  # exclude today's partial data
        if row_date not in new_rows:
            new_rows[row_date] = []
        new_rows[row_date].append(row)

if not new_rows:
    sys.exit(0)

# Append each complete day with its own date header
with open(local_path, 'a', newline='') as f:
    writer = csv.writer(f)
    for d in sorted(new_rows.keys()):
        f.write(f'---,{d},,,,,,,,\n')
        for row in new_rows[d]:
            writer.writerow(row)

PYEOF

rm "$TMP"
