# TanyaTalk Admin Reference

## Usage CSV Download

Download the full session cost log (all clients, all sessions, cost in USD).

Run in Terminal — saves to Desktop:

```bash
curl -H "Authorization: Bearer <RAILWAY_ADMIN_TOKEN>" \
  https://worker-production-32fb.up.railway.app/admin/usage-csv \
  -o ~/Desktop/tanya_usage.csv
```

Replace `<RAILWAY_ADMIN_TOKEN>` with the current token from Railway environment variables (`ADMIN_BEARER_TOKEN`).

Then open `tanya_usage.csv` on your Desktop in Numbers or Excel.

**Columns:** `log_id, timestamp, phone_hash, user, model, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, approx_usd`

---

> Note: The admin token was previously hardcoded in `tanya_brain/03-Resources/Admin Links.md` and committed to git.
> That token should be considered compromised — rotate it in Railway and update `ADMIN_BEARER_TOKEN`.
