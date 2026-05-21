"""
Async SQLite billing store — replaces six JSON flat files.

Tables:
  paid_access            — which phone hashes have an active subscription
  extra_messages         — top-up credits per phone hash
  free_trial_completed   — phone hashes that have used their free trial
  free_trial_msg_counts  — in-progress free trial message counter (persisted across restarts)
  subscription_starts    — 30-day billing anchor date per phone hash
  monthly_usage          — (phone_hash, period_key) → message count
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

logger = logging.getLogger(__name__)

_DB_PATH: Path | None = None
_init_lock = asyncio.Lock()
_initialized = False


def configure(db_path: Path) -> None:
    global _DB_PATH
    _DB_PATH = db_path


def _conn() -> aiosqlite.Connection:
    assert _DB_PATH is not None, "billing_db.configure() must be called before use"
    return aiosqlite.connect(_DB_PATH)


async def init_db() -> None:
    global _initialized
    async with _init_lock:
        if _initialized:
            return
        async with _conn() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS paid_access (
                    phone_hash TEXT PRIMARY KEY,
                    granted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS extra_messages (
                    phone_hash TEXT PRIMARY KEY,
                    amount     INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS free_trial_completed (
                    phone_hash TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS free_trial_msg_counts (
                    phone_hash TEXT PRIMARY KEY,
                    count      INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS subscription_starts (
                    phone_hash TEXT PRIMARY KEY,
                    start_date TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monthly_usage (
                    phone_hash TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    count      INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (phone_hash, period_key)
                );
                CREATE TABLE IF NOT EXISTS new_user_onboards (
                    phone_hash   TEXT PRIMARY KEY,
                    onboarded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stripe_customer_ids (
                    phone_hash  TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL
                );
            """)
            await db.commit()
        _initialized = True
        logger.info("billing.db initialized at %s", _DB_PATH)


# ---------------------------------------------------------------------------
# One-time migration from JSON flat files (runs on startup if files present)
# ---------------------------------------------------------------------------

async def migrate_from_json(logs_dir: Path) -> None:
    """Import existing JSON billing data into SQLite. Renames each file to .json.migrated on success."""

    async def _migrate_paid_access() -> None:
        p = logs_dir / "paid_access.json"
        if not p.exists():
            return
        try:
            hashes = json.loads(p.read_text(encoding="utf-8"))
            today = datetime.date.today().isoformat()
            async with _conn() as db:
                for ph in (hashes if isinstance(hashes, list) else []):
                    await db.execute(
                        "INSERT OR IGNORE INTO paid_access (phone_hash, granted_at) VALUES (?, ?)",
                        (ph, today),
                    )
                await db.commit()
            p.rename(p.with_suffix(".json.migrated"))
            logger.info("Migrated paid_access.json (%d entries)", len(hashes))
        except Exception as e:
            logger.error("paid_access migration failed: %s", e)

    async def _migrate_extra_messages() -> None:
        p = logs_dir / "extra_messages.json"
        if not p.exists():
            return
        try:
            data: dict = json.loads(p.read_text(encoding="utf-8"))
            async with _conn() as db:
                for ph, amount in data.items():
                    await db.execute(
                        "INSERT OR REPLACE INTO extra_messages (phone_hash, amount) VALUES (?, ?)",
                        (ph, int(amount)),
                    )
                await db.commit()
            p.rename(p.with_suffix(".json.migrated"))
            logger.info("Migrated extra_messages.json (%d entries)", len(data))
        except Exception as e:
            logger.error("extra_messages migration failed: %s", e)

    async def _migrate_free_trial_completed() -> None:
        p = logs_dir / "free_trial_completed.json"
        if not p.exists():
            return
        try:
            hashes = json.loads(p.read_text(encoding="utf-8"))
            async with _conn() as db:
                for ph in (hashes if isinstance(hashes, list) else []):
                    await db.execute(
                        "INSERT OR IGNORE INTO free_trial_completed (phone_hash) VALUES (?)",
                        (ph,),
                    )
                await db.commit()
            p.rename(p.with_suffix(".json.migrated"))
            logger.info("Migrated free_trial_completed.json (%d entries)", len(hashes))
        except Exception as e:
            logger.error("free_trial_completed migration failed: %s", e)

    async def _migrate_free_trial_msg_counts() -> None:
        p = logs_dir / "free_trial_msg_counts.json"
        if not p.exists():
            return
        try:
            data: dict = json.loads(p.read_text(encoding="utf-8"))
            async with _conn() as db:
                for ph, count in data.items():
                    await db.execute(
                        "INSERT OR REPLACE INTO free_trial_msg_counts (phone_hash, count) VALUES (?, ?)",
                        (ph, int(count)),
                    )
                await db.commit()
            p.rename(p.with_suffix(".json.migrated"))
            logger.info("Migrated free_trial_msg_counts.json (%d entries)", len(data))
        except Exception as e:
            logger.error("free_trial_msg_counts migration failed: %s", e)

    async def _migrate_subscription_starts() -> None:
        p = logs_dir / "subscription_starts.json"
        if not p.exists():
            return
        try:
            data: dict = json.loads(p.read_text(encoding="utf-8"))
            async with _conn() as db:
                for ph, start_date in data.items():
                    await db.execute(
                        "INSERT OR IGNORE INTO subscription_starts (phone_hash, start_date) VALUES (?, ?)",
                        (ph, start_date),
                    )
                await db.commit()
            p.rename(p.with_suffix(".json.migrated"))
            logger.info("Migrated subscription_starts.json (%d entries)", len(data))
        except Exception as e:
            logger.error("subscription_starts migration failed: %s", e)

    async def _migrate_monthly_usage() -> None:
        p = logs_dir / "monthly_usage.json"
        if not p.exists():
            return
        try:
            data: dict = json.loads(p.read_text(encoding="utf-8"))
            async with _conn() as db:
                for ph, periods in data.items():
                    if isinstance(periods, dict):
                        for period_key, count in periods.items():
                            await db.execute(
                                "INSERT OR REPLACE INTO monthly_usage (phone_hash, period_key, count) VALUES (?, ?, ?)",
                                (ph, period_key, int(count)),
                            )
                await db.commit()
            p.rename(p.with_suffix(".json.migrated"))
            logger.info("Migrated monthly_usage.json")
        except Exception as e:
            logger.error("monthly_usage migration failed: %s", e)

    await _migrate_paid_access()
    await _migrate_extra_messages()
    await _migrate_free_trial_completed()
    await _migrate_free_trial_msg_counts()
    await _migrate_subscription_starts()
    await _migrate_monthly_usage()


# ---------------------------------------------------------------------------
# Paid access
# ---------------------------------------------------------------------------

async def grant_access(phone_hash: str) -> None:
    async with _conn() as db:
        await db.execute(
            "INSERT OR REPLACE INTO paid_access (phone_hash, granted_at) VALUES (?, ?)",
            (phone_hash, datetime.date.today().isoformat()),
        )
        await db.commit()


async def revoke_access(phone_hash: str) -> None:
    async with _conn() as db:
        await db.execute("DELETE FROM paid_access WHERE phone_hash = ?", (phone_hash,))
        await db.commit()


async def has_access(phone_hash: str) -> bool:
    async with _conn() as db:
        async with db.execute(
            "SELECT 1 FROM paid_access WHERE phone_hash = ?", (phone_hash,)
        ) as cur:
            return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Extra messages (top-up credits, carry over indefinitely)
# ---------------------------------------------------------------------------

async def get_extra_messages(phone_hash: str) -> int:
    async with _conn() as db:
        async with db.execute(
            "SELECT amount FROM extra_messages WHERE phone_hash = ?", (phone_hash,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def add_extra_messages(phone_hash: str, amount: int) -> int:
    """Add credits; return new total."""
    async with _conn() as db:
        await db.execute(
            """INSERT INTO extra_messages (phone_hash, amount) VALUES (?, ?)
               ON CONFLICT(phone_hash) DO UPDATE SET amount = amount + excluded.amount""",
            (phone_hash, amount),
        )
        await db.commit()
        async with db.execute(
            "SELECT amount FROM extra_messages WHERE phone_hash = ?", (phone_hash,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else amount


async def consume_extra_message(phone_hash: str) -> None:
    async with _conn() as db:
        await db.execute(
            "UPDATE extra_messages SET amount = MAX(0, amount - 1) WHERE phone_hash = ?",
            (phone_hash,),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Free trial completed
# ---------------------------------------------------------------------------

async def mark_trial_completed(phone_hash: str) -> None:
    async with _conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO free_trial_completed (phone_hash) VALUES (?)",
            (phone_hash,),
        )
        await db.commit()


async def has_completed_trial(phone_hash: str) -> bool:
    async with _conn() as db:
        async with db.execute(
            "SELECT 1 FROM free_trial_completed WHERE phone_hash = ?", (phone_hash,)
        ) as cur:
            return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Free trial message counts (persisted so restarts don't lose progress)
# ---------------------------------------------------------------------------

async def get_trial_msg_count(phone_hash: str) -> int:
    async with _conn() as db:
        async with db.execute(
            "SELECT count FROM free_trial_msg_counts WHERE phone_hash = ?", (phone_hash,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def save_trial_msg_count(phone_hash: str, count: int) -> None:
    async with _conn() as db:
        await db.execute(
            "INSERT OR REPLACE INTO free_trial_msg_counts (phone_hash, count) VALUES (?, ?)",
            (phone_hash, count),
        )
        await db.commit()


async def delete_trial_msg_count(phone_hash: str) -> None:
    async with _conn() as db:
        await db.execute(
            "DELETE FROM free_trial_msg_counts WHERE phone_hash = ?", (phone_hash,)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Subscription starts (30-day billing period anchor)
# ---------------------------------------------------------------------------

async def record_subscription_start(phone_hash: str) -> None:
    """Record today as this client's billing anchor (idempotent — won't overwrite)."""
    async with _conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO subscription_starts (phone_hash, start_date) VALUES (?, ?)",
            (phone_hash, datetime.date.today().isoformat()),
        )
        await db.commit()


async def get_billing_period_key(phone_hash: str) -> str:
    """Return ISO start date of this client's current 30-day billing period."""
    async with _conn() as db:
        async with db.execute(
            "SELECT start_date FROM subscription_starts WHERE phone_hash = ?", (phone_hash,)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return datetime.date.today().strftime("%Y-%m")
    try:
        start = datetime.date.fromisoformat(row[0])
        days_elapsed = (datetime.date.today() - start).days
        period_start = start + datetime.timedelta(days=30 * (days_elapsed // 30))
        return period_start.isoformat()
    except Exception:
        return datetime.date.today().strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Monthly message usage
# ---------------------------------------------------------------------------

async def get_monthly_message_count(phone_hash: str) -> int:
    period_key = await get_billing_period_key(phone_hash)
    async with _conn() as db:
        async with db.execute(
            "SELECT count FROM monthly_usage WHERE phone_hash = ? AND period_key = ?",
            (phone_hash, period_key),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def increment_monthly_message_count(phone_hash: str) -> int:
    """Increment count for current billing period; return new total."""
    period_key = await get_billing_period_key(phone_hash)
    async with _conn() as db:
        await db.execute(
            """INSERT INTO monthly_usage (phone_hash, period_key, count) VALUES (?, ?, 1)
               ON CONFLICT(phone_hash, period_key) DO UPDATE SET count = count + 1""",
            (phone_hash, period_key),
        )
        await db.commit()
        async with db.execute(
            "SELECT count FROM monthly_usage WHERE phone_hash = ? AND period_key = ?",
            (phone_hash, period_key),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 1


async def credit_monthly_messages(phone_hash: str, amount: int) -> int:
    """Decrease count by amount (floor 0). Returns new count."""
    period_key = await get_billing_period_key(phone_hash)
    async with _conn() as db:
        await db.execute(
            """UPDATE monthly_usage SET count = MAX(0, count - ?)
               WHERE phone_hash = ? AND period_key = ?""",
            (amount, phone_hash, period_key),
        )
        await db.commit()
        async with db.execute(
            "SELECT count FROM monthly_usage WHERE phone_hash = ? AND period_key = ?",
            (phone_hash, period_key),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ---------------------------------------------------------------------------
# Daily new-user onboarding cap
# ---------------------------------------------------------------------------

async def get_new_user_count_last_24h() -> int:
    """Count distinct new users onboarded in the rolling 24-hour window."""
    async with _conn() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM new_user_onboards WHERE onboarded_at > datetime('now', '-24 hours')"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def record_new_user_onboard(phone_hash: str) -> None:
    """Record a successful new-user onboard with current UTC timestamp. Idempotent."""
    async with _conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO new_user_onboards (phone_hash, onboarded_at) VALUES (?, datetime('now'))",
            (phone_hash,),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Right-to-deletion
# ---------------------------------------------------------------------------

async def delete_monthly_usage(phone_hash: str) -> None:
    """Wipe usage counts only. Trial flag and access record are intentionally preserved
    so a returning deleted client cannot re-use the free trial."""
    async with _conn() as db:
        await db.execute("DELETE FROM monthly_usage WHERE phone_hash = ?", (phone_hash,))
        await db.execute("DELETE FROM free_trial_msg_counts WHERE phone_hash = ?", (phone_hash,))
        await db.commit()


# ---------------------------------------------------------------------------
# Stripe customer ID cache
# ---------------------------------------------------------------------------

async def store_stripe_customer_id(phone_hash: str, customer_id: str) -> None:
    async with _conn() as db:
        await db.execute(
            "INSERT OR REPLACE INTO stripe_customer_ids (phone_hash, customer_id) VALUES (?, ?)",
            (phone_hash, customer_id),
        )
        await db.commit()


async def get_stripe_customer_id(phone_hash: str) -> str | None:
    async with _conn() as db:
        async with db.execute(
            "SELECT customer_id FROM stripe_customer_ids WHERE phone_hash = ?", (phone_hash,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None
