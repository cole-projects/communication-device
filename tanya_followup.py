"""
Follow-up engine: persisted 48h timers (APScheduler + SQLite), mini-session state,
and transport-agnostic outbound messaging via a send_message hook.
"""
from __future__ import annotations

import asyncio
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
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

FU_INTERVAL_SEC = 172800  # exactly 48 hours


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
_send_message: Callable[[int, str], Awaitable[None]] | None = None
_merge_focus_cb: Callable[[str, str], Awaitable[None]] | None = None
_open_session_cb: Callable[..., Awaitable[None]] | None = None

_scheduler: AsyncIOScheduler | None = None
_mini: dict[int, MiniSessionCtx] = {}
_post_fu1: dict[int, dict] = {}  # chat_id -> {session_path, fu1_text, session_num, ...}
_guard = asyncio.Lock()


def configure(
    *,
    claude: AsyncAnthropic,
    claude_model: str,
    claude_haiku_model: str,
    send_message: Callable[[int, str], Awaitable[None]],
    merge_focus_for_next_session: Callable[[str, str], Awaitable[None]],
    open_coaching_session: Callable[..., Awaitable[None]],
) -> None:
    global _claude, _claude_model, _claude_haiku_model, _send_message
    global _merge_focus_cb, _open_session_cb
    _claude = claude
    _claude_model = claude_model
    _claude_haiku_model = claude_haiku_model
    _send_message = send_message
    _merge_focus_cb = merge_focus_for_next_session
    _open_session_cb = open_coaching_session


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


def cancel_all_followup_jobs_for_chat(chat_id: int) -> None:
    if not _scheduler:
        return
    for job in list(_scheduler.get_jobs()):
        jid = job.id
        if jid.startswith(f"fu1-{chat_id}-") or jid.startswith(f"fu2-{chat_id}-"):
            try:
                _scheduler.remove_job(jid)
                logger.info("Removed follow-up job %s", jid)
            except Exception as e:
                logger.warning("Could not remove job %s: %s", jid, e)
    _post_fu1.pop(chat_id, None)
    clear_mini(chat_id)


def _schedule_fu2(chat_id: int, session_path: Path, client_name: str, session_num: int, base_id: str) -> None:
    if not _scheduler:
        return
    run_at = datetime.now(timezone.utc) + timedelta(seconds=FU_INTERVAL_SEC)
    jid = f"fu2-{chat_id}-{session_num}-{base_id.split('-')[-1]}"
    _scheduler.add_job(
        followup_job_fire,
        "date",
        run_date=run_at,
        kwargs={
            "chat_id": chat_id,
            "session_path_str": str(session_path),
            "client_name": client_name,
            "fu_index": 2,
            "correlation": base_id,
        },
        id=jid,
        replace_existing=True,
    )
    logger.info("Scheduled follow-up 2 for chat_id=%s at %s (job %s)", chat_id, run_at, jid)


def schedule_follow_up_1(
    chat_id: int,
    client_name: str,
    session_path: Path,
    session_num: int,
    ended_at_utc: datetime,
) -> None:
    if not _scheduler:
        logger.warning("Scheduler not initialized; skip follow-up scheduling")
        return
    run_at = ended_at_utc + timedelta(seconds=FU_INTERVAL_SEC)
    base_id = f"fu1-{chat_id}-{session_num}-{int(ended_at_utc.timestamp())}"
    _scheduler.add_job(
        followup_job_fire,
        "date",
        run_date=run_at,
        kwargs={
            "chat_id": chat_id,
            "session_path_str": str(session_path),
            "client_name": client_name,
            "fu_index": 1,
            "correlation": base_id,
        },
        id=base_id,
        replace_existing=False,
    )
    logger.info(
        "Scheduled follow-up 1 for chat_id=%s at %s (job %s)",
        chat_id,
        run_at,
        base_id,
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
        (r"\*\*Follow-up 1 sent text:\*\*\s*", "fu1_sent_text"),
    ):
        mm = re.search(label + r"(.+?)(?=\n- \*\*|\Z)", block, re.DOTALL | re.IGNORECASE)
        if mm:
            out[key] = mm.group(1).strip()
    if "fu1_sent_text" not in out:
        fm = re.search(r"Follow-up 1 sent text:\*\*\s*(.+)", session_text, re.IGNORECASE)
        if fm:
            out["fu1_sent_text"] = fm.group(1).strip()
    return out


async def _send_session_prompt_question(update: Update, ctxm: MiniSessionCtx, client_name: str) -> None:
    """Second assistant turn: ask now vs later (or momentum session) after LISTENING phase completes."""
    if not _claude:
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
        reply2 = (data.get("reply") or "").strip().replace("\u2014", ",").replace("\u2013", ",")
    except Exception as e:
        logger.error("Mini-session prompt error: %s", e)
        reply2 = (
            "want to do a session around that now, or save it for when you're ready?"
            if not ctxm.good_news_only
            else "want to book a session soon to keep this energy going?"
        )
    ctxm.history.append({"role": "assistant", "content": reply2})
    await update.message.reply_text(reply2)


async def _generate_follow_up_message(
    client_name: str,
    session_path: Path,
    fu_index: int,
    *,
    previous_fu_text: str | None = None,
) -> str:
    text = await asyncio.to_thread(session_path.read_text, encoding="utf-8")
    data = read_follow_up_extraction(text)
    extra2 = ""
    if fu_index == 2:
        extra2 = (
            "This is a second follow-up. The client did not respond to the first message. "
            "Do not repeat the same angle. Choose a different specific moment or commitment from the session. "
            "The tone should be softer and more open — give them space rather than pressing the same point. "
            "Still warm, still specific, still human. 1-3 sentences. No em dashes."
        )
    prev_clause = ""
    if previous_fu_text:
        prev_clause = f"The first follow-up you sent was: {previous_fu_text}\nAvoid repeating that hook.\n"

    prompt = f"""Generate a follow-up text message from Tanya to client {client_name}. It must feel like Tanya personally thought of them, not a system. Keep it to 1-3 sentences maximum. Reference something specific from the session data below: a commitment they made, an emotional moment, or a phrase they used. Never use the words: checking in, follow-up, just wanted to, hope you're doing well. Never use em dashes. It should read like a warm text from someone who genuinely cares. The gold standard: the client thinks 'I can't believe you remembered that.'

Session file excerpts (Follow-Up Extraction):
- Commitments: {data.get('commitments', '(none)')}
- Emotional moments: {data.get('emotional_moments', '(none)')}
- Flagged topics: {data.get('flagged_topics', '(none)')}
- Phase context: {data.get('phase_context', '(none)')}

{prev_clause}
{extra2}

Return ONLY the message text, nothing else."""

    assert _claude is not None
    response = await _claude.messages.create(
        model=_claude_haiku_model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip().replace("\u2014", ",").replace("\u2013", ",")


async def _append_session_file_note(session_path: Path, note: str) -> None:
    def _app() -> None:
        with session_path.open("a", encoding="utf-8") as f:
            f.write(note)

    await asyncio.to_thread(_app)


async def followup_job_fire(
    chat_id: int,
    session_path_str: str,
    client_name: str,
    fu_index: int,
    correlation: str,
) -> None:
    session_path = Path(session_path_str)
    if not session_path.exists():
        logger.error("Follow-up: session file missing %s", session_path)
        return
    if _send_message is None:
        return
    try:
        prev = None
        st = await asyncio.to_thread(session_path.read_text, encoding="utf-8")
        d = read_follow_up_extraction(st)
        if fu_index == 2:
            prev = d.get("fu1_sent_text") or None
        msg = await _generate_follow_up_message(client_name, session_path, fu_index, previous_fu_text=prev)
        await _send_message(chat_id, msg)
        if fu_index == 1:
            note = (
                f"\n- **Follow-up 1 sent text:** {msg}\n"
                f"- **Primary hook used:** (see message)\n"
            )
            await _append_session_file_note(session_path, note)
            _post_fu1[chat_id] = {
                "session_path": session_path,
                "fu1_text": msg,
                "correlation": correlation,
            }
            sess_m = re.search(r"Session (\d+)", session_path.name)
            sn = int(sess_m.group(1)) if sess_m else 1
            _schedule_fu2(chat_id, session_path, client_name, sn, correlation)
        logger.info("Follow-up %d sent to chat_id=%s", fu_index, chat_id)
    except Exception as e:
        logger.error("Follow-up job failed: %s", e)


def on_user_message_cancel_fu2(chat_id: int) -> bool:
    """Remove pending follow-up 2 if any. Returns True if a post-FU1 window was open."""
    had = chat_id in _post_fu1
    if not _scheduler:
        return had
    for job in list(_scheduler.get_jobs()):
        if job.id.startswith(f"fu2-{chat_id}-"):
            try:
                _scheduler.remove_job(job.id)
                logger.info("Cancelled %s (client responded)", job.id)
            except Exception:
                pass
    return had


def enter_mini_session_after_fu1(chat_id: int, fu1_text: str | None) -> None:
    info = _post_fu1.pop(chat_id, None) or {}
    text = fu1_text if fu1_text is not None else info.get("fu1_text")
    _mini[chat_id] = MiniSessionCtx(state=MiniState.LISTENING, follow_up_1_text=text)


def clear_mini(chat_id: int) -> None:
    _mini.pop(chat_id, None)


def in_mini_session(chat_id: int) -> bool:
    return chat_id in _mini


async def handle_mini_session_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str) -> None:
    chat_id = update.effective_chat.id
    client_name = (update.effective_user.first_name or "Client").strip()
    ctxm = _mini.get(chat_id)
    if not ctxm or not _claude:
        return

    ctxm.history.append({"role": "user", "content": user_text})

    if ctxm.state == MiniState.LISTENING:
        sys = f"""You are Tanya in MINI-SESSION mode. The client texted back after you sent a short personal check-in (not a formal coaching session).

LISTENING rules:
- No coaching moves, no powerful questions, no frameworks, no BecomingYou language.
- Warm friend energy. Short. Match their tone. Room to be vulnerable.
- If they share a real struggle that has been present since their last session, your reply gently names it in one short beat (no analysis, no reframe), and set advance to "problem_named" with problem_short a 3-8 word label.
- If it is only good news and no struggle, celebrate briefly and set advance to "good_news" (problem_short null).
- If you need more listening, set advance to "still_listening".

Never use em dashes. Output ONLY valid JSON (no markdown):
{{"reply":"<message>","advance":"still_listening|problem_named|good_news","problem_short":null|string}}"""

    else:
        ctx = (
            f"momentum / keeping progress going"
            if ctxm.good_news_only
            else f"the issue: {ctxm.problem_summary or 'what they shared'}"
        )
        sys = f"""You are Tanya in MINI-SESSION mode. You need to ask if they want to start a coaching session now or later, framed around {ctx}.

One short message. Reference the issue naturally, not generically. No em dashes.

Output ONLY JSON: {{"reply":"<your message>","choice":"session_now|session_later|unclear"}}

If they already clearly said now or later in their last message, set choice accordingly and make reply a brief acknowledgment plus confirmation."""

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
        reply = (data.get("reply") or "").strip().replace("\u2014", ",").replace("\u2013", ",")
    except Exception as e:
        logger.error("Mini-session Claude error: %s", e)
        reply = "thanks for texting back. i'm here."
        data = {}

    ctxm.history.append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)

    if ctxm.state == MiniState.LISTENING:
        adv = (data.get("advance") or "still_listening").lower()
        ps = data.get("problem_short")
        if adv == "problem_named" and ps:
            ctxm.problem_summary = str(ps).strip()
            if _merge_focus_cb:
                await _merge_focus_cb(client_name, ctxm.problem_summary)
            ctxm.good_news_only = False
            ctxm.state = MiniState.SESSION_PROMPT
        elif adv == "good_news":
            ctxm.good_news_only = True
            ctxm.problem_summary = None
            ctxm.state = MiniState.SESSION_PROMPT
        else:
            return
        await _send_session_prompt_question(update, ctxm, client_name)
        return

    choice = (data.get("choice") or "unclear").lower()
    low = user_text.lower()
    if choice == "session_now" or choice == "session_later":
        final = choice
    elif any(w in low for w in ("now", "let's do it", "lets do it", "yes now", "start now")):
        final = "session_now"
    elif any(w in low for w in ("later", "not now", "another time", "tomorrow", "next week")):
        final = "session_later"
    else:
        final = "unclear"

    if final == "session_now":
        clear_mini(chat_id)
        if _open_session_cb:
            await _open_session_cb(update, context, client_name)
    elif final == "session_later":
        clear_mini(chat_id)
