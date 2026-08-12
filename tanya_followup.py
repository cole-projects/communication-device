"""
Follow-up engine: persisted 48h timers (APScheduler + SQLite), mini-session state,
and transport-agnostic outbound messaging via a send_message hook.

V1 design: one follow-up per session only, starting at Session 2 (Session 1 / free trial
never schedules follow-up). Timer cancels on any inbound message. New timer starts only
after the next session ends.
Three outcomes after follow-up fires:
  (1) no response — no further action
  (2) client wants a session — Haiku acknowledges, opens Sonnet session, counts toward 250
  (3) client declines — Haiku closes warmly, no timer reset
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

import anthropic
from anthropic import AsyncAnthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

logger = logging.getLogger(__name__)


async def _claude_create(_client: AsyncAnthropic, **kwargs) -> anthropic.types.Message:
    delays = [1, 2, 4, 8, 16]
    for attempt, delay in enumerate(delays, 1):
        try:
            return await _client.messages.create(**kwargs)
        except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
            if attempt == len(delays):
                raise
            logger.warning("Claude API transient error (attempt %d): %s — retrying in %ds", attempt, e, delay)
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover

import os as _os
FU_INTERVAL_SEC = int(_os.getenv("FOLLOWUP_DELAY_SECONDS", "172800"))  # default 48 hours; override for testing
FU_EXTRACTION_RETRY_SEC = int(_os.getenv("FOLLOWUP_EXTRACTION_RETRY_SECONDS", "300"))  # 5 min
FU_EXTRACTION_MAX_RETRIES = int(_os.getenv("FOLLOWUP_EXTRACTION_MAX_RETRIES", "24"))  # ~2 hours
MIN_SESSION_NUM_FOR_FOLLOWUP = 2


def _phone_key(phone: str) -> str:
    """16-char SHA-256 prefix — must match tanya_bot.phone_to_hash for job cancel."""
    from tanya_bot import phone_to_hash
    return phone_to_hash(phone)[:16]


class MiniState(str, Enum):
    LISTENING = "listening"
    SESSION_PROMPT = "session_prompt"


@dataclass
class MiniSessionCtx:
    state: MiniState
    history: list[dict] = field(default_factory=list)
    follow_up_1_text: str | None = None


# Populated by configure() before scheduler starts
_claude: AsyncAnthropic | None = None
_claude_model: str = ""
_claude_haiku_model: str = ""
_send_message: Callable[[str, str], Awaitable[None]] | None = None
_typing_on_cb: Callable[[str], Awaitable[None]] | None = None
_merge_focus_cb: Callable[[str, str], Awaitable[None]] | None = None
_open_session_cb: Callable[..., Awaitable[None]] | None = None
_check_monthly_cap_cb: Callable[[str], Awaitable[bool]] | None = None  # phone -> True if capped

_scheduler: AsyncIOScheduler | None = None
_mini: dict[str, MiniSessionCtx] = {}
_post_fu1: dict[str, dict] = {}  # phone -> {session_path, fu1_text, correlation}
_followup_state_path: Path | None = None
_followup_state_lock = threading.Lock()


def _mini_to_dict(ctx: MiniSessionCtx) -> dict:
    return {
        "state": ctx.state.value,
        "history": ctx.history,
        "follow_up_1_text": ctx.follow_up_1_text,
    }


def _mini_from_dict(data: dict) -> MiniSessionCtx:
    return MiniSessionCtx(
        state=MiniState(data["state"]),
        history=data.get("history", []),
        follow_up_1_text=data.get("follow_up_1_text"),
    )


def _session_followup_sent(session_path: Path) -> bool:
    try:
        return "<!-- followup:sent" in session_path.read_text(encoding="utf-8")
    except OSError:
        return False


def _read_session_followup_message(session_path: Path) -> str | None:
    try:
        text = session_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"<!-- followup:sent\s+(\{.*?\})\s+-->", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1)).get("text")
    except json.JSONDecodeError:
        return None


def _mark_session_followup_sent(session_path: Path, message: str) -> None:
    payload = json.dumps({"text": message}, ensure_ascii=False)
    with session_path.open("a", encoding="utf-8") as f:
        f.write(f"\n<!-- followup:sent {payload} -->\n")


def _session_ended_at_from_extraction(session_path: Path) -> datetime | None:
    try:
        text = session_path.read_text(encoding="utf-8")
    except OSError:
        return None
    raw = read_follow_up_extraction(text).get("session_ended_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ensure_post_fu1_restored(phone: str, session_path: Path, fu1_text: str | None) -> None:
    if phone in _post_fu1:
        return
    if not fu1_text:
        return
    _post_fu1[phone] = {
        "session_path": session_path,
        "fu1_text": fu1_text,
        "pending_send": False,
    }
    _persist_followup_state()


def _persist_followup_state() -> None:
    if not _followup_state_path:
        return
    with _followup_state_lock:
        payload = {
            "post_fu1": {
                phone: {
                    "session_path": str(v["session_path"]) if v.get("session_path") else "",
                    "fu1_text": v.get("fu1_text"),
                    "pending_send": bool(v.get("pending_send")),
                }
                for phone, v in _post_fu1.items()
            },
            "mini": {phone: _mini_to_dict(ctx) for phone, ctx in _mini.items()},
        }
        _followup_state_path.parent.mkdir(parents=True, exist_ok=True)
        _followup_state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _restore_followup_state() -> None:
    if not _followup_state_path or not _followup_state_path.exists():
        return
    try:
        data = json.loads(_followup_state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not restore follow-up state: %s", e)
        return
    _post_fu1.clear()
    _mini.clear()
    for phone, entry in (data.get("post_fu1") or {}).items():
        session_path_str = entry.get("session_path", "")
        _post_fu1[phone] = {
            "session_path": Path(session_path_str) if session_path_str else None,
            "fu1_text": entry.get("fu1_text"),
            "pending_send": bool(entry.get("pending_send")),
        }
    for phone, entry in (data.get("mini") or {}).items():
        try:
            _mini[phone] = _mini_from_dict(entry)
        except (KeyError, ValueError) as e:
            logger.warning("Skipping invalid mini-session state for %s: %s", phone[:8], e)
    if _post_fu1 or _mini:
        logger.info(
            "Follow-up state restored: %d post-FU window(s), %d mini-session(s)",
            len(_post_fu1),
            len(_mini),
        )


def configure(
    *,
    claude: AsyncAnthropic,
    claude_model: str,
    claude_haiku_model: str,
    send_message: Callable[[str, str], Awaitable[None]],
    merge_focus_for_next_session: Callable[[str, str], Awaitable[None]],
    open_coaching_session: Callable[..., Awaitable[None]],
    check_monthly_cap: Callable[[str], Awaitable[bool]] | None = None,
    typing_on: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    global _claude, _claude_model, _claude_haiku_model, _send_message
    global _merge_focus_cb, _open_session_cb, _check_monthly_cap_cb, _typing_on_cb
    _claude = claude
    _claude_model = claude_model
    _claude_haiku_model = claude_haiku_model
    _send_message = send_message
    _merge_focus_cb = merge_focus_for_next_session
    _open_session_cb = open_coaching_session
    _check_monthly_cap_cb = check_monthly_cap
    _typing_on_cb = typing_on


def init_scheduler(jobstore_sqlite_path: Path) -> None:
    global _scheduler, _followup_state_path
    if _scheduler is not None:
        return
    jobstore_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    _followup_state_path = jobstore_sqlite_path.parent / "followup_state.json"
    url = f"sqlite:///{jobstore_sqlite_path.resolve()}"
    _scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=url)},
        job_defaults={"coalesce": True, "max_instances": 1},
    )


async def _recover_incomplete_mini_sessions() -> None:
    """Resume mini-sessions interrupted after the client texted but before Tanya replied."""
    for phone, ctx in list(_mini.items()):
        if not ctx.history or ctx.history[-1]["role"] != "user":
            continue
        last_user = ctx.history.pop()["content"]
        _persist_followup_state()
        logger.info(
            "Resuming interrupted mini-session for phone_key=%s (state=%s)",
            _phone_key(phone), ctx.state.value,
        )
        try:
            await handle_mini_session_turn(phone, last_user)
        except Exception as e:
            logger.error(
                "Failed to resume mini-session for phone_key=%s: %s",
                _phone_key(phone), e,
            )


async def start_scheduler() -> None:
    if _scheduler and not _scheduler.running:
        _scheduler.start()
        _restore_followup_state()
        await _recover_pending_followup_sends()
        await _recover_incomplete_mini_sessions()
        logger.info("Follow-up scheduler started (SQLite job store)")


async def shutdown_scheduler() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Follow-up scheduler shut down")


def cancel_all_followup_jobs_for_chat(phone: str) -> None:
    if not _scheduler:
        return
    pk = _phone_key(phone)
    for job in list(_scheduler.get_jobs()):
        jid = job.id
        if jid.startswith(f"fu1-{pk}-"):
            try:
                _scheduler.remove_job(jid)
                logger.info("Removed follow-up job %s", jid)
            except Exception as e:
                logger.warning("Could not remove job %s: %s", jid, e)
    _post_fu1.pop(phone, None)
    clear_mini(phone)
    _persist_followup_state()


def ensure_follow_up_scheduled(
    phone: str,
    client_name: str,
    session_path: Path,
    session_num: int,
    ended_at_utc: datetime,
) -> None:
    """Schedule (or keep) one follow-up job per session — idempotent across finalize retries."""
    if session_num < MIN_SESSION_NUM_FOR_FOLLOWUP:
        logger.info(
            "Follow-up not scheduled for phone_key=%s session %d — session 1 only",
            _phone_key(phone), session_num,
        )
        return
    if not _scheduler:
        logger.warning("Scheduler not initialized; skip follow-up scheduling")
        return
    if _session_followup_sent(session_path):
        logger.info(
            "Follow-up already sent for phone_key=%s session %d — not rescheduling",
            _phone_key(phone), session_num,
        )
        return
    pk = _phone_key(phone)
    job_id = f"fu1-{pk}-{session_num}"
    run_at = ended_at_utc + timedelta(seconds=FU_INTERVAL_SEC)
    now = datetime.now(timezone.utc)
    if run_at <= now:
        run_at = now + timedelta(seconds=30)
    _scheduler.add_job(
        followup_job_fire,
        "date",
        run_date=run_at,
        kwargs={
            "phone": phone,
            "session_path_str": str(session_path),
            "client_name": client_name,
            "session_num": session_num,
            "retry_count": 0,
        },
        id=job_id,
        replace_existing=True,
    )
    logger.info(
        "Scheduled follow-up for phone_key=%s at %s (job %s)",
        pk,
        run_at,
        job_id,
    )


def schedule_follow_up_1(
    phone: str,
    client_name: str,
    session_path: Path,
    session_num: int,
    ended_at_utc: datetime,
) -> None:
    ensure_follow_up_scheduled(phone, client_name, session_path, session_num, ended_at_utc)


def _reschedule_followup_for_extraction(
    phone: str,
    session_path: Path,
    client_name: str,
    session_num: int,
    retry_count: int,
) -> None:
    if not _scheduler:
        return
    pk = _phone_key(phone)
    job_id = f"fu1-{pk}-{session_num}"
    run_at = datetime.now(timezone.utc) + timedelta(seconds=FU_EXTRACTION_RETRY_SEC)
    _scheduler.add_job(
        followup_job_fire,
        "date",
        run_date=run_at,
        kwargs={
            "phone": phone,
            "session_path_str": str(session_path),
            "client_name": client_name,
            "session_num": session_num,
            "retry_count": retry_count,
        },
        id=job_id,
        replace_existing=True,
    )
    logger.info(
        "Follow-up waiting on extraction for phone_key=%s session %d — retry %d at %s",
        pk, session_num, retry_count, run_at,
    )


def read_follow_up_extraction(session_text: str) -> dict[str, str]:
    """Parse ## Follow-Up Extraction section from session markdown."""
    out: dict[str, str] = {}
    m = re.search(r"## Follow-Up Extraction\s*(.*?)(?=\n## |\Z)", session_text, re.DOTALL | re.IGNORECASE)
    if not m:
        return out
    block = m.group(1)
    for label, key in (
        (r"\*\*session_ended_at[^:]*:\*\*\s*", "session_ended_at"),
        (r"\*\*Commitments:\*\*\s*", "commitments"),
        (r"\*\*Emotional moments:\*\*\s*", "emotional_moments"),
        (r"\*\*Flagged topics:\*\*\s*", "flagged_topics"),
        (r"\*\*Phase context:\*\*\s*", "phase_context"),
    ):
        mm = re.search(label + r"(.+?)(?=\n- \*\*|\Z)", block, re.DOTALL | re.IGNORECASE)
        if mm:
            out[key] = mm.group(1).strip()
    return out


async def _generate_follow_up_message(
    client_name: str,
    session_path: Path,
) -> str:
    """Generate a one-line check-in that implies Tanya is thinking of them — does not invite conversation."""
    text = await asyncio.to_thread(session_path.read_text, encoding="utf-8")
    data = read_follow_up_extraction(text)

    prompt = f"""Generate a follow-up text message from Tanya to client {client_name}.

Structure: 2 sentences total.
- Sentence 1: A warm statement that references something specific from the session — a commitment they made, an emotional moment, or a phrase they used. Implies Tanya is thinking of them.
- Sentence 2: One short, natural question tied to what was just referenced. Examples of the right tone: "How have things been going since then?", "Have you given that any thought?", "Any movement on that since we talked?" Vary the phrasing — never repeat the same question.

Rules:
- Never use: checking in, follow-up, just wanted to, hope you're doing well.
- Never use em dashes.
- Both sentences should sound like a warm text from someone who genuinely remembers — not a form letter.
- Gold standard: the client thinks "I can't believe she remembered that."

Session excerpts:
- Commitments: {data.get('commitments', '(none)')}
- Emotional moments: {data.get('emotional_moments', '(none)')}
- Flagged topics: {data.get('flagged_topics', '(none)')}
- Phase context: {data.get('phase_context', '(none)')}

Return ONLY the message text, nothing else."""

    assert _claude is not None
    response = await _claude_create(_claude,
        model=_claude_haiku_model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip().replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ").replace("–", ", ").replace(" ,", ",")


async def followup_job_fire(
    phone: str,
    session_path_str: str,
    client_name: str,
    session_num: int = 0,
    retry_count: int = 0,
) -> None:
    session_path = Path(session_path_str)
    if not session_path.exists():
        logger.error("Follow-up: session file missing %s", session_path)
        return
    if _send_message is None:
        return

    if session_num < MIN_SESSION_NUM_FOR_FOLLOWUP:
        logger.info(
            "Follow-up skipped for phone_key=%s session %d — session 2+ only",
            _phone_key(phone), session_num,
        )
        return

    if _session_followup_sent(session_path):
        saved_msg = _read_session_followup_message(session_path)
        _ensure_post_fu1_restored(phone, session_path, saved_msg)
        logger.info(
            "Follow-up already sent for phone_key=%s session %d — skipping duplicate fire",
            _phone_key(phone), session_num,
        )
        return

    session_text = await asyncio.to_thread(session_path.read_text, encoding="utf-8")
    if not read_follow_up_extraction(session_text):
        if retry_count >= FU_EXTRACTION_MAX_RETRIES:
            logger.error(
                "Follow-up abandoned for phone_key=%s session %d — extraction never appeared",
                _phone_key(phone), session_num,
            )
            return
        _reschedule_followup_for_extraction(
            phone, session_path, client_name, session_num, retry_count + 1,
        )
        return

    # Skip if client has used their full message allotment this month
    if _check_monthly_cap_cb and await _check_monthly_cap_cb(phone):
        logger.info("Follow-up skipped for phone_key=%s — monthly cap reached", _phone_key(phone))
        return

    try:
        msg = await _generate_follow_up_message(client_name, session_path)
        _post_fu1[phone] = {
            "session_path": session_path,
            "fu1_text": msg,
            "pending_send": True,
        }
        _persist_followup_state()
        await _send_message(phone, msg)
        _mark_session_followup_sent(session_path, msg)
        _post_fu1[phone] = {
            "session_path": session_path,
            "fu1_text": msg,
            "pending_send": False,
        }
        _persist_followup_state()
        logger.info("Follow-up sent to phone_key=%s", _phone_key(phone))
    except Exception as e:
        logger.error("Follow-up job failed: %s", e)


async def _recover_pending_followup_sends() -> None:
    """Finish follow-up sends interrupted by redeploy (state persisted before send)."""
    for phone, info in list(_post_fu1.items()):
        session_path = info.get("session_path")
        if not session_path:
            continue
        if _session_followup_sent(session_path):
            if info.get("pending_send"):
                info["pending_send"] = False
            if not info.get("fu1_text"):
                info["fu1_text"] = _read_session_followup_message(session_path)
            _persist_followup_state()
            continue
        if not info.get("pending_send"):
            continue
        fu1_text = info.get("fu1_text")
        if not fu1_text or not _send_message:
            continue
        try:
            await _send_message(phone, fu1_text)
            _mark_session_followup_sent(session_path, fu1_text)
            info["pending_send"] = False
            _persist_followup_state()
            logger.info("Recovered pending follow-up send for phone_key=%s", _phone_key(phone))
        except Exception as e:
            logger.error("Failed to recover pending follow-up for phone_key=%s: %s", _phone_key(phone), e)


def on_user_message_cancel_followup(phone: str) -> bool:
    """Cancel any pending follow-up job when the client sends any message. Returns True if in post-FU window."""
    had = phone in _post_fu1
    if not _scheduler:
        return had
    pk = _phone_key(phone)
    for job in list(_scheduler.get_jobs()):
        if job.id.startswith(f"fu1-{pk}-"):
            try:
                _scheduler.remove_job(job.id)
                logger.info("Cancelled %s (client sent a message)", job.id)
            except Exception as e:
                logger.warning("Could not remove job %s: %s", job.id, e)
    return had


def enter_mini_session_after_fu1(phone: str, fu1_text: str | None) -> None:
    info = _post_fu1.pop(phone, None) or {}
    text = fu1_text if fu1_text is not None else info.get("fu1_text")
    _mini[phone] = MiniSessionCtx(state=MiniState.LISTENING, follow_up_1_text=text)
    _persist_followup_state()


def clear_mini(phone: str) -> None:
    if phone in _mini:
        _mini.pop(phone, None)
        _persist_followup_state()


def _format_mini_session_transcript(history: list[dict]) -> str:
    """Format mini-session turns for profile ## Focus for Next Session."""
    lines: list[str] = []
    for msg in history:
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        speaker = "Tanya" if msg.get("role") == "assistant" else "Client"
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


def in_mini_session(phone: str) -> bool:
    return phone in _mini


async def handle_mini_session_turn(phone: str, user_text: str) -> None:
    ctxm = _mini.get(phone)
    if not ctxm or not _claude or not _send_message:
        return

    if _typing_on_cb:
        await _typing_on_cb(phone)
    ctxm.history.append({"role": "user", "content": user_text})
    _persist_followup_state()

    if ctxm.state == MiniState.LISTENING:
        # Turn 3: one response — warm acknowledgment + session question.
        checkin_ref = f' You sent them: "{ctxm.follow_up_1_text}".' if ctxm.follow_up_1_text else ""
        sys = (
            f"You are Tanya.{checkin_ref} The client just replied to your personal check-in.\n\n"
            "Write ONE response (2-3 short sentences max):\n"
            "- Acknowledge what they said warmly and briefly. No coaching, no frameworks, warm and present.\n"
            "- Then ask if they would like to talk about this in a session.\n\n"
            "No em dashes. Output ONLY JSON: {\"reply\":\"<message>\"}"
        )
        try:
            response = await _claude_create(_claude,
                model=_claude_haiku_model,
                max_tokens=300,
                system=sys,
                messages=ctxm.history[-8:],
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            reply = (json.loads(raw).get("reply") or "").strip()
        except Exception as e:
            logger.error("Mini-session LISTENING error: %s", e)
            reply = "I hear you. Would you like to talk about this in a session?"
        reply = reply.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ").replace("–", ", ").replace(" ,", ",")
        ctxm.history.append({"role": "assistant", "content": reply})
        await _send_message(phone, reply)
        ctxm.state = MiniState.SESSION_PROMPT
        _persist_followup_state()
        return

    # Turn 4 (SESSION_PROMPT): client responded to "would you like a session?"
    # Explicit decline → close warmly. Everything else → open session.
    sys = (
        "You are Tanya. You just asked if the client wants to start a coaching session.\n\n"
        "Read their reply:\n"
        "- If they clearly decline (no, not now, I'm fine, maybe later, pass) → close warmly in 1 sentence, set choice to \"declined\"\n"
        "- For everything else (yes, sure, ambiguous, still talking, any engagement at all) → brief warm acknowledgment, set choice to \"session_now\"\n\n"
        "No em dashes. Output ONLY JSON: {\"reply\":\"<message>\",\"choice\":\"session_now|declined\"}"
    )
    try:
        response = await _claude_create(_claude,
            model=_claude_haiku_model,
            max_tokens=200,
            system=sys,
            messages=ctxm.history[-8:],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        reply = (data.get("reply") or "").strip()
        choice = (data.get("choice") or "session_now").lower()
    except Exception as e:
        logger.error("Mini-session SESSION_PROMPT error: %s", e)
        reply = "I'm here whenever you're ready."
        choice = "session_now"

    reply = reply.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ").replace("–", ", ").replace(" ,", ",")
    ctxm.history.append({"role": "assistant", "content": reply})
    await _send_message(phone, reply)

    opening_session = choice != "declined"
    if opening_session and _merge_focus_cb:
        transcript = _format_mini_session_transcript(ctxm.history)
        if transcript:
            try:
                await _merge_focus_cb(phone, transcript)
            except Exception as e:
                logger.error("Mini-session merge_focus failed for %s: %s", _phone_key(phone), e)

    clear_mini(phone)

    if opening_session and _open_session_cb:
        asyncio.ensure_future(_open_session_cb(phone, ""))
