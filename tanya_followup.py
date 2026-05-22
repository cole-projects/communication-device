"""
Follow-up engine: persisted 48h timers (APScheduler + SQLite), mini-session state,
and transport-agnostic outbound messaging via a send_message hook.

V1 design: one follow-up per session only. Timer cancels on any inbound message.
New timer starts only after the next session ends.
Three outcomes after follow-up fires:
  (1) no response — no further action
  (2) client wants a session — Haiku acknowledges, opens Sonnet session, counts toward 250
  (3) client declines — Haiku closes warmly, no timer reset
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
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


def _phone_key(phone: str) -> str:
    """16-char SHA-256 prefix of the phone number — used in scheduler job IDs only."""
    return hashlib.sha256(phone.encode()).hexdigest()[:16]


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


def configure(
    *,
    claude: AsyncAnthropic,
    claude_model: str,
    claude_haiku_model: str,
    send_message: Callable[[str, str], Awaitable[None]],
    merge_focus_for_next_session: Callable[[str, str], Awaitable[None]],
    open_coaching_session: Callable[..., Awaitable[None]],
    check_monthly_cap: Callable[[str], bool] | None = None,
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
    global _scheduler
    if _scheduler is not None:
        return
    jobstore_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{jobstore_sqlite_path.resolve()}"
    _scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=url)},
        job_defaults={"coalesce": True, "max_instances": 1},
    )


async def start_scheduler() -> None:
    if _scheduler and not _scheduler.running:
        _scheduler.start()
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


def schedule_follow_up_1(
    phone: str,
    client_name: str,
    session_path: Path,
    session_num: int,
    ended_at_utc: datetime,
) -> None:
    if not _scheduler:
        logger.warning("Scheduler not initialized; skip follow-up scheduling")
        return
    pk = _phone_key(phone)
    run_at = ended_at_utc + timedelta(seconds=FU_INTERVAL_SEC)
    job_id = f"fu1-{pk}-{session_num}-{int(ended_at_utc.timestamp())}"
    _scheduler.add_job(
        followup_job_fire,
        "date",
        run_date=run_at,
        kwargs={
            "phone": phone,
            "session_path_str": str(session_path),
            "client_name": client_name,
        },
        id=job_id,
        replace_existing=False,
    )
    logger.info(
        "Scheduled follow-up for phone_key=%s at %s (job %s)",
        pk,
        run_at,
        job_id,
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
) -> None:
    session_path = Path(session_path_str)
    if not session_path.exists():
        logger.error("Follow-up: session file missing %s", session_path)
        return
    if _send_message is None:
        return

    # Skip if client has used their full message allotment this month
    if _check_monthly_cap_cb and await _check_monthly_cap_cb(phone):
        logger.info("Follow-up skipped for phone_key=%s — monthly cap reached", _phone_key(phone))
        return

    try:
        msg = await _generate_follow_up_message(client_name, session_path)
        await _send_message(phone, msg)
        _post_fu1[phone] = {
            "session_path": session_path,
            "fu1_text": msg,
        }
        logger.info("Follow-up sent to phone_key=%s", _phone_key(phone))
    except Exception as e:
        logger.error("Follow-up job failed: %s", e)


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


def clear_mini(phone: str) -> None:
    _mini.pop(phone, None)


def in_mini_session(phone: str) -> bool:
    return phone in _mini


async def handle_mini_session_turn(phone: str, user_text: str) -> None:
    ctxm = _mini.get(phone)
    if not ctxm or not _claude or not _send_message:
        return

    if _typing_on_cb:
        await _typing_on_cb(phone)
    ctxm.history.append({"role": "user", "content": user_text})

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
    clear_mini(phone)

    if choice != "declined" and _open_session_cb:
        await _open_session_cb(phone, "")
