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

from anthropic import AsyncAnthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

logger = logging.getLogger(__name__)

FU_INTERVAL_SEC = 172800  # exactly 48 hours


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
    problem_summary: str | None = None
    follow_up_1_text: str | None = None
    good_news_only: bool = False


# Populated by configure() before scheduler starts
_claude: AsyncAnthropic | None = None
_claude_model: str = ""
_claude_haiku_model: str = ""
_send_message: Callable[[str, str], Awaitable[None]] | None = None
_typing_on_cb: Callable[[str], Awaitable[None]] | None = None
_merge_focus_cb: Callable[[str, str], Awaitable[None]] | None = None
_open_session_cb: Callable[..., Awaitable[None]] | None = None
_check_monthly_cap_cb: Callable[[str], bool] | None = None  # phone -> True if capped

_scheduler: AsyncIOScheduler | None = None
_mini: dict[str, MiniSessionCtx] = {}
_post_fu1: dict[str, dict] = {}  # phone -> {session_path, fu1_text, correlation}
_guard = asyncio.Lock()


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

Rules:
- 1 sentence only. Maximum 2 short sentences.
- References something specific from the session: a commitment they made, an emotional moment, or a phrase they used.
- Implies Tanya is thinking of them — does NOT invite a response or ask a question.
- Never use: checking in, follow-up, just wanted to, hope you're doing well.
- Never use em dashes.
- Reads like a warm text from someone who genuinely remembers — not a prompt to reply.
- Gold standard: the client thinks "I can't believe she remembered that."

Session excerpts:
- Commitments: {data.get('commitments', '(none)')}
- Emotional moments: {data.get('emotional_moments', '(none)')}
- Flagged topics: {data.get('flagged_topics', '(none)')}
- Phase context: {data.get('phase_context', '(none)')}

Return ONLY the message text, nothing else."""

    assert _claude is not None
    response = await _claude.messages.create(
        model=_claude_haiku_model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip().replace(" — ", ", ").replace("—", ",").replace(" – ", ", ").replace("–", ",").replace(" ,", ",")


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
    if _check_monthly_cap_cb and _check_monthly_cap_cb(phone):
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
            except Exception:
                pass
    return had


def enter_mini_session_after_fu1(phone: str, fu1_text: str | None) -> None:
    info = _post_fu1.pop(phone, None) or {}
    text = fu1_text if fu1_text is not None else info.get("fu1_text")
    _mini[phone] = MiniSessionCtx(state=MiniState.LISTENING, follow_up_1_text=text)


def clear_mini(phone: str) -> None:
    _mini.pop(phone, None)


def in_mini_session(phone: str) -> bool:
    return phone in _mini


async def _send_session_prompt_question(phone: str, ctxm: MiniSessionCtx) -> None:
    """Ask if they want a session now or later after LISTENING phase names the issue."""
    if not _claude or not _send_message:
        return
    ctx = (
        "keeping the positive momentum going"
        if ctxm.good_news_only
        else f"what you named: {ctxm.problem_summary or 'this'}"
    )
    sys = f"""You are Tanya. Send ONE short message asking if they want to start a coaching session now or later, framed around {ctx}. Reference the issue naturally. No em dashes. No two questions.

Output ONLY JSON: {{"reply":"<message>"}}"""
    try:
        response = await _claude.messages.create(
            model=_claude_haiku_model,
            max_tokens=250,
            system=sys,
            messages=ctxm.history[-16:],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        reply2 = (data.get("reply") or "").strip().replace(" — ", ", ").replace("—", ",").replace(" – ", ", ").replace("–", ",").replace(" ,", ",")
    except Exception as e:
        logger.error("Mini-session prompt error: %s", e)
        reply2 = (
            "want to do a session around that now, or save it for when you're ready?"
            if not ctxm.good_news_only
            else "want to book a session soon to keep this energy going?"
        )
    ctxm.history.append({"role": "assistant", "content": reply2})
    await _send_message(phone, reply2)


async def handle_mini_session_turn(phone: str, user_text: str) -> None:
    ctxm = _mini.get(phone)
    if not ctxm or not _claude or not _send_message:
        return

    if _typing_on_cb:
        await _typing_on_cb(phone)
    ctxm.history.append({"role": "user", "content": user_text})

    if ctxm.state == MiniState.LISTENING:
        sys = """You are Tanya in MINI-SESSION mode. The client texted back after you sent a short personal check-in (not a formal coaching session).

LISTENING rules:
- No coaching moves, no powerful questions, no frameworks, no BecomingYou language.
- Warm friend energy. Short. Match their tone. Room to be vulnerable.
- If they share a real struggle that has been present since their last session, your reply gently names it in one short beat (no analysis, no reframe), and set advance to "problem_named" with problem_short a 3-8 word label.
- If it is only good news and no struggle, celebrate briefly and set advance to "good_news" (problem_short null).
- If they decline or say they're fine / not interested, set advance to "declined".
- If you need more listening, set advance to "still_listening".

Never use em dashes. Output ONLY valid JSON (no markdown):
{"reply":"<message>","advance":"still_listening|problem_named|good_news|declined","problem_short":null|string}"""

    else:
        ctx = (
            "momentum / keeping progress going"
            if ctxm.good_news_only
            else f"the issue: {ctxm.problem_summary or 'what they shared'}"
        )
        sys = f"""You are Tanya in MINI-SESSION mode. You need to ask if they want to start a coaching session now or later, framed around {ctx}.

One short message. Reference the issue naturally, not generically. No em dashes.

Output ONLY JSON: {{"reply":"<your message>","choice":"session_now|session_later|unclear"}}

If they already clearly said now or later in their last message, set choice accordingly and make reply a brief acknowledgment plus confirmation.
If they decline or say they're not interested, set choice to "declined" and reply warmly closing the conversation."""

    try:
        response = await _claude.messages.create(
            model=_claude_haiku_model,
            max_tokens=500,
            system=sys,
            messages=ctxm.history[-16:],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        reply = (data.get("reply") or "").strip().replace(" — ", ", ").replace("—", ",").replace(" – ", ", ").replace("–", ",").replace(" ,", ",")
    except Exception as e:
        logger.error("Mini-session Claude error: %s", e)
        reply = "thanks for texting back. i'm here."
        data = {}

    ctxm.history.append({"role": "assistant", "content": reply})
    await _send_message(phone, reply)

    if ctxm.state == MiniState.LISTENING:
        adv = (data.get("advance") or "still_listening").lower()
        ps = data.get("problem_short")
        if adv == "problem_named" and ps:
            ctxm.problem_summary = str(ps).strip()
            if _merge_focus_cb:
                await _merge_focus_cb(phone, ctxm.problem_summary)
            ctxm.good_news_only = False
            ctxm.state = MiniState.SESSION_PROMPT
            await _send_session_prompt_question(phone, ctxm)
        elif adv == "good_news":
            ctxm.good_news_only = True
            ctxm.problem_summary = None
            ctxm.state = MiniState.SESSION_PROMPT
            await _send_session_prompt_question(phone, ctxm)
        elif adv == "declined":
            clear_mini(phone)
        return

    choice = (data.get("choice") or "unclear").lower()
    low = user_text.lower()
    if choice in ("session_now", "session_later", "declined"):
        final = choice
    elif any(w in low for w in ("now", "let's do it", "lets do it", "yes now", "start now")):
        final = "session_now"
    elif any(w in low for w in ("later", "not now", "another time", "tomorrow", "next week")):
        final = "session_later"
    elif any(w in low for w in ("no", "nope", "not interested", "i'm good", "im good", "all good")):
        final = "declined"
    else:
        final = "unclear"

    if final == "session_now":
        clear_mini(phone)
        if _open_session_cb:
            await _open_session_cb(phone, "")
    elif final == "session_later":
        clear_mini(phone)
    elif final == "declined":
        clear_mini(phone)
