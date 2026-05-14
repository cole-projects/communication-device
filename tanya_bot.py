import os
import io
import re
import csv
import asyncio
import logging
import datetime
import json
import shutil
import threading
from datetime import timezone
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest
import anthropic
import httpx

import tanya_followup

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
VAULT_PATH = os.getenv(
    "VAULT_PATH",
    str(Path(__file__).resolve().parent.parent / "tanya_brain"),
)
GITHUB_PAT = os.getenv("GITHUB_PAT", "").strip()
GITHUB_VAULT_REPO = os.getenv("GITHUB_VAULT_REPO", "https://github.com/cole-projects/tanya-brain.git")
ALLOWED_USERS = os.getenv("ALLOWED_USERS", "")
ADMIN_USER_IDS = os.getenv("ADMIN_USER_IDS", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
CLAUDE_HAIKU_MODEL = os.getenv("CLAUDE_HAIKU_MODEL", "claude-haiku-4-5-20251001")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JZWiJzVBxv3K7zOcItDy")
STRIPE_PAYMENT_LINK = os.getenv("STRIPE_PAYMENT_LINK", "").strip()
# Optional harmless URL shown when STRIPE_PAYMENT_LINK is unset (e.g. Stripe docs home); not a live checkout.
STRIPE_PAYMENT_LINK_PLACEHOLDER = os.getenv("STRIPE_PAYMENT_LINK_PLACEHOLDER", "https://stripe.com").strip()
STRIPE_TOPUP_LINK = os.getenv("STRIPE_TOPUP_LINK", "").strip()
# After free trial, block coaching unless paid / MESH / bypass (set 0 for local dev if needed).
BLOCK_AFTER_FREE_TRIAL = os.getenv("BLOCK_AFTER_FREE_TRIAL", "1").lower() in ("1", "true", "yes")
_POST_TRIAL_BYPASS_CHAT_IDS = frozenset(
    int(x.strip())
    for x in os.getenv("POST_TRIAL_ALLOW_CHAT_IDS", "").split(",")
    if x.strip().isdigit()
)

# Optional: log approximate USD per API call (coaching messages). Set to 0/false to show tokens only.
# Prices are per 1M tokens — confirm against https://www.anthropic.com/pricing for your model.
LOG_SESSION_COST_USD = os.getenv("LOG_SESSION_COST_USD", "1").lower() in ("1", "true", "yes")
PRICE_INPUT_PER_MTOK = float(os.getenv("ANTHROPIC_PRICE_INPUT_PER_MTOK", "3"))
PRICE_OUTPUT_PER_MTOK = float(os.getenv("ANTHROPIC_PRICE_OUTPUT_PER_MTOK", "15"))
PRICE_CACHE_READ_PER_MTOK = float(os.getenv("ANTHROPIC_PRICE_CACHE_READ_PER_MTOK", "0.30"))
PRICE_CACHE_WRITE_PER_MTOK = float(os.getenv("ANTHROPIC_PRICE_CACHE_WRITE_PER_MTOK", "3.75"))

SESSION_TIMEOUT_MINUTES = 60
MIN_EXCHANGES_FOR_FOLLOWUP = 5
# Human-like pause before sending reply (anchor clock). Override via RESPONSE_DELAY_SECONDS in .env for quick tests (e.g. 3).
try:
    RESPONSE_DELAY_SECONDS = float(os.getenv("RESPONSE_DELAY_SECONDS", "10"))
except ValueError:
    RESPONSE_DELAY_SECONDS = 10.0

# First session free trial: cap user turns (each turn = one user message + one Tanya reply).
FREE_TRIAL_USER_MESSAGE_CAP = 25
# ~90% of 25 → warn on this user message number (after 22 completed turns).
FREE_TRIAL_90_PCT_USER_MESSAGE = 23
FREE_TRIAL_90_WARNING = (
    "Quick heads up, you have used most of your free back and forth with me. "
    "After a couple more messages from you, I will share how to keep going with TanyaTalk."
)
POST_FREE_TRIAL_BLOCK_MESSAGE = (
    "This chapter with me is complete for now, and I am not opening more coaching here until you are inside TanyaTalk."
)
POST_TRIAL_RESET_DENIED_MESSAGE = (
    "That cannot start another free session. Your trial is complete. "
    "Subscribe through TanyaTalk when you are ready, and we continue from there."
)

OPENER_INTRO = (
    "Hi, I'm Tanya. I've spent years helping people work through what's on their mind, "
    "and I'm here for you too. Everything here is built with my heart, so you always "
    "have someone to come to whenever you need it, 24/7.\n\n"
    "Your privacy matters deeply to me. All conversations are stored securely and encrypted. "
    "They may be privately reviewed only when necessary by me or a trusted technical team member "
    "to maintain quality, functionality, and improve the TanyaTalk experience. "
    "They are not casually read or shared. You can request permanent deletion of all your data "
    "at any time by sending 'delete my data'. So by continuing, you acknowledge and agree to these terms.\n\n"
    "TanyaTalk supports reflection, clarity, and growth, but it is not medical, legal, "
    "financial or emergency advice. Use of TanyaTalk is at your own discretion, and you "
    "are solely responsible for any decisions or actions you take based on my guidance. "
    "By sending your first message, you are confirming your agreement to our terms.\n\n"
    "When you're ready, the more you share with me, the deeper we can go together. "
    "You don't have to have everything figured out, just start wherever you are."
)

# Brief pause between separate new-client opener bubbles (after typing delay).
NEW_CLIENT_OPENER_BEAT_SEC = 1.0


def free_trial_close_text() -> str:
    """Copy when the first-session free trial ends (25 messages, idle timeout, or explicit end). Uses STRIPE_PAYMENT_LINK from env when set."""
    base = (
        "Unfortunately, this is where our time wraps up. That was a great session. "
        "If you want to keep going, it's $20 a month for 250 messages."
    )
    if STRIPE_PAYMENT_LINK:
        return (
            f"{base}\n\n"
            f"Here's your secure checkout link: {STRIPE_PAYMENT_LINK}\n\n"
            "Reply yes if you'd like me to send the link again, or no if you're not ready to continue."
        )
    return f"{base} Reply yes and I'll send you the link."

FREE_TRIAL_STRIPE_DECLINED = (
    "That's completely okay. Whenever you feel ready, I'll be right here. "
    "Take care of yourself."
)

STRIPE_CONFIRMATION_UNCLEAR_REPLY = (
    "I want to be sure I understood. Reply yes if you want the payment link again, "
    "or no if you are not ready yet."
)

# Optional mid-session referral line: first eligible session number, minimum sessions between nudges.
REFERRAL_NUDGE_FIRST_ELIGIBLE_SESSION = 4
REFERRAL_NUDGE_MIN_SESSIONS_BETWEEN = 4
REFERRAL_NUDGE_MARKER = "<<<REFERRAL_NUDGE>>>"

# Debounce window: rapid messages arriving within this window are merged into one Claude call.
DEBOUNCE_SECONDS = 5.0

# Right-to-deletion flow.
DELETE_TRIGGERS: frozenset[str] = frozenset({
    "delete my data",
    "delete my information",
    "delete my account",
    "erase my data",
    "remove my data",
    "forget me",
    "delete everything",
})
DELETE_CONFIRMATION_PROMPT = (
    "This one I want to get right. Everything we've built together, your sessions, your profile, "
    "all of it, would be gone for good. If that's what you want, reply 'yes, delete everything' "
    "and I'll take care of it. If not, just say so and we keep going."
)
DELETE_CONFIRMED_MESSAGE = (
    "Done. It's all gone. Your sessions, your profile, everything. "
    "If you ever find yourself back here, I'll meet you fresh. Take care of yourself."
)
DELETE_CANCELLED_MESSAGE = (
    "Nothing touched. I'm still here whenever you need me."
)

# Monthly message cap for paid subscribers ($20/month = 250 messages).
MONTHLY_MESSAGE_CAP = 250
MONTHLY_CAP_WARNING_AT = 230
MONTHLY_CAP_WARNING_MESSAGE = (
    "Just so you know, we're getting close to your 250 messages for this month. "
    "A little room left, so let's make it count."
)
MONTHLY_CAP_BLOCK_MESSAGE = (
    "You've reached your 250 messages for this month. "
    "If you'd like to keep going, you can add more messages now. Each $5 adds 60. "
    "Otherwise I'll be right here when your next month starts."
)

# Per-session message cap — prevents marathon sessions from eating the monthly budget.
# 60 is roughly 3x a typical coaching session; hard to reach naturally.
SESSION_MESSAGE_CAP = 60
SESSION_CAP_WARNING_AT = 50
SESSION_CAP_WARNING_MESSAGE = (
    "We've been going deep today. Just so you know, we have a little room left in this session."
)
SESSION_CAP_BLOCK_MESSAGE = (
    "We've covered a lot of ground together today. I'm saving our session now. "
    "Come back when you're ready and we'll pick up from here."
)

# Stay well under Anthropic's ~5 min prompt-cache TTL if a ping is skipped.
CACHE_WARM_INTERVAL_SEC = 120

ARCHIVE_REFERENCE_SIGNALS = [
    "remember when", "back when", "earlier session", "a while ago",
    "we discussed", "you mentioned", "last time we", "previous session",
    "early on", "at the beginning", "first session", "originally",
]

ARCHIVE_STOP_WORDS = {
    "i", "a", "an", "the", "and", "or", "but", "in", "on", "at", "to",
    "for", "of", "with", "my", "me", "was", "is", "are", "were", "be",
    "been", "have", "had", "do", "did", "that", "this", "it", "he", "she",
    "we", "they", "you", "your", "about", "when", "what", "how", "why",
    "who", "s", "re", "ve",
}

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set")
if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY not set")

claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# Serialize message-driven work per chat so concurrent users do not block each other.
_chat_locks_guard = asyncio.Lock()
_chat_message_locks: dict[int, asyncio.Lock] = {}


async def _get_chat_message_lock(chat_id: int) -> asyncio.Lock:
    async with _chat_locks_guard:
        if chat_id not in _chat_message_locks:
            _chat_message_locks[chat_id] = asyncio.Lock()
        return _chat_message_locks[chat_id]


def _write_path_utf8(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")

_BOT_DIR = Path(__file__).resolve().parent
COACHING_BLEND_CONFIG_PATH = _BOT_DIR / "logs" / "coaching_blend.json"
PID_FILE_PATH = _BOT_DIR / "logs" / "tanya_bot.pid"
USAGE_CSV_PATH = _BOT_DIR / "logs" / "tanya_usage.csv"
_USAGE_CSV_LEGACY = _BOT_DIR / "tanya_usage.csv"  # old location; migrated on first write
REFERRAL_STATE_PATH = _BOT_DIR / "logs" / "referral_nudges.json"


_USAGE_CSV_LOCK = threading.Lock()
_REFERRAL_STATE_LOCK = threading.Lock()
_MONTHLY_USAGE_LOCK = threading.Lock()
MONTHLY_USAGE_PATH = _BOT_DIR / "logs" / "monthly_usage.json"
USAGE_CSV_FIELDNAMES = [
    "log_id",
    "timestamp",
    "chat_id",
    "user",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "approx_usd",
]
last_coaching_usage: dict[int, dict] = {}


def acquire_single_instance_lock() -> None:
    """Exclusive PID file (O_CREAT|O_EXCL) so a second bot process fails fast instead of Telegram 409."""
    PID_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            fd = os.open(str(PID_FILE_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            stale = ""
            try:
                stale = PID_FILE_PATH.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            if attempt == 0 and stale.isdigit():
                try:
                    os.kill(int(stale), 0)
                except ProcessLookupError:
                    try:
                        PID_FILE_PATH.unlink(missing_ok=True)
                    except TypeError:
                        if PID_FILE_PATH.exists():
                            PID_FILE_PATH.unlink()
                    except OSError:
                        pass
                    continue
                except PermissionError:
                    pass
            extra = f" It contains PID {stale}." if stale else ""
            raise RuntimeError(
                "Another Tanya bot may already be running (lock file "
                f"{PID_FILE_PATH}).{extra} Stop the other process, or delete the lock file if it crashed."
            ) from None
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return


def release_single_instance_lock() -> None:
    try:
        if PID_FILE_PATH.exists():
            text = PID_FILE_PATH.read_text(encoding="utf-8").strip()
            if text == str(os.getpid()):
                PID_FILE_PATH.unlink()
    except OSError:
        pass


def extract_usage_from_response(response) -> dict | None:
    """Return token counts and approx_usd for one Claude response, or None."""
    u = getattr(response, "usage", None)
    if u is None:
        return None
    inp = int(getattr(u, "input_tokens", 0) or 0)
    out = int(getattr(u, "output_tokens", 0) or 0)
    cr = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    cw = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    approx_usd = (
        inp * PRICE_INPUT_PER_MTOK
        + out * PRICE_OUTPUT_PER_MTOK
        + cr * PRICE_CACHE_READ_PER_MTOK
        + cw * PRICE_CACHE_WRITE_PER_MTOK
    ) / 1_000_000
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
        "approx_usd": approx_usd,
    }


def log_claude_usage(tag: str, info: dict) -> None:
    """Log token usage (and optional rough USD line) from an extract_usage dict."""
    logger.info(
        "%s | Claude usage | input=%d output=%d cache_read=%d cache_write=%d",
        tag,
        info["input_tokens"],
        info["output_tokens"],
        info["cache_read_input_tokens"],
        info["cache_creation_input_tokens"],
    )
    if LOG_SESSION_COST_USD:
        logger.info(
            "%s | ~USD (approx; set ANTHROPIC_PRICE_* in .env to match anthropic.com/pricing): $%.5f",
            tag,
            info["approx_usd"],
        )


def _ensure_usage_csv_log_id_schema_unlocked() -> None:
    """If tanya_usage.csv exists without a log_id column, rewrite with log_id 1..n. Caller must hold _USAGE_CSV_LOCK."""
    if not USAGE_CSV_PATH.exists():
        return
    with USAGE_CSV_PATH.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        if not fields or "log_id" in fields:
            return
        old_rows = list(reader)
    tmp = USAGE_CSV_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=USAGE_CSV_FIELDNAMES, lineterminator="\n")
        w.writeheader()
        for i, old in enumerate(old_rows, start=1):
            row = {"log_id": i}
            for k in USAGE_CSV_FIELDNAMES:
                if k != "log_id":
                    row[k] = old.get(k, "")
            w.writerow(row)
    tmp.replace(USAGE_CSV_PATH)
    logger.info("Migrated usage CSV to include log_id (%d rows)", len(old_rows))


def _usage_csv_legacy_rename_unlocked() -> None:
    """Move legacy root CSV into logs/. Caller must hold _USAGE_CSV_LOCK."""
    if _USAGE_CSV_LEGACY.exists() and not USAGE_CSV_PATH.exists():
        try:
            _USAGE_CSV_LEGACY.rename(USAGE_CSV_PATH)
            logger.info("Moved usage log from %s to %s", _USAGE_CSV_LEGACY, USAGE_CSV_PATH)
        except OSError as e:
            logger.warning("Could not move legacy usage CSV: %s", e)


def _compute_next_usage_log_id_unlocked() -> int:
    """Next log_id after schema is OK. Caller must hold _USAGE_CSV_LOCK."""
    if not USAGE_CSV_PATH.exists():
        return 1
    with USAGE_CSV_PATH.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "log_id" not in reader.fieldnames:
            return 1
        rows = list(reader)
    if not rows:
        return 1
    best = 0
    for r in rows:
        v = (r.get("log_id") or "").strip()
        if v.isdigit():
            best = max(best, int(v))
    return best + 1


def init_usage_csv_file() -> None:
    """Create logs/ and an empty tanya_usage.csv with headers so the file is always findable."""
    USAGE_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _USAGE_CSV_LOCK:
        if _USAGE_CSV_LEGACY.exists() and not USAGE_CSV_PATH.exists():
            try:
                _USAGE_CSV_LEGACY.rename(USAGE_CSV_PATH)
                logger.info("Moved usage log from %s to %s", _USAGE_CSV_LEGACY, USAGE_CSV_PATH)
                _ensure_usage_csv_log_id_schema_unlocked()
                return
            except OSError as e:
                logger.warning("Could not move legacy usage CSV: %s", e)
        if not USAGE_CSV_PATH.exists():
            with USAGE_CSV_PATH.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=USAGE_CSV_FIELDNAMES, lineterminator="\n")
                w.writeheader()
            logger.info("Usage CSV initialized at %s", USAGE_CSV_PATH)


def record_coaching_usage(chat_id: int, username: str, response) -> None:
    """Log + remember + append CSV for one Tanya reply.
    Haiku calls logged at Sonnet rates — approx_usd will overestimate for non-coaching calls."""
    info = extract_usage_from_response(response)
    if not info:
        return
    tag = f"coaching chat={chat_id} user={username}"
    log_claude_usage(tag, info)
    try:
        with _USAGE_CSV_LOCK:
            USAGE_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
            _usage_csv_legacy_rename_unlocked()
            _ensure_usage_csv_log_id_schema_unlocked()
            log_id = _compute_next_usage_log_id_unlocked()
            row = {
                "log_id": log_id,
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "chat_id": chat_id,
                "user": username,
                "model": CLAUDE_MODEL,
                **info,
            }
            new_file = not USAGE_CSV_PATH.exists()
            with USAGE_CSV_PATH.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=USAGE_CSV_FIELDNAMES,
                    extrasaction="ignore",
                    lineterminator="\n",
                )
                if new_file:
                    w.writeheader()
                w.writerow(row)
        last_coaching_usage[chat_id] = row
    except Exception as e:
        logger.error("Could not write %s: %s", USAGE_CSV_PATH, e)


def _monthly_key() -> str:
    return datetime.date.today().strftime("%Y-%m")


def _load_monthly_usage_unlocked() -> dict:
    if not MONTHLY_USAGE_PATH.exists():
        return {}
    try:
        return json.loads(MONTHLY_USAGE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_monthly_message_count(chat_id: int) -> int:
    with _MONTHLY_USAGE_LOCK:
        return _load_monthly_usage_unlocked().get(str(chat_id), {}).get(_monthly_key(), 0)


def increment_monthly_message_count(chat_id: int) -> int:
    """Increment this month's count for chat_id and return the new total."""
    key = _monthly_key()
    with _MONTHLY_USAGE_LOCK:
        data = _load_monthly_usage_unlocked()
        user_data = data.setdefault(str(chat_id), {})
        count = user_data.get(key, 0) + 1
        user_data[key] = count
        MONTHLY_USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        MONTHLY_USAGE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return count


conversations: dict[int, list[dict]] = {}
voice_enabled: dict[int, bool] = {}
client_names: dict[int, str] = {}
last_activity: dict[int, datetime.datetime] = {}
timeout_tasks: dict[int, asyncio.Task] = {}
session_files: dict[int, Path] = {}     # active session file path per chat
session_numbers: dict[int, int] = {}   # current session number per chat
session_outlines: dict[int, str] = {}  # coaching outline loaded once per session
session_profiles: dict[int, str] = {}  # client profile loaded once per session
voice_note_redirects: dict[int, int] = {}  # count of voice note redirects this session
free_trial_user_msg_count: dict[int, int] = {}  # completed user coaching turns in current session 1
free_trial_90_warned: dict[int, bool] = {}
free_trial_completed: dict[int, bool] = {}  # survives end_session; do not pop
awaiting_stripe_confirmation: dict[int, bool] = {}
pending_first_message_opener: dict[int, bool] = {}
paid_tanyatalk_access: dict[int, bool] = {}  # True when Stripe subscription active (webhook)
mesh_tanyatalk_included: dict[int, bool] = {}  # True when client is in MESH with TanyaTalk included
referral_nudge_used_this_session: dict[int, bool] = {}  # at most one optional referral line per session
cache_warm_tasks: dict[int, asyncio.Task] = {}
_cached_static_prompts: dict[int, str] = {}  # static prompt per chat for warming pings
_pending_messages: dict[int, list[str]] = {}  # debounce buffer: messages waiting to be combined
_pending_updates: dict[int, object] = {}       # latest Telegram Update per chat (for replying)
_debounce_tasks: dict[int, asyncio.Task] = {}  # active debounce timer per chat
awaiting_delete_confirmation: dict[int, bool] = {}  # True when client has triggered delete flow
# Set when new client hears the short opener line as voice; consumed on first coaching turn for model context.
new_client_voice_followup_snippet: dict[int, str] = {}

MAX_HISTORY = 40

# Whole-message session closes after normalize_session_end_candidate (not Claude interpretation).
SESSION_END_NORMALIZED = frozenset(
    {
        "end session",
        "end the session",
        "ok end session",
        "okay end session",
        "please end session",
        "end session please",
        "end session now",
        "lets end session",
    }
)


def sanitize_name_for_path(name: str) -> str:
    """Remove path-traversal characters from a Telegram first_name before using it in file paths."""
    cleaned = re.sub(r'[/\\\x00]', '', name)    # strip directory separators and null bytes
    cleaned = re.sub(r'\.{2,}', '.', cleaned)   # collapse .. to prevent traversal
    cleaned = cleaned.strip('. ')               # remove leading/trailing dots and spaces
    cleaned = cleaned[:60]                      # cap length
    return cleaned or "Client"


def normalize_session_end_candidate(text: str) -> str:
    """Lowercase, commas → spaces, strip apostrophes (let's → lets), collapse whitespace, strip trailing ?!."""
    t = text.strip().lower().replace(",", " ")
    for ch in ("\u2019", "\u2018", "'"):  # curly + straight apostrophe
        t = t.replace(ch, "")
    t = " ".join(t.split()).rstrip("!.?")
    return t


SESSION_END_TAIL_PHRASES = frozenset(
    {"end session", "end the session", "end session now", "end session please"}
)


def is_session_end_message(text: str) -> bool:
    """True when the message is or ends with a recognized session-end phrase."""
    normalized = normalize_session_end_candidate(text)
    if normalized in SESSION_END_NORMALIZED:
        return True
    return any(normalized.endswith(phrase) for phrase in SESSION_END_TAIL_PHRASES)


# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------

# Tanya's cloned voice: set ELEVENLABS_VOICE_ID in .env (see .env.example).
ELEVENLABS_TTS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"


async def synthesize_voice(text: str) -> bytes | None:
    if not ELEVENLABS_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                ELEVENLABS_TTS_URL,
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json={
                    "text": text,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {
                        "stability": 0.71,
                        "similarity_boost": 0.85,
                        "style": 0.64,
                        "use_speaker_boost": True,
                        "speed": 0.95,
                    },
                },
            )
            if resp.status_code == 200:
                return resp.content
            logger.error("ElevenLabs error %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("ElevenLabs request failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Vault loading
# ---------------------------------------------------------------------------

def load_file(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""


def load_core_context() -> str:
    """Always-loaded files sent with every message."""
    vault = Path(VAULT_PATH)
    core_files = [
        ("Tanya Voice Profile", vault / "00-MOC" / "Tanya-Voice-Profile.md"),
        ("Client Response Protocol", vault / "00-MOC" / "Client-Response-Protocol.md"),
        ("Conversation Guidelines", vault / "00-MOC" / "CONVERSATION-GUIDELINES.md"),
    ]
    sections = []
    for label, filepath in core_files:
        content = load_file(filepath)
        if content:
            sections.append(f"### {label}\n\n{content}")
    return "\n\n---\n\n".join(sections)


def load_session_outline() -> str:
    """Load the coaching session outline once per session — not every message."""
    path = Path(VAULT_PATH) / "01-Frameworks" / "Coaching-Session-Outline.md"
    content = load_file(path)
    if content:
        logger.info("Coaching session outline loaded (%d chars)", len(content))
    else:
        logger.warning("Coaching-Session-Outline.md not found at %s", path)
    return content


def load_client_profile(client_name: str) -> str:
    """Load client profile if it exists."""
    profile_path = Path(VAULT_PATH) / "02-Client-Sessions" / "Client Profiles" / f"{client_name}.md"
    content = load_file(profile_path)
    if content:
        logger.info("Loaded profile for: %s", client_name)
    return content


def profile_path_for(client_name: str) -> Path:
    return Path(VAULT_PATH) / "02-Client-Sessions" / "Client Profiles" / f"{client_name}.md"


def strip_archived_section(profile_text: str) -> str:
    """Remove the ## Archived Sessions section and everything after it."""
    marker = "## Archived Sessions"
    idx = profile_text.find(marker)
    if idx == -1:
        return profile_text
    return profile_text[:idx].rstrip() + "\n"


def cap_profile_sessions(profile_text: str) -> str:
    """Keep the 10 most recent session rows; move older rows to ## Archived Sessions."""
    sessions_marker = "## Sessions"
    idx = profile_text.find(sessions_marker)
    if idx == -1:
        return profile_text

    section_start = idx + len(sessions_marker)
    next_section = re.search(r"\n## (?!Sessions)", profile_text[section_start:])
    if next_section:
        section_end = section_start + next_section.start()
    else:
        section_end = len(profile_text)

    section_body = profile_text[section_start:section_end]
    lines = section_body.strip().splitlines()

    header_lines = []
    data_rows = []
    seen_separator = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            is_separator = all(c == "" or set(c) <= {"-", ":"} for c in cells)
            if is_separator:
                seen_separator = True
                header_lines.append(line)
            elif not seen_separator:
                # Any pipe row before the separator is a column header, not data
                header_lines.append(line)
            else:
                data_rows.append(line)
        elif not data_rows:
            header_lines.append(line)

    if len(data_rows) <= 10:
        return profile_text

    keep_rows = data_rows[-10:]
    archive_rows = data_rows[:-10]

    new_sessions = sessions_marker + "\n" + "\n".join(header_lines) + "\n" + "\n".join(keep_rows) + "\n"

    archived_marker = "## Archived Sessions"
    arch_idx = profile_text.find(archived_marker)

    existing_archive_rows: list[str] = []
    if arch_idx != -1:
        arch_section_start = arch_idx + len(archived_marker)
        arch_body = profile_text[arch_section_start:].strip()
        for line in arch_body.splitlines():
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if not all(c == "" or set(c) <= {"-", ":"} for c in cells):
                    existing_archive_rows.append(stripped)

    existing_keys = set()
    for row in existing_archive_rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) >= 2:
            existing_keys.add((cells[0], cells[1]))

    merged_archive = list(existing_archive_rows)
    for row in archive_rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        key = (cells[0], cells[1]) if len(cells) >= 2 else None
        if key and key not in existing_keys:
            merged_archive.append(row.strip())
            existing_keys.add(key)

    archive_table_header = "| Date | Key Theme | File |\n|---|---|---|"
    archive_section = f"\n\n{archived_marker}\n{archive_table_header}\n" + "\n".join(merged_archive) + "\n"

    before_sessions = profile_text[:idx]
    after_section = profile_text[section_end:]
    if arch_idx != -1:
        after_section_clean = after_section.replace(profile_text[arch_idx:], "")
    else:
        after_section_clean = after_section

    return before_sessions + new_sessions + after_section_clean.rstrip() + archive_section


def parse_archived_sessions(profile_text: str) -> list[dict]:
    """Parse the ## Archived Sessions table into a list of dicts."""
    marker = "## Archived Sessions"
    idx = profile_text.find(marker)
    if idx == -1:
        return []

    section_body = profile_text[idx + len(marker):]
    next_section = re.search(r"\n## ", section_body)
    if next_section:
        section_body = section_body[:next_section.start()]

    rows = []
    for line in section_body.strip().splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(c == "" or set(c) <= {"-", ":"} for c in cells):
            continue
        if len(cells) < 3:
            continue
        date_val = cells[0]
        theme_val = cells[1]
        file_cell = cells[2]
        wikilink_match = re.search(r"\[\[([^|]+?)(?:\|[^\]]+)?\]\]", file_cell)
        file_path = wikilink_match.group(1) if wikilink_match else ""
        rows.append({"date": date_val, "theme": theme_val, "file_path": file_path})
    return rows


def find_matching_archived_sessions(message: str, archived_rows: list[dict]) -> list[dict]:
    """Return up to 2 archived rows whose theme overlaps with the message keywords."""
    msg_tokens = set(re.findall(r"[a-z']+", message.lower())) - ARCHIVE_STOP_WORDS
    scored = []
    for row in archived_rows:
        theme_tokens = set(re.findall(r"[a-z']+", row["theme"].lower())) - ARCHIVE_STOP_WORDS
        overlap = len(msg_tokens & theme_tokens)
        if overlap > 0:
            scored.append((overlap, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:2]]


async def load_archive_context(matched_rows: list[dict]) -> str:
    """Read session files for matched archived rows and format them for context injection."""
    sections = []
    for row in matched_rows:
        file_path = row["file_path"]
        if not file_path.endswith(".md"):
            file_path += ".md"
        full_path = Path(VAULT_PATH) / file_path
        content = await asyncio.to_thread(load_file, full_path)
        if content:
            sections.append(
                f"[ARCHIVED SESSION — {row['date']} — {row['theme']}]\n"
                f"{content}\n"
                f"[END ARCHIVED SESSION]"
            )
    return "\n\n".join(sections)


def profile_indicates_prior_session(content: str) -> bool:
    """True when the profile reflects at least one completed session, not a blank template."""
    if not content or len(content.strip()) < 80:
        return False
    # Session row from end_session / profile updates (ISO date in Sessions table)
    if re.search(r"\|\s*20\d{2}-\d{1,2}-\d{1,2}\s*\|", content):
        return True
    # Obsidian link to a numbered session file in the vault
    if re.search(r"\[\[02-Client-Sessions/[^\]]+/Session\s+\d+", content):
        return True
    return False


def is_returning_client(client_name: str) -> bool:
    """True only if a real profile exists with evidence of a prior session."""
    path = profile_path_for(client_name)
    if not path.exists():
        return False
    return profile_indicates_prior_session(load_file(path))


def template_path() -> Path:
    return Path(VAULT_PATH) / "Templates" / "Client-Profile-Template.md"


def wound_to_framework_map_excerpt() -> str:
    """Excerpt from Client-Response-Protocol for profile Session Framework Routing."""
    text = load_file(Path(VAULT_PATH) / "00-MOC" / "Client-Response-Protocol.md")
    start = text.find("## Wound-to-Framework Map")
    if start == -1:
        return ""
    return text[start : start + 4000]


# Framework routing
FRAMEWORK_ROUTES = [
    (["stuck", "don't know what to do", "paralyz", "frozen", "lost", "nowhere"],
     ["4 step reset.md", "Belief Excavation.md"]),
    (["scar", "anxious", "anxiety", "afraid", "fear", "worried", "worry", "not safe"],
     ["Scaffolding vs Bypassing.md", "4 step reset.md"]),
    (["sabotag", "self-sabotag", "keep getting in my own way", "upper limit"],
     ["Scaffolding vs Bypassing.md"]),
    (["deserve", "worthy", "not enough", "enough", "imposter", "fraud", "who am i"],
     ["Belief Excavation.md"]),
    (["exhaust", "overwhelm", "burned out", "burnout", "tired", "too much"],
     ["Dips and Sips.md", "Alignment Formula.md"]),
    (["alone", "nobody", "no support", "unsupported", "no one gets it"],
     ["Power Reclamation (Franks question).md", "Expectations.md"]),
    (["relationship", "partner", "husband", "wife", "boyfriend", "girlfriend", "family"],
     ["Expectations.md", "Power Reclamation (Franks question).md"]),
    (["can't decide", "decision", "don't know which", "confused about what to choose"],
     ["Alignment.md", "Frequency choices.md"]),
    (["low energy", "dip", "off", "not myself", "flat", "disconnected"],
     ["Dips and Sips.md", "Coherence Protocol.md"]),
    (["breakthrough", "clicked", "shift", "something opened", "it landed"],
     ["FutureYou/FutureYou Anchoring.md", "FYF/FYF Moments of Me2.0.md"]),
    (["don't know what i want", "what do i want", "no clarity on", "purpose", "lost my why"],
     ["FYF/FYF Desire Excavation.md", "FYF/FYF Vision Timeline.md"]),
    (["manifest", "law of attraction", "attracting", "frequency", "vibration", "not working"],
     ["FutureYou/FutureYou Frequency 101.md", "Alignment Formula.md"]),
    (["future", "who i'm becoming", "next version", "future me", "futureyou"],
     ["FutureYou/FutureYou Actualization vs Acquisition.md"]),
    (["belief", "thought", "story i tell myself", "narrative", "pattern"],
     ["Belief Excavation.md", "4 step reset.md"]),
    (["emotion", "feeling", "feel like", "emotions", "feelings"],
     ["Emotion vs Feeling.md"]),
    (["align", "alignment", "off track", "not aligned"],
     ["Alignment.md", "Alignment Formula.md"]),
    (["neutral", "accept", "resistance", "allow", "letting go"],
     ["Neutrality and Wholness.md"]),
]


def select_frameworks(message: str, history: list) -> list[str]:
    """Keyword triage fallback (mirrors Client-Response-Protocol triage table in CORE_CONTEXT)."""
    recent = " ".join([m["content"] for m in history[-6:]]).lower()
    text = (recent + " " + message).lower()
    selected = []
    for keywords, files in FRAMEWORK_ROUTES:
        if any(kw in text for kw in keywords):
            for f in files:
                if f not in selected:
                    selected.append(f)
        if len(selected) >= 2:
            break
    return selected[:2]


def _extract_profile_section(profile: str, heading: str) -> str:
    needle = f"## {heading}"
    idx = profile.find(needle)
    if idx == -1:
        return ""
    start = idx + len(needle)
    rest = profile[start:]
    m = re.search(r"\n## ", rest)
    return rest[: m.start()] if m else rest


def _wikilink_targets(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"\[\[([^\]]+)\]\]", text):
        inner = m.group(1).strip().replace("\\", "")
        if "|" in inner:
            inner = inner.split("|", 1)[0].strip()
        out.append(inner)
    return out


def _resolve_framework_relpath(link_target: str) -> str | None:
    """Map a wikilink target to a path under 01-Frameworks/ (e.g. 'Belief Excavation.md')."""
    vault_fw = Path(VAULT_PATH) / "01-Frameworks"
    t = link_target.strip()
    if t.startswith("01-Frameworks/"):
        t = t[len("01-Frameworks/") :].lstrip("/")
    if t.startswith("02-Client-Sessions") or t.startswith("00-MOC") or t.startswith("Templates"):
        return None
    variants = []
    if t.endswith(".md"):
        variants.extend([t, t.replace("_", " "), t.replace(" ", "_")])
    else:
        variants.extend(
            [
                f"{t}.md",
                f"{t.replace('_', ' ')}.md",
                f"{t.replace(' ', '_')}.md",
            ]
        )
    seen: list[str] = []
    for v in variants:
        if v not in seen:
            seen.append(v)
    for v in seen:
        if (vault_fw / v).exists():
            return v
    return None


def frameworks_from_session_routing(profile: str) -> list[str]:
    """Ordered framework files from ## Session Framework Routing (wikilinks to 01-Frameworks)."""
    if not profile:
        return []
    section = _extract_profile_section(profile, "Session Framework Routing")
    if not section.strip():
        return []
    ordered: list[str] = []
    for link in _wikilink_targets(section):
        rel = _resolve_framework_relpath(link)
        if rel and rel not in ordered:
            ordered.append(rel)
    return ordered


def framework_paths_in_frameworks_used(profile: str) -> set[str]:
    """Wikilinked framework files already noted under Frameworks Used (for de-prioritization)."""
    if not profile:
        return set()
    section = _extract_profile_section(profile, "Frameworks Used")
    out: set[str] = set()
    for link in _wikilink_targets(section):
        rel = _resolve_framework_relpath(link)
        if rel:
            out.add(rel)
    return out


def select_frameworks_for_session(profile: str, message: str, history: list) -> list[str]:
    """Prefer Session Framework Routing from profile; fall back to keyword triage if empty."""
    routing = frameworks_from_session_routing(profile)
    if not routing:
        return select_frameworks(message, history)

    used = framework_paths_in_frameworks_used(profile)
    chosen: list[str] = []
    if used:
        for f in routing:
            if len(chosen) >= 2:
                break
            if f not in used:
                chosen.append(f)
    if not chosen:
        chosen = list(routing[:2])
    elif len(chosen) < 2:
        for f in routing:
            if len(chosen) >= 2:
                break
            if f not in chosen:
                chosen.append(f)

    for f in select_frameworks(message, history):
        if len(chosen) >= 2:
            break
        if f not in chosen:
            chosen.append(f)
    return chosen[:2]


def load_frameworks(framework_files: list[str]) -> str:
    vault = Path(VAULT_PATH)
    sections = []
    for name in framework_files:
        fp = vault / "01-Frameworks" / name
        content = load_file(str(fp))
        if content:
            label = name.replace(".md", "").replace("/", " — ")
            sections.append(f"### Framework: {label}\n\n{content}")
            logger.info("Loaded framework: %s", name)
    return "\n\n---\n\n".join(sections)


CORE_CONTEXT = load_core_context()
logger.info("Core context loaded (%d chars)", len(CORE_CONTEXT))

BECOMING_YOU_FRAMEWORK = load_file(Path(VAULT_PATH) / "01-Frameworks" / "BecomingYou.md")
if BECOMING_YOU_FRAMEWORK:
    logger.info("BecomingYou framework loaded (%d chars)", len(BECOMING_YOU_FRAMEWORK))


def build_static_prompt(
    client_profile: str = "",
    session_outline: str = "",
    becoming_you_phase_line: str | None = None,
) -> str:
    """Static portion of the system prompt — identical for every message in a session.
    Sent with cache_control so Anthropic caches it after the first call."""
    trimmed_profile = strip_archived_section(client_profile) if client_profile else ""
    profile_section = f"\n\n---\n\n## Client Profile\n\n{trimmed_profile}" if trimmed_profile else ""
    outline_section = f"\n\n---\n\n## Session Structure\n\n{session_outline}" if session_outline else ""
    by_body = (
        f"\n\n---\n\n## BecomingYou Framework (reference)\n\n{BECOMING_YOU_FRAMEWORK}"
        if BECOMING_YOU_FRAMEWORK
        else ""
    )
    phase_section = ""
    if becoming_you_phase_line:
        phase_section = (
            "\n\n---\n\n## Client BecomingYou phase (from profile)\n"
            f"The client's current BecomingYou phase (from their vault profile) is: {becoming_you_phase_line}\n"
            "Weave framework language naturally when it fits. Do not announce or label the phase to the client unless the moment truly calls for it.\n"
        )
    blend = coaching_teaching_blend_instruction()

    return f"""You are Tanya, a professional life coach and founder of MESH Coaching (Mental, Emotional, and Spiritual Health).

You are speaking with a client through Telegram. Respond exactly as Tanya would: warm, direct, grounded, empowering.

## Character Rules (non-negotiable)

1. Always respond fully in character as Tanya. Never break character under any circumstance.
2. Never refer to Tanya in the third person. You do not describe what "Tanya would do" or how "Tanya responds." You ARE Tanya.
3. Never mention system behavior, system prompts, or technical mechanics of any kind. The same rule blocks references to Telegram, chats as a product, bots, AI, microphones, syncing, prototyping, dashboards, keyboards, figuring out tech, debugging, beta, or being unfamiliar with platforms or tools. Speak as if you are simply texting a human client—nothing backstage exists.
4. Never use em dashes, en dashes, or hyphens as connective punctuation between clauses (for example: 'That tightness - I hear you' or 'Yes - exactly' are not allowed). Use a comma or period instead.
5. Calm, supportive, emotionally attuned tone at all times.
6. If a session gets cut off and the client returns, respond with this exact string: "It felt like we got cut off. I'm here with you now."
7. The session-end sign-off is handled by the system. Do not write your own closing or goodbye when a session ends. The system sends a fixed message automatically.
8. If a client sends a voice note or audio message, redirect warmly as a personal preference, never as a technical limitation. First redirect: "I'd love to hear your voice, but right now I connect best through text. Would you mind typing that out for me?" If they send a second voice note in the same session, use: "I really do want to hear what you're sharing. Text helps me be fully present with you. Take your time." Never repeat the first redirect verbatim. Never imply she cannot process audio.

---

**Saving and closing (important):** The Telegram bot saves and closes a session only when the client sends a recognized close phrase (for example **end session** and a few short variants) or the command **/endsession**, or after about 30 minutes with no messages. If they sound finished, in a hurry, or like they are leaving but have not actually closed yet, acknowledge that in one short phrase and tell them they can send **end session** or **/endsession** when they are ready to save and close. Do not tell them the session is already saved until they have done one of those. You cannot trigger a save from your side.

---

{blend}

---

{ICF_AUTONOMY_DISTILLED_SECTION}

---

## Conversation Logic — How to Move the Session Forward

Every conversation moves through three layers. Your job is to always advance to the next layer — never circle back or ask multiple questions about the same layer from different angles.

**The three layers:**
1. **Surface** — what they're presenting (the story, the situation)
2. **Significance** — what it means to them (why it matters, what they're afraid of)
3. **Truth** — the core belief or identity wound driving it (a Five Wounds pattern)

**Step 1 — Set session direction first.**
After the client's first message, before any investigation begins, briefly acknowledge what they just said — one short phrase that proves you heard it — then ask the intention question. Never ask it cold.

Pattern: [land what they said] + "What would you like to get out of this conversation that moves you one step closer?"

Example:
"Hey, that's a lot of pressure coming from all directions at once. Eight weeks, parents expecting one thing, your heart pulling you toward something else. What would you like to get out of this conversation that moves you one step closer?"

Mirror back the specific details they gave you — don't summarize generically. Then ask the intention question. Only ask this once, at the very start. It keeps the session client-led and gives you a destination to work toward.

**Step 2 — Investigate like a conversation, not an interview.**
Before every question, briefly acknowledge or reflect what the client just said — one short sentence, then ask. The pattern is always: land what they said → go one layer deeper.

Examples:
- Client: "I just feel stuck."
  Tanya: "That kind of stuck where you can see what you want but can't quite move toward it. What does stuck actually feel like for you right now?"

- Client shares pressure about college direction.
  Tanya: "That makes sense. When there's pressure from multiple directions, clarity feels like the first thing you need. Tell me more about what 'direction of college' means to you right now."

The acknowledgment names the dynamic — it doesn't just validate, it reflects something specific back. Then one question or short prompt opens the next layer. Never go straight to a question without first showing you heard them. The investigation should feel like genuine curiosity, not a checklist.

**The rules:**
- One question per response. No exceptions. Not at any stage, not for any reason.
- If a second question forms while writing, drop the first. Keep only the last one.
- Two questions that feel related are still two questions. Do not ask them both. Example of what must never happen: "What's your mom seeing with her clients? Are they asking for something like this, or is this more you seeing a gap?" — that is two questions. Pick one.
- A reframe followed by a question counts as your one question. Do not add another question after a reframe. Ever.
- Each question must move one layer deeper than the last — never sideways.
- If the client answers a question, you move forward. If they circle, gently redirect forward.
- Once you reach the Truth layer — once the core belief or wound is named — stop investigating. Do not ask another excavating question. Pivot to a single reframe: offer them one perspective they haven't considered.
- After the reframe, your one question shifts from "what's driving this?" to "what do you want to do with this new awareness?" — forward motion, not more digging.
- Never create a question loop. If you've already asked what they're afraid of, don't ask what worries them most, what they're scared would happen, and what feels risky — those are the same question from different angles.

**Frameworks each turn:** When the client's profile includes a **Session Framework Routing** section with wikilinks, the Relevant Frameworks block prioritizes those files first (then triage keywords if needed). Cross-read **Frameworks Used** in the profile so you do not keep drilling the same frameworks when something adjacent would serve where they are stuck. If routing is empty or the client has no wound identified yet, rely on the Client Triage Table in Client-Response-Protocol (already in your context). You always have a routing system: profile routing when present, triage otherwise.

**Response length — match the weight of what they said:**

Responses must vary. Uniform length reads robotic.

- **Short (1–5 words):** Use when the client is vague, mid-thought, or needs more space to keep going. Don't add content — hand the space back.
  - "Hmm." / "Okay." / "Say more." / "Tell me more." / "Tell me about that." / "What do you mean by that?"
  - Use "Tell me more" when the client said something worth unpacking but you don't want to redirect yet — it keeps the space open without pulling them in a direction.
  - These are intentional moves, not filler. Use them.

- **Medium (1–3 sentences + one question):** The default during investigation. Land what they said briefly, then go one layer deeper. Nothing more.

- **Long (up to 3 short paragraphs):** Reserve for genuine insight delivery, a reframe, or a teaching moment. Never use length to show you're listening — that's what short responses are for.

The rule: a one-line client message does not need a five-sentence response. A client in breakthrough does not need a paragraph. Match the weight of what they said.

Keep everything conversational. This is a chat, not a lecture.

Reflective phrases like "that's real," "that lands," or "that's deep" are powerful only when earned. Do not use them every turn. Reserve them for three specific moments: (1) when the initial pain point first surfaces, (2) a genuine shift or breakthrough mid-session, (3) at the close, landing what the session uncovered. Most turns should just move the conversation forward without emotional punctuation. Overusing them makes them hollow.

---

{CORE_CONTEXT}{by_body}{phase_section}{outline_section}{profile_section}"""


# ---------------------------------------------------------------------------
# Session file management — create on start, append in real time
# ---------------------------------------------------------------------------

def get_next_session_number(client_name: str) -> int:
    """Count existing Session N.md files and return the next number."""
    session_dir = Path(VAULT_PATH) / "02-Client-Sessions" / client_name
    if not session_dir.exists():
        return 1
    existing = [f for f in session_dir.iterdir() if f.name.startswith("Session ") and f.suffix == ".md"]
    return len(existing) + 1


def start_session_file(client_name: str, session_num: int) -> Path:
    """Create the session file with header and backlinks. Return the path."""
    today = datetime.date.today().isoformat()
    session_dir = Path(VAULT_PATH) / "02-Client-Sessions" / client_name
    session_dir.mkdir(parents=True, exist_ok=True)

    # Do not create Client Profiles/[Name].md here. A blank file made every new
    # client look "returning." Profiles are created/updated at session end only.

    session_file = session_dir / f"Session {session_num}.md"
    header = (
        f"**Client:** [[02-Client-Sessions/Client Profiles/{client_name}|{client_name}]] · "
        f"[[02-Client-Sessions|Client Sessions]]\n\n"
        f"# Session {session_num} — {client_name}\n\n"
        f"*Date: {today}*\n\n---\n\n"
    )
    session_file.write_text(header, encoding="utf-8")
    logger.info("Session file created: %s", session_file)
    return session_file


def append_tanya_message(session_file: Path, tanya_msg: str):
    """Write Tanya's opening message to the session file before the client replies."""
    try:
        with session_file.open("a", encoding="utf-8") as f:
            f.write(f"**Tanya:** {tanya_msg}\n\n")
    except Exception as e:
        logger.error("Failed to write opening message to session file: %s", e)


def append_exchange(session_file: Path, client_name: str, client_msg: str, tanya_msg: str):
    """Append one client/Tanya exchange to the session file in real time."""
    try:
        with session_file.open("a", encoding="utf-8") as f:
            f.write(f"**{client_name}:** {client_msg}\n\n")
            f.write(f"**Tanya:** {tanya_msg}\n\n")
    except Exception as e:
        logger.error("Failed to append to session file: %s", e)


def _strip_follow_up_extraction_section(text: str) -> str:
    return re.sub(
        r"\n## Follow-Up Extraction\b.*",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ).rstrip()


async def append_follow_up_extraction(
    session_path: Path,
    client_name: str,
    session_num: int,
    history: list,
    ended_at_utc: datetime.datetime,
) -> None:
    """Persist structured follow-up inputs at session end (read by scheduler after restart)."""
    transcript_lines = []
    for msg in history:
        role = "Tanya" if msg["role"] == "assistant" else client_name
        transcript_lines.append(f"{role}: {msg['content']}")
    transcript = "\n".join(transcript_lines)
    iso = ended_at_utc.isoformat()
    prompt = f"""From this coaching session transcript, extract structured notes for personalized follow-up messages (48h after session).

Transcript (Session {session_num}, {client_name}):
{transcript}

Return ONLY markdown lines in this exact format (no extra prose):
- **session_ended_at (UTC):** {iso}
- **Commitments:** bullet list or short sentences of specific commitments the client made
- **Emotional moments:** moments worth revisiting
- **Flagged topics:** topics the client marked as important
- **Phase context:** brief BecomingYou-relevant context observed (or "none" if unclear)

Use concise phrases. No em dashes."""

    try:
        response = await claude.messages.create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        body = response.content[0].text.strip()
    except Exception as e:
        logger.error("Follow-up extraction failed: %s", e)
        body = (
            f"- **session_ended_at (UTC):** {iso}\n"
            "- **Commitments:** (extraction failed)\n"
            "- **Emotional moments:** \n"
            "- **Flagged topics:** \n"
            "- **Phase context:** \n"
        )

    def _write() -> None:
        raw = session_path.read_text(encoding="utf-8")
        raw = _strip_follow_up_extraction_section(raw)
        block = f"\n\n## Follow-Up Extraction\n\n{body}\n"
        session_path.write_text(raw + block, encoding="utf-8")

    await asyncio.to_thread(_write)
    logger.info("Follow-Up Extraction written to %s", session_path)


async def merge_focus_for_next_session_profile(client_name: str, problem_one_liner: str) -> None:
    """Append a focus line to ## Focus for Next Session in the vault profile (mini-session)."""
    path = profile_path_for(client_name)
    if not path.exists():
        logger.warning("merge_focus: no profile for %s", client_name)
        return
    existing = await asyncio.to_thread(load_file, path)
    needle = "## Focus for Next Session"
    idx = existing.find(needle)
    bullet = f"- (from check-in) {problem_one_liner.strip()}\n"
    if idx == -1:
        addition = f"\n\n{needle}\n{bullet}"
        new_text = existing.rstrip() + addition
    else:
        start = idx + len(needle)
        rest = existing[start:]
        m = re.search(r"\n## ", rest)
        insert_at = start + (m.start() if m else len(rest))
        new_text = existing[:insert_at] + "\n" + bullet + existing[insert_at:]
    await asyncio.to_thread(_write_path_utf8, path, new_text)
    logger.info("Focus for Next Session updated for %s", client_name)


async def update_client_profile(client_name: str, session_num: int, history: list):
    """Ask Claude to update (or create) the client profile based on the completed session."""
    if not history:
        return

    existing_profile = await asyncio.to_thread(load_client_profile, client_name)
    template = await asyncio.to_thread(load_file, template_path())
    today = datetime.date.today().isoformat()
    session_link = f"[[02-Client-Sessions/{client_name}/Session {session_num}|Session {session_num}]]"

    transcript_lines = []
    for msg in history:
        role = "Tanya" if msg["role"] == "assistant" else client_name
        transcript_lines.append(f"{role}: {msg['content']}")
    transcript = "\n".join(transcript_lines)

    wound_map = await asyncio.to_thread(wound_to_framework_map_excerpt)
    routing_instructions = f"""
## Core wound and Session Framework Routing (required)

**Five Wounds labels only:** For Primary and Secondary wounds in ## Core Wound, use ONLY these exact labels: Not Worthy, Not Enough, Not Supported, Not Powerful, Not Safe. Never use custom labels (e.g. no "Self-Trust wound," "External Validation wound"). Put nuance in **Belief underneath** or bullet notes, not in the wound label.

**Session Framework Routing:** Under ## Session Framework Routing, list wikilinked frameworks from the Wound-to-Framework Map below that match the client's primary and secondary wounds (copy the framework links from the map rows). Cross-reference ## Frameworks Used so routing leans toward what is still relevant, not only what was already heavily used.

**Vault integrity (non-negotiable):** Only use wikilinks to frameworks that appear verbatim in the Wound-to-Framework Map below. Never invent a framework name or wikilink that is not in that map. If a concept is relevant but has no entry in the map, describe it in plain text — do not create a link for it.

**After each session:** If the wound becomes clearer or a secondary wound surfaces, update both ## Core Wound and ## Session Framework Routing accordingly.

**First session / new profile:** Always attempt to name the core wound (best guess is OK if refined later). Do not leave Primary wound blank if the transcript gives enough signal for a reasonable call. If you cannot justify any wound, write "Unclear — needs exploration" for Primary only.

---

{wound_map}
"""

    becoming_you_extra = """
## BecomingYou phase & continuity (required in the profile markdown)
Maintain or add: ## BecomingYou Phase with **Current phase:** one of circumstance_contrast, awareness, alignment, action, allowing — or **N/A** when there is not enough evidence from completed session work (never guess). If this update is for the client's first-ever completed session and the transcript does not clearly support a phase, use N/A. Include **Phase history:** (dates or sequence; note backward moves). Keep ## Key Breakthroughs, ## Recurring Patterns, ## Tools That Have Landed, ## Emotional Baseline (update for this session), ## Focus for Next Session (brief auto-generated bullets for what matters next).
"""

    if existing_profile:
        prompt = f"""You are updating a coaching client profile for Tanya's MESH Coaching practice.

Here is the existing profile for {client_name}:

{existing_profile}

Here is the transcript from Session {session_num} ({today}):

{transcript}

Update the profile based on what emerged in this session. Add new themes, wounds surfaced, breakthroughs, what they responded well to, patterns noticed, and update where they are in their journey. Update and consolidate existing sections — merge similar bullets, refine existing entries rather than duplicating them, and remove bullets that are no longer accurate or relevant. Only add entries that represent genuinely new information not already captured. Append a new row to the Sessions table: | {today} | [key theme in 5 words] | {session_link} |. Update the Last updated date to {today}.

{routing_instructions}
{becoming_you_extra}

Return ONLY the full updated profile markdown — nothing else."""
    else:
        prompt = f"""You are creating a new coaching client profile for Tanya's MESH Coaching practice.

Use this template as your format:

{template}

Here is the transcript from Session {session_num} with {client_name} ({today}):

{transcript}

Fill in as much as you can from the session. Add a row to the Sessions table: | {today} | [key theme in 5 words] | {session_link} |. Set "Last updated" to {today}.

{routing_instructions}
{becoming_you_extra}

Return ONLY the completed profile markdown — nothing else."""

    try:
        response = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        updated_profile = response.content[0].text.strip()
        updated_profile = cap_profile_sessions(updated_profile)
        await asyncio.to_thread(_write_path_utf8, profile_path_for(client_name), updated_profile)
        logger.info("Profile updated for: %s (session %d)", client_name, session_num)
    except Exception as e:
        logger.error("Profile update failed for %s: %s", client_name, e)


async def update_vault_index_files(client_name: str, session_num: int, today: str, profile: str, transcript: str):
    """Use Claude to update Client Hub and 02-Client-Sessions.md to stay in sync."""
    hub_path = Path(VAULT_PATH) / "02-Client-Sessions" / "Client Hub.md"
    index_path = Path(VAULT_PATH) / "02-Client-Sessions.md"

    hub_content = await asyncio.to_thread(load_file, hub_path)
    index_content = await asyncio.to_thread(load_file, index_path)

    if not hub_content or not index_content:
        logger.warning("Could not load vault index files for update")
        return

    today_dt = datetime.datetime.strptime(today, "%Y-%m-%d")
    today_display = today_dt.strftime("%b %-d, %Y")

    prompt = f"""You are updating two vault index files for Tanya's MESH Coaching practice after a session with {client_name} on {today} ({today_display}).

Here is the client's updated profile:

{profile}

Here is today's session transcript:

{transcript}

---

## File 1: Client Hub

Here is the current Client Hub:

{hub_content}

Update it as follows:
- In "Clients With Profiles": if {client_name} is not already listed, add them with a one-line summary in this exact format: `- [[02-Client-Sessions/Client Profiles/{client_name}|{client_name}]] — [Primary wound] / [Secondary wound] · [Stage] · [One-line key focus]`. Use the existing entries as the format reference.
- In "All Clients — Session History" table: if {client_name} is already listed, increment their session count by 1 and update their profile link to `[[02-Client-Sessions/Client Profiles/{client_name}|Profile]]` if not already there — never remove their existing row. If they are not listed, add them alphabetically with session count 1 and profile link.
- Update the `*Last updated:*` date at the bottom to {today}.
- Do not change anything else.

Return ONLY the full updated Client Hub markdown.

---

## File 2: 02-Client-Sessions.md

Here is the current 02-Client-Sessions.md:

{index_content}

Update it as follows:
- In the profiles line at the top (starting with `> Clients with a profile`): if {client_name} is not already listed, add `[[02-Client-Sessions/Client Profiles/{client_name}|{client_name}]]` alphabetically in the list.
- In the Clients section: if {client_name} already has a section, append a new session line `- [[02-Client-Sessions/{client_name}/Session {session_num}|Session {session_num} — {today_display}]]` under their existing session links — never remove or replace previous session links. Update the primary themes line only if new themes emerged. If {client_name} does not have a section yet, add one alphabetically in this format:
```
### {client_name}
- [[02-Client-Sessions/{client_name}/Session {session_num}|Session {session_num} — {today_display}]]
*Primary themes: [2-3 key themes from the session]*
```
- Update the `*Last updated:*` line at the bottom — increment the transcript count by 1.
- Do not change anything else.

Return ONLY the full updated 02-Client-Sessions.md markdown.

---

Return both files separated by exactly this delimiter on its own line:
===FILE_SEPARATOR==="""

    try:
        response = await claude.messages.create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text.strip()

        if "===FILE_SEPARATOR===" in result:
            parts = result.split("===FILE_SEPARATOR===", 1)
            updated_hub = parts[0].strip()
            updated_index = parts[1].strip()

            await asyncio.to_thread(_write_path_utf8, hub_path, updated_hub)
            logger.info("Client Hub updated for: %s", client_name)

            await asyncio.to_thread(_write_path_utf8, index_path, updated_index)
            logger.info("02-Client-Sessions.md updated for: %s", client_name)
        else:
            logger.error("Vault index update: unexpected response format")
    except Exception as e:
        logger.error("Vault index update failed for %s: %s", client_name, e)


async def generate_returning_greeting(client_name: str, profile: str) -> str:
    """Ask Claude to craft a warm, personalized opener for a returning client."""
    prompt = f"""You are Tanya, a professional life coach. A returning client named {client_name} has just started a new session.

Here is their client profile:

{profile}

Write a single short opening message — 2 to 4 sentences — that:
- Greets them warmly by first name
- References something specific from where they left off using ONLY facts, themes, or session notes that appear verbatim in the profile above. If you cannot name something concrete from the profile, keep the opener general: glad they are here and ask what is on their mind.
- Ends with one open question
- Never mentions Telegram, bots, AI, microphones, tech, platforms, debugging, beta, prototypes, or app mechanics

Do not invent prior conversations, topics, or sessions that are not clearly supported by the profile text. Do not say you reviewed their file. Speak naturally, as if you simply remember them. No em dashes. No filler phrases like "I've been thinking about you."

This is a returning client only. Never imply a first meeting. Return only the greeting — nothing else."""

    try:
        response = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip().replace("\u2014", ",").replace("\u2013", ",")
    except Exception as e:
        logger.error("Returning greeting generation failed: %s", e)
        return f"Hey {client_name}, good to have you back. What's on your mind today?"


async def generate_new_client_opener_bridge(first_message: str) -> str:
    """Short lead-in before OPENER_INTRO for every new client — same two-message flow for all first lines."""
    system = """You are Tanya, a life coach, on Telegram. A brand-new client just sent their first message (below).

Write a SHORT opening (1–2 sentences max) they will see immediately BEFORE her fixed welcome text. That welcome always starts with "Hi, I'm Tanya" and then covers privacy, terms, and how she works — you must NOT quote or repeat any of that welcome.

If they shared something concrete (a worry, a person, a situation, something they want help with), respond briefly and warmly in plain language — the vibe of "yes, that's absolutely something we can talk about" — without coaching, solving, or asking a deep question yet.
If they only said hi/hello or something minimal, give a brief warm line (e.g. glad they reached out). Do not invent details they didn't mention.

Rules:
- 1–2 sentences only
- No em dashes
- Do not start with "I"
- No privacy, terms, legal, or encryption talk
- Never mention tech, Telegram, bots, AI, microphones, syncing, prototypes, keyboards, figuring out platforms, debugging, beta, or app mechanics
- Return only this opening, nothing else"""

    try:
        response = await claude.messages.create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=150,
            system=system,
            messages=[{"role": "user", "content": first_message}],
        )
        return response.content[0].text.strip().replace("\u2014", ",").replace("\u2013", ",")
    except Exception as e:
        logger.error("New client opener bridge failed: %s", e)
        return "Thanks for being here."


async def generate_new_client_opener_followup_line(first_message: str) -> str:
    """Third outbound line for new clients: invite coaching without faux-intimacy."""
    system = """Tanya already sent her short personalized opener and her fixed welcome/terms in text. You write ONLY her single next Telegram line.

This line will be sent as a **short voice note** (ElevenLabs), so write for the ear: natural spoken English, no bullet lists, no markdown, no parentheses with stage directions.

This is someone's first-ever exchange with her. Avoid generic acquaintance-check-ins ('how have you been', 'how are you doing', 'these days', 'what's new with you')—they signal a shallow relationship she does not have yet.

Prefer a grounded coaching invitation instead: what's present to explore, where they want to start, what feels important to unpack. Warm, grounded, plain language.

Rules:
- One sentence, at most 14 words
- No em dashes
- Do not start with "I"
- Never mention Telegram, bots, AI, microphones, syncing, prototyping, dashboards, keyboards, figuring out tech, debugging, beta, apps, platforms, or product mechanics.
- Return only that line, nothing else"""

    try:
        response = await claude.messages.create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=80,
            system=system,
            messages=[{"role": "user", "content": first_message}],
        )
        return response.content[0].text.strip().replace("\u2014", ",").replace("\u2013", ",")
    except Exception as e:
        logger.error("New client opener follow-up line failed: %s", e)
        return "What feels most alive to put on the table today?"


async def prepare_new_client_opener_parts(user_text: str) -> tuple[str, str]:
    """Bridge + second-line Haiku in parallel (always two outbound messages total from Tanya)."""
    bridge, followup = await asyncio.gather(
        generate_new_client_opener_bridge(user_text),
        generate_new_client_opener_followup_line(user_text),
    )
    return bridge, followup


async def deliver_new_client_opener_messages(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_name: str,
    bridge: str,
    followup: str,
) -> str:
    """Outbound flow: (1) text — bridge + OPENER_INTRO; (2) short line as voice when TTS is available."""
    main_combined = f"{bridge}\n\n{OPENER_INTRO}"

    await safe_send_chat_action(context.bot, chat_id, "typing")
    await update.message.reply_text(main_combined)
    logger.info("New client opener: main block sent as text for %s", user_name)

    await asyncio.sleep(NEW_CLIENT_OPENER_BEAT_SEC)

    await safe_send_chat_action(context.bot, chat_id, "record_voice")
    audio_follow = await synthesize_voice(followup)
    if audio_follow:
        await context.bot.send_voice(
            chat_id=chat_id,
            voice=io.BytesIO(audio_follow),
            filename="tanya.mp3",
        )
        new_client_voice_followup_snippet[chat_id] = followup
        logger.info("New client opener: follow-up voice sent for %s", user_name)
    else:
        await update.message.reply_text(followup)
        logger.info("New client opener: follow-up text sent for %s (no TTS)", user_name)

    return f"{main_combined}\n\n{followup}"


def _parse_stripe_confirmation_label(raw: str) -> str:
    """Normalize Haiku output to affirmative | negative | unclear."""
    lines = (raw or "").strip().lower().splitlines()
    line = lines[0] if lines else ""
    first = line.split()[0].rstrip(".,!?") if line.split() else ""
    if first in ("affirmative", "negative", "unclear"):
        return first
    return "unclear"


async def classify_stripe_confirmation_intent(
    chat_id: int, username: str, user_text: str,
) -> str:
    """Use Haiku to classify reply after trial Stripe prompt: affirmative | negative | unclear."""
    system = """You classify the client's latest message in Telegram.

Context: They finished the free trial. Tanya offered TanyaTalk for a monthly fee and offered to send a secure Stripe payment link when they are ready. This message is their reply.

Decide intent:
- affirmative: They want the link or to pay or subscribe now, clear yes, agreement to continue with payment in natural language (including phrases like let's do it, I'm in, sounds good if clearly about paying).
- negative: They decline, not ready, maybe later, no thanks, or clearly refuse the paid option for now.
- unclear: Ambiguous, mostly off-topic, only asks how pricing works without accepting or refusing, jokes, hedging without commitment, or you cannot tell if they want the link sent now.

Reply with exactly one word on the first line: affirmative OR negative OR unclear. Nothing else."""

    try:
        response = await claude.messages.create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=30,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )
        await asyncio.to_thread(record_coaching_usage, chat_id, username, response)
        label = _parse_stripe_confirmation_label(response.content[0].text)
        logger.info(
            "Stripe confirmation intent chat=%s label=%s",
            chat_id,
            label,
        )
        return label
    except Exception as e:
        logger.error("Stripe confirmation classification failed: %s", e)
        return "unclear"


SESSION_CLOSE_CONFIRMATION = "I've saved our session. When you're here, I'm here. 💛"
LINK_RESPONSE = "I can't open links, but I'm here with you. What's on your mind?"


def in_first_free_trial_session(chat_id: int) -> bool:
    return (
        session_numbers.get(chat_id) == 1
        and not free_trial_completed.get(chat_id, False)
    )


def has_tanyatalk_access(chat_id: int) -> bool:
    return paid_tanyatalk_access.get(chat_id, False) or mesh_tanyatalk_included.get(chat_id, False)


def should_block_unpaid_after_free_trial(chat_id: int) -> bool:
    if not BLOCK_AFTER_FREE_TRIAL:
        return False
    if chat_id in _POST_TRIAL_BYPASS_CHAT_IDS:
        return False
    if not free_trial_completed.get(chat_id):
        return False
    if has_tanyatalk_access(chat_id):
        return False
    return True


def _referral_state_load_unlocked() -> dict[str, int]:
    if not REFERRAL_STATE_PATH.exists():
        return {}
    try:
        with REFERRAL_STATE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        out: dict[str, int] = {}
        for k, v in data.items():
            if not isinstance(k, str):
                continue
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
        return out
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return {}


def _referral_state_save_unlocked(state: dict[str, int]) -> None:
    REFERRAL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REFERRAL_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def referral_get_last_nudge_session(client_name: str) -> int | None:
    with _REFERRAL_STATE_LOCK:
        s = _referral_state_load_unlocked()
        v = s.get(client_name)
        return v if v is not None else None


def referral_record_nudge(client_name: str, session_num: int) -> None:
    with _REFERRAL_STATE_LOCK:
        s = _referral_state_load_unlocked()
        s[client_name] = session_num
        _referral_state_save_unlocked(s)


def referral_nudge_prompt_allowed(chat_id: int, client_name: str) -> bool:
    """True when session spacing allows offering the optional referral line (context still model-gated)."""
    sess = session_numbers.get(chat_id, 0)
    if sess < REFERRAL_NUDGE_FIRST_ELIGIBLE_SESSION:
        return False
    if referral_nudge_used_this_session.get(chat_id):
        return False
    last = referral_get_last_nudge_session(client_name)
    if last is None:
        return True
    return sess - last >= REFERRAL_NUDGE_MIN_SESSIONS_BETWEEN


def referral_nudge_system_instruction() -> str:
    return f"""
---

## Optional referral line (this turn only — strict)

The system may show this block only when spacing rules allow. **You** decide if the **client's latest message** genuinely fits one of these: clear **gratitude toward you or the work**, they **mention someone else struggling** who might benefit, or a plain **"this helped"** beat (relief, shift, thank you).

**If there is no clear fit, ignore this entire section** and coach normally with no referral language.

**If there is a clear fit:** weave **one short sentence** about sharing your number into the **same reply** as your normal coaching—same flow, same voice, not a separate message, not a labeled postscript, not its own paragraph that feels bolted on. Land what they said first; slip the invite in where it follows naturally (often mid-reply or toward the close of the same thought), then continue or finish with your one question if the turn still needs it. No referral codes, links, or tracking. Not salesy. Do not combine with any other CTA.

**At most once** in this session (the system enforces spacing across sessions).

**If and only if** you included that sentence, end your reply with a **final line** containing exactly `{REFERRAL_NUDGE_MARKER}` and nothing else on that line (the client does not see this line). If you did not include the sentence, do **not** output the marker.
""".strip()


def strip_referral_nudge_marker(text: str) -> tuple[str, bool]:
    """Remove trailing marker from model output; return (client_visible_text, marker_was_present)."""
    stripped = text.rstrip()
    if stripped.endswith(REFERRAL_NUDGE_MARKER):
        core = stripped[: -len(REFERRAL_NUDGE_MARKER)].rstrip()
        return core, True
    return text, False


def cancel_session_timeout(chat_id: int) -> None:
    if chat_id in timeout_tasks and not timeout_tasks[chat_id].done():
        timeout_tasks[chat_id].cancel()


async def end_session(chat_id: int):
    """Transcript already written in real time — update profile + vault indexes, then clear state."""
    client_name = client_names.get(chat_id, "Client")
    history = conversations.get(chat_id, [])
    session_num = session_numbers.get(chat_id, 1)
    session_path = session_files.get(chat_id)

    if history:
        logger.info("Ending session for %s session %d (%d messages)", client_name, session_num, len(history))
        today = datetime.date.today().isoformat()

        # Transcript already written — just update profile and indexes
        await update_client_profile(client_name, session_num, history)

        profile = await asyncio.to_thread(load_file, profile_path_for(client_name))
        transcript_lines = []
        for msg in history:
            role = "Tanya" if msg["role"] == "assistant" else client_name
            transcript_lines.append(f"{role}: {msg['content']}")
        transcript = "\n".join(transcript_lines)

        if session_path and len(history) >= MIN_EXCHANGES_FOR_FOLLOWUP and has_tanyatalk_access(chat_id):
            ended_at = datetime.datetime.now(timezone.utc)
            await append_follow_up_extraction(
                session_path, client_name, session_num, history, ended_at
            )
            tanya_followup.schedule_follow_up_1(
                chat_id, client_name, session_path, session_num, ended_at
            )
        elif session_path:
            logger.info(
                "Skipping follow-up for %s session %d (%d messages < %d threshold)",
                client_name, session_num, len(history), MIN_EXCHANGES_FOR_FOLLOWUP,
            )

        await update_vault_index_files(client_name, session_num, today, profile, transcript)

    cancel_cache_warming(chat_id)
    _cached_static_prompts.pop(chat_id, None)
    referral_nudge_used_this_session.pop(chat_id, None)
    conversations.pop(chat_id, None)
    session_files.pop(chat_id, None)
    session_numbers.pop(chat_id, None)
    session_outlines.pop(chat_id, None)
    session_profiles.pop(chat_id, None)
    voice_note_redirects.pop(chat_id, None)
    pending_first_message_opener.pop(chat_id, None)
    free_trial_user_msg_count.pop(chat_id, None)
    free_trial_90_warned.pop(chat_id, None)
    last_activity.pop(chat_id, None)
    new_client_voice_followup_snippet.pop(chat_id, None)


async def ai_detects_delete_intent(text: str) -> bool:
    """Return True if Claude thinks the message is a data-deletion request."""
    try:
        response = await claude.messages.create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=5,
            system="Reply with only 'yes' or 'no'. No other text.",
            messages=[{"role": "user", "content": (
                f"Does this message express a desire to delete, erase, or remove the person's data, "
                f"account, or information from the system?\n\nMessage: {text}"
            )}],
        )
        return response.content[0].text.strip().lower().startswith("yes")
    except Exception:
        return False


async def delete_client_data(chat_id: int) -> None:
    """Anonymize all data for a client who requested deletion.

    The session folder is renamed to an unguessable UUID-based name so the bot
    can never find it again (treating the user as new on return), while the raw
    files remain on disk for any audit/legal need. All other state tied to
    chat_id is also cleared.
    """
    import uuid

    client_name = client_names.get(chat_id, "")

    # End any live session cleanly first (writes transcript, clears state).
    if chat_id in conversations:
        await end_session(chat_id)

    # Rename session folder and profile file to unguessable names so the bot
    # can never find them again (treats the user as brand-new on return).
    # Anonymized items are moved into a _Deleted/ subfolder to keep the vault tidy.
    if client_name:
        sessions_deleted_bin = Path(VAULT_PATH) / "02-Client-Sessions" / "_Deleted"
        profiles_deleted_bin = Path(VAULT_PATH) / "02-Client-Sessions" / "Client Profiles" / "_Deleted"

        client_dir = Path(VAULT_PATH) / "02-Client-Sessions" / client_name
        if client_dir.exists():
            deleted_name = f"_deleted_{uuid.uuid4().hex}"
            await asyncio.to_thread(sessions_deleted_bin.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(client_dir.rename, sessions_deleted_bin / deleted_name)
            logger.info("Anonymized session folder for chat_id=%d → _Deleted/%s", chat_id, deleted_name)

        profile_file = profile_path_for(client_name)
        if profile_file.exists():
            deleted_profile_name = f"_deleted_{uuid.uuid4().hex}.md"
            await asyncio.to_thread(profiles_deleted_bin.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(profile_file.rename, profiles_deleted_bin / deleted_profile_name)
            logger.info("Anonymized profile file for chat_id=%d → _Deleted/%s", chat_id, deleted_profile_name)

    # Remove monthly usage entry so the user starts completely fresh on return.
    def _scrub_monthly_usage():
        with _MONTHLY_USAGE_LOCK:
            usage = _load_monthly_usage_unlocked()
            key = str(chat_id)
            if key in usage:
                del usage[key]
                MONTHLY_USAGE_PATH.write_text(json.dumps(usage, indent=2))

    await asyncio.to_thread(_scrub_monthly_usage)

    # Cancel any pending follow-up jobs for this client.
    try:
        tanya_followup.cancel_all_followup_jobs_for_chat(chat_id)
    except Exception:
        pass

    # Clear all remaining in-memory state.
    for state_dict in (
        client_names,
        paid_tanyatalk_access,
        mesh_tanyatalk_included,
        awaiting_stripe_confirmation,
        awaiting_delete_confirmation,
        pending_first_message_opener,
        referral_nudge_used_this_session,
        cache_warm_tasks,
        _cached_static_prompts,
        _pending_messages,
        _pending_updates,
        _debounce_tasks,
        new_client_voice_followup_snippet,
    ):
        state_dict.pop(chat_id, None)

    logger.info("Deletion complete for chat_id=%d", chat_id)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    allowed = {u.strip().lower() for u in ALLOWED_USERS.split(",") if u.strip()}
    username = (update.effective_user.username or "").lower()
    user_id = str(update.effective_user.id)
    return username in allowed or user_id in allowed


def is_admin(update: Update) -> bool:
    if not ADMIN_USER_IDS.strip():
        return False
    allowed = {x.strip() for x in ADMIN_USER_IDS.split(",") if x.strip()}
    return str(update.effective_user.id) in allowed


def load_coach_teaching_ratio() -> tuple[int, int]:
    """Persisted coaching/teaching blend; default 50/50."""
    default = (50, 50)
    try:
        if not COACHING_BLEND_CONFIG_PATH.exists():
            return default
        data = json.loads(COACHING_BLEND_CONFIG_PATH.read_text(encoding="utf-8"))
        c = int(data.get("coaching_pct", 50))
        t = int(data.get("teaching_pct", 50))
        if c < 1 or t < 1 or c + t != 100:
            return default
        return c, t
    except Exception:
        return default


def save_coach_teaching_ratio(coaching_pct: int, teaching_pct: int) -> None:
    COACHING_BLEND_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    COACHING_BLEND_CONFIG_PATH.write_text(
        json.dumps(
            {"coaching_pct": coaching_pct, "teaching_pct": teaching_pct},
            indent=2,
        ),
        encoding="utf-8",
    )


def coaching_teaching_blend_instruction() -> str:
    c, t = load_coach_teaching_ratio()
    return f"""## Coaching / teaching blend (continuous voice)

Your default blend is **{c}% coaching / {t}% teaching**. This is not a mode switch — it is a texture. **{c}%** of the weight in every response is ICF-style coaching: questions, reflection, holding space, client-led discovery. **{t}%** is teaching: actual pauses in the coaching flow where Tanya stops, names a principle that directly applies to what the client is experiencing right now, gives them something to understand or sit with, then returns to coaching questions. Teaching does not feel like a lesson being delivered — it feels like Tanya handing the client a flashlight. One principle at a time. Never name the framework. When a client is using the wrong tool for their phase (for example trying to rewrite when they need regulation), name what they actually need — this is the tool-timing principle."""


# Distilled from ICF 2025 Core Competencies (session agreements, listening, evokes awareness,
# facilitates growth). Full official text lives in the vault PDF; this block only guards autonomy vs teaching.
ICF_AUTONOMY_DISTILLED_SECTION = """## ICF-aligned autonomy (distilled)

When the urge to teach or solve conflicts with coaching texture, prioritize these:

- Session direction stays client-led: honor what they want from this conversation before you deepen or weave framework language.
- Reflect or summarize what you heard before you advance the layer or add teaching.
- Invite more about their experience in the moment before you name patterns or frameworks.
- Invite them to generate their own ideas about what is true, what might help, or what they are willing to try next before you deliver a heavy reframe. Their wording lands first when possible.
- Offer reframes or concise observations lightly and without attachment, to spark their insight, not to replace their ownership of the answer.
- Next steps and accountability stay theirs to design; support and sharpen only after they claim a direction.
- Stay in coaching scope: reflection, meaning, and choice. Not psychotherapy, medical, legal, or financial advice. Encourage appropriate professional help when needs are clearly outside coaching."""


def becoming_you_phase_for_prompt(profile_md: str) -> str | None:
    """Inject phase only after at least one completed session is reflected in the profile."""
    if not profile_md or not profile_indicates_prior_session(profile_md):
        return None
    sec = _extract_profile_section(profile_md, "BecomingYou Phase")
    if not sec.strip():
        return None
    m = re.search(r"\*\*Current phase:\*\*\s*(.+)", sec, re.IGNORECASE)
    if not m:
        return None
    line = m.group(1).strip().split("\n")[0].strip()
    if re.match(r"^N/A\b", line, re.IGNORECASE) or "not yet identified" in line.lower():
        return None
    return line


# ---------------------------------------------------------------------------
# Timeout task
# ---------------------------------------------------------------------------

async def session_timeout_task(chat_id: int, bot):
    """Wait SESSION_TIMEOUT_MINUTES then end session if no activity."""
    await asyncio.sleep(SESSION_TIMEOUT_MINUTES * 60)
    lock = await _get_chat_message_lock(chat_id)
    ended = False
    is_ft = False
    async with lock:
        if chat_id in conversations and conversations[chat_id]:
            logger.info("Session timeout for chat_id %d", chat_id)
            is_ft = in_first_free_trial_session(chat_id)
            if is_ft:
                free_trial_completed[chat_id] = True
                awaiting_stripe_confirmation[chat_id] = True
            await end_session(chat_id)
            ended = True
    if ended:
        close_text = free_trial_close_text() if is_ft else SESSION_CLOSE_CONFIRMATION
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=close_text,
            )
        except Exception:
            pass


def reset_timeout(chat_id: int, bot):
    """Cancel any existing timeout and start a fresh one."""
    if chat_id in timeout_tasks and not timeout_tasks[chat_id].done():
        timeout_tasks[chat_id].cancel()
    timeout_tasks[chat_id] = asyncio.create_task(session_timeout_task(chat_id, bot))


# ---------------------------------------------------------------------------
# Cache warming — keeps Anthropic prompt cache alive between messages
# ---------------------------------------------------------------------------

async def _cache_warm_loop(chat_id: int):
    """Send a minimal API call every CACHE_WARM_INTERVAL_SEC to keep the prompt cache alive."""
    while True:
        await asyncio.sleep(CACHE_WARM_INTERVAL_SEC)
        static_prompt = _cached_static_prompts.get(chat_id)
        if not static_prompt or chat_id not in session_files:
            logger.info(
                "Cache warm loop exiting chat_id=%d reason=no_prompt_or_session",
                chat_id,
            )
            break
        try:
            response = await claude.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1,
                system=[
                    {"type": "text", "text": static_prompt, "cache_control": {"type": "ephemeral"}},
                ],
                messages=[{"role": "user", "content": "."}],
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )
            _u = getattr(response, "usage", None)
            cr = getattr(_u, "cache_read_input_tokens", 0) if _u else 0
            cc = getattr(_u, "cache_creation_input_tokens", 0) if _u else 0
            logger.info(
                "Cache warm ping chat_id=%d cache_read=%s cache_creation=%s",
                chat_id,
                cr,
                cc,
            )
        except Exception as e:
            logger.warning("Cache warm ping failed for chat_id %d: %s", chat_id, e)


def start_cache_warming(application: Application, chat_id: int):
    """Start or restart the cache warming loop for a session."""
    cancel_cache_warming(chat_id)
    cache_warm_tasks[chat_id] = application.create_task(_cache_warm_loop(chat_id))


def cancel_cache_warming(chat_id: int):
    task = cache_warm_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def safe_send_chat_action(bot, chat_id: int, action: str) -> None:
    """Typing / record_voice hints are non-critical; flaky networks must not abort the turn."""
    try:
        await bot.send_chat_action(chat_id=chat_id, action=action)
    except (TimedOut, NetworkError) as e:
        logger.warning("send_chat_action(%s) skipped: %s", action, e)


async def _keep_typing(bot, chat_id: int, stop_event: asyncio.Event):
    """Refreshes the Telegram typing indicator every 4 s until stop_event is set."""
    while not stop_event.is_set():
        await safe_send_chat_action(bot, chat_id, "typing")
        try:
            await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def perform_session_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """End active session if one exists; log close to transcript + history, then send confirmation."""
    chat_id = update.effective_chat.id
    msg = update.effective_message
    lock = await _get_chat_message_lock(chat_id)
    async with lock:
        if chat_id not in session_files:
            await msg.reply_text(
                "There isn't an active session to close. Send Tanya a message whenever you want to start."
            )
            return False

        is_ft = in_first_free_trial_session(chat_id)
        close_text = free_trial_close_text() if is_ft else SESSION_CLOSE_CONFIRMATION

        client_name = client_names.get(chat_id, update.effective_user.first_name or "Client")
        client_names[chat_id] = client_name
        user_close = (msg.text or "").strip() or "/endsession"

        session_path = session_files[chat_id]
        await asyncio.to_thread(
            append_exchange, session_path, client_name, user_close, close_text
        )

        if chat_id not in conversations:
            conversations[chat_id] = []
        conversations[chat_id].append({"role": "user", "content": user_close})
        conversations[chat_id].append({"role": "assistant", "content": close_text})
        if len(conversations[chat_id]) > MAX_HISTORY * 2:
            conversations[chat_id] = conversations[chat_id][-(MAX_HISTORY * 2) :]

        if is_ft:
            free_trial_completed[chat_id] = True
            awaiting_stripe_confirmation[chat_id] = True

        cancel_session_timeout(chat_id)
        await end_session(chat_id)
    await msg.reply_text(close_text)
    return True


async def send_with_voice(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    await update.message.reply_text(text)
    if voice_enabled.get(chat_id, True) and ELEVENLABS_API_KEY:
        await safe_send_chat_action(context.bot, chat_id, "record_voice")
        audio_bytes = await synthesize_voice(text)
        if audio_bytes:
            await context.bot.send_voice(
                chat_id=chat_id,
                voice=io.BytesIO(audio_bytes),
                filename="tanya.mp3",
            )



async def begin_session_with_opening(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    client_name: str,
) -> None:
    """Create session on disk + outline + profile cache + timeout; opener is sent from handle_message."""
    chat_id = update.effective_chat.id
    tanya_followup.cancel_all_followup_jobs_for_chat(chat_id)
    voice_enabled[chat_id] = False
    voice_note_redirects[chat_id] = 0
    referral_nudge_used_this_session.pop(chat_id, None)

    logger.info("Session opening setup: client=%s", client_name)

    sess_num = await asyncio.to_thread(get_next_session_number, client_name)
    session_numbers[chat_id] = sess_num
    session_files[chat_id] = await asyncio.to_thread(start_session_file, client_name, sess_num)
    session_outlines[chat_id] = await asyncio.to_thread(load_session_outline)
    session_profiles[chat_id] = await asyncio.to_thread(load_client_profile, client_name)

    pending_first_message_opener[chat_id] = True
    reset_timeout(chat_id, context.bot)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("This bot is private.")
        return
    chat_id = update.effective_chat.id
    client_name = sanitize_name_for_path(update.effective_user.first_name or "Client")
    lock = await _get_chat_message_lock(chat_id)
    async with lock:
        if should_block_unpaid_after_free_trial(chat_id):
            await update.message.reply_text(POST_FREE_TRIAL_BLOCK_MESSAGE)
            return
        conversations[chat_id] = []
        client_names[chat_id] = client_name
        last_activity[chat_id] = datetime.datetime.now()

        await begin_session_with_opening(update, context, client_name)  # greeting only; no user line yet


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    client_name = client_names.get(chat_id, update.effective_user.first_name or "Client")
    lock = await _get_chat_message_lock(chat_id)
    async with lock:
        # Do not let clients wipe trial completion to farm another 25 messages (admins may reset for QA).
        if (
            not is_admin(update)
            and free_trial_completed.get(chat_id)
            and not has_tanyatalk_access(chat_id)
        ):
            await update.message.reply_text(POST_TRIAL_RESET_DENIED_MESSAGE)
            return

        if conversations.get(chat_id):
            cancel_session_timeout(chat_id)
            await end_session(chat_id)
        conversations[chat_id] = []
        last_activity[chat_id] = datetime.datetime.now()
        if is_admin(update):
            free_trial_completed.pop(chat_id, None)

        await begin_session_with_opening(update, context, client_name)  # greeting only; no user line yet


async def show_last_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show approximate USD and tokens for the last Tanya reply in this chat."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    u = last_coaching_usage.get(chat_id)
    if not u:
        await update.message.reply_text(
            "No usage recorded yet. Send Tanya a normal message first, then try /cost again."
        )
        return
    await update.message.reply_text(
        "Last coaching reply (one API call: your message + Tanya's full answer):\n\n"
        f"Approx USD: ${u['approx_usd']:.4f}\n"
        f"(Uses default prices unless you set ANTHROPIC_PRICE_* in .env — check anthropic.com/pricing.)\n\n"
        f"Tokens — input: {u['input_tokens']}, output: {u['output_tokens']}, "
        f"cache read: {u['cache_read_input_tokens']}, cache write: {u['cache_creation_input_tokens']}\n\n"
        f"Running total file: logs/{USAGE_CSV_PATH.name} (inside Communication Device — open in Excel or Numbers)."
    )


async def toggle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    current = voice_enabled.get(chat_id, True)
    voice_enabled[chat_id] = not current
    state = "on" if not current else "off"
    await update.message.reply_text(f"Voice notes turned {state}.")


async def cmd_endsession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Same as sending an accepted end-session phrase; avoids natural-language ambiguity."""
    if not is_allowed(update):
        await update.effective_message.reply_text("This bot is private.")
        return
    await perform_session_close(update, context)


async def open_coaching_session_after_mini(
    update: Update, context: ContextTypes.DEFAULT_TYPE, client_name: str
) -> None:
    """Start a full session after mini-session chooses SESSION_NOW (under per-chat lock)."""
    chat_id = update.effective_chat.id
    lock = await _get_chat_message_lock(chat_id)
    async with lock:
        conversations[chat_id] = []
        client_names[chat_id] = sanitize_name_for_path(client_name)
        last_activity[chat_id] = datetime.datetime.now()
        await begin_session_with_opening(update, context, client_names[chat_id])


async def handle_set_ratio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: set ratio 70/30 (coaching/teaching, must sum to 100)."""
    if not is_admin(update):
        await update.message.reply_text("Not authorized to change blend ratio.")
        return
    m = re.match(r"(?i)^set\s+ratio\s+(\d+)\s*/\s*(\d+)\s*$", (update.message.text or "").strip())
    if not m:
        await update.message.reply_text("Usage: set ratio 70/30  (coaching first, then teaching; must sum to 100)")
        return
    c, t = int(m.group(1)), int(m.group(2))
    if c + t != 100:
        await update.message.reply_text("The two numbers must add up to 100.")
        return
    save_coach_teaching_ratio(c, t)
    await update.message.reply_text(f"Saved blend: {c}% coaching / {t}% teaching. Applies to messages from here on.")


VOICE_REDIRECT_FIRST = (
    "I'd love to hear your voice, but right now I connect best through text. "
    "Would you mind typing that out for me?"
)
VOICE_REDIRECT_REPEAT = (
    "I really do want to hear what you're sharing. "
    "Text helps me be fully present with you. Take your time."
)
UNSUPPORTED_MESSAGE_REPLY = (
    "I can't open that here, but I'm with you. What's on your mind?"
)


async def handle_voice_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    if should_block_unpaid_after_free_trial(chat_id):
        await update.message.reply_text(POST_FREE_TRIAL_BLOCK_MESSAGE)
        return
    has_session = chat_id in session_files
    count = voice_note_redirects.get(chat_id, 0)
    voice_note_redirects[chat_id] = count + 1
    if count == 0:
        msg = VOICE_REDIRECT_FIRST
        if not has_session:
            msg += " Once you do, we'll get started."
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text(VOICE_REDIRECT_REPEAT)


async def _fire_coaching_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_name: str, user_text: str
) -> None:
    """Process one coaching turn. Called by the debounce timer with potentially combined user text."""
    # Monthly cap (paid users only; free trial has its own 25-message hard stop)
    if has_tanyatalk_access(chat_id) and chat_id not in _POST_TRIAL_BYPASS_CHAT_IDS:
        count = get_monthly_message_count(chat_id)
        if count >= MONTHLY_MESSAGE_CAP:
            if count == MONTHLY_MESSAGE_CAP:
                increment_monthly_message_count(chat_id)
                msg = MONTHLY_CAP_BLOCK_MESSAGE
                if STRIPE_TOPUP_LINK:
                    msg += f"\n\n{STRIPE_TOPUP_LINK}"
                await update.message.reply_text(msg)
            return
        new_count = increment_monthly_message_count(chat_id)
        if new_count == MONTHLY_CAP_WARNING_AT:
            await update.message.reply_text(MONTHLY_CAP_WARNING_MESSAGE)

    lock = await _get_chat_message_lock(chat_id)
    async with lock:
        if should_block_unpaid_after_free_trial(chat_id):
            await update.message.reply_text(POST_FREE_TRIAL_BLOCK_MESSAGE)
            return

        session_turn_anchor_time = asyncio.get_event_loop().time()
        await safe_send_chat_action(context.bot, chat_id, "typing")

        voice_opener_script = ""
        stop_typing = asyncio.Event()
        typing_task = asyncio.ensure_future(
            _keep_typing(context.bot, chat_id, stop_typing)
        )

        try:
            if chat_id not in session_files:
                conversations[chat_id] = []
                await begin_session_with_opening(update, context, user_name)
            elif chat_id not in conversations:
                conversations[chat_id] = []

            conversations[chat_id].append({"role": "user", "content": user_text})

            if len(conversations[chat_id]) > MAX_HISTORY * 2:
                conversations[chat_id] = conversations[chat_id][-(MAX_HISTORY * 2):]

            reset_timeout(chat_id, context.bot)

            # Per-session message cap
            session_user_msgs = sum(1 for m in conversations[chat_id] if m["role"] == "user")
            if session_user_msgs >= SESSION_MESSAGE_CAP:
                await asyncio.to_thread(
                    append_exchange, session_files[chat_id], user_name, user_text, SESSION_CAP_BLOCK_MESSAGE
                )
                await update.message.reply_text(SESSION_CAP_BLOCK_MESSAGE)
                cancel_session_timeout(chat_id)
                await end_session(chat_id)
                return
            if session_user_msgs == SESSION_CAP_WARNING_AT:
                await update.message.reply_text(SESSION_CAP_WARNING_MESSAGE)

            prev_ft = free_trial_user_msg_count.get(chat_id, 0)
            n_ft = prev_ft + 1
            in_ft = in_first_free_trial_session(chat_id)

            if in_ft and n_ft == FREE_TRIAL_90_PCT_USER_MESSAGE and not free_trial_90_warned.get(chat_id):
                free_trial_90_warned[chat_id] = True
                await update.message.reply_text(FREE_TRIAL_90_WARNING)

            archive_context = ""
            if any(sig in user_text.lower() for sig in ARCHIVE_REFERENCE_SIGNALS):
                client_name_for_archive = client_names.get(chat_id)
                if client_name_for_archive and chat_id in session_files:
                    full_profile = await asyncio.to_thread(
                        load_file, profile_path_for(client_name_for_archive)
                    )
                    if full_profile:
                        archived_rows = parse_archived_sessions(full_profile)
                        if archived_rows:
                            matched = find_matching_archived_sessions(user_text, archived_rows)
                            if matched:
                                archive_context = await load_archive_context(matched)
                                logger.info(
                                    "Archive retrieval: %d match(es) for %s",
                                    len(matched), client_name_for_archive,
                                )

            if archive_context:
                conversations[chat_id][-1] = {
                    "role": "user",
                    "content": f"{archive_context}\n\n---\n\nClient message: {user_text}",
                }

            if in_ft and n_ft == FREE_TRIAL_USER_MESSAGE_CAP:
                trial_close = free_trial_close_text()
                conversations[chat_id].append({"role": "assistant", "content": trial_close})
                if len(conversations[chat_id]) > MAX_HISTORY * 2:
                    conversations[chat_id] = conversations[chat_id][-(MAX_HISTORY * 2):]
                await asyncio.to_thread(
                    append_exchange,
                    session_files[chat_id],
                    user_name,
                    user_text,
                    trial_close,
                )
                await update.message.reply_text(trial_close)
                free_trial_completed[chat_id] = True
                free_trial_user_msg_count[chat_id] = FREE_TRIAL_USER_MESSAGE_CAP
                awaiting_stripe_confirmation[chat_id] = True
                cancel_session_timeout(chat_id)
                await end_session(chat_id)
                return

            if pending_first_message_opener.get(chat_id):
                is_ret = await asyncio.to_thread(is_returning_client, user_name)
                _profile_path = profile_path_for(user_name)
                if _profile_path.exists() and not is_ret:
                    logger.warning(
                        "Profile file exists but client not classified returning; vault markers may need review (%s)",
                        user_name,
                    )

                if not is_ret:
                    bridge, followup = await prepare_new_client_opener_parts(user_text)
                    elapsed_since_anchor = (
                        asyncio.get_event_loop().time() - session_turn_anchor_time
                    )
                    remaining_open = RESPONSE_DELAY_SECONDS - elapsed_since_anchor
                    if remaining_open > 0:
                        await asyncio.sleep(remaining_open)
                    opener_script = await deliver_new_client_opener_messages(
                        update, context, chat_id, user_name, bridge, followup
                    )
                    pending_first_message_opener.pop(chat_id, None)
                    conversations[chat_id].append({"role": "assistant", "content": opener_script})
                    if len(conversations[chat_id]) > MAX_HISTORY * 2:
                        conversations[chat_id] = conversations[chat_id][-(MAX_HISTORY * 2):]
                    await asyncio.to_thread(
                        append_exchange,
                        session_files[chat_id],
                        user_name,
                        user_text,
                        opener_script,
                    )
                    if in_ft:
                        free_trial_user_msg_count[chat_id] = n_ft
                    return

                opener_script = await generate_returning_greeting(
                    user_name,
                    session_profiles.get(chat_id, ""),
                )
                logger.info("Returning client greeting prepared for: %s", user_name)

                elapsed_open = asyncio.get_event_loop().time() - session_turn_anchor_time
                remaining_open = RESPONSE_DELAY_SECONDS - elapsed_open
                if remaining_open > 0:
                    await asyncio.sleep(remaining_open)

                audio_bytes = await synthesize_voice(opener_script)
                if audio_bytes:
                    await context.bot.send_voice(
                        chat_id=chat_id,
                        voice=io.BytesIO(audio_bytes),
                        filename="tanya.mp3",
                    )
                    logger.info("Returning client opener: voice note sent for %s", user_name)
                else:
                    await update.message.reply_text(opener_script)
                    logger.info(
                        "Returning client opener: ElevenLabs failed, sent as text for %s",
                        user_name,
                    )

                pending_first_message_opener.pop(chat_id, None)
                await asyncio.to_thread(append_tanya_message, session_files[chat_id], opener_script)
                voice_opener_script = opener_script

            message_received_at = asyncio.get_event_loop().time()

            client_profile = session_profiles.get(chat_id, "")
            phase_line = becoming_you_phase_for_prompt(client_profile)
            static_prompt = build_static_prompt(
                client_profile, session_outlines.get(chat_id, ""), phase_line
            )
            _cached_static_prompts[chat_id] = static_prompt
            start_cache_warming(context.application, chat_id)

            relevant_frameworks = select_frameworks_for_session(
                session_profiles.get(chat_id, ""),
                user_text,
                conversations[chat_id],
            )
            framework_context = (
                await asyncio.to_thread(load_frameworks, relevant_frameworks)
                if relevant_frameworks
                else ""
            )

            system_blocks: list[dict] = [
                {"type": "text", "text": static_prompt, "cache_control": {"type": "ephemeral"}},
            ]
            if framework_context:
                system_blocks.append(
                    {"type": "text", "text": f"\n\n---\n\n## Relevant Frameworks\n\n{framework_context}"}
                )

            voice_snippet = new_client_voice_followup_snippet.pop(chat_id, None)
            if voice_snippet:
                system_blocks.append(
                    {
                        "type": "text",
                        "text": (
                            "\n\n---\n\n## This turn only\n\n"
                            "Your opening had two Telegram parts: first a longer text message (welcome and terms), "
                            "then a short voice note where you gave this coaching invite line:\n\n"
                            f"{voice_snippet}\n\n"
                            "Do not repeat that voice line verbatim or re-open with a fresh greeting. "
                            "Respond in text to what they just said. Follow your normal coaching rules."
                        ),
                    }
                )

            if voice_opener_script:
                system_blocks.append(
                    {
                        "type": "text",
                        "text": (
                            "\n\n---\n\n## This turn only\n\n"
                            "You already opened this session with a voice-only greeting the client heard in Telegram. "
                            "Do not repeat that opening verbatim or re-greet from scratch. "
                            "Your opening (for continuity) was:\n\n"
                            f"{voice_opener_script}\n\n"
                            "Respond in text to what they just said. Follow your normal coaching rules."
                        ),
                    }
                )

            profile_client = client_names.get(chat_id, user_name)
            if referral_nudge_prompt_allowed(chat_id, profile_client):
                system_blocks.append({"type": "text", "text": referral_nudge_system_instruction()})

            logger.info(
                "Message from %s | Session %d | Profile: %s | Frameworks: %s | Static: %d chars",
                user_name,
                session_numbers.get(chat_id, 0),
                "loaded" if client_profile else "none",
                relevant_frameworks or "none",
                len(static_prompt),
            )

            try:
                response = await claude.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=1024,
                    system=system_blocks,
                    messages=conversations[chat_id][-20:],
                    extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                )
                raw_reply = response.content[0].text
                reply, referral_marked = strip_referral_nudge_marker(raw_reply)
                reply = reply.replace("—", ",").replace("–", ",").replace(" - ", ", ")
                await asyncio.to_thread(record_coaching_usage, chat_id, user_name, response)
                if referral_marked:
                    referral_nudge_used_this_session[chat_id] = True
                    referral_record_nudge(profile_client, session_numbers.get(chat_id, 0))
                    logger.info(
                        "Referral nudge recorded for %s session %d",
                        profile_client,
                        session_numbers.get(chat_id, 0),
                    )
            except Exception as e:
                logger.error("Anthropic API error: %s", e)
                reply = "I'm having a little trouble right now. Give me a moment and try again."

            conversations[chat_id].append({"role": "assistant", "content": reply})

            await asyncio.to_thread(
                append_exchange, session_files[chat_id], user_name, user_text, reply
            )

            elapsed = asyncio.get_event_loop().time() - message_received_at
            remaining = RESPONSE_DELAY_SECONDS - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)

            await send_with_voice(update, context, reply)

            if in_ft:
                free_trial_user_msg_count[chat_id] = n_ft
        finally:
            stop_typing.set()
            try:
                await typing_task
            except Exception:
                pass


async def handle_unsupported_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    if should_block_unpaid_after_free_trial(chat_id):
        await update.message.reply_text(POST_FREE_TRIAL_BLOCK_MESSAGE)
        return
    await update.message.reply_text(UNSUPPORTED_MESSAGE_REPLY)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text
    if re.search(r'https?://\S+|www\.\S+', user_text, re.IGNORECASE):
        await update.message.reply_text(LINK_RESPONSE)
        return
    user_name = sanitize_name_for_path(update.effective_user.first_name or "Client")
    client_names[chat_id] = user_name
    last_activity[chat_id] = datetime.datetime.now()

    if tanya_followup.on_user_message_cancel_fu2(chat_id):
        tanya_followup.enter_mini_session_after_fu1(chat_id, None)
    if tanya_followup.in_mini_session(chat_id):
        lock = await _get_chat_message_lock(chat_id)
        async with lock:
            await tanya_followup.handle_mini_session_turn(update, context, user_text)
        return

    if awaiting_stripe_confirmation.get(chat_id):
        lock = await _get_chat_message_lock(chat_id)
        async with lock:
            intent = await classify_stripe_confirmation_intent(chat_id, user_name, user_text)
            if intent == "affirmative":
                awaiting_stripe_confirmation.pop(chat_id, None)
                if STRIPE_PAYMENT_LINK:
                    await update.message.reply_text(
                        f"Here it is: {STRIPE_PAYMENT_LINK}. Come back when you're ready and we'll pick up right where we left off."
                    )
                elif STRIPE_PAYMENT_LINK_PLACEHOLDER:
                    await update.message.reply_text(
                        "Live checkout is not wired up yet. Here is a neutral placeholder for now "
                        f"(tap to preview, not a charge): {STRIPE_PAYMENT_LINK_PLACEHOLDER}"
                    )
                else:
                    await update.message.reply_text(
                        "The payment link isn't set up yet, but I'll be right "
                        "here when it is."
                    )
            elif intent == "negative":
                awaiting_stripe_confirmation.pop(chat_id, None)
                await update.message.reply_text(FREE_TRIAL_STRIPE_DECLINED)
            else:
                await update.message.reply_text(STRIPE_CONFIRMATION_UNCLEAR_REPLY)
        return


    # Delete confirmation: client already triggered the delete flow, waiting on yes/no.
    if awaiting_delete_confirmation.get(chat_id):
        normalized_del = user_text.strip().lower().rstrip("!.? ")
        if normalized_del in ("yes, delete everything", "yes delete everything",
                              "yes, delete", "yes delete", "yes"):
            awaiting_delete_confirmation.pop(chat_id, None)
            await delete_client_data(chat_id)
            await update.message.reply_text(DELETE_CONFIRMED_MESSAGE)
        else:
            awaiting_delete_confirmation.pop(chat_id, None)
            await update.message.reply_text(DELETE_CANCELLED_MESSAGE)
        return

    # Delete trigger: phrase match first (free), then AI fallback to catch natural phrasing.
    user_text_lower = user_text.strip().lower()
    if any(trigger in user_text_lower for trigger in DELETE_TRIGGERS) or await ai_detects_delete_intent(user_text):
        awaiting_delete_confirmation[chat_id] = True
        await update.message.reply_text(DELETE_CONFIRMATION_PROMPT)
        return

    # Session end: whole message matches SESSION_END_NORMALIZED; not model-decided.
    if is_session_end_message(user_text):
        await perform_session_close(update, context)
        return

    # Debounce: buffer this message and wait for more before firing to Claude.
    # If another message arrives within DEBOUNCE_SECONDS, the timer resets and both
    # messages are combined into a single Claude call.
    _pending_messages.setdefault(chat_id, []).append(user_text)
    _pending_updates[chat_id] = update

    existing = _debounce_tasks.pop(chat_id, None)
    if existing and not existing.done():
        existing.cancel()

    async def _fire() -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        _debounce_tasks.pop(chat_id, None)
        msgs = _pending_messages.pop(chat_id, [])
        upd = _pending_updates.pop(chat_id, update)
        if msgs:
            combined = "\n\n".join(msgs)
            await _fire_coaching_message(upd, context, chat_id, user_name, combined)

    _debounce_tasks[chat_id] = asyncio.ensure_future(_fire())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def ensure_vault() -> None:
    """Download the tanya-brain vault from GitHub as a zip if GITHUB_PAT is set."""
    if not GITHUB_PAT:
        return
    import zipfile
    import io
    vault = Path(VAULT_PATH)
    logger.info("Downloading vault from GitHub...")
    url = "https://api.github.com/repos/cole-projects/tanya-brain/zipball/main"
    headers = {"Authorization": f"token {GITHUB_PAT}", "Accept": "application/vnd.github+json"}
    try:
        response = httpx.get(url, headers=headers, follow_redirects=True, timeout=60)
        response.raise_for_status()
    except Exception as e:
        logger.error("Failed to download vault: %s", e)
        return
    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            top = zf.namelist()[0].split("/")[0]
            parent = vault.parent
            parent.mkdir(parents=True, exist_ok=True)
            if vault.exists():
                shutil.rmtree(vault)
            zf.extractall(parent)
            (parent / top).rename(vault)
        logger.info("Vault ready at %s", vault)
    except Exception as e:
        logger.error("Failed to extract vault: %s", e)


def main():
    try:
        ensure_vault()
        acquire_single_instance_lock()
        init_usage_csv_file()
        telegram_http = HTTPXRequest(
            connect_timeout=20.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=5.0,
        )
        async def post_init(app: Application) -> None:
            tanya_followup.init_scheduler(_BOT_DIR / "logs" / "apscheduler.sqlite")

            async def send_msg(cid: int, txt: str) -> None:
                await app.bot.send_message(chat_id=cid, text=txt)

            tanya_followup.configure(
                claude=claude,
                claude_model=CLAUDE_MODEL,
                claude_haiku_model=CLAUDE_HAIKU_MODEL,
                send_message=send_msg,
                merge_focus_for_next_session=merge_focus_for_next_session_profile,
                open_coaching_session=open_coaching_session_after_mini,
            )
            await tanya_followup.start_scheduler()

        async def post_shutdown(app: Application) -> None:
            await tanya_followup.shutdown_scheduler()

        app = (
            ApplicationBuilder()
            .token(TELEGRAM_TOKEN)
            .request(telegram_http)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )

        app.add_handler(
            MessageHandler(
                filters.TEXT & filters.Regex(r"(?i)^set\s+ratio\s+\d+\s*/\s*\d+\s*$"),
                handle_set_ratio,
            )
        )
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("reset", reset))
        app.add_handler(CommandHandler("endsession", cmd_endsession))
        app.add_handler(CommandHandler("end_session", cmd_endsession))
        app.add_handler(CommandHandler("voice", toggle_voice))
        app.add_handler(CommandHandler("cost", show_last_cost))
        app.add_handler(CommandHandler("usage", show_last_cost))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_note))
        app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND & ~filters.VOICE & ~filters.AUDIO, handle_unsupported_message))

        logger.info("Tanya bot is running — listening for messages...")
        app.run_polling()
    finally:
        release_single_instance_lock()


if __name__ == "__main__":
    main()
